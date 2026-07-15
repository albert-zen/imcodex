from __future__ import annotations

import asyncio
import json

import pytest
import httpx
from fastapi.testclient import TestClient

from imcodex.channels import ChannelAccessPolicy, QQChannelAdapter, create_app
from imcodex.channels.middleware import UnifiedChannelMiddleware
from imcodex.channels.qq import OP_DISPATCH, OP_HELLO, RECONNECT_MAX_DELAY_S
from imcodex.channels.api import _InboundWebhookGuard
from imcodex.models import InboundMessage, OutboundMessage
from imcodex.store import ConversationStore


class StubService:
    async def handle_inbound(self, message):
        return [
            OutboundMessage(
                channel_id=message.channel_id,
                conversation_id=message.conversation_id,
                message_type="accepted",
                text="Accepted",
            )
        ]


class CountingService:
    def __init__(self, store: ConversationStore) -> None:
        self.store = store
        self.calls: list[InboundMessage] = []

    async def handle_inbound(self, message: InboundMessage):
        self.calls.append(message)
        return [
            OutboundMessage(
                channel_id=message.channel_id,
                conversation_id=message.conversation_id,
                message_type="accepted",
                text="Accepted",
            )
        ]


def test_webhook_inbound_returns_messages() -> None:
    client = TestClient(create_app(StubService(), inbound_token="webhook-secret"))

    response = client.post(
        "/api/channels/webhook/inbound",
        headers={"Authorization": "Bearer webhook-secret"},
        json={
            "channel_id": "demo",
            "conversation_id": "conv-1",
            "user_id": "u1",
            "message_id": "m1",
            "text": "hello",
        },
    )

    assert response.status_code == 200
    assert response.json()["messages"][0]["message_type"] == "accepted"


def test_webhook_inbound_rejects_invalid_bearer_token() -> None:
    client = TestClient(create_app(StubService(), inbound_token="webhook-secret"))

    response = client.post(
        "/api/channels/webhook/inbound",
        headers={"Authorization": "Bearer wrong"},
        json={
            "channel_id": "demo",
            "conversation_id": "conv-1",
            "user_id": "u1",
            "message_id": "m1",
            "text": "hello",
        },
    )

    assert response.status_code == 401


def test_webhook_inbound_without_token_is_loopback_only() -> None:
    app = create_app(StubService())
    remote_client = TestClient(app, client=("198.51.100.10", 50000))
    loopback_client = TestClient(app, client=("127.0.0.1", 50000))
    body = {
        "channel_id": "demo",
        "conversation_id": "conv-1",
        "user_id": "u1",
        "message_id": "m1",
        "text": "hello",
    }

    assert remote_client.post("/api/channels/webhook/inbound", json=body).status_code == 403
    assert loopback_client.post("/api/channels/webhook/inbound", json=body).status_code == 200


def test_webhook_authenticates_before_parsing_json() -> None:
    remote = TestClient(
        create_app(StubService()),
        client=("198.51.100.10", 50000),
    )
    protected = TestClient(
        create_app(StubService(), inbound_token="webhook-secret"),
        client=("198.51.100.10", 50000),
    )

    assert (
        remote.post(
            "/api/channels/webhook/inbound",
            content=b"not-json",
            headers={"Content-Type": "application/json"},
        ).status_code
        == 403
    )
    assert (
        protected.post(
            "/api/channels/webhook/inbound",
            content=b"not-json",
            headers={"Content-Type": "application/json"},
        ).status_code
        == 401
    )


def test_webhook_rejects_non_ascii_authorization_bytes_without_crashing() -> None:
    guard = _InboundWebhookGuard(object(), configured_token="webhook-secret")

    assert guard._authorization_denial(
        scope={"client": ("198.51.100.10", 50000)},
        authorization=b"Bearer \xff",
    ) == (401, "Invalid inbound webhook credentials.")


def test_webhook_rejects_oversized_body_before_model_parsing() -> None:
    client = TestClient(create_app(StubService(), inbound_token="webhook-secret"))

    response = client.post(
        "/api/channels/webhook/inbound",
        headers={"Authorization": "Bearer webhook-secret"},
        json={
            "channel_id": "gateway",
            "conversation_id": "conv-1",
            "user_id": "u1",
            "message_id": "m1",
            "text": "x" * (64 * 1024),
        },
    )

    assert response.status_code == 413


