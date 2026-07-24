from __future__ import annotations

import copy
import os
from pathlib import Path
import stat
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from imcodex.bridge.outbound_artifacts import OutboundArtifactStager
from imcodex.bridge.terminal_delivery import TerminalDeliveryMixin
from imcodex.channels.artifacts import (
    append_artifact_failures,
    record_artifact_delivery,
    record_artifact_failure,
)
from imcodex.delivery_api import (
    DELIVERY_PATH,
    DELIVERY_TOKEN_HEADER,
    install_delivery_route,
)
from imcodex.store import ConversationStore


class Sink:
    def __init__(
        self,
        *,
        reject_artifact: bool = False,
        confirm_then_fail: bool = False,
        confirm_then_reject: bool = False,
        reject_all_artifacts: bool = False,
    ) -> None:
        self.messages = []
        self.artifact_contents = []
        self.reject_artifact = reject_artifact
        self.confirm_then_fail = confirm_then_fail
        self.confirm_then_reject = confirm_then_reject
        self.reject_all_artifacts = reject_all_artifacts

    def can_deliver(self, channel_id: str) -> bool:
        return channel_id == "telegram"

    def prepare_durable_message(self, message) -> None:
        return None

    async def send_message(self, message) -> None:
        self.messages.append(message)
        self.artifact_contents = [
            Path(artifact.local_path).read_bytes() for artifact in message.artifacts
        ]
        if self.confirm_then_fail:
            record_artifact_delivery(
                message,
                message.artifacts[0],
                platform_message_id="platform-1",
            )
            raise RuntimeError("later delivery failed")
        if self.confirm_then_reject:
            record_artifact_delivery(
                message,
                message.artifacts[0],
                platform_message_id="platform-1",
            )
            append_artifact_failures(
                message,
                [f"{message.artifacts[1].filename}: platform rejected the file"],
            )
            message.artifacts = []
            return
        if self.reject_all_artifacts:
            for artifact in message.artifacts:
                record_artifact_failure(
                    message,
                    artifact,
                    error="platform rejected the file",
                )
                append_artifact_failures(
                    message,
                    [f"{artifact.filename}: platform rejected the file"],
                )
            message.artifacts = []
            return
        if self.reject_artifact:
            append_artifact_failures(
                message,
                [f"{message.artifacts[0].filename}: platform rejected the file"],
            )
            message.artifacts = []


class DeliveryService:
    def __init__(self, tmp_path: Path, sink: Sink) -> None:
        self.sink = sink
        self.stager = OutboundArtifactStager(tmp_path / "outbound-media")
        self.routes = {"thread-current": ("telegram", "chat:current")}

    def can_deliver_outbound(self, channel_id: str) -> bool:
        return self.sink.can_deliver(channel_id)

    def resolve_outbound_route(self, source_thread_id: str) -> tuple[str, str]:
        try:
            return self.routes[source_thread_id]
        except KeyError:
            raise ValueError(
                "The current Codex thread is not attached to an IM conversation."
            ) from None

    def validate_outbound_message(self, message) -> None:
        return None

    async def stage_outbound_upload(self, content: bytes, **kwargs):
        return self.stager.stage_upload(content, **kwargs)

    async def discard_outbound_uploads(self, artifacts) -> None:
        self.stager.release(artifacts)
        self.stager.cleanup_unreferenced(set())

    async def deliver_outbound_message(self, message):
        self.stager.release(message.artifacts)
        self.sink.prepare_durable_message(message)
        await self.sink.send_message(message)
        return [message], True, True

    def owns_outbound_delivery(self, delivery_id: str) -> bool:
        return False


class QueuedDeliveryService(DeliveryService):
    async def deliver_outbound_message(self, message):
        return [message], False, True

    def owns_outbound_delivery(self, delivery_id: str) -> bool:
        return True


class RejectingDeliveryService(DeliveryService):
    def validate_outbound_message(self, message) -> None:
        raise PermissionError("route is outside the configured access policy")


class CheckpointReceiptDeliveryService(DeliveryService):
    async def deliver_outbound_message(self, message):
        checkpoint = copy.deepcopy(message)
        artifact = checkpoint.artifacts[0]
        record_artifact_failure(
            checkpoint,
            artifact,
            error="platform rejected the file",
        )
        checkpoint.metadata["artifact_receipts"][0]["local_path"] = (
            "/previous-process/result.txt"
        )
        checkpoint.artifacts = []
        return [checkpoint], True, True


