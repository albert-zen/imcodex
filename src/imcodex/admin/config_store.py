"""Comment-preserving, revision-checked storage for the admin config console."""

from __future__ import annotations

import hashlib
import hmac
import importlib.util
import ipaddress
import os
import re
import secrets
import subprocess
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Iterator, Mapping
from urllib.parse import urlsplit

from ..app_server_target import resolve_app_server_target

from .config_schema import (
    CONFIG_FIELDS,
    CONFIG_FIELDS_BY_KEY,
    ConfigFieldDefinition,
    FieldValueError,
)


_ASSIGNMENT = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=(.*?)(?:\r?\n)?$")
_DOTENV_PROVENANCE_KEY = "IMCODEX_DOTENV_IMPORTED_KEYS"
_LAUNCHER_RELOADABLE_KEY = "IMCODEX_LAUNCHER_RELOADABLE_KEYS"
_WINDOWS_LOCK_TIMEOUT_S = 30.0


class ConfigStoreError(RuntimeError):
    """Base error for configuration storage failures."""


class ConfigConflictError(ConfigStoreError):
    def __init__(self, *, expected: str, current: str) -> None:
        super().__init__("Configuration changed since it was loaded; reload and try again")
        self.expected = expected
        self.current = current
        self.expected_revision = expected
        self.current_revision = current


