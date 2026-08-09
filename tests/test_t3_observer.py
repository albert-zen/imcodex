from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
import pytest

from imcodex.appserver.thread_observer import NativeThreadObserverError
from imcodex.t3.client import (
    T3NativeThreadObserver,
    T3ObserverConfig,
    _select_project,
    validate_t3_api_url,
)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("http://127.0.0.1:3773/", "http://127.0.0.1:3773"),
        ("http://[::1]:3773", "http://[::1]:3773"),
        ("http://localhost:3773", "http://localhost:3773"),
        ("https://t3.example.test", "https://t3.example.test"),
    ],
)
def test_t3_api_url_accepts_loopback_http_or_remote_https(url: str, expected: str) -> None:
    assert validate_t3_api_url(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "http://t3.example.test",
        "https://user:secret@t3.example.test",
        "https://t3.example.test/path",
        "https://t3.example.test?token=secret",
        "https://t3.example.test#secret",
    ],
)
def test_t3_api_url_rejects_unsafe_forms(url: str) -> None:
    with pytest.raises(ValueError):
        validate_t3_api_url(url)


@pytest.mark.asyncio
async def test_t3_observer_resolves_route_attaches_and_rereads_token(tmp_path: Path) -> None:
    token_file = tmp_path / "token"
    token_file.write_text("token-one\n", encoding="utf-8")
    token_file.chmod(0o600)
    authorizations: list[str] = []
    post_bodies: list[dict] = []
    mapping: dict | None = None

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal mapping
        authorizations.append(request.headers["authorization"])
        if request.url.path.endswith("/native-thread-attachments") and request.method == "GET":
            return httpx.Response(200, json=[] if mapping is None else [mapping])
        if request.url.path.endswith("/shell"):
            return httpx.Response(
                200,
                json={
                    "projects": [
                        {
                            "id": "project-1",
                            "workspaceRoot": "/work/repo",
                            "defaultModelSelection": {"instanceId": "codex-1"},
                        }
                    ]
                },
            )
        post_bodies.append(json.loads(request.content))
        mapping = {
            "threadId": "t3-thread-1",
            "nativeThreadId": "native-1",
            "projectId": "project-1",
            "providerInstanceId": "codex-1",
            "state": "ready",
            "historyImported": False,
        }
        return httpx.Response(200, json=mapping)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://127.0.0.1:3773",
    ) as http_client:
        observer = T3NativeThreadObserver(
            T3ObserverConfig(
                api_url="http://127.0.0.1:3773",
                token_file=token_file,
            ),
            http_client=http_client,
        )
        first = await observer.ensure_ready(native_thread_id="native-1", cwd="/work/repo/wt")
        token_file.write_text("token-two\n", encoding="utf-8")
        second = await observer.ensure_ready(native_thread_id="native-1", cwd="/elsewhere")

    assert first.status == second.status == "ready"
    assert post_bodies == [
        {
            "nativeThreadId": "native-1",
            "projectId": "project-1",
            "providerInstanceId": "codex-1",
            "createIfMissing": True,
        },
        {
            "nativeThreadId": "native-1",
            "projectId": "project-1",
            "providerInstanceId": "codex-1",
            "createIfMissing": True,
        },
    ]
    assert authorizations[:3] == ["Bearer token-one"] * 3
    assert authorizations[3:] == ["Bearer token-two"] * 2


@pytest.mark.asyncio
async def test_t3_observer_maps_error_without_exposing_response_or_token(tmp_path: Path) -> None:
    token_file = tmp_path / "token"
    token_file.write_text("top-secret-token", encoding="utf-8")
    token_file.chmod(0o600)

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, text="response-secret and top-secret-token")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://127.0.0.1:3773",
    ) as http_client:
        observer = T3NativeThreadObserver(
            T3ObserverConfig("http://127.0.0.1:3773", token_file),
            http_client=http_client,
        )
        with pytest.raises(NativeThreadObserverError) as raised:
            await observer.ensure_ready(native_thread_id="native-1", cwd="/work/repo")

    assert raised.value.code == "attachment_conflict"
    assert str(raised.value) == "attachment_conflict"


