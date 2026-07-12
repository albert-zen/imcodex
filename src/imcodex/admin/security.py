from __future__ import annotations

import ipaddress
import secrets
from urllib.parse import urlsplit

from starlette.responses import JSONResponse


ADMIN_PATH_PREFIX = "/admin"
ADMIN_CSRF_HEADER = b"x-imcodex-csrf"
_MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_SECURITY_HEADERS = (
    (
        b"content-security-policy",
        b"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        b"connect-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'",
    ),
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"no-referrer"),
    (b"cache-control", b"no-store, max-age=0"),
)


class AdminAccessGuard:
    """Keep the configuration console local even when the main HTTP bind is public."""

    def __init__(self, app, *, csrf_token: str) -> None:
        self.app = app
        self.csrf_token = csrf_token

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") != "http" or not _is_admin_path(str(scope.get("path") or "")):
            await self.app(scope, receive, send)
            return

        denial = self._denial(scope)
        if denial is not None:
            status_code, detail = denial
            response = JSONResponse({"detail": detail}, status_code=status_code)
            await response(scope, receive, self._secure_send(send))
            return

        await self.app(scope, receive, self._secure_send(send))

    def _denial(self, scope) -> tuple[int, str] | None:
        client = scope.get("client")
        client_host = str(client[0]) if isinstance(client, (tuple, list)) and client else ""
        if not _is_loopback_host(client_host):
            return 403, "The IMCodex configuration console is available only from this computer."

        host = _header(scope, b"host").decode("latin-1").strip()
        if not host or not _is_loopback_authority(host):
            return 421, "The configuration console requires a loopback Host header."

        method = str(scope.get("method") or "GET").upper()
        if method not in _MUTATING_METHODS:
            return None

        supplied = _header(scope, ADMIN_CSRF_HEADER).decode("ascii", errors="ignore")
        if not supplied or not secrets.compare_digest(supplied, self.csrf_token):
            return 403, "The configuration console CSRF token is missing or invalid."

        origin = _header(scope, b"origin").decode("latin-1").strip()
        if origin and not _origin_matches_request(origin, scope=scope, host=host):
            return 403, "The configuration console rejected a cross-origin request."
        return None

    @staticmethod
    def _secure_send(send):
        async def secured(message) -> None:
            if message.get("type") == "http.response.start":
                headers = list(message.get("headers") or ())
                existing = {name.lower() for name, _value in headers}
                headers.extend(header for header in _SECURITY_HEADERS if header[0] not in existing)
                message = {**message, "headers": headers}
            await send(message)

        return secured


def generate_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def _is_admin_path(path: str) -> bool:
    return path == ADMIN_PATH_PREFIX or path.startswith(f"{ADMIN_PATH_PREFIX}/")


def _header(scope, name: bytes) -> bytes:
    for key, value in scope.get("headers") or ():
        if key.lower() == name:
            return value
    return b""


def _is_loopback_host(host: str) -> bool:
    normalized = host.strip().strip("[]").rstrip(".").lower()
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def _is_loopback_authority(authority: str) -> bool:
    try:
        parsed = urlsplit(f"http://{authority}")
        if parsed.username is not None or parsed.password is not None:
            return False
        if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
            return False
        if parsed.port is not None and not 1 <= parsed.port <= 65535:
            return False
    except ValueError:
        return False
    return bool(parsed.hostname and _is_loopback_host(parsed.hostname))


def _origin_matches_request(origin: str, *, scope, host: str) -> bool:
    try:
        parsed = urlsplit(origin)
    except ValueError:
        return False
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return False
    if parsed.username is not None or parsed.password is not None:
        return False
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        return False
    if not parsed.hostname or not _is_loopback_host(parsed.hostname):
        return False
    request_scheme = str(scope.get("scheme") or "http").lower()
    return parsed.scheme == request_scheme and parsed.netloc.lower() == host.lower()
