from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import httpx

from imcodex.channels_cli import _send


def test_channels_send_posts_workspace_artifact_to_running_bridge(
    tmp_path: Path,
    monkeypatch,
) -> None:
    run_dir = tmp_path / "run"
    current = run_dir / "current"
    current.mkdir(parents=True)
    (current / "health.json").write_text(
        json.dumps(
            {
                "instance_id": "instance-1",
                "http": {"listening": True, "host": "0.0.0.0", "port": 8123},
            }
        ),
        encoding="utf-8",
    )
    (current / "delivery-token").write_text("delivery-secret\n", encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    artifact = workspace / "result.md"
    artifact.write_text("# Result\n", encoding="utf-8")
    monkeypatch.chdir(workspace)
    captured = {}

    def fake_post(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return httpx.Response(
            200,
            request=httpx.Request("POST", url),
            json={"status": "delivered", "delivery_id": "stable-1"},
        )

    monkeypatch.setattr("imcodex.channels_cli.httpx.post", fake_post)
    output: list[str] = []

    status = _send(
        SimpleNamespace(run_dir=run_dir),
        channel_id="telegram",
        conversation_id="chat:1",
        text_value="done",
        artifact_values=["result.md"],
        delivery_id="stable-1",
        output=output.append,
    )

    assert status == 0
    assert captured["url"] == "http://127.0.0.1:8123/_imcodex/tools/deliver"
    assert captured["headers"]["x-imcodex-instance"] == "instance-1"
    assert captured["headers"]["x-imcodex-delivery-token"] == "delivery-secret"
    payload = json.loads(captured["data"]["payload"])
    assert payload["delivery_id"] == "stable-1"
    assert payload["artifacts"][0]["kind"] == "file"
    assert json.loads(output[0])["status"] == "delivered"


def test_channels_send_rejects_artifact_outside_current_workspace(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "secret.md"
    outside.write_text("secret", encoding="utf-8")
    monkeypatch.chdir(workspace)
    output: list[str] = []

    status = _send(
        SimpleNamespace(run_dir=tmp_path / "run"),
        channel_id="telegram",
        conversation_id="chat:1",
        text_value="",
        artifact_values=[str(outside)],
        delivery_id="",
        output=output.append,
    )

    assert status == 2
    assert json.loads(output[0])["status"] == "invalid"
