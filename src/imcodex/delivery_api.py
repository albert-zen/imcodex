from __future__ import annotations

import asyncio
import hmac
import ipaddress
import json
import os
from pathlib import Path
import secrets
from typing import Callable

from fastapi import FastAPI, HTTPException, Request
from starlette.datastructures import UploadFile
from starlette.datastructures import FormData
from starlette.formparsers import MultiPartException, MultiPartParser
from starlette.responses import JSONResponse

from .bridge.outbound_artifacts import OutboundArtifactStager
from .models import OutboundArtifact, OutboundMessage
from .observability.health import BRIDGE_INSTANCE_HEADER
from .windows_security import secure_windows_path


DELIVERY_PATH = "/_imcodex/tools/deliver"
DELIVERY_TOKEN_HEADER = "x-imcodex-delivery-token"
DELIVERY_TOKEN_FILE = "delivery-token"
MAX_DELIVERY_ARTIFACTS = 4
MAX_DELIVERY_ARTIFACT_BYTES = 25 * 1024 * 1024
MAX_DELIVERY_BODY_BYTES = (
    MAX_DELIVERY_ARTIFACTS * MAX_DELIVERY_ARTIFACT_BYTES + 64 * 1024
)


class _BoundedDeliveryParser(MultiPartParser):
    """Reject large uploads while streaming, before Starlette spools them."""

    def __init__(self, *args, max_file_size: int, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._max_file_size = max_file_size
        self._current_file_size = 0

    def on_part_begin(self) -> None:
        super().on_part_begin()
        self._current_file_size = 0

    def on_part_data(self, data: bytes, start: int, end: int) -> None:
        if self._current_part.file is not None:
            self._current_file_size += end - start
            if self._current_file_size > self._max_file_size:
                raise MultiPartException("Delivery artifact is too large.")
        super().on_part_data(data, start, end)


class LocalDeliveryCredential:
    def __init__(
        self,
        run_dir: Path,
        *,
        prepare: Callable[[], None] | None = None,
    ) -> None:
        self.path = Path(run_dir) / "current" / DELIVERY_TOKEN_FILE
        self.token = secrets.token_urlsafe(32)
        self.prepare = prepare

    def publish(self) -> None:
        if self.prepare is not None:
            self.prepare()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(self.path, flags, 0o600)
        except FileExistsError as exc:
            raise RuntimeError("Local delivery credential already exists") from exc
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(self.token + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        except Exception:
            self.path.unlink(missing_ok=True)
            raise
        try:
            if os.name == "nt":
                secure_windows_path(self.path)
            else:
                os.chmod(self.path, 0o600)
        except Exception:
            self.path.unlink(missing_ok=True)
            raise

    def clear(self) -> None:
        try:
            if self.path.read_text(encoding="utf-8").strip() == self.token:
                self.path.unlink(missing_ok=True)
        except OSError:
            pass


def install_delivery_route(
    app: FastAPI,
    runtime,
    *,
    data_dir: Path,
    run_dir: Path,
) -> LocalDeliveryCredential:
    stager = OutboundArtifactStager(Path(data_dir) / "outbound-media" / "tool")
    stage_lock = asyncio.Lock()
    credential = LocalDeliveryCredential(
        run_dir,
        prepare=lambda: stager.cleanup_unreferenced(set()),
    )

    @app.post(DELIVERY_PATH, include_in_schema=False)
    async def deliver(request: Request):
        _authorize_local_instance(request, runtime, credential)
        content_length = request.headers.get("content-length", "")
        try:
            if content_length and int(content_length) > MAX_DELIVERY_BODY_BYTES:
                raise HTTPException(status_code=413, detail="Delivery body is too large.")
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid Content-Length header.") from None
        content_type = request.headers.get("content-type", "").partition(";")[0].strip()
        if content_type == "application/x-www-form-urlencoded":
            body = bytearray()
            try:
                async for chunk in _bounded_request_stream(request, limit=64 * 1024):
                    body.extend(chunk)
            except MultiPartException:
                raise HTTPException(
                    status_code=413,
                    detail="Delivery form is too large.",
                ) from None
            from urllib.parse import parse_qsl

            try:
                items = parse_qsl(
                    body.decode("utf-8"),
                    keep_blank_values=True,
                    strict_parsing=True,
                    max_num_fields=1,
                )
            except (UnicodeDecodeError, ValueError):
                raise HTTPException(
                    status_code=422,
                    detail="Delivery form is invalid.",
                ) from None
            form = FormData(items)
        elif content_type == "multipart/form-data":
            parser = _BoundedDeliveryParser(
                request.headers,
                _bounded_request_stream(request),
                max_files=MAX_DELIVERY_ARTIFACTS,
                max_fields=1,
                max_part_size=64 * 1024,
                max_file_size=MAX_DELIVERY_ARTIFACT_BYTES,
            )
            try:
                form = await parser.parse()
            except MultiPartException as exc:
                detail = str(exc)
                status_code = 413 if "too large" in detail.casefold() else 422
                raise HTTPException(status_code=status_code, detail=detail) from None
        else:
            raise HTTPException(
                status_code=415,
                detail="Delivery request must be form encoded.",
            )
        if set(form.keys()) - {"payload", "artifacts"}:
            await _raise_form_error(form, 422, "Unsupported delivery fields.")
        payload_values = form.getlist("payload")
        if len(payload_values) != 1 or isinstance(payload_values[0], UploadFile):
            await _raise_form_error(form, 422, "payload must be one JSON text field.")
        try:
            payload = json.loads(str(payload_values[0]))
        except (TypeError, ValueError, json.JSONDecodeError):
            await _raise_form_error(form, 422, "Delivery payload is invalid JSON.")
        if not isinstance(payload, dict):
            await _raise_form_error(form, 422, "Delivery payload must be an object.")
        channel_id = str(payload.get("channel_id") or "").strip()
        conversation_id = str(payload.get("conversation_id") or "").strip()
        text = str(payload.get("text") or "")
        delivery_id = str(payload.get("delivery_id") or "").strip()
        manifest = payload.get("artifacts") or []
        uploads = form.getlist("artifacts")
        if not channel_id or not conversation_id or not delivery_id:
            await _raise_form_error(
                form,
                422,
                "channel_id, conversation_id, and delivery_id are required.",
            )
        if not isinstance(manifest, list) or len(manifest) != len(uploads):
            await _raise_form_error(
                form,
                422,
                "Artifact manifest does not match uploads.",
            )
        if len(uploads) > MAX_DELIVERY_ARTIFACTS:
            await _raise_form_error(form, 422, "At most 4 artifacts may be delivered.")
        if any(not isinstance(upload, UploadFile) for upload in uploads):
            await _raise_form_error(form, 422, "artifacts must be uploaded files.")
        sink = getattr(runtime.service, "outbound_sink", None)
        can_deliver = getattr(sink, "can_deliver", None)
        if sink is None or not callable(can_deliver) or not can_deliver(channel_id):
            await _raise_form_error(form, 404, "Configured channel is unavailable.")

        artifacts: list[OutboundArtifact] = []
        try:
            async with stage_lock:
                for item, upload in zip(manifest, uploads, strict=True):
                    if not isinstance(item, dict):
                        raise ValueError("artifact manifest entries must be objects")
                    content = await upload.read()
                    artifacts.append(
                        await asyncio.to_thread(
                            stager.stage_upload,
                            content,
                            kind=str(item.get("kind") or "file"),
                            content_type=str(
                                upload.content_type or item.get("content_type") or ""
                            ),
                            filename=str(upload.filename or item.get("filename") or ""),
                        )
                    )
        except ValueError as exc:
            async with stage_lock:
                await asyncio.to_thread(_clear_delivery_artifacts, artifacts)
            raise HTTPException(status_code=422, detail=str(exc)) from None
        finally:
            for upload in uploads:
                if isinstance(upload, UploadFile):
                    await upload.close()
        if not text.strip() and not artifacts:
            raise HTTPException(status_code=422, detail="Delivery has no content.")

        try:
            message = OutboundMessage(
                channel_id=channel_id,
                conversation_id=conversation_id,
                message_type="tool_delivery",
                text=text,
                metadata={"delivery_id": delivery_id, "source": "channels.send"},
                artifacts=list(artifacts),
            )
            sink.prepare_durable_message(message)
            try:
                await sink.send_message(message)
            except PermissionError as exc:
                return JSONResponse(
                    _delivery_receipt(
                        message,
                        artifacts,
                        status="rejected",
                        error=str(exc),
                    ),
                    status_code=403,
                )
            except Exception as exc:
                return JSONResponse(
                    _delivery_receipt(
                        message,
                        artifacts,
                        status="failed",
                        error=f"{type(exc).__name__}: delivery was not confirmed",
                    ),
                    status_code=502,
                )
            receipt = _delivery_receipt(message, artifacts, status="delivered")
            status_code = 207 if receipt["status"] == "partial" else 200
            return JSONResponse(receipt, status_code=status_code)
        finally:
            async with stage_lock:
                await asyncio.to_thread(_clear_delivery_artifacts, artifacts)

    return credential


async def _bounded_request_stream(
    request: Request,
    *,
    limit: int = MAX_DELIVERY_BODY_BYTES,
):
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > limit:
            raise MultiPartException("Delivery body is too large.")
        yield chunk


async def _raise_form_error(form, status_code: int, detail: str) -> None:
    for _name, value in form.multi_items():
        if isinstance(value, UploadFile):
            await value.close()
    raise HTTPException(status_code=status_code, detail=detail)


def _clear_delivery_artifacts(artifacts: list[OutboundArtifact]) -> None:
    """Remove request-scoped uploads after the channel adapter has returned."""

    for artifact in artifacts:
        candidate = Path(artifact.local_path)
        try:
            if candidate.is_file() and not candidate.is_symlink():
                candidate.unlink(missing_ok=True)
        except OSError:
            # Global startup cleanup remains the fallback after interruption.
            continue


def _authorize_local_instance(
    request: Request,
    runtime,
    credential: LocalDeliveryCredential,
) -> None:
    client_host = str(request.client.host if request.client is not None else "")
    try:
        loopback = ipaddress.ip_address(client_host).is_loopback
    except ValueError:
        loopback = False
    context = getattr(getattr(runtime, "observability", None), "context", None)
    instance_id = str(getattr(context, "instance_id", "") or "")
    supplied = request.headers.get(BRIDGE_INSTANCE_HEADER, "")
    supplied_token = request.headers.get(DELIVERY_TOKEN_HEADER, "")
    if (
        not loopback
        or not instance_id
        or not hmac.compare_digest(supplied, instance_id)
        or not hmac.compare_digest(supplied_token, credential.token)
    ):
        raise HTTPException(status_code=403, detail="Local delivery request was rejected.")


def _delivery_receipt(
    message: OutboundMessage,
    artifacts: list[OutboundArtifact],
    *,
    status: str,
    error: str = "",
) -> dict[str, object]:
    failures = message.metadata.get("artifact_failures") or []
    failure_strings = [str(item) for item in failures if isinstance(item, str)]
    recorded = message.metadata.get("artifact_receipts") or []
    items = []
    for artifact in artifacts:
        delivery = next(
            (
                item
                for item in recorded
                if isinstance(item, dict)
                and item.get("local_path") == artifact.local_path
            ),
            {},
        )
        failure = ""
        if not delivery:
            for index, candidate in enumerate(failure_strings):
                if candidate.startswith(f"{artifact.filename}:"):
                    failure = failure_strings.pop(index)
                    break
        delivered = bool(delivery)
        items.append(
            {
                "filename": artifact.filename,
                "kind": artifact.kind,
                "status": (
                    "failed"
                    if failure
                    else "delivered"
                    if delivered or status == "delivered"
                    else "unknown"
                ),
                "error": failure.partition(":")[2].strip() if failure else "",
                "platform_message_id": str(delivery.get("platform_message_id") or ""),
                "delivery_identity": str(delivery.get("delivery_identity") or ""),
            }
        )
    overall = status
    if status == "delivered" and any(item["status"] == "failed" for item in items):
        overall = "partial"
    return {
        "delivery_id": str(message.metadata.get("delivery_id") or ""),
        "channel_id": message.channel_id,
        "conversation_id": message.conversation_id,
        "status": overall,
        "text_status": "unknown" if status == "failed" else status,
        "artifacts": items,
        "error": error,
    }