def test_webhook_cannot_claim_a_built_in_channel_route() -> None:
    store = ConversationStore(clock=lambda: 1.0)
    service = CountingService(store)
    client = TestClient(create_app(service, inbound_token="webhook-secret"))

    response = client.post(
        "/api/channels/webhook/inbound",
        headers={"Authorization": "Bearer webhook-secret"},
        json={
            "channel_id": "telegram",
            "conversation_id": "chat:123456",
            "user_id": "attacker",
            "message_id": "m1",
            "text": "bind this route",
        },
    )

    assert response.status_code == 409
    assert service.calls == []


@pytest.mark.parametrize(
    "extra",
    [
        {
            "attachments": [
                {
                    "kind": "image",
                    "content_type": "image/png",
                    "local_path": "/etc/passwd",
                    "size_bytes": 1,
                }
            ]
        },
        {"input_error": "image_download_failed"},
    ],
)
def test_webhook_cannot_inject_internal_attachment_fields(extra: dict) -> None:
    store = ConversationStore(clock=lambda: 1.0)
    service = CountingService(store)
    client = TestClient(create_app(service, inbound_token="webhook-secret"))
    body = {
        "channel_id": "gateway",
        "conversation_id": "conv-1",
        "user_id": "u1",
        "message_id": "m1",
        "text": "inspect",
        **extra,
    }

    response = client.post(
        "/api/channels/webhook/inbound",
        headers={"Authorization": "Bearer webhook-secret"},
        json=body,
    )

    assert response.status_code == 422
    assert service.calls == []


def test_webhook_uses_persisted_message_id_deduplication(tmp_path) -> None:
    store = ConversationStore(state_path=tmp_path / "state.json", clock=lambda: 1.0)
    service = CountingService(store)
    client = TestClient(create_app(service, inbound_token="webhook-secret"))
    body = {
        "channel_id": "gateway",
        "conversation_id": "conv-1",
        "user_id": "u1",
        "message_id": "m1",
        "text": "/status",
    }

    first = client.post(
        "/api/channels/webhook/inbound",
        headers={"Authorization": "Bearer webhook-secret"},
        json=body,
    )
    second = client.post(
        "/api/channels/webhook/inbound",
        headers={"Authorization": "Bearer webhook-secret"},
        json=body,
    )

    assert first.status_code == 200
    assert second.status_code == 200
    assert len(service.calls) == 1
    assert second.json()["messages"][0]["text"] == "Accepted"


def test_webhook_delivers_immediate_messages_to_configured_outbound_sink() -> None:
    class Sink:
        def __init__(self) -> None:
            self.messages: list[OutboundMessage] = []

        async def send_message(self, message: OutboundMessage) -> None:
            self.messages.append(message)

    store = ConversationStore(clock=lambda: 1.0)
    service = CountingService(store)
    sink = Sink()
    service.outbound_sink = sink
    client = TestClient(create_app(service, inbound_token="webhook-secret"))
    body = {
        "channel_id": "gateway",
        "conversation_id": "conv-1",
        "user_id": "u1",
        "message_id": "m1",
        "text": "/status",
    }

    response = client.post(
        "/api/channels/webhook/inbound",
        headers={"Authorization": "Bearer webhook-secret"},
        json=body,
    )

    assert response.status_code == 200
    assert [message.text for message in sink.messages] == ["Accepted"]
    assert response.json()["messages"][0]["text"] == "Accepted"


def test_webhook_retries_cached_delivery_without_reexecuting_command(tmp_path) -> None:
    class FlakySink:
        def __init__(self) -> None:
            self.attempts = 0
            self.delivered: list[OutboundMessage] = []

        async def send_message(self, message: OutboundMessage) -> None:
            self.attempts += 1
            if self.attempts == 1:
                raise httpx.HTTPStatusError(
                    "503",
                    request=httpx.Request("POST", "https://gateway.example/outbound"),
                    response=httpx.Response(503),
                )
            self.delivered.append(message)

    store = ConversationStore(state_path=tmp_path / "state.json", clock=lambda: 1.0)
    service = CountingService(store)
    sink = FlakySink()
    service.outbound_sink = sink
    client = TestClient(
        create_app(service, inbound_token="webhook-secret"),
        raise_server_exceptions=False,
    )
    body = {
        "channel_id": "gateway",
        "conversation_id": "conv-1",
        "user_id": "u1",
        "message_id": "m1",
        "text": "/new",
    }

    first = client.post(
        "/api/channels/webhook/inbound",
        headers={"Authorization": "Bearer webhook-secret"},
        json=body,
    )
    second = client.post(
        "/api/channels/webhook/inbound",
        headers={"Authorization": "Bearer webhook-secret"},
        json=body,
    )

    assert first.status_code == 500
    assert second.status_code == 200
    assert len(service.calls) == 1
    assert sink.attempts == 2
    assert [message.text for message in sink.delivered] == ["Accepted"]
    assert sink.delivered[0].metadata["delivery_id"].startswith("imcodex:")
    assert second.json()["messages"][0]["text"] == "Accepted"


