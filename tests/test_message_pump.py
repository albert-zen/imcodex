from __future__ import annotations

from imcodex.bridge.message_pump import MessagePump


def test_message_pump_suppresses_completed_event_after_early_final_result() -> None:
    pump = MessagePump()

    early = pump.record_agent_message(
        thread_id="thr_1",
        turn_id="turn_1",
        phase="final_answer",
        text="Native final answer",
        emit_commentary=True,
    )
    completed = pump.finalize_turn(
        thread_id="thr_1",
        turn_id="turn_1",
        status="completed",
    )

    assert early is not None
    assert early.message_type == "turn_result"
    assert early.text == "Native final answer"
    assert completed is None


def test_message_pump_surfaces_failed_completion_after_early_final_result() -> None:
    pump = MessagePump()

    early = pump.record_agent_message(
        thread_id="thr_1",
        turn_id="turn_1",
        phase="final_answer",
        text="Native final answer",
        emit_commentary=True,
    )
    pump.record_command(
        thread_id="thr_1",
        turn_id="turn_1",
        command="pytest -q",
        emit_progress=False,
    )
    pump.record_file_change(
        thread_id="thr_1",
        turn_id="turn_1",
        paths=["src/imcodex/store.py"],
        emit_progress=False,
    )
    failed = pump.finalize_turn(
        thread_id="thr_1",
        turn_id="turn_1",
        status="failed",
    )

    assert early is not None
    assert failed is not None
    assert failed.message_type == "turn_result"
    assert "Turn failed." in failed.text
    assert "Native final answer" in failed.text
    assert "Executed `pytest -q`" in failed.text
    assert "src/imcodex/store.py" in failed.text


def test_message_pump_uses_buffered_delta_when_terminal_status_arrives_without_final_answer() -> None:
    pump = MessagePump()

    progress = pump.record_delta(
        thread_id="thr_1",
        turn_id="turn_1",
        delta="Inspecting the current cwd binding.",
        emit_progress=True,
    )
    terminal = pump.finalize_turn(
        thread_id="thr_1",
        turn_id="turn_1",
        status="failed",
    )

    assert progress is not None
    assert progress.message_type == "turn_progress"
    assert terminal is not None
    assert "Turn failed." in terminal.text
    assert "Inspecting the current cwd binding." in terminal.text


def test_message_pump_suppresses_duplicate_commentary_progress_within_same_turn() -> None:
    pump = MessagePump()

    first = pump.record_agent_message(
        thread_id="thr_1",
        turn_id="turn_1",
        phase="draft",
        text="Checking the repository layout.",
        emit_commentary=True,
    )
    duplicate = pump.record_agent_message(
        thread_id="thr_1",
        turn_id="turn_1",
        phase="draft",
        text="Checking the repository layout.",
        emit_commentary=True,
    )

    assert first is not None
    assert first.message_type == "turn_progress"
    assert duplicate is None


def test_message_pump_suppresses_duplicate_tool_progress_within_same_turn() -> None:
    pump = MessagePump()

    first = pump.record_command(
        thread_id="thr_1",
        turn_id="turn_1",
        command="pytest -q",
        emit_progress=True,
    )
    duplicate = pump.record_command(
        thread_id="thr_1",
        turn_id="turn_1",
        command="pytest -q",
        emit_progress=True,
    )
    files = pump.record_file_change(
        thread_id="thr_1",
        turn_id="turn_1",
        paths=["src/imcodex/store.py"],
        emit_progress=True,
    )
    duplicate_files = pump.record_file_change(
        thread_id="thr_1",
        turn_id="turn_1",
        paths=["src/imcodex/store.py"],
        emit_progress=True,
    )

    assert first is not None
    assert first.text == "Executed `pytest -q`"
    assert duplicate is None
    assert files is not None
    assert "src/imcodex/store.py" in files.text
    assert duplicate_files is None


def test_message_pump_suppresses_repeated_progress_even_after_other_updates() -> None:
    pump = MessagePump()

    first = pump.record_agent_message(
        thread_id="thr_1",
        turn_id="turn_1",
        phase="draft",
        text="Checking the repository layout.",
        emit_commentary=True,
    )
    command = pump.record_command(
        thread_id="thr_1",
        turn_id="turn_1",
        command="pytest -q",
        emit_progress=True,
    )
    repeated = pump.record_agent_message(
        thread_id="thr_1",
        turn_id="turn_1",
        phase="draft",
        text="Checking the repository layout.",
        emit_commentary=True,
    )

    assert first is not None
    assert command is not None
    assert repeated is None
