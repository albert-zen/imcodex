from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path

import httpx
import pytest

from imcodex.channels.media import MediaDownloadError
from imcodex.channels.weixin_ilink import (
    ILinkError,
    WeixinILinkTransport,
    WeixinImageReference,
)
from imcodex.channels.weixin_state import (
    WeixinCredentials,
    WeixinStateStore,
    WeixinTransportState,
)


PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)
ENCRYPTED_PNG = base64.b64decode(
    "yLuipry+RbJL9obQb6z5cKOr95SiKJpxmdT8+emDMR3txLaTQfYurVDio+edPs/dnIGLDX9YQz0ywV7fe1ruJW4RW+D+zNqHbJHENP8ePNE="
)
AES_KEY_HEX = "000102030405060708090a0b0c0d0e0f"
AES_KEY_RAW_BASE64 = "AAECAwQFBgcICQoLDA0ODw=="
AES_KEY_HEX_BASE64 = "MDAwMTAyMDMwNDA1MDYwNzA4MDkwYTBiMGMwZDBlMGY="


class ChunkedByteStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks

    async def __aiter__(self):
        for chunk in self.chunks:
            yield chunk

    async def aclose(self) -> None:
        return None


def _collect_into(target: list[bytes]):
    async def write(chunk: bytes) -> None:
        target.append(chunk)

    return write


async def _discard_chunk(_chunk: bytes) -> None:
    return None


def test_weixin_state_store_round_trips_sensitive_state_with_private_permissions(
    tmp_path: Path,
) -> None:
    store = WeixinStateStore(tmp_path / "weixin")
    credentials = WeixinCredentials(
        account_id="bot@im.bot",
        bot_token="bot-secret",
        base_url="https://ilinkai.weixin.qq.com",
        owner_user_id="owner@im.wechat",
    )
    state = WeixinTransportState(get_updates_buf="cursor")
    state.set_context_token("owner@im.wechat", "context-secret")

    store.save_credentials(credentials)
    store.save_transport_state(state)

    assert store.load_credentials() == WeixinCredentials(
        account_id="bot@im.bot",
        bot_token="bot-secret",
        base_url="https://ilinkai.weixin.qq.com",
        owner_user_id="owner@im.wechat",
        saved_at=store.load_credentials().saved_at,
    )
    assert store.load_transport_state() == state
    if os.name != "nt":
        assert store.root.stat().st_mode & 0o777 == 0o700
        assert store.credentials_path.stat().st_mode & 0o777 == 0o600
        assert store.transport_state_path.stat().st_mode & 0o777 == 0o600


def test_weixin_transport_state_bounds_context_tokens() -> None:
    state = WeixinTransportState()

    for index in range(4):
        state.set_context_token(f"u{index}@im.wechat", f"token{index}", limit=3)

    assert state.context_tokens == {
        "u1@im.wechat": "token1",
        "u2@im.wechat": "token2",
        "u3@im.wechat": "token3",
    }


def test_weixin_state_store_fails_closed_on_corrupt_state(
    tmp_path: Path,
) -> None:
    store = WeixinStateStore(tmp_path)
    store.transport_state_path.write_text("not-json", encoding="utf-8")
    store.credentials_path.write_text("not-json", encoding="utf-8")
    if os.name != "nt":
        os.chmod(store.root, 0o700)
        os.chmod(store.transport_state_path, 0o600)
        os.chmod(store.credentials_path, 0o600)

    with pytest.raises(RuntimeError, match="Could not read Weixin transport state"):
        store.load_transport_state()
    with pytest.raises(RuntimeError, match="Could not read Weixin state file"):
        store.load_credentials()