@pytest.mark.asyncio
async def test_existing_mapping_requires_unique_route(tmp_path: Path) -> None:
    token_file = tmp_path / "token"
    token_file.write_text("secret", encoding="utf-8")
    token_file.chmod(0o600)
    mappings = [
        {
            "threadId": "t3-one",
            "nativeThreadId": "native-1",
            "projectId": "project-1",
            "providerInstanceId": "codex-1",
            "state": "ready",
        },
        {
            "threadId": "t3-two",
            "nativeThreadId": "native-1",
            "projectId": "project-2",
            "providerInstanceId": "codex-2",
            "state": "ready",
        },
    ]

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=mappings)
        body = json.loads(request.content)
        selected = next(
            item
            for item in mappings
            if item["projectId"] == body["projectId"]
            and item["providerInstanceId"] == body["providerInstanceId"]
        )
        return httpx.Response(200, json=selected)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://127.0.0.1:3773",
    ) as http_client:
        ambiguous = T3NativeThreadObserver(
            T3ObserverConfig("http://127.0.0.1:3773", token_file),
            http_client=http_client,
        )
        with pytest.raises(NativeThreadObserverError) as raised:
            await ambiguous.ensure_ready(native_thread_id="native-1", cwd="/work")
        assert raised.value.code == "mapping_resolution_ambiguous"

        explicit = T3NativeThreadObserver(
            T3ObserverConfig(
                "http://127.0.0.1:3773",
                token_file,
                project_id="project-2",
                provider_instance_id="codex-2",
            ),
            http_client=http_client,
        )
        attachment = await explicit.ensure_ready(native_thread_id="native-1", cwd="/work")

    assert attachment.project_id == "project-2"
    assert attachment.provider_instance_id == "codex-2"


def test_project_resolution_uses_unique_longest_ancestor() -> None:
    selected = _select_project(
        {
            "projects": [
                {"id": "outer", "workspaceRoot": "/work"},
                {"id": "inner", "workspaceRoot": "/work/repo"},
            ]
        },
        cwd="/work/repo/worktrees/feature",
        explicit_project_id=None,
    )

    assert selected["id"] == "inner"


def test_project_resolution_fails_on_equal_ambiguity() -> None:
    with pytest.raises(NativeThreadObserverError) as raised:
        _select_project(
            {
                "projects": [
                    {"id": "one", "workspaceRoot": "/work/repo"},
                    {"id": "two", "workspaceRoot": "/work/repo"},
                ]
            },
            cwd="/work/repo/wt",
            explicit_project_id=None,
        )

    assert raised.value.code == "project_resolution_ambiguous"


@pytest.mark.parametrize(
    ("patch", "code"),
    [
        ({"nativeThreadId": "other"}, "native_thread_mismatch"),
        ({"state": "pending"}, "session_not_ready"),
        ({"projectId": "other"}, "route_mismatch"),
    ],
)
def test_attachment_response_must_match_exact_ready_route(patch: dict, code: str) -> None:
    payload = {
        "threadId": "t3-thread-1",
        "nativeThreadId": "native-1",
        "projectId": "project-1",
        "providerInstanceId": "codex-1",
        "state": "ready",
    }
    payload.update(patch)

    with pytest.raises(NativeThreadObserverError) as raised:
        T3NativeThreadObserver._parse_attachment(
            payload,
            expected_native_thread_id="native-1",
            expected_project_id="project-1",
            expected_provider_instance_id="codex-1",
        )

    assert raised.value.code == code


def test_project_resolution_uses_shell_thread_worktree_shape() -> None:
    selected = _select_project(
        {
            "projects": [
                {
                    "id": "project-1",
                    "title": "IMCodex",
                    "workspaceRoot": "/work/repo",
                    "defaultModelSelection": {
                        "provider": "codex",
                        "instanceId": "codex-1",
                    },
                }
            ],
            "threads": [
                {
                    "id": "t3-thread-1",
                    "projectId": "project-1",
                    "worktreePath": "/private/worktrees/feature",
                }
            ],
        },
        cwd="/private/worktrees/feature/src",
        explicit_project_id=None,
    )
    assert selected["id"] == "project-1"


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission enforcement")
@pytest.mark.asyncio
async def test_t3_observer_rejects_insecure_or_symlink_token_files(tmp_path: Path) -> None:
    target = tmp_path / "target-token"
    target.write_text("secret", encoding="utf-8")
    target.chmod(0o600)
    symlink = tmp_path / "token-link"
    symlink.symlink_to(target)

    async def handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("HTTP must not be attempted with an unsafe token file")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://127.0.0.1:3773",
    ) as http_client:
        for path in (target, symlink):
            if path == target:
                target.chmod(0o644)
            observer = T3NativeThreadObserver(
                T3ObserverConfig("http://127.0.0.1:3773", path),
                http_client=http_client,
            )
            with pytest.raises(NativeThreadObserverError) as raised:
                await observer.ensure_ready(native_thread_id="native-1", cwd="/work")
            assert raised.value.code == "token_unavailable"
            target.chmod(0o600)


@pytest.mark.asyncio
async def test_t3_observer_rejects_empty_token_file(tmp_path: Path) -> None:
    token_file = tmp_path / "token"
    token_file.write_text("\n", encoding="utf-8")
    token_file.chmod(0o600)

    async def handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("HTTP must not be attempted with an empty token")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://127.0.0.1:3773",
    ) as http_client:
        observer = T3NativeThreadObserver(
            T3ObserverConfig("http://127.0.0.1:3773", token_file),
            http_client=http_client,
        )
        with pytest.raises(NativeThreadObserverError) as raised:
            await observer.ensure_ready(native_thread_id="native-1", cwd="/work")

    assert raised.value.code == "token_unavailable"
