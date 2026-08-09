from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
from pathlib import Path

import pytest


def _load_probe_module():
    probe_path = Path(__file__).resolve().parents[1] / "scripts" / "probe-shared-codex-app-server.py"
    spec = importlib.util.spec_from_file_location("imcodex_shared_probe", probe_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.skipif(os.name == "nt", reason="the probe uses a Unix process group")
async def test_probe_cleanup_terminates_the_isolated_process_tree() -> None:
    probe = _load_probe_module()
    process = await asyncio.create_subprocess_exec(
        "/bin/sh",
        "-c",
        "sleep 60 & wait",
        start_new_session=True,
    )

    try:
        await probe._terminate_isolated_process_tree(process, grace_seconds=0.1)
        assert process.returncode is not None
        with pytest.raises(ProcessLookupError):
            os.killpg(process.pid, 0)
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


def _term_ignoring_child_code() -> str:
    return (
        "import signal,time;"
        "signal.signal(signal.SIGHUP,signal.SIG_IGN);"
        "signal.signal(signal.SIGTERM,signal.SIG_IGN);"
        "print('ready',flush=True);"
        "time.sleep(60)"
    )


@pytest.mark.skipif(os.name == "nt", reason="the probe uses a Unix process group")
async def test_probe_cleanup_kills_term_ignoring_descendant() -> None:
    parent_code = (
        "import subprocess,sys;"
        f"p=subprocess.Popen([sys.executable,'-c',{_term_ignoring_child_code()!r}],"
        "stdout=subprocess.PIPE,text=True);"
        "p.stdout.readline();p.wait()"
    )
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        parent_code,
        start_new_session=True,
    )

    await probe_cleanup_and_assert_group_gone(process)


@pytest.mark.skipif(os.name == "nt", reason="the probe uses a Unix process group")
async def test_probe_cleanup_kills_orphan_after_parent_already_exited() -> None:
    parent_code = (
        "import subprocess,sys;"
        f"p=subprocess.Popen([sys.executable,'-c',{_term_ignoring_child_code()!r}],"
        "stdout=subprocess.PIPE,text=True);"
        "p.stdout.readline()"
    )
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        parent_code,
        start_new_session=True,
    )
    await asyncio.wait_for(process.wait(), 5)

    await probe_cleanup_and_assert_group_gone(process)


async def probe_cleanup_and_assert_group_gone(process: asyncio.subprocess.Process) -> None:
    probe = _load_probe_module()
    try:
        await probe._terminate_isolated_process_tree(process, grace_seconds=0.1)
        with pytest.raises(ProcessLookupError):
            os.killpg(process.pid, 0)
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


def test_probe_safe_diagnostics_exclude_error_messages_and_payloads() -> None:
    probe = _load_probe_module()
    messages = [
        {
            "method": "error",
            "params": {
                "error": {
                    "code": 503,
                    "message": "credential-must-not-appear",
                    "codexErrorInfo": {"type": "upstreamUnavailable"},
                },
                "payload": {"type": "credential-looking-alphanumeric-value", "code": 999},
            },
        }
    ]

    summary = probe._safe_event_signatures(messages)

    assert "credential-must-not-appear" not in summary
    assert "credential-looking-alphanumeric-value" not in summary
    assert "999" not in summary
    assert '"method":"error"' in summary
    assert '"type":"upstreamUnavailable"' in summary
    assert '"code":503' in summary


async def test_probe_reader_diagnostic_excludes_exception_message() -> None:
    probe = _load_probe_module()

    async def fail_with_secret() -> None:
        raise RuntimeError("credential-looking-reader-secret")

    reader_task = asyncio.create_task(fail_with_secret())
    stderr_task = asyncio.create_task(asyncio.sleep(0, result=b""))
    await asyncio.gather(reader_task, stderr_task, return_exceptions=True)
    client = object.__new__(probe.JsonlClient)
    client.process = type("Process", (), {"returncode": 1})()
    client.messages = []
    client._reader_task = reader_task
    client._stderr_task = stderr_task

    diagnostic = client._diagnostic_state()

    assert "credential-looking-reader-secret" not in diagnostic
    assert "failed:RuntimeError" in diagnostic