def _app(
    tmp_path: Path,
    sink: Sink,
    *,
    delivery_service: DeliveryService | None = None,
) -> FastAPI:
    app = FastAPI()
    runtime = SimpleNamespace(
        service=delivery_service or DeliveryService(tmp_path, sink),
        observability=SimpleNamespace(
            context=SimpleNamespace(instance_id="instance-1")
        ),
    )
    credential = install_delivery_route(
        app,
        runtime,
        run_dir=tmp_path / "run",
    )
    credential.publish()
    app.state.delivery_token = credential.token
    return app


def _headers(app: FastAPI, *, token: str | None = None) -> dict[str, str]:
    return {
        "x-imcodex-instance": "instance-1",
        DELIVERY_TOKEN_HEADER: token or app.state.delivery_token,
    }


def test_delivery_endpoint_stages_file_and_returns_machine_receipt(tmp_path: Path) -> None:
    sink = Sink()
    app = _app(tmp_path, sink)
    client = TestClient(app, client=("127.0.0.1", 50000))

    response = client.post(
        DELIVERY_PATH,
        headers=_headers(app),
        data={
            "payload": (
                '{"channel_id":"telegram","conversation_id":"chat:1",'
                '"text":"done","delivery_id":"stable-1",'
                '"artifacts":[{"kind":"file"}]}'
            )
        },
        files={"artifacts": ("requirements.md", b"# Requirements\n", "text/markdown")},
    )

    assert response.status_code == 200
    receipt = response.json()
    assert receipt["delivery_id"] == "stable-1"
    assert receipt["status"] == "delivered"
    assert receipt["artifacts"] == [
        {
            "filename": "requirements.md",
            "kind": "file",
            "status": "delivered",
            "error": "",
            "platform_message_id": "",
            "delivery_identity": "",
        }
    ]
    artifact = sink.messages[0].artifacts[0]
    assert Path(artifact.local_path).parent == tmp_path / "outbound-media"
    assert not Path(artifact.local_path).exists()
    assert sink.artifact_contents == [b"# Requirements\n"]


def test_delivery_endpoint_reports_partial_artifact_failure(tmp_path: Path) -> None:
    app = _app(tmp_path, Sink(reject_artifact=True))
    client = TestClient(
        app,
        client=("127.0.0.1", 50000),
    )

    response = client.post(
        DELIVERY_PATH,
        headers=_headers(app),
        data={
            "payload": (
                '{"channel_id":"telegram","conversation_id":"chat:1",'
                '"text":"done","delivery_id":"stable-1",'
                '"artifacts":[{"kind":"file"}]}'
            )
        },
        files={"artifacts": ("requirements.md", b"# Requirements\n", "text/markdown")},
    )

    assert response.status_code == 207
    assert response.json()["status"] == "partial"
    assert response.json()["artifacts"][0]["status"] == "failed"


def test_delivery_endpoint_builds_receipt_from_durable_checkpoint_message(
    tmp_path: Path,
) -> None:
    sink = Sink()
    service = CheckpointReceiptDeliveryService(tmp_path, sink)
    app = _app(tmp_path, sink, delivery_service=service)
    client = TestClient(app, client=("127.0.0.1", 50000))

    response = client.post(
        DELIVERY_PATH,
        headers=_headers(app),
        data={
            "payload": (
                '{"channel_id":"telegram","conversation_id":"chat:1",'
                '"text":"done","delivery_id":"external-stable-id",'
                '"artifacts":[{"kind":"file"}]}'
            )
        },
        files={"artifacts": ("result.txt", b"result", "text/plain")},
    )

    assert response.status_code == 207
    assert response.json()["delivery_id"] == "external-stable-id"
    assert response.json()["artifacts"] == [
        {
            "filename": "result.txt",
            "kind": "file",
            "status": "failed",
            "error": "platform rejected the file",
            "platform_message_id": "",
            "delivery_identity": "",
        }
    ]


def test_delivery_endpoint_accepts_plain_text_form(tmp_path: Path) -> None:
    sink = Sink()
    app = _app(tmp_path, sink)
    client = TestClient(app, client=("127.0.0.1", 50000))

    response = client.post(
        DELIVERY_PATH,
        headers=_headers(app),
        data={
            "payload": (
                '{"channel_id":"telegram","conversation_id":"chat:1",'
                '"text":"done","delivery_id":"stable-text"}'
            )
        },
    )

    assert response.status_code == 200
    assert response.json()["status"] == "delivered"
    assert sink.messages[0].text == "done"


