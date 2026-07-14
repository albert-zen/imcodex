from __future__ import annotations

import hashlib
import os
import shutil
import stat
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import pytest

from imcodex.admin import ConfigConflictError, ConfigStore, ConfigValidationError
from imcodex.admin import config_store as config_store_module
from imcodex.channels.weixin_state import WeixinCredentials, WeixinStateStore


def test_missing_file_has_stable_defaults_and_revision(tmp_path: Path) -> None:
    store = ConfigStore(tmp_path / ".env", environ={})

    snapshot = store.read()

    assert snapshot.revision == store.read().revision
    assert len(snapshot.revision) == 64
    assert snapshot.revision != hashlib.sha256(b"").hexdigest()
    assert snapshot.values["IMCODEX_HTTP_PORT"] == 8000
    assert snapshot.values["IMCODEX_QQ_ENABLED"] is False
    assert snapshot.secrets["IMCODEX_QQ_CLIENT_SECRET"] == {
        "configured": False,
        "source": "default",
        "editable": True,
    }
    assert store.restart_required(snapshot) is False


def test_update_preserves_comments_unknown_settings_and_crlf(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_bytes(
        b"# keep this comment\r\nUNKNOWN_SETTING=keep exactly\r\nIMCODEX_HTTP_PORT=8000\r\n# nearby comment\r\n"
    )
    store = ConfigStore(path, environ={})
    original = store.read()

    updated = store.update(
        expected_revision=original.revision,
        values={"IMCODEX_HTTP_PORT": 8123, "IMCODEX_LOG_LEVEL": "DEBUG"},
    )

    contents = path.read_bytes()
    assert b"# keep this comment\r\nUNKNOWN_SETTING=keep exactly\r\n" in contents
    assert b"IMCODEX_HTTP_PORT=8123\r\n# nearby comment\r\n" in contents
    assert contents.endswith(b"IMCODEX_LOG_LEVEL=DEBUG\r\n")
    assert updated.revision != hashlib.sha256(contents).hexdigest()
    assert updated.revision != original.revision


def test_secrets_are_never_returned_from_dotenv_or_environment(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text("IMCODEX_QQ_CLIENT_SECRET=dotenv-secret\n", encoding="utf-8")
    store = ConfigStore(
        path,
        environ={"IMCODEX_TELEGRAM_BOT_TOKEN": "environment-secret"},
    )

    payload = store.read().to_dict()
    rendered = repr(payload)

    assert "dotenv-secret" not in rendered
    assert "environment-secret" not in rendered
    assert "IMCODEX_QQ_CLIENT_SECRET" not in payload["values"]
    assert payload["secrets"]["IMCODEX_QQ_CLIENT_SECRET"] == {
        "configured": True,
        "source": "dotenv",
        "editable": True,
    }
    assert payload["secrets"]["IMCODEX_TELEGRAM_BOT_TOKEN"] == {
        "configured": True,
        "source": "environment",
        "editable": False,
        "overridden_by": ["IMCODEX_TELEGRAM_BOT_TOKEN"],
    }
    secret_field = next(field for field in payload["fields"] if field["key"] == "IMCODEX_QQ_CLIENT_SECRET")
    assert "value" not in secret_field
    assert "default" not in secret_field


def test_secret_updates_support_preserve_replace_and_clear(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text(
        "# secret comment\nIMCODEX_QQ_CLIENT_SECRET=old-secret\nUNCHANGED=yes\n",
        encoding="utf-8",
    )
    store = ConfigStore(path, environ={})

    preserved = store.update(
        expected_revision=store.read().revision,
        secrets={"IMCODEX_QQ_CLIENT_SECRET": {"action": "preserve"}},
    )
    assert "old-secret" in path.read_text(encoding="utf-8")

    replaced = store.update(
        expected_revision=preserved.revision,
        secrets={
            "IMCODEX_QQ_CLIENT_SECRET": {
                "action": "replace",
                "value": "new-secret",
            }
        },
    )
    contents = path.read_text(encoding="utf-8")
    assert "old-secret" not in contents
    assert "IMCODEX_QQ_CLIENT_SECRET=new-secret" in contents
    assert replaced.secrets["IMCODEX_QQ_CLIENT_SECRET"]["configured"] is True

    cleared = store.update(
        expected_revision=replaced.revision,
        secrets={"IMCODEX_QQ_CLIENT_SECRET": {"action": "clear"}},
    )
    contents = path.read_text(encoding="utf-8")
    assert "IMCODEX_QQ_CLIENT_SECRET" not in contents
    assert "# secret comment\nUNCHANGED=yes\n" == contents
    assert cleared.secrets["IMCODEX_QQ_CLIENT_SECRET"]["configured"] is False


def test_process_environment_is_reported_and_cannot_be_edited(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text("IMCODEX_HTTP_PORT=8000\nIMCODEX_APP_SERVER_URL=unix://\n", encoding="utf-8")
    store = ConfigStore(
        path,
        environ={
            "IMCODEX_HTTP_PORT": "9000",
            "IMCODEX_CORE_MODE": "spawned-stdio",
        },
    )
    snapshot = store.read()
    states = {state.definition.key: state for state in snapshot.fields}

    assert snapshot.values["IMCODEX_HTTP_PORT"] == 9000
    assert states["IMCODEX_HTTP_PORT"].source == "environment"
    assert states["IMCODEX_HTTP_PORT"].editable is False
    assert states["IMCODEX_APP_SERVER_URL"].editable is False
    assert states["IMCODEX_APP_SERVER_URL"].overridden_by == ("IMCODEX_CORE_MODE",)
    assert states["IMCODEX_APP_SERVER_URL"].value == "stdio://"

    with pytest.raises(ConfigValidationError, match="process environment"):
        store.update(
            expected_revision=snapshot.revision,
            values={"IMCODEX_HTTP_PORT": 8123},
        )
    with pytest.raises(ConfigValidationError, match="process environment"):
        store.update(
            expected_revision=snapshot.revision,
            values={"IMCODEX_APP_SERVER_URL": "stdio://"},
        )


def test_launcher_imported_dotenv_values_remain_editable(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text(
        "IMCODEX_HTTP_PORT=8000\nIMCODEX_QQ_CLIENT_SECRET=file-secret\n",
        encoding="utf-8",
    )
    store = ConfigStore(
        path,
        environ={
            "IMCODEX_HTTP_PORT": "8000",
            "IMCODEX_QQ_CLIENT_SECRET": "file-secret",
            "IMCODEX_DOTENV_IMPORTED_KEYS": ("IMCODEX_HTTP_PORT,IMCODEX_QQ_CLIENT_SECRET"),
        },
    )

    snapshot = store.read()

    assert snapshot.values["IMCODEX_HTTP_PORT"] == 8000
    assert snapshot.secrets["IMCODEX_QQ_CLIENT_SECRET"] == {
        "configured": True,
        "source": "dotenv",
        "editable": True,
    }
    updated = store.update(
        expected_revision=snapshot.revision,
        values={"IMCODEX_HTTP_PORT": 8123},
        secrets={"IMCODEX_QQ_CLIENT_SECRET": {"action": "clear"}},
    )
    assert updated.values["IMCODEX_HTTP_PORT"] == 8123
    assert updated.secrets["IMCODEX_QQ_CLIENT_SECRET"]["configured"] is False
    assert "file-secret" not in path.read_text(encoding="utf-8")


def test_feishu_environment_alias_matches_runtime_fallback(tmp_path: Path) -> None:
    store = ConfigStore(
        tmp_path / ".env",
        environ={
            "IMCODEX_FEISHU_APP_ID": "",
            "IMCODEX_LARK_APP_ID": "cli_lark",
        },
    )

    snapshot = store.read()
    field = next(state for state in snapshot.fields if state.definition.key == "IMCODEX_FEISHU_APP_ID")

    assert field.value == "cli_lark"
    assert field.source == "environment"
    assert field.editable is False
    assert field.overridden_by == (
        "IMCODEX_FEISHU_APP_ID",
        "IMCODEX_LARK_APP_ID",
    )


def test_blank_legacy_target_environment_does_not_override_dotenv(
    tmp_path: Path,
) -> None:
    path = tmp_path / ".env"
    path.write_text("IMCODEX_APP_SERVER_URL=unix://\n", encoding="utf-8")
    store = ConfigStore(
        path,
        environ={
            "IMCODEX_CORE_MODE": "  ",
            "IMCODEX_CORE_URL": "",
        },
    )

    snapshot = store.read()
    field = next(state for state in snapshot.fields if state.definition.key == "IMCODEX_APP_SERVER_URL")

    assert field.value == "unix://"
    assert field.source == "dotenv"
    assert field.editable is True


@pytest.mark.parametrize(
    ("environ", "endpoint"),
    [
        ({"IMCODEX_CORE_MODE": "spawned-stdio"}, "stdio://"),
        ({"IMCODEX_CORE_URL": "ws://127.0.0.1:9001"}, "ws://127.0.0.1:9001"),
        ({"IMCODEX_CORE_PORT": "9002"}, "ws://127.0.0.1:9002"),
    ],
)
def test_legacy_target_environment_projects_effective_endpoint(
    tmp_path: Path,
    environ: dict[str, str],
    endpoint: str,
) -> None:
    store = ConfigStore(tmp_path / ".env", environ=environ)

    field = next(state for state in store.read().fields if state.definition.key == "IMCODEX_APP_SERVER_URL")

    assert field.value == endpoint
    assert field.source == "environment"
    assert field.editable is False


@pytest.mark.parametrize(
    ("contents", "endpoint"),
    [
        ("IMCODEX_CORE_MODE=spawned-stdio\n", "stdio://"),
        ("IMCODEX_CORE_URL=ws://127.0.0.1:9001\n", "ws://127.0.0.1:9001"),
        ("IMCODEX_CORE_PORT=9002\n", "ws://127.0.0.1:9002"),
    ],
)
def test_legacy_target_dotenv_projects_effective_endpoint(
    tmp_path: Path,
    contents: str,
    endpoint: str,
) -> None:
    path = tmp_path / ".env"
    path.write_text(contents, encoding="utf-8")
    store = ConfigStore(path, environ={})

    field = next(state for state in store.read().fields if state.definition.key == "IMCODEX_APP_SERVER_URL")

    assert field.value == endpoint
    assert field.source == "dotenv"
    assert field.editable is True


def test_saving_canonical_target_removes_all_legacy_target_lines(
    tmp_path: Path,
) -> None:
    path = tmp_path / ".env"
    path.write_text(
        "# target\nIMCODEX_CORE_MODE=dedicated-ws\nIMCODEX_CORE_URL=ws://127.0.0.1:8765\nIMCODEX_CORE_PORT=8765\n",
        encoding="utf-8",
    )
    store = ConfigStore(path, environ={})

    store.update(
        expected_revision=store.read().revision,
        values={"IMCODEX_APP_SERVER_URL": "unix://"},
    )

    contents = path.read_text(encoding="utf-8")
    assert "IMCODEX_APP_SERVER_URL=unix://" in contents
    assert "IMCODEX_CORE_" not in contents


def test_launcher_synthesized_target_is_reloadable_not_an_external_override(
    tmp_path: Path,
) -> None:
    path = tmp_path / ".env"
    store = ConfigStore(
        path,
        environ={
            "IMCODEX_APP_SERVER_URL": "unix://",
            "IMCODEX_LAUNCHER_RELOADABLE_KEYS": "IMCODEX_APP_SERVER_URL",
        },
    )

    before = store.read()
    field = next(state for state in before.fields if state.definition.key == "IMCODEX_APP_SERVER_URL")
    assert field.source == "default"
    assert field.editable is True

    after = store.update(
        expected_revision=before.revision,
        values={"IMCODEX_APP_SERVER_URL": "stdio://"},
    )
    assert after.values["IMCODEX_APP_SERVER_URL"] == "stdio://"


def test_stale_revision_does_not_overwrite_external_change(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text("UNKNOWN=one\n", encoding="utf-8")
    store = ConfigStore(path, environ={})
    stale = store.read()
    path.write_text("UNKNOWN=two\n", encoding="utf-8")
    current = store.read()

    with pytest.raises(ConfigConflictError) as error:
        store.update(
            expected_revision=stale.revision,
            values={"IMCODEX_HTTP_PORT": 8123},
        )

    assert error.value.expected == stale.revision
    assert error.value.current == current.revision
    assert error.value.current != hashlib.sha256(path.read_bytes()).hexdigest()
    assert path.read_text(encoding="utf-8") == "UNKNOWN=two\n"


def test_console_writers_are_serialized_across_store_instances(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text("IMCODEX_HTTP_PORT=8000\n", encoding="utf-8")
    first = ConfigStore(path, environ={})
    second = ConfigStore(path, environ={})
    first_revision = first.read().revision
    second_revision = second.read().revision
    assert first_revision != second_revision
    entered_write = Event()
    release_write = Event()
    original_write = first._write_atomic

    def delayed_write(raw: bytes) -> None:
        entered_write.set()
        assert release_write.wait(timeout=5)
        original_write(raw)

    first._write_atomic = delayed_write  # type: ignore[method-assign]

    with ThreadPoolExecutor(max_workers=2) as executor:
        first_update = executor.submit(
            first.update,
            expected_revision=first_revision,
            values={"IMCODEX_HTTP_PORT": 8123},
        )
        assert entered_write.wait(timeout=5)
        second_update = executor.submit(
            second.update,
            expected_revision=second_revision,
            values={"IMCODEX_HTTP_PORT": 9000},
        )
        release_write.set()

        assert first_update.result(timeout=5).values["IMCODEX_HTTP_PORT"] == 8123
        with pytest.raises(ConfigConflictError):
            second_update.result(timeout=5)

    assert path.read_text(encoding="utf-8") == "IMCODEX_HTTP_PORT=8123\n"


def test_restart_required_tracks_changes_since_store_startup(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text("IMCODEX_HTTP_PORT=8000\n", encoding="utf-8")
    store = ConfigStore(path, environ={})
    before = store.read()

    after = store.update(
        expected_revision=before.revision,
        values={"IMCODEX_HTTP_PORT": 8123},
    )

    assert store.restart_required(before) is False
    assert store.restart_required(after) is True


@pytest.mark.parametrize(
    ("values", "message"),
    [
        ({"IMCODEX_HTTP_PORT": "8000"}, "must be an integer"),
        ({"IMCODEX_HTTP_PORT": 0}, "must be at least 1"),
        ({"IMCODEX_QQ_ENABLED": 1}, "must be a boolean"),
        ({"IMCODEX_LOG_LEVEL": "TRACE"}, "must be one of"),
        ({"IMCODEX_FEISHU_DOMAIN": "example.com"}, "must be one of"),
        ({"IMCODEX_QQ_APP_ID": "bad\nvalue"}, "single line"),
        ({"IMCODEX_QQ_APP_ID": "bad\x00value"}, "single line"),
        ({"IMCODEX_QQ_APP_ID": "'ambiguous"}, "represented safely"),
        ({"IMCODEX_APP_SERVER_URL": "http://127.0.0.1"}, "must use unix://"),
        ({"IMCODEX_QQ_API_BASE": "https://bad host"}, "must not contain whitespace"),
        ({"IMCODEX_QQ_API_BASE": "https://example.com:bad"}, "must be a valid"),
        (
            {"IMCODEX_OUTBOUND_URL": "https://example.com/hook?token=secret"},
            "must not contain query or fragment",
        ),
        (
            {"IMCODEX_OUTBOUND_URL": "http://example.com/hook"},
            "requires HTTPS",
        ),
        ({"NOT_WHITELISTED": "value"}, "Unsupported configuration field"),
    ],
)
def test_values_are_strictly_validated(
    tmp_path: Path,
    values: dict[str, object],
    message: str,
) -> None:
    store = ConfigStore(tmp_path / ".env", environ={})

    with pytest.raises(ConfigValidationError, match=message):
        store.update(expected_revision=store.read().revision, values=values)


@pytest.mark.parametrize(
    "secret_update",
    [
        {"action": "replace", "value": ""},
        {"action": "replace", "value": "bad\nsecret"},
        {"action": "clear", "value": "must-not-be-accepted"},
        {"action": "unknown"},
    ],
)
def test_secret_updates_are_strictly_validated(
    tmp_path: Path,
    secret_update: dict[str, object],
) -> None:
    store = ConfigStore(tmp_path / ".env", environ={})

    with pytest.raises(ConfigValidationError):
        store.update(
            expected_revision=store.read().revision,
            secrets={"IMCODEX_QQ_CLIENT_SECRET": secret_update},
        )


def test_retry_maximum_must_not_be_below_initial_delay(tmp_path: Path) -> None:
    store = ConfigStore(tmp_path / ".env", environ={})

    with pytest.raises(ConfigValidationError, match="must be at least"):
        store.update(
            expected_revision=store.read().revision,
            values={
                "IMCODEX_APP_SERVER_RECONNECT_INITIAL_DELAY": 10.0,
                "IMCODEX_APP_SERVER_RECONNECT_MAX_DELAY": 5.0,
            },
        )


def test_retry_pair_validation_uses_process_environment_precedence(
    tmp_path: Path,
) -> None:
    path = tmp_path / ".env"
    path.write_text(
        "IMCODEX_APP_SERVER_RETRY_MAX_DELAY=30\n",
        encoding="utf-8",
    )
    store = ConfigStore(
        path,
        environ={"IMCODEX_APP_SERVER_RETRY_INITIAL_DELAY": "10"},
    )

    with pytest.raises(ConfigValidationError, match="must be at least"):
        store.update(
            expected_revision=store.read().revision,
            values={"IMCODEX_APP_SERVER_RETRY_MAX_DELAY": 5.0},
        )


def test_qq_cannot_be_enabled_without_credentials(tmp_path: Path) -> None:
    store = ConfigStore(tmp_path / ".env", environ={})

    with pytest.raises(ConfigValidationError, match="QQ requires"):
        store.update(
            expected_revision=store.read().revision,
            values={"IMCODEX_QQ_ENABLED": True},
        )

    updated = store.update(
        expected_revision=store.read().revision,
        values={"IMCODEX_QQ_ENABLED": True, "IMCODEX_QQ_APP_ID": "app-id"},
        secrets={
            "IMCODEX_QQ_CLIENT_SECRET": {
                "action": "replace",
                "value": "secret",
            }
        },
    )
    assert updated.values["IMCODEX_QQ_ENABLED"] is True


def test_telegram_requires_a_token_or_usable_private_token_file(
    tmp_path: Path,
) -> None:
    store = ConfigStore(tmp_path / ".env", environ={})

    with pytest.raises(ConfigValidationError, match="Telegram requires"):
        store.update(
            expected_revision=store.read().revision,
            values={"IMCODEX_TELEGRAM_ENABLED": True},
        )

    token_file = tmp_path / "telegram.token"
    token_file.write_text("123:token\n", encoding="utf-8")
    token_file.chmod(0o600)
    updated = store.update(
        expected_revision=store.read().revision,
        values={
            "IMCODEX_TELEGRAM_ENABLED": True,
            "IMCODEX_TELEGRAM_BOT_TOKEN_FILE": str(token_file),
        },
    )
    assert updated.values["IMCODEX_TELEGRAM_ENABLED"] is True


def test_feishu_requires_credentials_and_installed_transport(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = ConfigStore(tmp_path / ".env", environ={})

    with pytest.raises(ConfigValidationError, match="Feishu requires"):
        store.update(
            expected_revision=store.read().revision,
            values={"IMCODEX_FEISHU_ENABLED": True},
        )

    monkeypatch.setattr(
        config_store_module.importlib.util,
        "find_spec",
        lambda name: object() if name == "lark_channel" else None,
    )
    updated = store.update(
        expected_revision=store.read().revision,
        values={
            "IMCODEX_FEISHU_ENABLED": True,
            "IMCODEX_FEISHU_APP_ID": "cli_test",
        },
        secrets={
            "IMCODEX_FEISHU_APP_SECRET": {
                "action": "replace",
                "value": "secret",
            }
        },
    )
    assert updated.values["IMCODEX_FEISHU_ENABLED"] is True


def test_weixin_cannot_be_enabled_until_login_state_exists(tmp_path: Path) -> None:
    state_dir = tmp_path / "weixin-state"
    store = ConfigStore(tmp_path / ".env", environ={})

    with pytest.raises(ConfigValidationError, match="not logged in"):
        store.update(
            expected_revision=store.read().revision,
            values={
                "IMCODEX_WEIXIN_ENABLED": True,
                "IMCODEX_WEIXIN_STATE_DIR": str(state_dir),
            },
        )

    WeixinStateStore(state_dir).save_credentials(
        WeixinCredentials(
            account_id="bridge@im.bot",
            bot_token="token",
            base_url="https://ilinkai.weixin.qq.com",
        )
    )
    updated = store.update(
        expected_revision=store.read().revision,
        values={
            "IMCODEX_WEIXIN_ENABLED": True,
            "IMCODEX_WEIXIN_STATE_DIR": str(state_dir),
        },
    )
    assert updated.values["IMCODEX_WEIXIN_ENABLED"] is True


def test_remote_outbound_webhook_requires_bearer_token(tmp_path: Path) -> None:
    store = ConfigStore(tmp_path / ".env", environ={})

    with pytest.raises(ConfigValidationError, match="bearer token"):
        store.update(
            expected_revision=store.read().revision,
            values={"IMCODEX_OUTBOUND_URL": "https://example.com/hook"},
        )

    updated = store.update(
        expected_revision=store.read().revision,
        values={"IMCODEX_OUTBOUND_URL": "https://example.com/hook"},
        secrets={
            "IMCODEX_OUTBOUND_WEBHOOK_TOKEN": {
                "action": "replace",
                "value": "token",
            }
        },
    )
    assert updated.values["IMCODEX_OUTBOUND_URL"] == "https://example.com/hook"


def test_feishu_alias_is_canonicalized_without_leaking_secret(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text(
        "# legacy alias\nIMCODEX_LARK_APP_SECRET=legacy-secret\n",
        encoding="utf-8",
    )
    store = ConfigStore(path, environ={})
    before = store.read()
    assert before.secrets["IMCODEX_FEISHU_APP_SECRET"]["configured"] is True
    assert "legacy-secret" not in repr(before.to_dict())

    after = store.update(
        expected_revision=before.revision,
        secrets={
            "IMCODEX_FEISHU_APP_SECRET": {
                "action": "replace",
                "value": "canonical-secret",
            }
        },
    )

    contents = path.read_text(encoding="utf-8")
    assert "IMCODEX_LARK_APP_SECRET" not in contents
    assert "IMCODEX_FEISHU_APP_SECRET=canonical-secret" in contents
    assert "canonical-secret" not in repr(after.to_dict())


@pytest.mark.skipif(os.name != "posix", reason="POSIX mode bits only")
def test_successful_write_restricts_dotenv_permissions(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text("IMCODEX_HTTP_PORT=8000\n", encoding="utf-8")
    path.chmod(0o644)
    store = ConfigStore(path, environ={})

    store.update(
        expected_revision=store.read().revision,
        values={"IMCODEX_HTTP_PORT": 8123},
    )

    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.skipif(os.name != "nt", reason="Windows DACLs only")
def test_successful_write_restricts_existing_windows_dacl(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text("IMCODEX_HTTP_PORT=8000\n", encoding="utf-8")
    subprocess.run(
        [
            "icacls",
            str(path),
            "/grant",
            "*S-1-1-0:(R)",
        ],
        capture_output=True,
        text=True,
        check=True,
    )

    def access_sids() -> set[str]:
        powershell = shutil.which("pwsh") or "powershell.exe"
        script = (
            "$acl = Get-Acl -LiteralPath $env:IMCODEX_TEST_CONFIG_PATH; "
            "$acl.Access | ForEach-Object { "
            "$_.IdentityReference.Translate([System.Security.Principal.SecurityIdentifier]).Value "
            "}"
        )
        environment = os.environ.copy()
        environment["IMCODEX_TEST_CONFIG_PATH"] = str(path)
        result = subprocess.run(
            [powershell, "-NoLogo", "-NoProfile", "-Command", script],
            env=environment,
            capture_output=True,
            text=True,
            check=True,
        )
        return {line.strip() for line in result.stdout.splitlines() if line.strip()}

    assert "S-1-1-0" in access_sids()
    store = ConfigStore(path, environ={})
    store.update(
        expected_revision=store.read().revision,
        values={"IMCODEX_HTTP_PORT": 8123},
    )

    after = access_sids()
    assert "S-1-1-0" not in after
    assert len(after) == 1
