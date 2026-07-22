from __future__ import annotations

import os
from pathlib import Path
import stat
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from imcodex.channels.artifacts import append_artifact_failures, record_artifact_delivery
from imcodex.delivery_api import (
    DELIVERY_PATH,
    DELIVERY_TOKEN_HEADER,
    install_delivery_route,
)


class Sink:
    def __init__(
        self,
        *,
        reject_artifact: bool = False,
        confirm_then_fail: bool = False,
        confirm_then_reject: bool = False,
    ) -> None:
        self.messages = []
        self.artifact_contents = []
        self.reject_artifact = reject_artifact
        self.confirm_then_fail = confirm_then_fail
        self.confirm_then_reject = confirm_then_reject

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
        if self.reject_artifact:
            append_artifact_failures(
                message,
                [f"{message.artifacts[0].filename}: platform rejected the file"],
            )
            message.artifacts = []


def _app(tmp_path: Path, sink: Sink) -> FastAPI:
    app = FastAPI()
    runtime = SimpleNamespace(
        service=SimpleNamespace(outbound_sink=sink),
        observability=SimpleNamespace(
            context=SimpleNamespace(instance_id="instance-1")
        ),
    )
    credential = install_delivery_route(
        app,
        runtime,
        data_dir=tmp_path,
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
    assert Path(artifact.local_path).parent == tmp_path / "outbound-media" / "tool"
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
