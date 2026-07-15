from __future__ import annotations

import os
from pathlib import Path

import pytest

from imcodex.channels.weixin_login import WeixinLoginError, WeixinLoginFlow
from imcodex.channels.weixin_state import WeixinStateStore
from imcodex.channels_cli import run_channels_cli


class FakeLoginTransport:
    def __init__(self, statuses: list[dict]) -> None:
        self.statuses = list(statuses)
        self.fetch_calls: list[list[str]] = []
        self.poll_calls: list[dict] = []
        self.close_calls = 0

    async def fetch_qr_code(self, *, local_tokens: list[str]) -> dict:
        self.fetch_calls.append(local_tokens)
        index = len(self.fetch_calls)
        return {
            "qrcode": f"qr-secret-{index}",
            "qrcode_img_content": f"https://weixin.qq.com/qr/{index}",
        }

    async def poll_qr_status(self, **payload) -> dict:
        self.poll_calls.append(payload)
        return self.statuses.pop(0)

    async def close(self) -> None:
        self.close_calls += 1


@pytest.mark.asyncio
async def test_weixin_login_flow_persists_confirmed_credentials_without_printing_token(
    tmp_path: Path,
) -> None:
    outputs: list[str] = []
    transport = FakeLoginTransport(
        [
            {
                "status": "confirmed",
                "bot_token": "bot-secret",
                "ilink_bot_id": "bot@im.bot",
                "baseurl": "https://ilinkai.weixin.qq.com",
                "ilink_user_id": "owner@im.wechat",
            }
        ]
    )
    store = WeixinStateStore(tmp_path)
    flow = WeixinLoginFlow(
        state_store=store,
        transport=transport,
        output=outputs.append,
    )

    credentials = await flow.login(timeout_s=30)

    assert credentials.account_id == "bot@im.bot"
    assert credentials.owner_user_id == "owner@im.wechat"
    assert store.load_credentials().bot_token == "bot-secret"
    assert "bot-secret" not in "\n".join(outputs)


@pytest.mark.asyncio
async def test_weixin_login_handles_pair_code_and_official_idc_redirect(
    tmp_path: Path,
) -> None:
    transport = FakeLoginTransport(
        [
            {"status": "need_verifycode"},
            {
                "status": "scaned_but_redirect",
                "redirect_host": "sh.ilinkai.weixin.qq.com",
            },
            {
                "status": "confirmed",
                "bot_token": "bot-secret",
                "ilink_bot_id": "bot@im.bot",
                "baseurl": "https://sh.ilinkai.weixin.qq.com",
                "ilink_user_id": "owner@im.wechat",
            },
        ]
    )
    flow = WeixinLoginFlow(
        state_store=WeixinStateStore(tmp_path),
        transport=transport,
        input_func=lambda _prompt: "123456",
        output=lambda _text: None,
        sleep=lambda _delay: __import__("asyncio").sleep(0),
    )

    await flow.login(timeout_s=30)

    assert transport.poll_calls[1]["verify_code"] == "123456"
    assert transport.poll_calls[2]["base_url"] == "https://sh.ilinkai.weixin.qq.com"


@pytest.mark.asyncio
async def test_weixin_login_rejects_non_weixin_redirect(tmp_path: Path) -> None:
    transport = FakeLoginTransport([{"status": "scaned_but_redirect", "redirect_host": "attacker.example"}])
    flow = WeixinLoginFlow(
        state_store=WeixinStateStore(tmp_path),
        transport=transport,
        output=lambda _text: None,
    )

    with pytest.raises(WeixinLoginError, match="非微信域名"):
        await flow.login(timeout_s=30)


@pytest.mark.parametrize(
    ("account_id", "owner_user_id", "error"),
    [
        ("*", "owner@im.wechat", "bot 凭据"),
        ("bot@im.bot", "*@im.wechat", "扫码用户 ID"),
    ],
)
def test_weixin_login_rejects_wildcard_platform_identities(
    account_id: str,
    owner_user_id: str,
    error: str,
) -> None:
    with pytest.raises(WeixinLoginError, match=error):
        WeixinLoginFlow._credentials_from_confirmation(
            {
                "ilink_bot_id": account_id,
                "bot_token": "bot-secret",
                "baseurl": "https://ilinkai.weixin.qq.com",
                "ilink_user_id": owner_user_id,
            }
        )


