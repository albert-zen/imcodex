import imcodex.appserver_supervisor as supervisor_module
from imcodex.appserver_supervisor import AppServerSupervisor
import pytest
from pathlib import Path


class FakeProcess:
    def __init__(self) -> None:
        self.terminated = 0
        self.waited = 0

    def terminate(self) -> None:
        self.terminated += 1

    async def wait(self) -> None:
        self.waited += 1


def test_supervisor_builds_loopback_ws_command():
    supervisor = AppServerSupervisor(port=8765)

    command = supervisor.build_command()

    assert command[:3] == ["codex", "app-server", "--listen"]
    assert command[3] == "ws://127.0.0.1:8765"
    assert supervisor.ready_url.endswith("/readyz")


def test_supervisor_detects_readiness_from_probe_response():
    supervisor = AppServerSupervisor(port=8765)

    assert supervisor.is_ready("200 OK") is True
    assert supervisor.is_ready("503 Service Unavailable") is False


@pytest.mark.asyncio
async def test_supervisor_start_waits_for_ready_and_stop_terminates() -> None:
    calls = []
    process = FakeProcess()

    async def fake_spawn(*command):
        calls.append(command)
        return process

    probe_results = iter([503, 200])

    async def fake_probe(url: str):
        calls.append(("probe", url))
        return next(probe_results)

    supervisor = AppServerSupervisor(port=8765, spawn_process=fake_spawn, probe_ready=fake_probe)

    await supervisor.start()
    await supervisor.stop()

    assert calls[0][:3] == ("codex", "app-server", "--listen")
    assert calls[1][0] == "probe"
    assert calls[2][0] == "probe"
    assert process.terminated == 1
    assert process.waited == 1


@pytest.mark.asyncio
async def test_default_spawn_prefers_windows_executable(monkeypatch, tmp_path: Path) -> None:
    exe_path = tmp_path / "codex.exe"
    exe_path.write_text("", encoding="utf-8")
    seen = {}

    async def fake_exec(*command):
        seen["command"] = command
        return FakeProcess()

    monkeypatch.setattr(supervisor_module.os, "name", "nt")
    monkeypatch.setattr(
        supervisor_module.shutil,
        "which",
        lambda value: str(exe_path) if value == "codex.exe" else None,
    )
    monkeypatch.setattr(
        supervisor_module.asyncio,
        "create_subprocess_exec",
        fake_exec,
    )

    supervisor = AppServerSupervisor(port=8765)

    await supervisor._default_spawn("codex", "app-server", "--listen", "ws://127.0.0.1:8765")

    assert seen["command"][0] == str(exe_path)


@pytest.mark.asyncio
async def test_default_spawn_prefers_windows_cmd_shim_over_inaccessible_store_exe(
    monkeypatch,
    tmp_path: Path,
) -> None:
    cmd_path = tmp_path / "codex.cmd"
    cmd_path.write_text("@echo off\r\n", encoding="utf-8")
    seen = {}

    async def fake_exec(*command):
        seen["command"] = command
        return FakeProcess()

    monkeypatch.setattr(supervisor_module.os, "name", "nt")
    monkeypatch.setattr(
        supervisor_module.shutil,
        "which",
        lambda value: (
            str(cmd_path)
            if value == "codex.cmd"
            else r"C:\Program Files\WindowsApps\OpenAI.Codex\codex.exe"
            if value == "codex.exe"
            else None
        ),
    )
    monkeypatch.setattr(
        supervisor_module.asyncio,
        "create_subprocess_exec",
        fake_exec,
    )

    supervisor = AppServerSupervisor(port=8765)

    await supervisor._default_spawn("codex", "app-server", "--listen", "ws://127.0.0.1:8765")

    assert seen["command"][:3] == ("cmd.exe", "/c", str(cmd_path))


@pytest.mark.asyncio
async def test_default_probe_falls_back_to_tcp_when_http_readyz_is_not_available(monkeypatch) -> None:
    class FakeAsyncClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, url: str):
            raise supervisor_module.httpx.RemoteProtocolError("ws only")

    class FakeWriter:
        def close(self) -> None:
            return None

        async def wait_closed(self) -> None:
            return None

    async def fake_open_connection(host: str, port: int):
        return object(), FakeWriter()

    monkeypatch.setattr(supervisor_module.httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.setattr(supervisor_module.asyncio, "open_connection", fake_open_connection)

    supervisor = AppServerSupervisor(port=8765)

    result = await supervisor._default_probe("http://127.0.0.1:8765/readyz")

    assert result is True
