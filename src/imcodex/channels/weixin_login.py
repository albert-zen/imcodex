from __future__ import annotations

import asyncio
import re
import time
from typing import Callable
from urllib.parse import urlparse

from .weixin_ilink import DEFAULT_ILINK_BASE_URL, ILinkError
from .weixin_state import WeixinCredentials, WeixinStateStore


class WeixinLoginError(RuntimeError):
    pass


class WeixinLoginFlow:
    def __init__(
        self,
        *,
        state_store: WeixinStateStore,
        transport,
        input_func: Callable[[str], str] = input,
        output: Callable[[str], object] = print,
        sleep=asyncio.sleep,
        clock=time.monotonic,
    ) -> None:
        self.state_store = state_store
        self.transport = transport
        self.input_func = input_func
        self.output = output
        self.sleep = sleep
        self.clock = clock

    async def login(self, *, timeout_s: float = 480.0) -> WeixinCredentials:
        existing = self.state_store.load_credentials()
        local_tokens = [existing.bot_token] if existing is not None else []
        qrcode, qrcode_url = await self._fetch_qr(local_tokens)
        self._show_qr(qrcode_url)

        deadline = self.clock() + max(1.0, timeout_s)
        poll_base_url = DEFAULT_ILINK_BASE_URL
        verify_code = ""
        scanned_reported = False
        refresh_count = 0
        poll_failures = 0

        while self.clock() < deadline:
            try:
                response = await self.transport.poll_qr_status(
                    qrcode=qrcode,
                    verify_code=verify_code,
                    base_url=poll_base_url,
                )
                poll_failures = 0
            except ILinkError:
                poll_failures += 1
                if poll_failures >= 3:
                    raise WeixinLoginError("二维码状态查询连续失败，请检查网络后重试。") from None
                await self.sleep(2.0)
                continue
            status = str(response.get("status") or "")
            if status == "wait":
                pass
            elif status == "scaned":
                verify_code = ""
                if not scanned_reported:
                    self.output("二维码已扫描，正在确认……")
                    scanned_reported = True
            elif status == "need_verifycode":
                verify_code = await asyncio.to_thread(
                    self.input_func,
                    "请输入手机微信显示的数字：",
                )
                verify_code = verify_code.strip()
                if not verify_code or not verify_code.isdigit():
                    raise WeixinLoginError("配对码必须是数字。")
                continue
            elif status == "scaned_but_redirect":
                poll_base_url = self._redirect_base_url(str(response.get("redirect_host") or ""))
            elif status == "confirmed":
                credentials = self._credentials_from_confirmation(response)
                self.state_store.clear_transport_state()
                self.state_store.save_credentials(credentials)
                self.output("微信 iLink 登录成功。")
                if not credentials.owner_user_id:
                    self.output(
                        "警告：平台未返回扫码用户 ID；请配置 IMCODEX_WEIXIN_ALLOWED_USER_IDS。"
                    )
                return self.state_store.load_credentials() or credentials
            elif status == "expired":
                refresh_count += 1
                if refresh_count > 3:
                    raise WeixinLoginError("二维码多次过期，请稍后重新运行登录命令。")
                qrcode, qrcode_url = await self._fetch_qr(local_tokens)
                poll_base_url = DEFAULT_ILINK_BASE_URL
                verify_code = ""
                scanned_reported = False
                self.output("二维码已过期，已刷新：")
                self._show_qr(qrcode_url)
            elif status == "verify_code_blocked":
                raise WeixinLoginError("配对码多次错误，请稍后重新运行登录命令。")
            elif status == "binded_redirect":
                if existing is not None:
                    self.output("此微信已连接，保留现有本地凭据。")
                    return existing
                raise WeixinLoginError("平台报告已绑定，但本机没有可恢复的凭据。请先解除旧绑定。")
            else:
                raise WeixinLoginError(f"微信登录返回了不支持的状态：{status or '(empty)'}")
            await self.sleep(1.0)
        raise WeixinLoginError("微信登录超时，请重新运行登录命令。")

    async def _fetch_qr(self, local_tokens: list[str]) -> tuple[str, str]:
        response = await self.transport.fetch_qr_code(local_tokens=local_tokens)
        qrcode = str(response.get("qrcode") or "").strip()
        qrcode_url = str(response.get("qrcode_img_content") or "").strip()
        if not qrcode or not qrcode_url:
            raise WeixinLoginError("平台没有返回可用的微信登录二维码。")
        return qrcode, qrcode_url

    def _show_qr(self, qrcode_url: str) -> None:
        self.output("请用手机微信打开/扫描下面的二维码链接：")
        self.output(qrcode_url)

    @staticmethod
    def _credentials_from_confirmation(response: dict) -> WeixinCredentials:
        account_id = str(response.get("ilink_bot_id") or "").strip()
        bot_token = str(response.get("bot_token") or "").strip()
        base_url = str(response.get("baseurl") or DEFAULT_ILINK_BASE_URL).strip()
        owner_user_id = str(response.get("ilink_user_id") or "").strip()
        if not account_id or not bot_token:
            raise WeixinLoginError("微信已确认扫码，但没有返回完整的 bot 凭据。")
        parsed = urlparse(base_url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise WeixinLoginError("平台返回了不安全的 iLink API 地址。")
        hostname = parsed.hostname.lower().rstrip(".")
        if hostname != "weixin.qq.com" and not hostname.endswith(".weixin.qq.com"):
            raise WeixinLoginError("平台返回了非微信域名的 iLink API 地址。")
        return WeixinCredentials(
            account_id=account_id,
            bot_token=bot_token,
            base_url=base_url.rstrip("/"),
            owner_user_id=owner_user_id,
        )

    @staticmethod
    def _redirect_base_url(host: str) -> str:
        normalized = host.strip().lower().rstrip(".")
        if not re.fullmatch(r"[a-z0-9.-]+", normalized):
            raise WeixinLoginError("平台返回了无效的二维码轮询地址。")
        if normalized != "weixin.qq.com" and not normalized.endswith(".weixin.qq.com"):
            raise WeixinLoginError("平台返回了非微信域名的二维码轮询地址。")
        return f"https://{normalized}"