@pytest.mark.parametrize(
    "payload",
    [
        {
            "version": 2,
            "account_id": "bot@im.bot",
            "get_updates_buf": "cursor",
            "context_tokens": {},
        },
        {
            "version": 1,
            "account_id": ["bot@im.bot"],
            "get_updates_buf": {},
            "context_tokens": [],
        },
        {
            "version": 1,
            "account_id": "bot@im.bot",
            "get_updates_buf": "cursor",
            "context_tokens": {"*@im.wechat": "secret"},
        },
    ],
)
def test_weixin_state_store_fails_closed_on_wrong_transport_shape(
    tmp_path: Path,
    payload: dict,
) -> None:
    store = WeixinStateStore(tmp_path / "weixin")
    store.root.mkdir(parents=True)
    store.transport_state_path.write_text(json.dumps(payload), encoding="utf-8")
    if os.name != "nt":
        os.chmod(store.root, 0o700)
        os.chmod(store.transport_state_path, 0o600)

    with pytest.raises(RuntimeError, match="Invalid Weixin transport state"):
        store.load_transport_state()


@pytest.mark.parametrize(
    "field_name,invalid_value",
    [
        ("version", 2),
        ("account_id", ["bot@im.bot"]),
        ("base_url", "https://attacker.example"),
        ("owner_user_id", "*@im.wechat"),
    ],
)
def test_weixin_state_store_fails_closed_on_invalid_credentials(
    tmp_path: Path,
    field_name: str,
    invalid_value: object,
) -> None:
    store = WeixinStateStore(tmp_path / "weixin")
    store.save_credentials(
        WeixinCredentials(
            account_id="bot@im.bot",
            bot_token="bot-secret",
            base_url="https://ilinkai.weixin.qq.com",
            owner_user_id="owner@im.wechat",
        )
    )
    payload = json.loads(store.credentials_path.read_text(encoding="utf-8"))
    payload[field_name] = invalid_value
    store.credentials_path.write_text(json.dumps(payload), encoding="utf-8")
    if os.name != "nt":
        os.chmod(store.credentials_path, 0o600)

    with pytest.raises(RuntimeError, match="Invalid Weixin credential file"):
        store.load_credentials()


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission enforcement")
def test_weixin_state_store_fails_closed_when_chmod_fails(tmp_path: Path, monkeypatch) -> None:
    store = WeixinStateStore(tmp_path / "weixin")
    credentials = WeixinCredentials(
        account_id="bot@im.bot",
        bot_token="bot-secret",
        base_url="https://ilinkai.weixin.qq.com",
    )

    def fail_chmod(_path, _mode) -> None:
        raise OSError("chmod unavailable")

    monkeypatch.setattr("imcodex.channels.weixin_state.os.chmod", fail_chmod)

    with pytest.raises(RuntimeError, match="Could not secure Weixin state path"):
        store.save_credentials(credentials)

    assert not store.credentials_path.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission enforcement")
def test_weixin_state_store_rejects_existing_world_readable_secret(
    tmp_path: Path,
) -> None:
    store = WeixinStateStore(tmp_path / "weixin")
    store.save_credentials(
        WeixinCredentials(
            account_id="bot@im.bot",
            bot_token="bot-secret",
            base_url="https://ilinkai.weixin.qq.com",
        )
    )
    os.chmod(store.credentials_path, 0o644)

    with pytest.raises(RuntimeError, match="Insecure Weixin state permissions"):
        store.load_credentials()


def test_weixin_state_store_refuses_wildcard_owner_identity(tmp_path: Path) -> None:
    store = WeixinStateStore(tmp_path / "weixin")

    with pytest.raises(RuntimeError, match="invalid Weixin credentials"):
        store.save_credentials(
            WeixinCredentials(
                account_id="bot@im.bot",
                bot_token="bot-secret",
                base_url="https://ilinkai.weixin.qq.com",
                owner_user_id="*@im.wechat",
            )
        )


def test_weixin_state_store_refuses_nonofficial_base_url(tmp_path: Path) -> None:
    store = WeixinStateStore(tmp_path / "weixin")

    with pytest.raises(RuntimeError, match="invalid Weixin credentials"):
        store.save_credentials(
            WeixinCredentials(
                account_id="bot@im.bot",
                bot_token="bot-secret",
                base_url="https://attacker.example",
            )
        )


