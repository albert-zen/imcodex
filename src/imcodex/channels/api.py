from __future__ import annotations

import asyncio
from dataclasses import asdict
from dataclasses import dataclass, field
import ipaddress
import json
import logging
from pathlib import Path
import re
import secrets
from typing import Awaitable, Callable

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ConfigDict, ValidationError
from starlette.datastructures import FormData, UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser
from starlette.responses import JSONResponse

from ..models import InboundMessage, OutboundMessage
from .media import (
    MAX_IMAGE_BYTES,
    MAX_IMAGE_COUNT,
    ImageMediaMaterializer,
    materialize_inbound_images,
)
from .middleware import UnifiedChannelMiddleware
from .registry import BUILTIN_CHANNEL_IDS


logger = logging.getLogger(__name__)

INBOUND_WEBHOOK_PATH = "/api/channels/webhook/inbound"
MAX_INBOUND_WEBHOOK_BODY_BYTES = 64 * 1024
MAX_INBOUND_WEBHOOK_MULTIPART_BODY_BYTES = (
    MAX_IMAGE_COUNT * MAX_IMAGE_BYTES + 1024 * 1024
)
MAX_CONCURRENT_INBOUND_WEBHOOK_MULTIPART_REQUESTS = 2
MAX_INBOUND_WEBHOOK_MULTIPART_RETENTION_S = 30.0
MAX_INBOUND_WEBHOOK_FORM_CLOSE_GRACE_S = 1.0
MAX_INBOUND_WEBHOOK_FORM_CLEANUP_SHUTDOWN_S = 1.0
MAX_INBOUND_TEXT_CHARS = 32 * 1024
WEBHOOK_ID_LIMITS = {
    "channel_id": 64,
    "conversation_id": 1024,
    "user_id": 512,
    "message_id": 512,
}
CHANNEL_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")
WEBHOOK_FORM_FIELDS = frozenset(
    {
        "channel_id",
        "conversation_id",
        "user_id",
        "message_id",
        "text",
        "reply_to_message_id",
        "sent_at",
        "trace_id",
        "images",
    }
)


class _InboundWebhookBodyTooLarge(Exception):
    pass


class _InboundWebhookFileTooLarge(MultiPartException):
    def __init__(self) -> None:
        super().__init__("Inbound image file is too large.")


