from __future__ import annotations

import json
import subprocess
from pathlib import Path

from imcodex.appserver import (
    check_generated_server_request_schema_drift,
    compare_server_request_methods,
    extract_server_request_methods,
)
from imcodex.appserver.protocol_map import REJECTED_SERVER_REQUEST_METHODS


def test_extract_server_request_methods_from_generated_schema_shape() -> None:
    schema = {
        "title": "ServerRequest",
        "oneOf": [
            {
                "type": "object",
                "properties": {
                    "method": {"type": "string", "enum": ["item/commandExecution/requestApproval"]},
                    "params": {"type": "object"},
                },
            },
            {
                "$ref": "#/definitions/AttestationRequest",
            },
        ],
        "definitions": {
            "AttestationRequest": {
                "type": "object",
                "properties": {
                    "method": {"type": "string", "const": "attestation/generate"},
                    "params": {"type": "object"},
                },
            }
        },
    }

    assert extract_server_request_methods(schema) == frozenset(
        {
            "item/commandExecution/requestApproval",
            "attestation/generate",
        }
    )


def test_compare_server_request_methods_flags_uncovered_schema_methods() -> None:
    report = compare_server_request_methods(
        {
            "item/commandExecution/requestApproval",
            "native/newServerRequest",
        }
    )

    assert report.ok is False
    assert report.missing_methods == frozenset({"native/newServerRequest"})


def test_current_stable_server_request_schema_methods_are_explicitly_covered() -> None:
    schema_methods = {
        "item/commandExecution/requestApproval",
        "item/fileChange/requestApproval",
        "item/tool/requestUserInput",
        "mcpServer/elicitation/request",
        "item/permissions/requestApproval",
        "item/tool/call",
        "account/chatgptAuthTokens/refresh",
        "attestation/generate",
        "applyPatchApproval",
        "execCommandApproval",
    }

    report = compare_server_request_methods(schema_methods)

    assert report.ok is True
    assert report.missing_methods == frozenset()
    assert "attestation/generate" in REJECTED_SERVER_REQUEST_METHODS


def test_generated_schema_drift_check_uses_codex_schema_command() -> None:
    captured_commands: list[list[str]] = []

    def fake_run(command, **kwargs):
        captured_commands.append(list(command))
        assert kwargs["check"] is True
        assert kwargs["stdout"] == subprocess.PIPE
        assert kwargs["stderr"] == subprocess.PIPE
        output_dir = Path(command[command.index("--out") + 1])
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "ServerRequest.json").write_text(
            json.dumps(
                {
                    "title": "ServerRequest",
                    "oneOf": [
                        {
                            "type": "object",
                            "properties": {
                                "method": {
                                    "type": "string",
                                    "enum": ["item/commandExecution/requestApproval"],
                                }
                            },
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    report = check_generated_server_request_schema_drift(codex_bin="codex-test", run=fake_run)

    assert report.ok is True
    assert captured_commands[0][:3] == ["codex-test", "app-server", "generate-json-schema"]
    assert "--experimental" not in captured_commands[0]
    assert report.command == tuple(captured_commands[0])


def test_generated_schema_drift_check_can_include_experimental_schema() -> None:
    captured_commands: list[list[str]] = []

    def fake_run(command, **kwargs):
        del kwargs
        captured_commands.append(list(command))
        output_dir = Path(command[command.index("--out") + 1])
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "ServerRequest.json").write_text(
            json.dumps({"title": "ServerRequest", "oneOf": []}),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    check_generated_server_request_schema_drift(
        codex_bin="codex-test",
        include_experimental=True,
        run=fake_run,
    )

    assert "--experimental" in captured_commands[0]


def test_generated_schema_drift_check_reports_unavailable_codex() -> None:
    def fake_run(command, **kwargs):
        del command, kwargs
        raise FileNotFoundError("codex-test")

    report = check_generated_server_request_schema_drift(codex_bin="codex-test", run=fake_run)

    assert report.ok is False
    assert report.unavailable_reason is not None
    assert "unavailable" in report.unavailable_reason


def test_generated_schema_drift_check_reports_missing_schema_output() -> None:
    def fake_run(command, **kwargs):
        del kwargs
        output_dir = Path(command[command.index("--out") + 1])
        output_dir.mkdir(parents=True, exist_ok=True)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    report = check_generated_server_request_schema_drift(codex_bin="codex-test", run=fake_run)

    assert report.ok is False
    assert report.unavailable_reason is not None
    assert "could not be read" in report.unavailable_reason