def test_delivery_endpoint_resolves_current_thread_route_at_request_time(
    tmp_path: Path,
) -> None:
    sink = Sink()
    service = DeliveryService(tmp_path, sink)
    app = _app(tmp_path, sink, delivery_service=service)
    client = TestClient(app, client=("127.0.0.1", 50000))

    service.routes["thread-current"] = ("telegram", "chat:latest")
    response = client.post(
        DELIVERY_PATH,
        headers=_headers(app),
        data={
            "payload": (
                '{"source_thread_id":"thread-current",'
                '"text":"done","delivery_id":"stable-current"}'
            )
        },
    )

    assert response.status_code == 200
    assert response.json()["status"] == "delivered"
    assert response.json()["channel_id"] == "telegram"
    assert response.json()["conversation_id"] == "chat:latest"
    assert sink.messages[0].channel_id == "telegram"
    assert sink.messages[0].conversation_id == "chat:latest"
    assert sink.messages[0].metadata["source"] == "channels.send.current"


def test_delivery_endpoint_rejects_current_thread_without_im_binding(
    tmp_path: Path,
) -> None:
    sink = Sink()
    app = _app(tmp_path, sink)
    client = TestClient(app, client=("127.0.0.1", 50000))

    response = client.post(
        DELIVERY_PATH,
        headers=_headers(app),
        data={
            "payload": (
                '{"source_thread_id":"thread-unbound",'
                '"text":"done","delivery_id":"stable-unbound"}'
            )
        },
    )

    assert response.status_code == 409
    assert response.json()["status"] == "rejected"
    assert "not attached to an IM conversation" in response.json()["error"]
    assert sink.messages == []


def test_current_route_resolver_follows_latest_cross_channel_binding() -> None:
    class RouteResolver(TerminalDeliveryMixin):
        pass

    store = ConversationStore(clock=lambda: 1.0)
    store.bind_thread("qq", "old-conversation", "thread-current")
    store.bind_thread("telegram", "latest-conversation", "thread-current")
    resolver = RouteResolver()
    resolver.store = store

    assert resolver.resolve_outbound_route("thread-current") == (
        "telegram",
        "latest-conversation",
    )


def test_delivery_endpoint_rejects_ambiguous_current_and_explicit_route(
    tmp_path: Path,
) -> None:
    app = _app(tmp_path, Sink())
    client = TestClient(app, client=("127.0.0.1", 50000))

    response = client.post(
        DELIVERY_PATH,
        headers=_headers(app),
        data={
            "payload": (
                '{"source_thread_id":"thread-current",'
                '"channel_id":"telegram","conversation_id":"chat:1",'
                '"text":"done","delivery_id":"stable-ambiguous"}'
            )
        },
    )

    assert response.status_code == 422
    assert "cannot be combined" in response.json()["detail"]


def test_delivery_endpoint_reports_durably_queued_message(tmp_path: Path) -> None:
    sink = Sink()
    app = _app(
        tmp_path,
        sink,
        delivery_service=QueuedDeliveryService(tmp_path, sink),
    )
    client = TestClient(app, client=("127.0.0.1", 50000))

    response = client.post(
        DELIVERY_PATH,
        headers=_headers(app),
        data={
            "payload": (
                '{"channel_id":"telegram","conversation_id":"chat:1",'
                '"text":"done","delivery_id":"stable-queued"}'
            )
        },
    )

    assert response.status_code == 202
    assert response.json()["status"] == "queued"
    assert response.json()["text_status"] == "queued"


def test_delivery_endpoint_rejects_disallowed_route_before_outbox(tmp_path: Path) -> None:
    sink = Sink()
    service = RejectingDeliveryService(tmp_path, sink)
    app = _app(tmp_path, sink, delivery_service=service)
    client = TestClient(app, client=("127.0.0.1", 50000))

    response = client.post(
        DELIVERY_PATH,
        headers=_headers(app),
        data={
            "payload": (
                '{"channel_id":"telegram","conversation_id":"chat:1",'
                '"text":"done","delivery_id":"stable-rejected",'
                '"artifacts":[{"kind":"file"}]}'
            )
        },
        files={"artifacts": ("notes.txt", b"notes", "text/plain")},
    )

    assert response.status_code == 403
    assert response.json()["status"] == "rejected"
    assert list((tmp_path / "outbound-media").iterdir()) == []


def test_delivery_endpoint_requires_current_loopback_instance(tmp_path: Path) -> None:
    app = _app(tmp_path, Sink())
    client = TestClient(app, client=("127.0.0.1", 50000))

    response = client.post(DELIVERY_PATH, headers=_headers(app, token="wrong"))

    assert response.status_code == 403