class ConfigValidationError(ConfigStoreError, ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ConfigFieldState:
    definition: ConfigFieldDefinition
    source: str
    editable: bool
    configured: bool
    value: object | None = None
    overridden_by: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        result = self.definition.as_dict()
        result.update(
            source=self.source,
            editable=self.editable,
            configured=self.configured,
        )
        if self.overridden_by:
            result["overridden_by"] = list(self.overridden_by)
        if not self.definition.secret:
            result["value"] = self.value
        return result


@dataclass(frozen=True, slots=True)
class ConfigSnapshot:
    revision: str
    fields: tuple[ConfigFieldState, ...]

    @property
    def values(self) -> dict[str, object | None]:
        return {state.definition.key: state.value for state in self.fields if not state.definition.secret}

    @property
    def secrets(self) -> dict[str, dict[str, object]]:
        return {
            state.definition.key: {
                "configured": state.configured,
                "source": state.source,
                "editable": state.editable,
                **({"overridden_by": list(state.overridden_by)} if state.overridden_by else {}),
            }
            for state in self.fields
            if state.definition.secret
        }

    def as_dict(self) -> dict[str, object]:
        return {
            "revision": self.revision,
            "fields": [state.as_dict() for state in self.fields],
            "values": self.values,
            "secrets": self.secrets,
        }

    def to_dict(self) -> dict[str, object]:
        return self.as_dict()


class ConfigStore:
    """Read and update the whitelisted `.env` surface used by the admin UI."""

    def __init__(
        self,
        path: Path | str = Path(".env"),
        *,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self.path = Path(path)
        self.environ = os.environ if environ is None else environ
        self._lock = Lock()
        self._revision_key = secrets.token_bytes(32)
        self._startup_revision = _revision(self._read_bytes(), self._revision_key)

    def read(self) -> ConfigSnapshot:
        raw = self._read_bytes()
        return self._snapshot(raw)

    def restart_required(self, snapshot: ConfigSnapshot | None = None) -> bool:
        current = snapshot or self.read()
        return current.revision != self._startup_revision

    def update(
        self,
        *,
        expected_revision: str,
        values: Mapping[str, object] | None = None,
        secrets: Mapping[str, Mapping[str, object]] | None = None,
    ) -> ConfigSnapshot:
        values = values or {}
        secrets = secrets or {}
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with _config_file_lock(self.path):
                raw = self._read_bytes()
                current_revision = _revision(raw, self._revision_key)
                if not hmac.compare_digest(expected_revision, current_revision):
                    raise ConfigConflictError(expected=expected_revision, current=current_revision)

                try:
                    text = raw.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise ConfigStoreError("Configuration file must be valid UTF-8") from exc
                replacements = self._validate_updates(
                    values=values,
                    secrets=secrets,
                    assignments=_assignments(text),
                )
                if not replacements:
                    return self._snapshot(raw)

                updated = _apply_replacements(text, replacements)
                updated_raw = updated.encode("utf-8")
                latest_revision = _revision(self._read_bytes(), self._revision_key)
                if not hmac.compare_digest(latest_revision, current_revision):
                    raise ConfigConflictError(
                        expected=current_revision,
                        current=latest_revision,
                    )
                self._write_atomic(updated_raw)
                return self._snapshot(updated_raw)

    def _validate_updates(
        self,
        *,
        values: Mapping[str, object],
        secrets: Mapping[str, Mapping[str, object]],
        assignments: Mapping[str, str],
    ) -> dict[str, str | None]:
        overlap = set(values) & set(secrets)
        if overlap:
            raise ConfigValidationError(
                f"Fields cannot appear in both values and secrets: {', '.join(sorted(overlap))}"
            )

        replacements: dict[str, str | None] = {}
        for key, value in values.items():
            field = self._field(key)
            if field.secret:
                raise ConfigValidationError(f"{key} must be updated through the secrets payload")
            self._ensure_editable(field)
            try:
                replacements[key] = field.validate(value)
            except FieldValueError as exc:
                raise ConfigValidationError(str(exc)) from exc

        for key, update in secrets.items():
            field = self._field(key)
            if not field.secret:
                raise ConfigValidationError(f"{key} is not a secret field")
            self._ensure_editable(field)
            if not isinstance(update, Mapping):
                raise ConfigValidationError(f"{key} secret update must be an object")
            action = update.get("action", "preserve")
            if action == "preserve":
                if set(update) - {"action"}:
                    raise ConfigValidationError(f"{key} preserve does not accept a value")
                continue
            if action == "clear":
                if set(update) - {"action"}:
                    raise ConfigValidationError(f"{key} clear does not accept a value")
                replacements[key] = None
                continue
            if action != "replace":
                raise ConfigValidationError(f"{key} secret action must be preserve, replace, or clear")
            if set(update) - {"action", "value"} or "value" not in update:
                raise ConfigValidationError(f"{key} replace requires only a value")
            try:
                replacements[key] = field.validate(update["value"], secret_replacement=True)
            except FieldValueError as exc:
                raise ConfigValidationError(str(exc)) from exc

        self._validate_retry_pairs(replacements, assignments=assignments)
        self._validate_channel_requirements(
            replacements,
            assignments=assignments,
        )
        return replacements

    def _validate_retry_pairs(
        self,
        replacements: Mapping[str, str | None],
        *,
        assignments: Mapping[str, str],
    ) -> None:
        pairs = (
            (
                "IMCODEX_APP_SERVER_RETRY_INITIAL_DELAY",
                "IMCODEX_APP_SERVER_RETRY_MAX_DELAY",
            ),
            (
                "IMCODEX_APP_SERVER_RECONNECT_INITIAL_DELAY",
                "IMCODEX_APP_SERVER_RECONNECT_MAX_DELAY",
            ),
        )
        for initial_key, maximum_key in pairs:
            if initial_key not in replacements and maximum_key not in replacements:
                continue

            def effective(key: str) -> str | None:
                field = CONFIG_FIELDS_BY_KEY[key]
                if self._process_overrides(field):
                    return _effective_environment(field, self.environ)
                if key in replacements:
                    return replacements[key]
                return _effective_assignment(field, assignments)

            initial = effective(initial_key)
            maximum = effective(maximum_key)
            initial = str(CONFIG_FIELDS_BY_KEY[initial_key].default) if initial in {None, ""} else initial
            maximum = str(CONFIG_FIELDS_BY_KEY[maximum_key].default) if maximum in {None, ""} else maximum
            try:
                invalid = float(str(maximum)) < float(str(initial))
            except ValueError:
                invalid = False
            if invalid:
                raise ConfigValidationError(f"{maximum_key} must be at least {initial_key}")

    def _field(self, key: str) -> ConfigFieldDefinition:
        field = CONFIG_FIELDS_BY_KEY.get(key)
        if field is None:
            raise ConfigValidationError(f"Unsupported configuration field: {key}")
        return field

    def _ensure_editable(self, field: ConfigFieldDefinition) -> None:
        overrides = self._process_overrides(field)
        if overrides:
            raise ConfigValidationError(f"{field.key} is controlled by the process environment and is not editable")

    def _snapshot(self, raw: bytes) -> ConfigSnapshot:
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ConfigStoreError("Configuration file must be valid UTF-8") from exc
        assignments = _assignments(text)
        states = tuple(self._field_state(field, assignments) for field in CONFIG_FIELDS)
        return ConfigSnapshot(revision=_revision(raw, self._revision_key), fields=states)

    def _field_state(self, field: ConfigFieldDefinition, assignments: Mapping[str, str]) -> ConfigFieldState:
        overrides = self._process_overrides(field)
        raw: str | None
        if overrides:
            raw = (
                _effective_target_environment(self.environ)
                if field.key == "IMCODEX_APP_SERVER_URL"
                else _effective_environment(field, self.environ)
            )
            configured = any(bool(self.environ[name].strip()) for name in overrides)
            source = "environment"
        elif field.key == "IMCODEX_APP_SERVER_URL":
            raw = _effective_target_mapping(assignments)
            target_present = any(key in assignments for key in field.storage_keys)
            configured = any(bool(str(assignments.get(key) or "").strip()) for key in field.storage_keys)
            source = "dotenv" if target_present else "default"
        else:
            raw = _effective_assignment(field, assignments)
            configured = raw is not None and bool(raw.strip())
            source = "dotenv" if raw is not None else "default"
        if field.secret:
            value = None
        elif raw is None:
            value = field.default
        else:
            value = field.parse(raw)
        return ConfigFieldState(
            definition=field,
            source=source,
            editable=not overrides,
            configured=configured,
            value=value,
            overridden_by=overrides,
        )

    def _process_overrides(self, field: ConfigFieldDefinition) -> tuple[str, ...]:
        imported = _csv_environment_keys(
            self.environ,
            _DOTENV_PROVENANCE_KEY,
            _LAUNCHER_RELOADABLE_KEY,
        )
        return tuple(
            name
            for name in field.process_names
            if name in self.environ
            and name not in imported
            and (not field.environment_group or bool(str(self.environ[name]).strip()))
        )

    def _read_bytes(self) -> bytes:
        try:
            return self.path.read_bytes()
        except FileNotFoundError:
            return b""
        except OSError as exc:
            raise ConfigStoreError(f"Could not read configuration file: {self.path}") from exc

    def _write_atomic(self, raw: bytes) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary_name = tempfile.mkstemp(prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent)
        temporary = Path(temporary_name)
        try:
            if os.name == "posix":
                os.fchmod(fd, 0o600)
            elif os.name == "nt":
                _secure_windows_file(temporary)
            with os.fdopen(fd, "wb") as handle:
                fd = -1
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            if os.name == "nt" and self.path.exists():
                _secure_windows_file(self.path)
            _replace_file(temporary, self.path)
            if os.name == "nt":
                _secure_windows_file(self.path)
        except OSError as exc:
            raise ConfigStoreError(f"Could not write configuration file: {self.path}") from exc
        finally:
            if fd >= 0:
                os.close(fd)
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def _validate_channel_requirements(
        self,
        replacements: Mapping[str, str | None],
        *,
        assignments: Mapping[str, str],
    ) -> None:
        def touched(*keys: str) -> bool:
            return bool(set(keys) & set(replacements))

        def raw(key: str) -> str | None:
            field = CONFIG_FIELDS_BY_KEY[key]
            if self._process_overrides(field):
                return _effective_environment(field, self.environ)
            if key in replacements:
                return replacements[key]
            return _effective_assignment(field, assignments)

        def enabled(key: str) -> bool:
            value = raw(key)
            if value is None:
                return bool(CONFIG_FIELDS_BY_KEY[key].default)
            return str(value).strip().lower() in {"1", "true", "yes", "on"}

        def configured(key: str) -> bool:
            return bool(str(raw(key) or "").strip())

        qq_keys = (
            "IMCODEX_QQ_ENABLED",
            "IMCODEX_QQ_APP_ID",
            "IMCODEX_QQ_CLIENT_SECRET",
        )
        if touched(*qq_keys) and enabled("IMCODEX_QQ_ENABLED"):
            if not configured("IMCODEX_QQ_APP_ID") or not configured("IMCODEX_QQ_CLIENT_SECRET"):
                raise ConfigValidationError("QQ requires an App ID and client secret before it can be enabled")

        telegram_keys = (
            "IMCODEX_TELEGRAM_ENABLED",
            "IMCODEX_TELEGRAM_BOT_TOKEN",
            "IMCODEX_TELEGRAM_BOT_TOKEN_FILE",
        )
        if touched(*telegram_keys) and enabled("IMCODEX_TELEGRAM_ENABLED"):
            if not configured("IMCODEX_TELEGRAM_BOT_TOKEN") and not configured("IMCODEX_TELEGRAM_BOT_TOKEN_FILE"):
                raise ConfigValidationError("Telegram requires a bot token or bot token file before it can be enabled")
            if not configured("IMCODEX_TELEGRAM_BOT_TOKEN"):
                token_path = self._resolved_path(raw("IMCODEX_TELEGRAM_BOT_TOKEN_FILE"))
                try:
                    from ..channels.telegram import read_telegram_bot_token_file

                    read_telegram_bot_token_file(token_path)
                except (OSError, RuntimeError, ValueError) as exc:
                    raise ConfigValidationError(f"Telegram bot token file is not usable: {token_path}") from exc

        feishu_keys = (
            "IMCODEX_FEISHU_ENABLED",
            "IMCODEX_FEISHU_APP_ID",
            "IMCODEX_FEISHU_APP_SECRET",
        )
        if touched(*feishu_keys) and enabled("IMCODEX_FEISHU_ENABLED"):
            if not configured("IMCODEX_FEISHU_APP_ID") or not configured("IMCODEX_FEISHU_APP_SECRET"):
                raise ConfigValidationError("Feishu requires an App ID and app secret before it can be enabled")
            if importlib.util.find_spec("lark_channel") is None:
                raise ConfigValidationError(
                    "Feishu support is not installed; install imcodex with the feishu extra first"
                )

        weixin_keys = (
            "IMCODEX_WEIXIN_ENABLED",
            "IMCODEX_WEIXIN_STATE_DIR",
            "IMCODEX_DATA_DIR",
        )
        if touched(*weixin_keys) and enabled("IMCODEX_WEIXIN_ENABLED"):
            state_dir = raw("IMCODEX_WEIXIN_STATE_DIR")
            if state_dir:
                resolved_state_dir = self._resolved_path(state_dir)
            else:
                data_dir = raw("IMCODEX_DATA_DIR") or str(CONFIG_FIELDS_BY_KEY["IMCODEX_DATA_DIR"].default)
                resolved_state_dir = self._resolved_path(data_dir) / "channels" / "weixin"
            try:
                from ..channels.weixin_state import WeixinStateStore

                credentials = WeixinStateStore(resolved_state_dir).load_credentials()
            except (OSError, RuntimeError, ValueError) as exc:
                raise ConfigValidationError(
                    "Weixin login state is not usable; run the Weixin login command before enabling it"
                ) from exc
            if credentials is None:
                raise ConfigValidationError("Weixin is not logged in; run the Weixin login command before enabling it")

        webhook_keys = (
            "IMCODEX_OUTBOUND_URL",
            "IMCODEX_OUTBOUND_WEBHOOK_TOKEN",
        )
        if touched(*webhook_keys) and configured("IMCODEX_OUTBOUND_URL"):
            outbound_url = str(raw("IMCODEX_OUTBOUND_URL") or "")
            if not _is_loopback_url(outbound_url) and not configured("IMCODEX_OUTBOUND_WEBHOOK_TOKEN"):
                raise ConfigValidationError("Remote outbound webhooks require an outbound bearer token")

    def _resolved_path(self, value: object) -> Path:
        path = Path(str(value or "").strip()).expanduser()
        return path if path.is_absolute() else self.path.parent / path


def _revision(raw: bytes, key: bytes) -> str:
    return hmac.new(key, raw, hashlib.sha256).hexdigest()


@contextmanager
def _config_file_lock(path: Path) -> Iterator[None]:
    """Serialize console writers across processes without storing config state."""

    lock_path = path.with_name(f".{path.name}.imcodex.lock")
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    locked = False
    try:
        if os.name == "nt":
            if os.fstat(fd).st_size == 0:
                os.write(fd, b"\0")
            os.lseek(fd, 0, os.SEEK_SET)
            _acquire_windows_file_lock(fd)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX)
        locked = True
        yield
    except OSError as exc:
        raise ConfigStoreError(f"Could not lock configuration file: {path}") from exc
    finally:
        try:
            if locked and os.name == "nt":
                import msvcrt

                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            elif locked:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)