class _BoundedWebhookMultiPartParser(MultiPartParser):
    """Apply a byte limit while each upload is still being parsed."""

    def __init__(
        self,
        *args,
        max_file_size: int,
        max_part_size: int,
        **kwargs,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._max_file_size = max_file_size
        # Starlette introduced the bound as a parser attribute; newer
        # releases also expose it as an __init__ keyword. Assigning the
        # instance attribute keeps our declared minimum and current releases
        # on the same bounded path without version branching.
        self.max_part_size = max_part_size
        self._current_file_size = 0

    def on_part_begin(self) -> None:
        super().on_part_begin()
        self._current_file_size = 0

    def on_part_data(self, data: bytes, start: int, end: int) -> None:
        if self._current_part.file is not None:
            self._current_file_size += end - start
            if self._current_file_size > self._max_file_size:
                raise _InboundWebhookFileTooLarge()
        super().on_part_data(data, start, end)

    async def parse(self) -> FormData:
        try:
            return await super().parse()
        except BaseException:
            # Starlette closes these for parser/OSError failures. Also close
            # them when our outer streaming body guard or cancellation aborts.
            for file in self._files_to_close_on_error:
                file.close()
            raise


@dataclass(frozen=True, slots=True)
class _WebhookImageReference:
    upload: UploadFile = field(repr=False, compare=False)


class InboundWebhookRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    channel_id: str
    conversation_id: str
    user_id: str
    message_id: str
    text: str
    reply_to_message_id: str | None = None
    sent_at: str | None = None
    trace_id: str | None = None

    def to_inbound_message(self) -> InboundMessage:
        return InboundMessage(
            channel_id=self.channel_id,
            conversation_id=self.conversation_id,
            user_id=self.user_id,
            message_id=self.message_id,
            text=self.text,
            reply_to_message_id=self.reply_to_message_id,
            sent_at=self.sent_at,
            trace_id=self.trace_id,
        )


class _InboundWebhookGuard:
    """Authenticate and stream-bound the webhook before route parsing."""

    def __init__(self, app, *, configured_token: str) -> None:
        self.app = app
        self.configured_token = configured_token.strip()

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") != "http" or scope.get("path") != INBOUND_WEBHOOK_PATH:
            await self.app(scope, receive, send)
            return

        authorization = self._header(scope, b"authorization")
        denial = self._authorization_denial(scope=scope, authorization=authorization)
        if denial is not None:
            status_code, detail = denial
            await JSONResponse({"detail": detail}, status_code=status_code)(scope, receive, send)
            return

        content_length = self._header(scope, b"content-length")
        body_limit = self._body_limit(scope)
        if content_length:
            try:
                declared_size = int(content_length)
            except ValueError:
                await JSONResponse({"detail": "Invalid Content-Length."}, status_code=400)(scope, receive, send)
                return
            if declared_size < 0:
                await JSONResponse({"detail": "Invalid Content-Length."}, status_code=400)(scope, receive, send)
                return
            if declared_size > body_limit:
                await self._send_too_large(scope, receive, send)
                return

        received = 0

        async def bounded_receive():
            nonlocal received
            message = await receive()
            body = message.get("body") or b""
            received += len(body)
            if received > body_limit:
                raise _InboundWebhookBodyTooLarge
            return message

        await self.app(scope, bounded_receive, send)

    async def _send_too_large(self, scope, receive, send) -> None:
        await JSONResponse(
            {"detail": "Inbound webhook body is too large."},
            status_code=413,
        )(scope, receive, send)

    def _authorization_denial(
        self,
        *,
        scope,
        authorization: bytes,
    ) -> tuple[int, str] | None:
        if self.configured_token:
            scheme, _, supplied = authorization.partition(b" ")
            if scheme.lower() != b"bearer" or not secrets.compare_digest(
                supplied,
                self.configured_token.encode("utf-8"),
            ):
                return 401, "Invalid inbound webhook credentials."
            return None
        client = scope.get("client")
        client_host = str(client[0]) if isinstance(client, (tuple, list)) and client else ""
        try:
            is_loopback = ipaddress.ip_address(client_host).is_loopback
        except ValueError:
            is_loopback = False
        if not is_loopback:
            return (
                403,
                "Remote inbound webhook access requires IMCODEX_INBOUND_WEBHOOK_TOKEN.",
            )
        if self._header(scope, b"origin").strip():
            return (
                403,
                "Browser-origin webhook requests require IMCODEX_INBOUND_WEBHOOK_TOKEN.",
            )
        if self._is_multipart(scope) and self._header(
            scope,
            b"x-imcodex-webhook",
        ).strip() != b"1":
            return (
                403,
                "Loopback multipart requests require X-IMCodex-Webhook: 1.",
            )
        return None

    @staticmethod
    def _header(scope, name: bytes) -> bytes:
        for key, value in scope.get("headers") or ():
            if key.lower() == name:
                return value
        return b""

    def _body_limit(self, scope) -> int:
        if self._is_multipart(scope):
            return MAX_INBOUND_WEBHOOK_MULTIPART_BODY_BYTES
        return MAX_INBOUND_WEBHOOK_BODY_BYTES

    def _is_multipart(self, scope) -> bool:
        content_type = self._header(scope, b"content-type").split(b";", 1)[0]
        return content_type.strip().lower() == b"multipart/form-data"


class _WebhookResponseAdapter:
    def __init__(
        self,
        channel_id: str,
        *,
        outbound_sink=None,
    ) -> None:
        self.channel_id = channel_id
        self.messages: list[OutboundMessage] = []
        self.outbound_sink = outbound_sink

    async def send_message(self, message: OutboundMessage) -> None:
        self.messages.append(message)

    async def after_inbound_committed(self) -> None:
        if self.outbound_sink is not None:
            for message in self.messages:
                await self.outbound_sink.send_message(message)


def create_app(
    service,
    *,
    inbound_token: str = "",
    media_dir: Path | None = None,
    media_materializer: ImageMediaMaterializer[_WebhookImageReference] | None = None,
) -> FastAPI:
    app = FastAPI()
    middleware = UnifiedChannelMiddleware(service=service)
    materializer = media_materializer or ImageMediaMaterializer(
        root=media_dir or Path(".imcodex") / "channels" / "webhook" / "inbound-media",
        download=_download_webhook_upload,
    )
    app.state.webhook_media_materializer = materializer
    app.state.webhook_multipart_semaphore = asyncio.Semaphore(
        MAX_CONCURRENT_INBOUND_WEBHOOK_MULTIPART_REQUESTS
    )
    app.state.webhook_form_cleanup_tasks = set()

    def track_form_close(current_form: FormData) -> asyncio.Task[None]:
        task = asyncio.create_task(
            current_form.close(),
            name="imcodex-webhook-form-close",
        )
        tasks: set[asyncio.Task[None]] = app.state.webhook_form_cleanup_tasks
        tasks.add(task)

        def completed(done: asyncio.Task[None]) -> None:
            tasks.discard(done)
            try:
                done.result()
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                logger.warning(
                    "Inbound webhook form cleanup failed: %s",
                    type(exc).__name__,
                )

        task.add_done_callback(completed)
        return task

    async def wait_for_form_cleanup() -> None:
        tasks = tuple(app.state.webhook_form_cleanup_tasks)
        if not tasks:
            return
        _done, pending = await asyncio.wait(
            tasks,
            timeout=MAX_INBOUND_WEBHOOK_FORM_CLEANUP_SHUTDOWN_S,
        )
        if pending:
            # Keep the task objects owned by app.state. Process teardown is the
            # final OS-level owner for a filesystem close that never returns.
            logger.warning(
                "Inbound webhook form cleanup still pending at shutdown: %d",
                len(pending),
            )

    app.state.wait_for_webhook_form_cleanup = wait_for_form_cleanup
    app.router.add_event_handler("startup", materializer.start)
    app.router.add_event_handler("shutdown", materializer.stop)
    app.router.add_event_handler("shutdown", wait_for_form_cleanup)

    @app.exception_handler(_InboundWebhookBodyTooLarge)
    async def inbound_body_too_large(_request: Request, _exc: Exception):
        return JSONResponse(
            {"detail": "Inbound webhook body is too large."},
            status_code=413,
        )

    @app.post(INBOUND_WEBHOOK_PATH)
    async def webhook_inbound(http_request: Request) -> dict:
        content_type = (
            http_request.headers.get("content-type", "")
            .split(";", 1)[0]
            .strip()
            .lower()
        )
        if content_type == "multipart/form-data":
            acquired = False
            form: FormData | None = None
            multipart_timeout: asyncio.Timeout | None = None

            def release_capacity() -> None:
                nonlocal acquired
                if acquired:
                    app.state.webhook_multipart_semaphore.release()
                    acquired = False

            def retain_parsed_form(parsed_form: FormData) -> None:
                nonlocal form
                form = parsed_form

            async def await_close_grace(task: asyncio.Task[None]) -> None:
                try:
                    async with asyncio.timeout(
                        MAX_INBOUND_WEBHOOK_FORM_CLOSE_GRACE_S
                    ):
                        await asyncio.shield(task)
                except TimeoutError:
                    # The app-level task set retains cleanup ownership without
                    # delaying HTTP 408 or blocking new multipart parsers.
                    pass
                except Exception:
                    # The tracked-task callback emits a type-only warning.
                    pass

            async def release_multipart() -> None:
                nonlocal acquired, form
                current_form, form = form, None
                expired = bool(
                    multipart_timeout is not None
                    and multipart_timeout.expired()
                )
                # Once the retention deadline has fired, unblock new parsers
                # before attempting best-effort close of a potentially slow
                # temporary file.
                if expired:
                    release_capacity()
                close_task = (
                    track_form_close(current_form)
                    if current_form is not None
                    else None
                )
                try:
                    if close_task is not None:
                        if expired:
                            await await_close_grace(close_task)
                        else:
                            try:
                                await asyncio.shield(close_task)
                            except asyncio.CancelledError:
                                release_capacity()
                                await await_close_grace(close_task)
                                raise
                            except Exception:
                                # Cleanup failures are logged by the tracked
                                # task callback and must not replace the route's
                                # response or original validation exception.
                                pass
                finally:
                    release_capacity()
                    if (
                        multipart_timeout is not None
                        and not multipart_timeout.expired()
                    ):
                        # Staging (or a duplicate/preflight decision) has
                        # released the parsed form. Downstream Codex work and
                        # delivery are intentionally outside this deadline.
                        multipart_timeout.reschedule(None)

            try:
                async with asyncio.timeout(
                    MAX_INBOUND_WEBHOOK_MULTIPART_RETENTION_S
                ) as multipart_timeout:
                    try:
                        await app.state.webhook_multipart_semaphore.acquire()
                        acquired = True
                        request, image_references, form = await _parse_webhook_request(
                            http_request,
                            retain_form=retain_parsed_form,
                        )
                        if not image_references:
                            await release_multipart()
                        return await _handle_webhook_inbound(
                            request=request,
                            image_references=image_references,
                            middleware=middleware,
                            materializer=materializer,
                            service=service,
                            finalize_inbound=(
                                release_multipart if image_references else None
                            ),
                        )
                    finally:
                        await release_multipart()
            except TimeoutError:
                if multipart_timeout is None or not multipart_timeout.expired():
                    raise
                raise HTTPException(
                    status_code=408,
                    detail="Inbound multipart upload timed out.",
                ) from None
        request, image_references, _form = await _parse_webhook_request(http_request)
        return await _handle_webhook_inbound(
            request=request,
            image_references=image_references,
            middleware=middleware,
            materializer=materializer,
            service=service,
        )

    app.add_middleware(_InboundWebhookGuard, configured_token=inbound_token)
    return app


async def _handle_webhook_inbound(
    *,
    request: InboundWebhookRequest,
    image_references: tuple[_WebhookImageReference, ...],
    middleware: UnifiedChannelMiddleware,
    materializer: ImageMediaMaterializer[_WebhookImageReference],
    service,
    finalize_inbound: Callable[[], Awaitable[None]] | None = None,
) -> dict:
    for field_name, limit in WEBHOOK_ID_LIMITS.items():
        value = str(getattr(request, field_name) or "")
        if not value.strip():
            raise HTTPException(status_code=422, detail=f"{field_name} must not be empty.")
        if len(value) > limit:
            raise HTTPException(
                status_code=422,
                detail=f"{field_name} exceeds the {limit}-character limit.",
            )
    if CHANNEL_ID_PATTERN.fullmatch(request.channel_id) is None:
        raise HTTPException(status_code=422, detail="channel_id contains unsupported characters.")
    if request.channel_id in BUILTIN_CHANNEL_IDS:
        raise HTTPException(
            status_code=409,
            detail=(
                "The generic webhook cannot claim a built-in channel ID. Use a dedicated gateway channel namespace."
            ),
        )
    if not request.text.strip() and not image_references:
        raise HTTPException(
            status_code=422,
            detail="Inbound message must contain text or an image.",
        )
    if len(request.text) > MAX_INBOUND_TEXT_CHARS:
        raise HTTPException(status_code=413, detail="Inbound message text is too large.")
    message = request.to_inbound_message()
    adapter = _WebhookResponseAdapter(
        message.channel_id,
        outbound_sink=getattr(service, "outbound_sink", None),
    )
    prepare_inbound = None
    if image_references:

        async def prepare_inbound(inbound: InboundMessage) -> InboundMessage:
            return await materialize_inbound_images(
                inbound,
                image_references,
                materializer,
            )
    await middleware.handle_inbound(
        adapter,
        message,
        reply_to_message_id=message.reply_to_message_id or message.message_id,
        prepare_inbound=prepare_inbound,
        finalize_inbound=finalize_inbound,
        pending_attachment_count=len(image_references),
    )
    return {"messages": [asdict(item) for item in adapter.messages]}


async def _parse_webhook_request(
    request: Request,
    *,
    retain_form: Callable[[FormData], None] | None = None,
) -> tuple[
    InboundWebhookRequest,
    tuple[_WebhookImageReference, ...],
    FormData | None,
]:
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if content_type == "application/json" or content_type.endswith("+json"):
        try:
            payload = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
            raise HTTPException(status_code=422, detail="Inbound webhook JSON is invalid.") from None
        return _validate_webhook_model(payload), (), None

    if content_type != "multipart/form-data":
        raise HTTPException(
            status_code=415,
            detail="Inbound webhook content type must be application/json or multipart/form-data.",
        )

    parser = _BoundedWebhookMultiPartParser(
        request.headers,
        request.stream(),
        max_files=MAX_IMAGE_COUNT + 1,
        max_fields=len(WEBHOOK_FORM_FIELDS) + 4,
        max_part_size=MAX_INBOUND_TEXT_CHARS,
        max_file_size=MAX_IMAGE_BYTES,
    )
    try:
        form = await parser.parse()
    except _InboundWebhookFileTooLarge:
        raise HTTPException(status_code=413, detail="Inbound image file is too large.") from None
    except MultiPartException as exc:
        detail = str(exc)
        if "Too many files" in detail:
            raise HTTPException(status_code=422, detail="Too many inbound image files.") from None
        raise HTTPException(status_code=400, detail="Inbound multipart body is invalid.") from None

    if retain_form is not None:
        # Transfer ownership immediately after parsing so every later
        # validation failure uses the route's bounded, tracked close path.
        retain_form(form)

    async def close_unretained_form() -> None:
        if retain_form is None:
            await form.close()

    unknown_fields = {key for key, _value in form.multi_items()} - WEBHOOK_FORM_FIELDS
    if unknown_fields:
        await close_unretained_form()
        raise HTTPException(status_code=422, detail="Inbound multipart body contains unsupported fields.")

    payload: dict[str, object] = {}
    for name in WEBHOOK_FORM_FIELDS - {"images"}:
        values = form.getlist(name)
        if len(values) > 1 or any(isinstance(value, UploadFile) for value in values):
            await close_unretained_form()
            raise HTTPException(status_code=422, detail=f"{name} must be a single text field.")
        if values:
            payload[name] = str(values[0])
    payload.setdefault("text", "")

    uploads = form.getlist("images")
    if any(not isinstance(upload, UploadFile) for upload in uploads):
        await close_unretained_form()
        raise HTTPException(status_code=422, detail="images must contain uploaded files.")
    references = tuple(
        _WebhookImageReference(upload=upload)
        for upload in uploads[: MAX_IMAGE_COUNT + 1]
    )
    try:
        model = _validate_webhook_model(payload)
    except Exception:
        await close_unretained_form()
        raise
    return model, references, form


def _validate_webhook_model(payload: object) -> InboundWebhookRequest:
    try:
        return InboundWebhookRequest.model_validate(payload)
    except ValidationError as exc:
        raise RequestValidationError(exc.errors()) from exc


async def _download_webhook_upload(
    reference: _WebhookImageReference,
    write_chunk,
) -> None:
    while True:
        chunk = await reference.upload.read(64 * 1024)
        if not chunk:
            return
        await write_chunk(chunk)