def test_channels_cli_lists_builtin_channels(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    outputs: list[str] = []

    result = run_channels_cli(["list"], output=outputs.append)

    assert result == 0
    assert any("telegram" in line for line in outputs)
    assert any("feishu" in line for line in outputs)
    assert any("weixin" in line and "experimental" in line for line in outputs)


def test_channels_cli_doctor_reports_enabled_channel_without_secrets(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IMCODEX_TELEGRAM_ENABLED", "1")
    monkeypatch.setenv("IMCODEX_TELEGRAM_ALLOWED_USER_IDS", "42")
    outputs: list[str] = []

    result = run_channels_cli(["doctor"], output=outputs.append)

    rendered = "\n".join(outputs)
    assert result == 1
    assert "missing bot token" in rendered
    assert "Bearer" not in rendered


def test_channels_cli_doctor_accepts_qq_without_access_restrictions(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IMCODEX_QQ_ENABLED", "1")
    monkeypatch.setenv("IMCODEX_QQ_APP_ID", "app-id")
    monkeypatch.setenv("IMCODEX_QQ_CLIENT_SECRET", "secret")
    outputs: list[str] = []

    result = run_channels_cli(["doctor"], output=outputs.append)

    assert result == 0
    assert outputs == ["Channel configuration looks ready."]


@pytest.mark.parametrize("access_value", ["none,owner", "owner"])
def test_channels_cli_doctor_rejects_invalid_access_policy(
    monkeypatch,
    tmp_path: Path,
    access_value: str,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IMCODEX_QQ_ENABLED", "1")
    monkeypatch.setenv("IMCODEX_QQ_APP_ID", "app-id")
    monkeypatch.setenv("IMCODEX_QQ_CLIENT_SECRET", "secret")
    monkeypatch.setenv("IMCODEX_QQ_ALLOWED_USER_IDS", access_value)
    if access_value == "owner":
        monkeypatch.setenv("IMCODEX_QQ_ACCESS_MATCH", "bogus")
    outputs: list[str] = []

    result = run_channels_cli(["doctor"], output=outputs.append)

    assert result == 1
    assert "qq: invalid access restrictions" in "\n".join(outputs)


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission enforcement")
def test_channels_cli_doctor_rejects_insecure_telegram_token_file(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    token_file = tmp_path / "telegram-token"
    token_file.write_text("secret", encoding="utf-8")
    os.chmod(token_file, 0o644)
    monkeypatch.setenv("IMCODEX_TELEGRAM_ENABLED", "1")
    monkeypatch.setenv("IMCODEX_TELEGRAM_BOT_TOKEN_FILE", str(token_file))
    monkeypatch.setenv("IMCODEX_TELEGRAM_ALLOWED_USER_IDS", "42")
    outputs: list[str] = []

    result = run_channels_cli(["doctor"], output=outputs.append)

    assert result == 1
    assert "private file (0600)" in "\n".join(outputs)


def test_channels_cli_doctor_rejects_invalid_feishu_domain(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IMCODEX_FEISHU_ENABLED", "1")
    monkeypatch.setenv("IMCODEX_FEISHU_APP_ID", "cli_app")
    monkeypatch.setenv("IMCODEX_FEISHU_APP_SECRET", "secret")
    monkeypatch.setenv("IMCODEX_FEISHU_DOMAIN", "example.com")
    monkeypatch.setenv("IMCODEX_FEISHU_ALLOWED_USER_IDS", "ou_owner")
    monkeypatch.setattr(
        "imcodex.channels_cli.importlib.util.find_spec",
        lambda _name: object(),
    )
    outputs: list[str] = []

    result = run_channels_cli(["doctor"], output=outputs.append)

    rendered = "\n".join(outputs)
    assert result == 1
    assert "feishu: invalid domain" in rendered
    assert "must be 'feishu' or 'lark'" in rendered


def test_channels_cli_runs_weixin_login_and_closes_transport(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IMCODEX_DATA_DIR", str(tmp_path / "data"))
    transport = FakeLoginTransport(
        [
            {
                "status": "confirmed",
                "bot_token": "bot-secret",
                "ilink_bot_id": "bot@im.bot",
                "baseurl": "https://ilinkai.weixin.qq.com",
                "ilink_user_id": "owner@im.wechat",
            }
        ]
    )
    outputs: list[str] = []

    result = run_channels_cli(
        ["login", "weixin", "--timeout", "30"],
        transport_factory=lambda: transport,
        output=outputs.append,
    )

    store = WeixinStateStore(tmp_path / "data" / "channels" / "weixin")
    assert result == 0
    assert store.load_credentials().account_id == "bot@im.bot"
    assert transport.close_calls == 1
    assert "bot-secret" not in "\n".join(outputs)


def test_channels_cli_logout_requires_confirmation(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("IMCODEX_DATA_DIR", str(tmp_path / "data"))
    store = WeixinStateStore(tmp_path / "data" / "channels" / "weixin")
    store.credentials_path.parent.mkdir(parents=True)
    store.credentials_path.write_text("{}", encoding="utf-8")
    outputs: list[str] = []

    cancelled = run_channels_cli(
        ["logout", "weixin"],
        output=outputs.append,
        input_func=lambda _prompt: "n",
    )
    removed = run_channels_cli(
        ["logout", "weixin", "--yes"],
        output=outputs.append,
    )

    assert cancelled == 1
    assert removed == 0
    assert not store.credentials_path.exists()
