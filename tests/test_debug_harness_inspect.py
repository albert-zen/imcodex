from __future__ import annotations

import json
from collections import deque
from pathlib import Path

from imcodex.debug_harness.inspect import DebugHarnessInspector
from imcodex.debug_harness.models import DebugRunManifest


class _RecordingHttpClient:
    def __init__(self, conversation_body: dict, runtime_body: dict, thread_body: dict) -> None:
        self.conversation_body = conversation_body
        self.runtime_body = runtime_body
        self.thread_body = thread_body
        self.calls: list[str] = []

    def get(self, url: str):
        self.calls.append(url)

        class _Response:
            def __init__(self, body: dict) -> None:
                self._body = body

            def raise_for_status(self) -> None:
                return None

            def json(self) -> dict:
                return self._body

        if url.endswith("/api/debug/runtime"):
            return _Response(self.runtime_body)
        if "/api/debug/thread/" in url:
            return _Response(self.thread_body)
        return _Response(self.conversation_body)


class _SequencedHttpClient:
    def __init__(self, conversation_bodies: list[dict]) -> None:
        self.conversation_bodies = deque(conversation_bodies)

    def get(self, _url: str):
        class _Response:
            def __init__(self, body: dict) -> None:
                self._body = body

            def raise_for_status(self) -> None:
                return None

            def json(self) -> dict:
                return self._body

        if self.conversation_bodies:
            body = self.conversation_bodies.popleft()
        else:
            body = {}
        return _Response(body)


def _manifest(tmp_path: Path) -> DebugRunManifest:
    return DebugRunManifest(
        run_id="debug-1",
        pid=51234,
        port=8011,
        purpose="test",
        cwd=str(tmp_path / "cwd"),
        data_dir=str(tmp_path / "data"),
        run_dir=str(tmp_path / "run"),
        started_at="2026-04-19T10:30:01+08:00",
        status="running",
    )


def test_inspector_reads_instance_files_and_tails_events(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    current = Path(manifest.run_dir) / "current"
    current.mkdir(parents=True)
    (current / "instance.json").write_text(json.dumps({"instance_id": "inst-1"}), encoding="utf-8")
    (current / "health.json").write_text(json.dumps({"status": "healthy"}), encoding="utf-8")
    (current / "events.jsonl").write_text(
        "\n".join(
            [
                json.dumps({"event": "bridge.starting"}),
                json.dumps({"event": "appserver.connect.started"}),
                json.dumps({"event": "bridge.started"}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    inspector = DebugHarnessInspector(http_client=_RecordingHttpClient({}, {}, {}))

    summary = inspector.inspect_run(manifest, tail=2)

    assert summary["instance"]["instance_id"] == "inst-1"
    assert summary["health"]["status"] == "healthy"
    assert [event["event"] for event in summary["events"]] == [
        "appserver.connect.started",
        "bridge.started",
    ]


def test_inspector_fetches_live_runtime_and_conversation_state(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    http_client = _RecordingHttpClient(
        conversation_body={"binding": {"thread_id": "thr-1"}},
        runtime_body={"appserver": {"connection_mode": "shared-ws"}},
        thread_body={"thread_snapshot": {"thread_id": "thr-1"}},
    )
    inspector = DebugHarnessInspector(http_client=http_client)

    runtime_state = inspector.inspect_runtime_state(manifest)
    conversation_state = inspector.inspect_conversation(manifest, "debug", "conv-1")
    thread_state = inspector.inspect_thread(manifest, "thr-1")

    assert runtime_state["appserver"]["connection_mode"] == "shared-ws"
    assert conversation_state["binding"]["thread_id"] == "thr-1"
    assert thread_state["thread_snapshot"]["thread_id"] == "thr-1"


def test_inspector_waits_until_pending_requests_appear(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    inspector = DebugHarnessInspector(
        http_client=_SequencedHttpClient(
            [
                {"pending_requests": []},
                {"pending_requests": []},
                {"pending_requests": [{"request_id": "native-request-abcdef"}]},
            ]
        )
    )

    result = inspector.wait_for_pending_requests(
        manifest,
        "debug",
        "conv-1",
        timeout_s=1.0,
        interval_s=0.0,
    )

    assert result["pending_requests"][0]["request_id"] == "native-request-abcdef"


def test_inspector_waits_until_pending_requests_clear(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    inspector = DebugHarnessInspector(
        http_client=_SequencedHttpClient(
            [
                {"pending_requests": [{"request_id": "native-request-abcdef"}]},
                {"pending_requests": [{"request_id": "native-request-abcdef"}]},
                {"pending_requests": []},
            ]
        )
    )

    result = inspector.wait_until_no_pending_requests(
        manifest,
        "debug",
        "conv-1",
        timeout_s=1.0,
        interval_s=0.0,
    )

    assert result["pending_requests"] == []


def test_inspector_waits_until_active_turn_appears(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    inspector = DebugHarnessInspector(
        http_client=_SequencedHttpClient(
            [
                {"active_turn": None},
                {"active_turn": None},
                {"active_turn": {"turn_id": "turn-123", "status": "inProgress"}},
            ]
        )
    )

    result = inspector.wait_for_active_turn(
        manifest,
        "debug",
        "conv-1",
        timeout_s=1.0,
        interval_s=0.0,
    )

    assert result["active_turn"]["turn_id"] == "turn-123"
