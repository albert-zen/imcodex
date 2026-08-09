from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import ipaddress
import json
import os
from pathlib import Path, PurePath
import stat
import time
from typing import Any, Callable
from urllib.parse import urlsplit

import httpx

from ..appserver.thread_observer import (
    NativeThreadAttachment,
    NativeThreadObserverError,
)
from ..observability.runtime import emit_event, mark_integration_health


_ATTACHMENTS_PATH = "/api/orchestration/native-thread-attachments"
_SHELL_PATH = "/api/orchestration/shell"
_RETRYABLE_STATUSES = frozenset({500, 502, 503, 504})


def validate_t3_api_url(value: str) -> str:
    normalized = str(value or "").strip().rstrip("/")
    if not normalized or any(character.isspace() for character in normalized):
        raise ValueError("IMCODEX_T3_API_URL must be a valid HTTP(S) URL")
    try:
        parsed = urlsplit(normalized)
        host = parsed.hostname
        parsed.port
    except ValueError as exc:
        raise ValueError("IMCODEX_T3_API_URL must be a valid HTTP(S) URL") from exc
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc or not host:
        raise ValueError("IMCODEX_T3_API_URL must be an HTTP(S) URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("IMCODEX_T3_API_URL must not contain userinfo credentials")
    if parsed.query or parsed.fragment:
        raise ValueError("IMCODEX_T3_API_URL must not contain query or fragment")
    if parsed.path not in {"", "/"}:
        raise ValueError("IMCODEX_T3_API_URL must not contain a path")
    try:
        address = ipaddress.ip_address(host)
        loopback = address.is_loopback or bool(
            getattr(address, "ipv4_mapped", None) and address.ipv4_mapped.is_loopback
        )
    except ValueError:
        loopback = host.lower() == "localhost"
    if parsed.scheme.lower() == "http" and not loopback:
        raise ValueError("IMCODEX_T3_API_URL must use HTTPS unless it targets loopback")
    return normalized


@dataclass(frozen=True, slots=True)
class T3ObserverConfig:
    api_url: str
    token_file: Path
    connect_timeout_s: float = 2.0
    request_timeout_s: float = 10.0
    project_id: str | None = None
    provider_instance_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "api_url", validate_t3_api_url(self.api_url))
        object.__setattr__(self, "token_file", Path(self.token_file))
        if self.connect_timeout_s <= 0 or self.request_timeout_s <= 0:
            raise ValueError("T3 observer timeouts must be greater than zero")
        object.__setattr__(self, "project_id", _optional(self.project_id))
        object.__setattr__(self, "provider_instance_id", _optional(self.provider_instance_id))