@pytest.mark.parametrize(
    "field_name",
    ["channel_id", "conversation_id", "user_id", "message_id"],
)
def test_webhook_requires_non_empty_stable_routing_ids(field_name: str) -> None:
    store = ConversationStore(clock=lambda: 1.0)
    service = CountingService(store)
    client = TestClient(create_app(service, inbound_token="webhook-secret"))
    body = {
        "channel_id": "gateway",
        "conversation_id": "conv-1",
        "user_id": "u1",
        "message_id": "m1",
        "text": "/status",
    }
    body[field_name] = ""

    response = client.post(
        "/api/channels/webhook/inbound",
        headers={"Authorization": "Bearer webhook-secret"},
        json=body,
    )

    assert response.status_code == 422
    assert service.calls == []


def test_qq_adapter_normalizes_group_mention_message() -> None:
    adapter = QQChannelAdapter(
        enabled=True,
        app_id="app",
        client_secret="secret",
        middleware=object(),
    )

    inbound = adapter.parse_inbound_event(
        "GROUP_AT_MESSAGE_CREATE",
        {
            "id": "msg-1",
            "content": "<@123>  inspect repo",
            "group_openid": "group-1",
            "author": {"member_openid": "user-1"},
        },
    )

    assert inbound is not None
    assert inbound.conversation_id == "group:group-1"
    assert inbound.text == "inspect repo"