def test_weixin_logout_clear_removes_temporary_secret_files(tmp_path: Path) -> None:
    store = WeixinStateStore(tmp_path / "weixin")
    store.root.mkdir(parents=True)
    credentials_tmp = store.credentials_path.with_suffix(".json.tmp")
    transport_tmp = store.transport_state_path.with_suffix(".json.tmp")
    credentials_tmp.write_text("secret", encoding="utf-8")
    transport_tmp.write_text("context", encoding="utf-8")

    store.clear()

    assert not credentials_tmp.exists()
    assert not transport_tmp.exists()


@pytest.mark.asyncio
async def test_ilink_fetch_qr_uses_required_compatibility_headers() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={"qrcode": "qr-secret", "qrcode_img_content": "https://example/qr"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        transport = WeixinILinkTransport(http_client=client)
        result = await transport.fetch_qr_code(local_tokens=["old-token"])

    assert result["qrcode"] == "qr-secret"
    request = requests[0]
    assert request.url.path == "/ilink/bot/get_bot_qrcode"
    assert request.url.params["bot_type"] == "3"
    assert request.headers["ilink-app-id"] == "bot"
    assert request.headers["ilink-app-clientversion"] == "132102"
    assert request.headers["authorizationtype"] == "ilink_bot_token"
    assert base64.b64decode(request.headers["x-wechat-uin"]).decode("ascii").isdigit()
    assert json.loads(request.content) == {"local_token_list": ["old-token"]}


@pytest.mark.asyncio
async def test_ilink_get_updates_sends_cursor_and_bearer_token() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"ret": 0, "msgs": [], "get_updates_buf": "next"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        transport = WeixinILinkTransport(
            token="bot-secret",
            http_client=client,
        )
        result = await transport.get_updates(get_updates_buf="cursor", timeout_ms=35_000)

    assert result["get_updates_buf"] == "next"
    request = requests[0]
    assert request.headers["authorization"] == "Bearer bot-secret"
    body = json.loads(request.content)
    assert body["get_updates_buf"] == "cursor"
    assert body["base_info"]["bot_agent"] == "IMCodex/0.1.0"


@pytest.mark.asyncio
async def test_ilink_streams_and_decrypts_image_without_api_authorization() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            stream=ChunkedByteStream(
                [
                    ENCRYPTED_PNG[:3],
                    ENCRYPTED_PNG[3:21],
                    ENCRYPTED_PNG[21:67],
                    ENCRYPTED_PNG[67:],
                ]
            ),
        )

    plaintext_chunks: list[bytes] = []
    reference = WeixinImageReference(
        encrypted_query_param="ticket+/=",
        aes_key="invalid-but-lower-priority",
        aeskey=AES_KEY_HEX,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        transport = WeixinILinkTransport(
            token="bot-secret",
            http_client=client,
        )
        await transport.download_image(reference, _collect_into(plaintext_chunks))

    assert b"".join(plaintext_chunks) == PNG
    assert len(requests) == 1
    request = requests[0]
    assert request.url.host == "novac2c.cdn.weixin.qq.com"
    assert request.url.path == "/c2c/download"
    assert request.url.params["encrypted_query_param"] == "ticket+/="
    assert request.headers["Accept-Encoding"] == "identity"
    assert "authorization" not in request.headers
    assert "authorizationtype" not in request.headers
    assert "x-wechat-uin" not in request.headers
    assert "ticket" not in repr(reference)
    assert AES_KEY_HEX not in repr(reference)


@pytest.mark.asyncio
async def test_ilink_rejects_encoded_cdn_response_before_decryption() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Accept-Encoding"] == "identity"
        return httpx.Response(
            200,
            content=ENCRYPTED_PNG,
            headers={"Content-Encoding": "gzip"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        transport = WeixinILinkTransport(http_client=client)
        with pytest.raises(MediaDownloadError):
            await transport.download_image(
                WeixinImageReference(
                    encrypted_query_param="encoded-ticket",
                    aeskey=AES_KEY_HEX,
                ),
                _discard_chunk,
            )


@pytest.mark.parametrize("encoded_key", [AES_KEY_RAW_BASE64, AES_KEY_HEX_BASE64])
@pytest.mark.asyncio
async def test_ilink_accepts_documented_media_aes_key_encodings(
    encoded_key: str,
) -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=ChunkedByteStream([ENCRYPTED_PNG]))

    plaintext_chunks: list[bytes] = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        transport = WeixinILinkTransport(http_client=client)
        await transport.download_image(
            WeixinImageReference(
                encrypted_query_param="key-encoding-ticket",
                aes_key=encoded_key,
            ),
            _collect_into(plaintext_chunks),
        )

    assert b"".join(plaintext_chunks) == PNG


@pytest.mark.asyncio
async def test_ilink_prefers_full_url_and_supports_plain_image_payload() -> None:
    requests: list[httpx.Request] = []
    full_url = (
        "https://novac2c.cdn.weixin.qq.com/c2c/download"
        "?encrypted_query_param=full%2Bticket"
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, stream=ChunkedByteStream([PNG]))

    plaintext_chunks: list[bytes] = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        transport = WeixinILinkTransport(token="bot-secret", http_client=client)
        await transport.download_image(
            WeixinImageReference(
                encrypted_query_param="unused-fallback-ticket",
                full_url=full_url,
            ),
            _collect_into(plaintext_chunks),
        )

    assert b"".join(plaintext_chunks) == PNG
    assert len(requests) == 1
    assert str(requests[0].url) == full_url
    assert "authorization" not in requests[0].headers


@pytest.mark.parametrize(
    "full_url",
    [
        "http://novac2c.cdn.weixin.qq.com/c2c/download?x=1",
        "https://attacker.example/download?x=1",
        "https://user@novac2c.cdn.weixin.qq.com/download?x=1",
        "https://novac2c.cdn.weixin.qq.com:444/download?x=1",
        "https://novac2c.cdn.weixin.qq.com/download?x=1#fragment",
        "https://novac2c.cdn.weixin.qq.com/download?bad value",
    ],
)
@pytest.mark.asyncio
async def test_ilink_rejects_untrusted_image_full_url_before_network(
    full_url: str,
) -> None:
    requests = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(200, content=PNG)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        transport = WeixinILinkTransport(http_client=client)
        with pytest.raises(MediaDownloadError):
            await transport.download_image(
                WeixinImageReference(full_url=full_url),
                _discard_chunk,
            )

    assert requests == 0


@pytest.mark.asyncio
async def test_ilink_rejects_redirect_invalid_key_and_invalid_padding() -> None:
    responses = [
        httpx.Response(302, headers={"Location": "https://attacker.example/image"}),
        httpx.Response(200, stream=ChunkedByteStream([b"\x00" * 16])),
    ]
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return responses.pop(0)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        transport = WeixinILinkTransport(http_client=client)
        with pytest.raises(MediaDownloadError):
            await transport.download_image(
                WeixinImageReference(encrypted_query_param="redirect-ticket"),
                _discard_chunk,
            )
        with pytest.raises(MediaDownloadError):
            await transport.download_image(
                WeixinImageReference(
                    encrypted_query_param="bad-key-ticket",
                    aes_key="not-base64!",
                ),
                _discard_chunk,
            )
        with pytest.raises(MediaDownloadError):
            await transport.download_image(
                WeixinImageReference(
                    encrypted_query_param="bad-padding-ticket",
                    aeskey=AES_KEY_HEX,
                ),
                _discard_chunk,
            )

    # Invalid keys fail before opening the CDN request, and redirects are not followed.
    assert len(requests) == 2


@pytest.mark.asyncio
async def test_ilink_send_text_retries_rate_limit_with_stable_client_id() -> None:
    attempts: list[httpx.Request] = []
    delays: list[float] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(request)
        if len(attempts) == 1:
            return httpx.Response(429, headers={"Retry-After": "2"})
        return httpx.Response(200, json={"ret": 0})

    async def capture_sleep(delay: float) -> None:
        delays.append(delay)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        transport = WeixinILinkTransport(
            token="bot-secret",
            http_client=client,
            sleep=capture_sleep,
        )
        client_id = await transport.send_text(
            to_user_id="owner@im.wechat",
            text="done",
            context_token="context-secret",
            client_id="stable-id",
        )

    assert client_id == "stable-id"
    assert delays == [2.0]
    assert len(attempts) == 2
    bodies = [json.loads(request.content) for request in attempts]
    assert bodies[0] == bodies[1]
    assert bodies[0]["msg"] == {
        "from_user_id": "",
        "to_user_id": "owner@im.wechat",
        "client_id": "stable-id",
        "message_type": 2,
        "message_state": 2,
        "item_list": [{"type": 1, "text_item": {"text": "done"}}],
        "context_token": "context-secret",
    }


@pytest.mark.asyncio
async def test_ilink_send_text_rejects_nonzero_protocol_result() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ret": -14, "errmsg": "stale"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        transport = WeixinILinkTransport(token="bot-secret", http_client=client)
        with pytest.raises(ILinkError) as exc_info:
            await transport.send_text(
                to_user_id="owner@im.wechat",
                text="done",
                context_token="context-secret",
            )

    assert exc_info.value.code == -14
    assert "bot-secret" not in str(exc_info.value)
    assert "context-secret" not in str(exc_info.value)


@pytest.mark.asyncio
async def test_ilink_uploads_encrypted_image_and_sends_media_reference() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/getuploadurl"):
            return httpx.Response(200, json={"ret": 0, "upload_param": "upload-ticket"})
        if request.url.path == "/c2c/upload":
            return httpx.Response(200, headers={"x-encrypted-param": "download-ticket"})
        if request.url.path.endswith("/sendmessage"):
            return httpx.Response(200, json={"ret": 0})
        return httpx.Response(404)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        transport = WeixinILinkTransport(token="bot-secret", http_client=client)
        client_id = await transport.send_artifact(
            to_user_id="owner@im.wechat",
            content=PNG,
            filename="preview.png",
            kind="image",
            context_token="context-secret",
            client_id="stable-artifact-id",
        )

    assert client_id == "stable-artifact-id"
    assert [request.url.path for request in requests] == [
        "/ilink/bot/getuploadurl",
        "/c2c/upload",
        "/ilink/bot/sendmessage",
    ]
    assert [request.method for request in requests] == ["POST", "PUT", "POST"]

    upload_request = json.loads(requests[0].content)
    assert upload_request["media_type"] == 1
    assert upload_request["rawsize"] == len(PNG)
    assert upload_request["rawfilemd5"] == hashlib.md5(PNG).hexdigest()
    assert upload_request["filesize"] > len(PNG)
    assert upload_request["filesize"] % 16 == 0
    assert upload_request["no_need_thumb"] is True
    assert len(upload_request["aeskey"]) == 32

    assert requests[1].url.params["encrypted_query_param"] == "upload-ticket"
    assert requests[1].url.params["filekey"] == upload_request["filekey"]
    assert requests[1].content != PNG
    assert len(requests[1].content) == upload_request["filesize"]

    send_request = json.loads(requests[2].content)
    message = send_request["msg"]
    assert message["client_id"] == "stable-artifact-id"
    assert message["to_user_id"] == "owner@im.wechat"
    assert message["context_token"] == "context-secret"
    image_item = message["item_list"][0]
    assert image_item["type"] == 2
    assert image_item["image_item"]["mid_size"] == upload_request["filesize"]
    media = image_item["image_item"]["media"]
    assert media["encrypt_query_param"] == "download-ticket"
    assert media["encrypt_type"] == 1
    assert base64.b64decode(media["aes_key"]).decode("ascii") == upload_request["aeskey"]


def test_ilink_rejects_insecure_base_url() -> None:
    with pytest.raises(ValueError, match="must be an HTTPS origin"):
        WeixinILinkTransport(base_url="http://ilinkai.weixin.qq.com")

    with pytest.raises(ValueError, match="official weixin.qq.com host"):
        WeixinILinkTransport(base_url="https://attacker.example")