class T3NativeThreadObserver:
    """Stateless adapter over T3's native-thread attachment authority."""

    def __init__(
        self,
        config: T3ObserverConfig,
        *,
        http_client: httpx.AsyncClient | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.config = config
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._owns_client = http_client is None
        timeout = httpx.Timeout(
            timeout=config.request_timeout_s,
            connect=config.connect_timeout_s,
        )
        self._client = http_client or httpx.AsyncClient(
            base_url=config.api_url,
            timeout=timeout,
            follow_redirects=False,
            trust_env=False,
        )

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def ensure_ready(
        self,
        *,
        native_thread_id: str,
        cwd: str | None,
    ) -> NativeThreadAttachment:
        native_thread_id = str(native_thread_id or "").strip()
        if not native_thread_id:
            raise NativeThreadObserverError("invalid_native_thread")
        started = time.monotonic()
        emit_event(
            component="t3",
            event="t3.attach.started",
            message="T3 native-thread attachment started",
        )
        try:
            existing = await self._find_existing(native_thread_id)
            project_id, provider_instance_id = (
                self._route_from_mapping(existing)
                if existing is not None
                else await self._resolve_route(cwd)
            )
            payload = {
                "nativeThreadId": native_thread_id,
                "projectId": project_id,
                "providerInstanceId": provider_instance_id,
                "createIfMissing": True,
            }
            response = await self._request_json("POST", _ATTACHMENTS_PATH, json_payload=payload)
            attachment = self._parse_attachment(
                response,
                expected_native_thread_id=native_thread_id,
                expected_project_id=project_id,
                expected_provider_instance_id=provider_instance_id,
            )
        except NativeThreadObserverError as exc:
            self._mark_failure(exc, started=started)
            raise
        except Exception as exc:
            error = NativeThreadObserverError("observer_unavailable")
            self._mark_failure(error, started=started, error_type=type(exc).__name__)
            raise error from exc
        mark_integration_health(
            "t3",
            enabled=True,
            requiredForImTurns=True,
            status="ready",
            reachable=True,
            authStatus="valid",
            lastSuccessAt=self._clock().astimezone().isoformat(),
            lastErrorCode=None,
        )
        emit_event(
            component="t3",
            event="t3.attach.succeeded",
            message="T3 native-thread attachment is ready",
            data={
                "duration_ms": round((time.monotonic() - started) * 1000),
                "disposition": attachment.disposition,
            },
        )
        return attachment

    async def _find_existing(self, native_thread_id: str) -> dict[str, Any] | None:
        payload = await self._request_json("GET", _ATTACHMENTS_PATH)
        matches = [
            item
            for item in _list_items(payload, "attachments", "data", "items")
            if str(item.get("nativeThreadId") or "") == native_thread_id
        ]
        if self.config.project_id is not None:
            matches = [
                item
                for item in matches
                if str(item.get("projectId") or "") == self.config.project_id
            ]
        if self.config.provider_instance_id is not None:
            matches = [
                item
                for item in matches
                if str(item.get("providerInstanceId") or "")
                == self.config.provider_instance_id
            ]
        if len(matches) > 1:
            raise NativeThreadObserverError("mapping_resolution_ambiguous")
        if len(matches) == 1:
            return matches[0]
        return None

    async def _resolve_route(self, cwd: str | None) -> tuple[str, str]:
        explicit_project = self.config.project_id
        explicit_provider = self.config.provider_instance_id
        if explicit_project and explicit_provider:
            return explicit_project, explicit_provider
        shell = await self._request_json("GET", _SHELL_PATH)
        project = _select_project(
            shell,
            cwd=cwd,
            explicit_project_id=explicit_project,
        )
        project_id = str(project.get("id") or project.get("projectId") or "").strip()
        provider_id = explicit_provider or _project_provider_instance_id(project)
        if not project_id:
            raise NativeThreadObserverError("project_resolution_failed")
        if not provider_id:
            raise NativeThreadObserverError("provider_resolution_failed")
        return project_id, provider_id

    @staticmethod
    def _route_from_mapping(mapping: dict[str, Any]) -> tuple[str, str]:
        project_id = str(mapping.get("projectId") or "").strip()
        provider_id = str(mapping.get("providerInstanceId") or "").strip()
        if not project_id or not provider_id:
            raise NativeThreadObserverError("invalid_mapping_response")
        return project_id, provider_id

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        json_payload: dict[str, Any] | None = None,
    ) -> Any:
        attempts = 2 if method == "POST" else 1
        for attempt in range(attempts):
            token = await self._read_token()
            try:
                response = await self._client.request(
                    method,
                    path,
                    headers={"Authorization": f"Bearer {token}"},
                    json=json_payload,
                )
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                if attempt + 1 < attempts:
                    continue
                raise NativeThreadObserverError("request_timeout") from exc
            status = response.status_code
            if status in _RETRYABLE_STATUSES and attempt + 1 < attempts:
                continue
            if status >= 400:
                raise NativeThreadObserverError(
                    _error_code_for_status(status),
                    http_status=status,
                )
            try:
                return response.json()
            except (json.JSONDecodeError, ValueError) as exc:
                raise NativeThreadObserverError("invalid_response") from exc
        raise NativeThreadObserverError("observer_unavailable")

    async def _read_token(self) -> str:
        try:
            token = await asyncio.to_thread(_read_private_token_file, self.config.token_file)
        except (OSError, UnicodeError) as exc:
            raise NativeThreadObserverError("token_unavailable") from exc
        if not token:
            raise NativeThreadObserverError("token_unavailable")
        return token

    @staticmethod
    def _parse_attachment(
        payload: Any,
        *,
        expected_native_thread_id: str,
        expected_project_id: str,
        expected_provider_instance_id: str,
    ) -> NativeThreadAttachment:
        if not isinstance(payload, dict):
            raise NativeThreadObserverError("invalid_response")
        native_thread_id = str(payload.get("nativeThreadId") or "")
        project_id = str(payload.get("projectId") or "")
        provider_id = str(payload.get("providerInstanceId") or "")
        status = str(payload.get("state") or "").lower()
        if native_thread_id != expected_native_thread_id:
            raise NativeThreadObserverError("native_thread_mismatch")
        if project_id != expected_project_id or provider_id != expected_provider_instance_id:
            raise NativeThreadObserverError("route_mismatch")
        if status != "ready":
            raise NativeThreadObserverError("session_not_ready")
        observer_thread_id = str(payload.get("threadId") or "").strip()
        if not observer_thread_id:
            raise NativeThreadObserverError("invalid_response")
        return NativeThreadAttachment(
            native_thread_id=native_thread_id,
            project_id=project_id,
            provider_instance_id=provider_id,
            observer_thread_id=observer_thread_id,
            disposition="attached",
            status=status,
        )

    def _mark_failure(
        self,
        error: NativeThreadObserverError,
        *,
        started: float,
        error_type: str | None = None,
    ) -> None:
        auth_status = "expired" if error.http_status == 401 else (
            "invalid" if error.http_status == 403 else "unknown"
        )
        mark_integration_health(
            "t3",
            enabled=True,
            requiredForImTurns=True,
            status="degraded",
            reachable=error.code not in {"request_timeout", "observer_unavailable"},
            authStatus=auth_status,
            lastFailureAt=self._clock().astimezone().isoformat(),
            lastErrorCode=error.code,
        )
        data: dict[str, Any] = {
            "duration_ms": round((time.monotonic() - started) * 1000),
            "error_code": error.code,
        }
        if error.http_status is not None:
            data["http_status"] = error.http_status
        if error_type:
            data["error_type"] = error_type
        emit_event(
            component="t3",
            event="t3.attach.failed",
            level="WARNING",
            message="T3 native-thread attachment failed",
            data=data,
        )