@pytest.mark.asyncio
async def test_qq_adapter_sends_markdown_messages_by_default() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/app/getAppAccessToken":
            return httpx.Response(200, json={"access_token": "token", "expires_in": 7200})
        return httpx.Response(200, json={"id": "out-1"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = QQChannelAdapter(
            enabled=True,
            app_id="app",
            client_secret="secret",
            middleware=object(),
            api_base="https://api.sgroup.qq.com",
            http_client=client,
            access_policy=ChannelAccessPolicy.allow_all(),
        )

        await adapter.send_message(
            OutboundMessage(
                channel_id="qq",
                conversation_id="group:group-1",
                message_type="turn_result",
                text="**Accepted**",
                metadata={"reply_to_message_id": "msg-1"},
            )
        )

    message_body = json.loads(requests[-1].content)
    assert requests[-1].url.path == "/v2/groups/group-1/messages"
    assert message_body == {
        "markdown": {"content": "**Accepted**"},
        "msg_type": 2,
        "msg_seq": 1,
        "msg_id": "msg-1",
    }


@pytest.mark.asyncio
async def test_qq_adapter_sends_plain_text_messages_when_disabled() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/app/getAppAccessToken":
            return httpx.Response(200, json={"access_token": "token", "expires_in": 7200})
        return httpx.Response(200, json={"id": "out-1"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = QQChannelAdapter(
            enabled=True,
            app_id="app",
            client_secret="secret",
            middleware=object(),
            api_base="https://api.sgroup.qq.com",
            http_client=client,
            markdown_enabled=False,
            access_policy=ChannelAccessPolicy.allow_all(),
        )

        await adapter.send_message(
            OutboundMessage(
                channel_id="qq",
                conversation_id="c2c:user-1",
                message_type="turn_result",
                text="**Accepted**",
            )
        )

    message_body = json.loads(requests[-1].content)
    assert requests[-1].url.path == "/v2/users/user-1/messages"
    assert message_body == {
        "content": "**Accepted**",
        "msg_type": 0,
        "msg_seq": 1,
    }


@pytest.mark.parametrize("status_code", [400, 403])
@pytest.mark.asyncio
async def test_qq_adapter_retries_plain_text_when_markdown_send_fails(
    status_code: int,
) -> None:
    message_requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/app/getAppAccessToken":
            return httpx.Response(200, json={"access_token": "token", "expires_in": 7200})
        message_requests.append(request)
        if len(message_requests) == 1:
            return httpx.Response(status_code, json={"message": "markdown unsupported"})
        return httpx.Response(200, json={"id": "out-1"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = QQChannelAdapter(
            enabled=True,
            app_id="app",
            client_secret="secret",
            middleware=object(),
            api_base="https://api.sgroup.qq.com",
            http_client=client,
            markdown_enabled=True,
            access_policy=ChannelAccessPolicy.allow_all(),
        )

        await adapter.send_message(
            OutboundMessage(
                channel_id="qq",
                conversation_id="group:group-1",
                message_type="turn_result",
                text="**Accepted**",
                metadata={"reply_to_message_id": "msg-1"},
            )
        )

    assert [json.loads(request.content) for request in message_requests] == [
        {
            "markdown": {"content": "**Accepted**"},
            "msg_type": 2,
            "msg_seq": 1,
            "msg_id": "msg-1",
        },
        {
            "content": "**Accepted**",
            "msg_type": 0,
            "msg_seq": 1,
            "msg_id": "msg-1",
        },
    ]


@pytest.mark.asyncio
async def test_qq_adapter_does_not_retry_plain_text_for_server_errors() -> None:
    message_requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/app/getAppAccessToken":
            return httpx.Response(200, json={"access_token": "token", "expires_in": 7200})
        message_requests.append(request)
        return httpx.Response(500, json={"message": "temporary failure"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = QQChannelAdapter(
            enabled=True,
            app_id="app",
            client_secret="secret",
            middleware=object(),
            api_base="https://api.sgroup.qq.com",
            http_client=client,
            markdown_enabled=True,
            access_policy=ChannelAccessPolicy.allow_all(),
        )

        with pytest.raises(httpx.HTTPStatusError):
            await adapter.send_message(
                OutboundMessage(
                    channel_id="qq",
                    conversation_id="group:group-1",
                    message_type="turn_result",
                    text="**Accepted**",
                )
            )

    assert len(message_requests) == 1


@pytest.mark.asyncio
async def test_qq_adapter_delegates_standardized_inbound_message_to_middleware() -> None:
    class CapturingMiddleware:
        def __init__(self) -> None:
            self.seen: list[InboundMessage] = []

        async def handle_inbound(self, adapter, inbound, *, reply_to_message_id=None):
            self.seen.append(inbound)
            await adapter.send_message(
                OutboundMessage(
                    channel_id="qq",
                    conversation_id=inbound.conversation_id,
                    message_type="turn_result",
                    text="Accepted",
                    metadata={"reply_to_message_id": reply_to_message_id} if reply_to_message_id else {},
                )
            )

    middleware = CapturingMiddleware()
    adapter = QQChannelAdapter(
        enabled=True,
        app_id="app",
        client_secret="secret",
        middleware=middleware,
        access_policy=ChannelAccessPolicy.allow_all(),
    )
    sent: list[OutboundMessage] = []

    async def capture(message: OutboundMessage) -> None:
        sent.append(message)

    adapter.send_message = capture  # type: ignore[method-assign]

    await adapter.handle_dispatch_event(
        "C2C_MESSAGE_CREATE",
        {
            "id": "msg-1",
            "content": "hello",
            "author": {"user_openid": "user-1"},
        },
    )

    assert sent
    assert middleware.seen
    assert middleware.seen[0].text == "hello"
    assert sent[0].message_type == "turn_result"
    assert sent[0].metadata["reply_to_message_id"] == "msg-1"


@pytest.mark.asyncio
async def test_qq_adapter_emits_ready_event_and_health_update(monkeypatch) -> None:
    observed_events: list[dict] = []
    observed_health: list[tuple[str, dict]] = []

    def capture_event(**payload) -> None:
        observed_events.append(payload)

    def capture_health(channel_id: str, **payload) -> None:
        observed_health.append((channel_id, payload))

    monkeypatch.setattr("imcodex.channels.qq.emit_event", capture_event)
    monkeypatch.setattr("imcodex.channels.qq.mark_channel_health", capture_health)

    class FakeWebSocket:
        def __init__(self) -> None:
            self.sent: list[dict] = []
            self._messages = iter(
                [
                    json.dumps({"op": OP_HELLO, "d": {"heartbeat_interval": 1}}),
                    json.dumps(
                        {
                            "op": OP_DISPATCH,
                            "t": "READY",
                            "d": {"session_id": "session-1"},
                        }
                    ),
                ]
            )

        async def send(self, raw: str) -> None:
            self.sent.append(json.loads(raw))

        def __aiter__(self):
            return self

        async def __anext__(self):
            try:
                return next(self._messages)
            except StopIteration as exc:
                raise StopAsyncIteration from exc

    class FakeConnection:
        def __init__(self, websocket: FakeWebSocket) -> None:
            self.websocket = websocket

        async def __aenter__(self) -> FakeWebSocket:
            return self.websocket

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

    async def fast_sleep(_seconds: float) -> None:
        await asyncio.sleep(0)

    adapter = QQChannelAdapter(
        enabled=True,
        app_id="app",
        client_secret="secret",
        middleware=object(),
        websocket_factory=lambda _url: FakeConnection(FakeWebSocket()),
        sleep=fast_sleep,
    )

    await adapter._run_session("ws://gateway", "token")

    assert [event["event"] for event in observed_events] == ["qq.gateway.ready"]
    assert observed_health == [("qq", {"connected": True, "session_id": "session-1", "status": "connected"})]


@pytest.mark.asyncio
async def test_qq_socket_reader_queues_messages_without_waiting_for_codex() -> None:
    class BlockingMiddleware:
        def __init__(self) -> None:
            self.seen: list[str] = []
            self.first_started = asyncio.Event()
            self.release_first = asyncio.Event()

        async def handle_inbound(self, _adapter, inbound, *, reply_to_message_id=None):
            self.seen.append(inbound.message_id)
            if inbound.message_id == "m1":
                self.first_started.set()
                await self.release_first.wait()

    class FakeWebSocket:
        def __init__(self) -> None:
            self.consumed = 0
            self.sent: list[dict] = []
            self.messages = [
                {"op": OP_HELLO, "d": {"heartbeat_interval": 60_000}},
                {"op": OP_DISPATCH, "s": 1, "t": "READY", "d": {"session_id": "session-1"}},
                {
                    "op": OP_DISPATCH,
                    "s": 2,
                    "t": "C2C_MESSAGE_CREATE",
                    "d": {"id": "m1", "content": "first", "author": {"user_openid": "u1"}},
                },
                {
                    "op": OP_DISPATCH,
                    "s": 3,
                    "t": "C2C_MESSAGE_CREATE",
                    "d": {"id": "m2", "content": "second", "author": {"user_openid": "u1"}},
                },
            ]

        async def send(self, raw: str) -> None:
            self.sent.append(json.loads(raw))

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self.consumed >= len(self.messages):
                raise StopAsyncIteration
            message = self.messages[self.consumed]
            self.consumed += 1
            return json.dumps(message)

    class FakeConnection:
        def __init__(self, websocket: FakeWebSocket) -> None:
            self.websocket = websocket

        async def __aenter__(self) -> FakeWebSocket:
            return self.websocket

        async def __aexit__(self, exc_type, exc, tb) -> None:
            return None

    middleware = BlockingMiddleware()
    websocket = FakeWebSocket()
    adapter = QQChannelAdapter(
        enabled=True,
        app_id="app",
        client_secret="secret",
        middleware=middleware,
        websocket_factory=lambda _url: FakeConnection(websocket),
        access_policy=ChannelAccessPolicy.allow_all(),
    )

    session = asyncio.create_task(adapter._run_session("ws://gateway", "token"))
    await middleware.first_started.wait()
    await asyncio.wait_for(session, timeout=0.2)

    assert websocket.consumed == len(websocket.messages)
    assert middleware.seen == ["m1"]
    assert adapter._last_seq == 1

    middleware.release_first.set()
    await asyncio.wait_for(adapter._inbound_queue.join(), timeout=1)

    assert middleware.seen == ["m1", "m2"]
    assert adapter._last_seq == 3
    await adapter.stop()


@pytest.mark.asyncio
async def test_qq_inbound_worker_retries_before_advancing_resume_sequence() -> None:
    class FlakyMiddleware:
        def __init__(self) -> None:
            self.attempts = 0

        async def handle_inbound(self, _adapter, _inbound, *, reply_to_message_id=None):
            self.attempts += 1
            if self.attempts == 1:
                raise OSError("temporary delivery failure")

    retry_waiting = asyncio.Event()
    release_retry = asyncio.Event()

    async def controlled_sleep(_delay: float) -> None:
        retry_waiting.set()
        await release_retry.wait()

    middleware = FlakyMiddleware()
    adapter = QQChannelAdapter(
        enabled=True,
        app_id="app",
        client_secret="secret",
        middleware=middleware,
        sleep=controlled_sleep,
        access_policy=ChannelAccessPolicy.allow_all(),
    )
    adapter._queue_dispatch_event(
        "C2C_MESSAGE_CREATE",
        {"id": "m1", "content": "first", "author": {"user_openid": "u1"}},
        9,
    )

    await retry_waiting.wait()
    assert adapter._last_seq is None
    release_retry.set()
    await asyncio.wait_for(adapter._inbound_queue.join(), timeout=1)

    assert middleware.attempts == 2
    assert adapter._last_seq == 9
    await adapter.stop()


def test_qq_startup_configuration_normalizes_credentials() -> None:
    adapter = QQChannelAdapter(
        enabled=True,
        app_id="  app-id  ",
        client_secret="  client-secret  ",
        middleware=object(),
        api_base="  https://api.sgroup.qq.com/  ",
        http_client=object(),
    )

    adapter.validate_startup_configuration()

    assert adapter.app_id == "app-id"
    assert adapter.client_secret == "client-secret"
    assert adapter.api_base == "https://api.sgroup.qq.com"


def test_qq_startup_configuration_rejects_blank_credentials() -> None:
    adapter = QQChannelAdapter(
        enabled=True,
        app_id="   ",
        client_secret="secret",
        middleware=object(),
        http_client=object(),
    )

    with pytest.raises(RuntimeError, match="requires app_id and client_secret"):
        adapter.validate_startup_configuration()


def test_qq_startup_configuration_rejects_invalid_api_base() -> None:
    adapter = QQChannelAdapter(
        enabled=True,
        app_id="app",
        client_secret="secret",
        middleware=object(),
        api_base="ftp://api.sgroup.qq.com",
        http_client=object(),
    )

    with pytest.raises(ValueError, match=r"IMCODEX_QQ_API_BASE must be an HTTP\(S\) URL"):
        adapter.validate_startup_configuration()


@pytest.mark.asyncio
async def test_qq_adapter_start_survives_initial_network_failure(monkeypatch) -> None:
    observed_health: list[tuple[str, dict]] = []

    def capture_health(channel_id: str, **payload) -> None:
        observed_health.append((channel_id, payload))

    monkeypatch.setattr("imcodex.channels.qq.mark_channel_health", capture_health)

    class FailingHttpClient:
        async def post(self, *_args, **_kwargs):
            request = httpx.Request("POST", "https://bots.qq.com/app/getAppAccessToken")
            raise httpx.ConnectError("network unavailable", request=request)

    sleep_started = asyncio.Event()
    release_sleep = asyncio.Event()
    delays: list[float] = []

    async def controlled_sleep(seconds: float) -> None:
        delays.append(seconds)
        sleep_started.set()
        await release_sleep.wait()

    adapter = QQChannelAdapter(
        enabled=True,
        app_id="app",
        client_secret="secret",
        middleware=object(),
        http_client=FailingHttpClient(),
        sleep=controlled_sleep,
    )

    await adapter.start()
    await asyncio.wait_for(sleep_started.wait(), timeout=1)

    assert adapter._runner_task is not None
    assert not adapter._runner_task.done()
    assert delays == [1.0]
    assert observed_health[0] == (
        "qq",
        {
            "enabled": True,
            "connected": False,
            "status": "connecting",
            "inbound_access_ready": True,
            "access_policy_mode": "platform",
            "access_match": "any",
            "allowed_user_count": 0,
            "allowed_conversation_count": 0,
        },
    )
    assert observed_health[-1] == (
        "qq",
        {
            "connected": False,
            "session_id": None,
            "status": "reconnecting",
            "error_type": "ConnectError",
            "retry_delay_s": 1.0,
        },
    )

    await adapter.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_stage", ["token", "gateway"])
async def test_qq_schema_errors_never_log_secret_response_fields(
    caplog,
    failure_stage: str,
) -> None:
    secret = "qq-response-super-secret"

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/app/getAppAccessToken":
            if failure_stage == "token":
                return httpx.Response(200, json={"accessToken": secret})
            return httpx.Response(200, json={"access_token": "valid", "expires_in": 7200})
        return httpx.Response(
            200,
            json={"websocket": f"wss://gateway.example/?ticket={secret}"},
        )

    async def stop_after_failure(_delay: float) -> None:
        adapter._stop_event.set()

    caplog.set_level("WARNING")
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = QQChannelAdapter(
            enabled=True,
            app_id="app",
            client_secret="secret",
            middleware=object(),
            http_client=client,
            sleep=stop_after_failure,
        )
        await adapter._run_forever()

    assert secret not in caplog.text


def test_qq_adapter_reconnect_delay_is_capped() -> None:
    adapter = QQChannelAdapter(
        enabled=True,
        app_id="app",
        client_secret="secret",
        middleware=object(),
    )

    assert adapter._reconnect_delay(0) == 1.0
    assert adapter._reconnect_delay(1) == 1.0
    assert adapter._reconnect_delay(3) == 4.0
    assert adapter._reconnect_delay(100) == RECONNECT_MAX_DELAY_S


@pytest.mark.asyncio
async def test_channel_middleware_keeps_distinct_message_ids_with_identical_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed_events: list[dict] = []

    def capture_event(**payload) -> None:
        observed_events.append(payload)

    monkeypatch.setattr("imcodex.channels.middleware.emit_event", capture_event)

    clock = iter([1.0, 1.0, 1.1, 1.1])
    store = ConversationStore(clock=lambda: next(clock))
    service = CountingService(store)
    middleware = UnifiedChannelMiddleware(service=service)

    class FakeAdapter:
        channel_id = "qq"

        def __init__(self) -> None:
            self.sent: list[OutboundMessage] = []

        async def send_message(self, message: OutboundMessage) -> None:
            self.sent.append(message)

    adapter = FakeAdapter()
    inbound_1 = InboundMessage(
        channel_id="qq",
        conversation_id="conv-1",
        user_id="u1",
        message_id="m1",
        text="Codex help这种命令你觉得会很重吗？",
    )
    inbound_2 = InboundMessage(
        channel_id="qq",
        conversation_id="conv-1",
        user_id="u1",
        message_id="m2",
        text="Codex help这种命令你觉得会很重吗？",
    )

    await middleware.handle_inbound(adapter, inbound_1, reply_to_message_id="m1")
    await middleware.handle_inbound(adapter, inbound_2, reply_to_message_id="m2")

    assert [message.message_type for message in adapter.sent] == [
        "accepted",
        "accepted",
    ]
    assert [message.message_id for message in service.calls] == ["m1", "m2"]
    assert [event["event"] for event in observed_events] == [
        "message.inbound.received",
        "message.outbound.sending",
        "message.outbound.sent",
        "message.inbound.received",
        "message.outbound.sending",
        "message.outbound.sent",
    ]


@pytest.mark.asyncio
async def test_channel_middleware_drops_persisted_duplicate_message_id(
    tmp_path,
) -> None:
    state_path = tmp_path / "state.json"
    first_store = ConversationStore(clock=lambda: 1.0, state_path=state_path)
    first_service = CountingService(first_store)
    first_middleware = UnifiedChannelMiddleware(service=first_service)

    class FakeAdapter:
        channel_id = "telegram"

        async def send_message(self, _message: OutboundMessage) -> None:
            return None

    inbound = InboundMessage(
        channel_id="telegram",
        conversation_id="chat:42",
        user_id="42",
        message_id="42:7",
        text="/status",
    )
    await first_middleware.handle_inbound(FakeAdapter(), inbound)

    reloaded_store = ConversationStore(clock=lambda: 10.0, state_path=state_path)
    reloaded_service = CountingService(reloaded_store)
    reloaded_middleware = UnifiedChannelMiddleware(service=reloaded_service)
    await reloaded_middleware.handle_inbound(FakeAdapter(), inbound)

    assert reloaded_service.calls == []