def _acquire_windows_file_lock(fd: int) -> None:
    """Acquire the lock byte with an observable deadline instead of msvcrt's hidden retry."""

    import errno
    import msvcrt

    deadline = time.monotonic() + _WINDOWS_LOCK_TIMEOUT_S
    while True:
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return
        except OSError as exc:
            if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                raise
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ConfigStoreError("Timed out waiting for another configuration writer") from exc
            time.sleep(min(0.05, remaining))


def _decode_value(raw: str) -> str:
    return raw.strip().strip("\"'")


def _assignments(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in text.splitlines(keepends=True):
        match = _ASSIGNMENT.match(line)
        if match is not None:
            values[match.group(1)] = _decode_value(match.group(2))
    return values


def _effective_assignment(field: ConfigFieldDefinition, assignments: Mapping[str, str]) -> str | None:
    primary = assignments.get(field.key)
    if primary is not None and (primary.strip() or not field.aliases):
        return primary
    for alias in field.aliases:
        value = assignments.get(alias)
        if value is not None:
            return value
    return primary


def _effective_environment(
    field: ConfigFieldDefinition,
    environ: Mapping[str, str],
) -> str | None:
    primary = environ.get(field.key)
    if primary is not None and (primary.strip() or not field.aliases):
        return primary
    for alias in field.aliases:
        value = environ.get(alias)
        if value is not None:
            return value
    return primary


def _effective_target_environment(environ: Mapping[str, str]) -> str | None:
    return _effective_target_mapping(environ)


def _effective_target_mapping(values: Mapping[str, str]) -> str | None:
    app_server_url = str(values.get("IMCODEX_APP_SERVER_URL") or "").strip() or None
    core_url = str(values.get("IMCODEX_CORE_URL") or "").strip() or None
    core_mode = str(values.get("IMCODEX_CORE_MODE") or "").strip() or None
    core_port = str(values.get("IMCODEX_CORE_PORT") or "").strip() or None
    if core_port is not None and app_server_url is None and core_url is None:
        try:
            port = int(core_port)
        except ValueError:
            return None
        if not 1 <= port <= 65535:
            return None
        core_url = f"ws://127.0.0.1:{port}"
        core_mode = core_mode or "dedicated-ws"
    try:
        return resolve_app_server_target(
            app_server_url=app_server_url,
            core_url=core_url,
            core_mode=core_mode,
        ).endpoint
    except ValueError:
        return None


def _csv_environment_keys(
    environ: Mapping[str, str],
    *names: str,
) -> set[str]:
    return {key.strip() for name in names for key in str(environ.get(name) or "").split(",") if key.strip()}


def _is_loopback_url(value: str) -> bool:
    host = str(urlsplit(value).hostname or "").rstrip(".").lower()
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _replace_file(temporary: Path, destination: Path) -> None:
    if os.name != "nt" or not destination.exists():
        os.replace(temporary, destination)
        return

    import ctypes
    from ctypes import wintypes

    replace_file = ctypes.WinDLL("kernel32", use_last_error=True).ReplaceFileW
    replace_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.LPVOID,
    )
    replace_file.restype = wintypes.BOOL
    if not replace_file(str(destination), str(temporary), None, 0x1, None, None):
        raise ctypes.WinError(ctypes.get_last_error())