def _optional(value: str | None) -> str | None:
    normalized = str(value or "").strip()
    return normalized or None


def _list_items(payload: Any, *keys: str) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        values = payload
    elif isinstance(payload, dict):
        values = []
        for key in keys:
            candidate = payload.get(key)
            if isinstance(candidate, list):
                values = candidate
                break
    else:
        values = []
    return [item for item in values if isinstance(item, dict)]


def _select_project(
    shell: Any,
    *,
    cwd: str | None,
    explicit_project_id: str | None,
) -> dict[str, Any]:
    projects = _list_items(shell, "projects")
    if explicit_project_id:
        matches = [
            project
            for project in projects
            if str(project.get("id") or project.get("projectId") or "") == explicit_project_id
        ]
        if len(matches) != 1:
            raise NativeThreadObserverError("project_resolution_failed")
        return matches[0]
    normalized_cwd = _normalized_path(cwd)
    if normalized_cwd is None:
        raise NativeThreadObserverError("project_resolution_failed")
    ranked: list[tuple[int, dict[str, Any]]] = []
    shell_threads = _list_items(shell, "threads")
    for project in projects:
        project_id = str(project.get("id") or project.get("projectId") or "")
        candidate_paths = [
            project.get("workspaceRoot"),
            project.get("path"),
        ]
        for thread in _list_items(project, "threads"):
            candidate_paths.append(thread.get("worktreePath"))
        for thread in shell_threads:
            if str(thread.get("projectId") or "") == project_id:
                candidate_paths.append(thread.get("worktreePath"))
        scores = [
            len(path.parts)
            for value in candidate_paths
            if (path := _normalized_path(value)) is not None
            and _is_path_ancestor(path, normalized_cwd)
        ]
        if scores:
            ranked.append((max(scores), project))
    if not ranked:
        raise NativeThreadObserverError("project_resolution_failed")
    best_score = max(score for score, _ in ranked)
    winners = [project for score, project in ranked if score == best_score]
    if len(winners) != 1:
        raise NativeThreadObserverError("project_resolution_ambiguous")
    return winners[0]


def _project_provider_instance_id(project: dict[str, Any]) -> str | None:
    selection = project.get("defaultModelSelection")
    if isinstance(selection, dict):
        return _optional(selection.get("instanceId"))
    return _optional(project.get("providerInstanceId"))


def _normalized_path(value: Any) -> PurePath | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    return PurePath(os.path.normcase(os.path.abspath(os.path.normpath(raw))))


def _is_path_ancestor(parent: PurePath, child: PurePath) -> bool:
    try:
        child.relative_to(parent)
    except ValueError:
        return False
    return True


def _error_code_for_status(status: int) -> str:
    if status == 401:
        return "auth_invalid"
    if status == 403:
        return "auth_forbidden"
    if status == 404:
        return "attachment_target_not_found"
    if status == 409:
        return "attachment_conflict"
    if status >= 500:
        return "observer_unavailable"
    return "invalid_request"


def _read_private_token_file(path: Path) -> str:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    if os.name != "nt":
        flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise OSError("token source is not a regular file")
        if os.name != "nt" and stat.S_IMODE(info.st_mode) & 0o077:
            raise OSError("token source is not private")
        with os.fdopen(descriptor, "r", encoding="utf-8", closefd=False) as stream:
            token = stream.read().strip()
    finally:
        os.close(descriptor)
    if not token:
        raise OSError("token source is empty")
    return token