def test_delivery_credential_is_private(tmp_path: Path) -> None:
    app = _app(tmp_path, Sink())
    credential_path = tmp_path / "run" / "current" / "delivery-token"

    assert credential_path.read_text(encoding="utf-8").strip() == app.state.delivery_token
    if os.name != "nt":
        assert stat.S_IMODE(credential_path.stat().st_mode) == 0o600


def test_delivery_endpoint_rejects_large_or_excessive_uploads(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr("imcodex.delivery_api.MAX_DELIVERY_ARTIFACT_BYTES", 4)
    app = _app(tmp_path, Sink())
    client = TestClient(app, client=("127.0.0.1", 50000))
    payload = (
        '{"channel_id":"telegram","conversation_id":"chat:1",'
        '"text":"done","delivery_id":"stable-1",'
        '"artifacts":[{"kind":"file"}]}'
    )

    oversized = client.post(
        DELIVERY_PATH,
        headers=_headers(app),
        data={"payload": payload},
        files={"artifacts": ("notes.txt", b"12345", "text/plain")},
    )
    excessive = client.post(
        DELIVERY_PATH,
        headers=_headers(app),
        data={
            "payload": (
                '{"channel_id":"telegram","conversation_id":"chat:1",'
                '"text":"done","delivery_id":"stable-2",'
                '"artifacts":[]}'
            )
        },
        files=[
            ("artifacts", (f"{index}.txt", b"x", "text/plain"))
            for index in range(5)
        ],
    )
    oversized_text = client.post(
        DELIVERY_PATH,
        headers=_headers(app),
        data={"payload": "x" * (70 * 1024)},
    )

    assert oversized.status_code == 413
    assert excessive.status_code == 422
    assert oversized_text.status_code == 413


def test_delivery_receipt_preserves_confirmed_artifact_before_later_failure(
    tmp_path: Path,
) -> None:
    app = _app(tmp_path, Sink(confirm_then_fail=True))
    client = TestClient(app, client=("127.0.0.1", 50000))

    response = client.post(
        DELIVERY_PATH,
        headers=_headers(app),
        data={
            "payload": (
                '{"channel_id":"telegram","conversation_id":"chat:1",'
                '"text":"done","delivery_id":"stable-1",'
                '"artifacts":[{"kind":"file"},{"kind":"file"}]}'
            )
        },
        files=[
            ("artifacts", ("one.txt", b"one", "text/plain")),
            ("artifacts", ("two.txt", b"two", "text/plain")),
        ],
    )

    assert response.status_code == 502
    assert [item["status"] for item in response.json()["artifacts"]] == [
        "delivered",
        "unknown",
    ]
    assert response.json()["artifacts"][0]["platform_message_id"] == "platform-1"


def test_delivery_receipt_disambiguates_same_named_artifacts(tmp_path: Path) -> None:
    app = _app(tmp_path, Sink(confirm_then_reject=True))
    client = TestClient(app, client=("127.0.0.1", 50000))

    response = client.post(
        DELIVERY_PATH,
        headers=_headers(app),
        data={
            "payload": (
                '{"channel_id":"telegram","conversation_id":"chat:1",'
                '"text":"done","delivery_id":"stable-1",'
                '"artifacts":[{"kind":"file"},{"kind":"file"}]}'
            )
        },
        files=[
            ("artifacts", ("result.txt", b"one", "text/plain")),
            ("artifacts", ("result.txt", b"two", "text/plain")),
        ],
    )

    assert response.status_code == 207
    assert [item["status"] for item in response.json()["artifacts"]] == [
        "delivered",
        "failed",
    ]


def test_delivery_receipt_reports_both_same_named_failures(tmp_path: Path) -> None:
    app = _app(tmp_path, Sink(reject_all_artifacts=True))
    client = TestClient(app, client=("127.0.0.1", 50000))

    response = client.post(
        DELIVERY_PATH,
        headers=_headers(app),
        data={
            "payload": (
                '{"channel_id":"telegram","conversation_id":"chat:1",'
                '"text":"done","delivery_id":"stable-1",'
                '"artifacts":[{"kind":"file"},{"kind":"file"}]}'
            )
        },
        files=[
            ("artifacts", ("result.txt", b"one", "text/plain")),
            ("artifacts", ("result.txt", b"two", "text/plain")),
        ],
    )

    assert response.status_code == 207
    assert [item["status"] for item in response.json()["artifacts"]] == [
        "failed",
        "failed",
    ]
