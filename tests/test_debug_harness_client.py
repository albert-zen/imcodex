from __future__ import annotations

from pathlib import Path

from imcodex.debug_harness.client import DebugHarnessClient
from imcodex.debug_harness.models import DebugRunManifest


class _RecordingHttpClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def post(self, url: str, json: dict):
        self.calls.append((url, json))

        class _Response:
            def raise_for_status(self) -> None:
                return None

            def json(self) -> dict:
                return {"messages": [{"text": json.get("text", "")}]}

        return _Response()


def _manifest() -> DebugRunManifest:
    return DebugRunManifest(
        run_id="debug-1",
        pid=51234,
        port=8011,
        purpose="test",
        cwd=str(Path(r"D:\desktop\imcodex-debug-lab\cwd\debug-1")),
        data_dir=str(Path(r"D:\desktop\imcodex-debug-lab\data\debug-1")),
        run_dir=str(Path(r"D:\desktop\imcodex-debug-lab\run\debug-1")),
        started_at="2026-04-19T10:30:01+08:00",
        status="running",
    )


def test_send_to_conversation_posts_single_webhook_message() -> None:
    http_client = _RecordingHttpClient()
    client = DebugHarnessClient(http_client=http_client)

    response = client.send(
        manifest=_manifest(),
        channel_id="debug",
        conversation_id="conv-1",
        user_id="u1",
        text="hello world",
    )

    assert response["messages"][0]["text"] == "hello world"
    assert len(http_client.calls) == 1
    url, payload = http_client.calls[0]
    assert url == "http://127.0.0.1:8011/api/channels/webhook/inbound"
    assert payload["conversation_id"] == "conv-1"
    assert payload["text"] == "hello world"


def test_send_to_thread_attaches_first_then_sends_message() -> None:
    http_client = _RecordingHttpClient()
    client = DebugHarnessClient(http_client=http_client)

    client.send(
        manifest=_manifest(),
        channel_id="debug",
        conversation_id="conv-1",
        user_id="u1",
        text="continue please",
        thread_id="019d-thread",
    )

    assert len(http_client.calls) == 2
    _, attach_payload = http_client.calls[0]
    _, message_payload = http_client.calls[1]
    assert attach_payload["text"] == "/thread attach 019d-thread"
    assert message_payload["text"] == "continue please"


def test_client_can_inject_client_pending_request_and_force_reset() -> None:
    http_client = _RecordingHttpClient()
    client = DebugHarnessClient(http_client=http_client)

    inject_response = client.inject_client_pending_request(
        manifest=_manifest(),
        request_id="native-request-abcdef",
        jsonrpc_id=99,
    )
    reset_response = client.force_client_reset(manifest=_manifest())

    assert inject_response["messages"][0]["text"] == ""
    assert reset_response["messages"][0]["text"] == ""
    assert len(http_client.calls) == 2
    inject_url, inject_payload = http_client.calls[0]
    reset_url, reset_payload = http_client.calls[1]
    assert inject_url == "http://127.0.0.1:8011/api/debug/inject/client-pending-request"
    assert inject_payload == {"request_id": "native-request-abcdef", "jsonrpc_id": 99}
    assert reset_url == "http://127.0.0.1:8011/api/debug/force/client-reset"
    assert reset_payload == {}


def test_client_can_inject_native_style_server_request() -> None:
    http_client = _RecordingHttpClient()
    client = DebugHarnessClient(http_client=http_client)

    response = client.inject_server_request(
        manifest=_manifest(),
        jsonrpc_id=99,
        method="item/commandExecution/requestApproval",
        request_id="native-request-abcdef",
        thread_id="thr-1",
        turn_id="turn-1",
        payload={"reason": "Need approval"},
    )

    assert response["messages"][0]["text"] == ""
    assert len(http_client.calls) == 1
    url, payload = http_client.calls[0]
    assert url == "http://127.0.0.1:8011/api/debug/inject/server-request"
    assert payload == {
        "id": 99,
        "method": "item/commandExecution/requestApproval",
        "request_id": "native-request-abcdef",
        "thread_id": "thr-1",
        "turn_id": "turn-1",
        "payload": {"reason": "Need approval"},
    }