def _secure_windows_file(path: Path) -> None:
    import csv
    import ctypes
    from ctypes import wintypes

    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    identity = subprocess.run(
        ["whoami", "/user", "/fo", "csv", "/nh"],
        capture_output=True,
        text=True,
        check=False,
        creationflags=creation_flags,
    )
    try:
        sid = next(csv.reader([identity.stdout.strip()]))[-1].strip()
    except (IndexError, StopIteration) as exc:
        raise OSError("Could not determine the current Windows user SID") from exc
    if identity.returncode != 0 or not re.fullmatch(r"S-\d+(?:-\d+)+", sid):
        raise OSError("Could not determine the current Windows user SID")

    # icacls /grant:r only replaces ACEs for the named SID; it leaves any
    # explicit Everyone or other-user ACEs in place.  Build a fresh protected
    # DACL instead so an already-permissive destination is actually narrowed
    # before ReplaceFileW preserves its security descriptor.
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    convert = advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW
    convert.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.DWORD),
    )
    convert.restype = wintypes.BOOL
    get_dacl = advapi32.GetSecurityDescriptorDacl
    get_dacl.argtypes = (
        wintypes.LPVOID,
        ctypes.POINTER(wintypes.BOOL),
        ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.BOOL),
    )
    get_dacl.restype = wintypes.BOOL
    set_named_security = advapi32.SetNamedSecurityInfoW
    set_named_security.argtypes = (
        wintypes.LPWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.LPVOID,
        wintypes.LPVOID,
        wintypes.LPVOID,
    )
    set_named_security.restype = wintypes.DWORD
    local_free = kernel32.LocalFree
    local_free.argtypes = (wintypes.HLOCAL,)
    local_free.restype = wintypes.HLOCAL

    security_descriptor = wintypes.LPVOID()
    descriptor_size = wintypes.DWORD()
    if not convert(
        f"D:P(A;;FA;;;{sid})",
        1,  # SDDL_REVISION_1
        ctypes.byref(security_descriptor),
        ctypes.byref(descriptor_size),
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        dacl_present = wintypes.BOOL()
        dacl_defaulted = wintypes.BOOL()
        dacl = wintypes.LPVOID()
        if not get_dacl(
            security_descriptor,
            ctypes.byref(dacl_present),
            ctypes.byref(dacl),
            ctypes.byref(dacl_defaulted),
        ) or not dacl_present:
            raise ctypes.WinError(ctypes.get_last_error())
        error = set_named_security(
            str(path),
            1,  # SE_FILE_OBJECT
            0x00000004 | 0x80000000,  # DACL_SECURITY_INFORMATION | PROTECTED_DACL_SECURITY_INFORMATION
            None,
            None,
            dacl,
            None,
        )
        if error:
            raise ctypes.WinError(error)
    finally:
        local_free(security_descriptor)


def _apply_replacements(text: str, replacements: Mapping[str, str | None]) -> str:
    lines = text.splitlines(keepends=True)
    newline = "\r\n" if any(line.endswith("\r\n") for line in lines) else "\n"
    for field in CONFIG_FIELDS:
        if field.key not in replacements:
            continue
        indexes = [
            index
            for index, line in enumerate(lines)
            if (match := _ASSIGNMENT.match(line)) is not None and match.group(1) in field.storage_keys
        ]
        replacement = replacements[field.key]
        if replacement is None:
            for index in reversed(indexes):
                del lines[index]
            continue

        serialized = f"{field.key}={_encode_value(replacement)}"
        if indexes:
            target = indexes[-1]
            ending = "\r\n" if lines[target].endswith("\r\n") else "\n" if lines[target].endswith("\n") else ""
            lines[target] = serialized + ending
            for index in reversed(indexes[:-1]):
                del lines[index]
        else:
            if lines and not lines[-1].endswith(("\n", "\r")):
                lines[-1] += newline
            lines.append(serialized + newline)
    return "".join(lines)


def _encode_value(value: str) -> str:
    if not value:
        return ""
    encoded = value
    if value != value.strip() or value[0] in {'"', "'"} or value[-1] in {'"', "'"}:
        if "'" not in value:
            encoded = f"'{value}'"
        elif '"' not in value:
            encoded = f'"{value}"'
        else:
            raise ConfigValidationError("Configuration value cannot be represented safely in this dotenv format")
    if _decode_value(encoded) != value:
        raise ConfigValidationError("Configuration value cannot be represented safely in this dotenv format")
    return encoded
