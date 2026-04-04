from imcodex.appserver_supervisor import AppServerSupervisor
import pytest


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
