from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from imcodex.admin import ConfigStore
from imcodex.admin.api import install_admin_routes


class _NativeBackend:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    async def read_global_settings(self) -> dict:
        return {
            "config": {
                "model": "gpt-test",
                "model_reasoning_effort": "high",
                "personality": None,
                "service_tier": "default",
                "features": {"fast_mode": True},
                "default_permissions": ":danger-full-access",
                "approval_policy": "never",
                "mcp_servers": {"private": {"env": {"TOKEN": "must-not-leak"}}},
            },
            "models": [
                {
                    "id": "gpt-test",
                    "displayName": "GPT Test",
                    "isDefault": True,
                    "serviceTiers": [{"id": "priority", "name": "Fast", "description": "Faster"}],
                }
            ],
            "reasoningEfforts": [{"reasoningEffort": "high"}],
            "profiles": [{"id": ":danger-full-access"}],
            "nativeProfilesSupported": True,
            "requirements": {"allowedPermissionProfiles": {":danger-full-access": True}},
        }

    async def set_global_model(self, value) -> dict:
        self.calls.append(("model", value))
        return {"status": "updated", "filePath": "/private/config.toml"}

    async def set_global_preferences(self, value) -> dict:
        self.calls.append(("preferences", value))
        return {"status": "updated"}

    async def set_global_reasoning_effort(self, value) -> dict:
        self.calls.append(("reasoning", value))
        return {"status": "updated"}

    async def set_global_personality(self, value) -> dict:
        self.calls.append(("personality", value))
        return {"status": "updated"}

    async def set_global_fast_mode(self, value) -> dict:
        self.calls.append(("fast", value))
        return {"status": "updated"}

    async def set_global_permission_mode(self, value) -> dict:
        self.calls.append(("permission", value))
        return {"status": "updated", "mode": value}


def _app(tmp_path: Path, *, backend: _NativeBackend | None = None) -> tuple[FastAPI, _NativeBackend]:
    resolved_backend = backend or _NativeBackend()
    app = FastAPI()
    runtime = SimpleNamespace(service=SimpleNamespace(backend=resolved_backend))
    install_admin_routes(
        app,
        runtime,
        config_store=ConfigStore(tmp_path / ".env", environ={}),
        csrf_token="csrf-test",
    )
    return app, resolved_backend


def _local_client(app: FastAPI) -> TestClient:
    return _client_with_address(app, "127.0.0.1")


def _client_with_address(app: FastAPI, host: str) -> TestClient:
    class ClientAddress:
        async def __call__(self, scope, receive, send) -> None:
            if scope.get("type") == "http":
                scope = {**scope, "client": (host, 51000)}
            await app(scope, receive, send)

    return TestClient(
        ClientAddress(),
        base_url="http://127.0.0.1",
    )


def test_admin_page_is_loopback_only_and_sends_strict_browser_headers(
    tmp_path: Path,
) -> None:
    app, _backend = _app(tmp_path)
    local = _local_client(app)

    response = local.get("/admin")

    assert response.status_code == 200
    assert "One place to tune your bridge" in response.text
    assert response.headers["cache-control"] == "no-store, max-age=0"
    assert response.headers["x-frame-options"] == "DENY"
    assert "default-src 'self'" in response.headers["content-security-policy"]
    assert local.get("/admin/assets/admin.js").status_code == 200
    assert local.get("/admin/assets/not-present.js").status_code == 404

    remote = _client_with_address(app, "203.0.113.20")
    assert remote.get("/admin").status_code == 403
    assert local.get("/admin", headers={"Host": "attacker.example"}).status_code == 421


def test_admin_frontend_uses_native_catalog_and_authoritative_restart_state(
    tmp_path: Path,
) -> None:
    app, _backend = _app(tmp_path)
    script = _local_client(app).get("/admin/assets/admin.js").text

    assert "function modelSupportsFastMode(model)" in script
    assert 'setting: "preferences"' in script
    assert "Object.fromEntries(preferenceKeys.map" in script
    assert "loadNative({ preserveOnError: true })" in script
    assert 'status.includes("overridden")' in script
    assert 'nativeSettingReadOnly("reasoningEffort", response)' in script
    assert 'state.nativeForcedDirty.add("fast")' in script
    assert "requestedForcedDirty.has(failure.key)" in script
    assert 'label: nativeDefaultLabel("model", response)' in script
    assert "supportsPersonality === false" in script
    assert "response.personalityAvailable !== false" in script
    assert "references.control.input.disabled = references.baseDisabled ||" in script
    assert "state.restartPending = response.restartRequired === true;" in script
    assert "state.restartPending = state.restartPending ||" not in script
    assert "if (!model || !Array.isArray(model.supportedReasoningEfforts)) return;" in script
    assert 'const modelValue = state.nativeDraft.get("model");' not in script


def test_admin_config_api_never_returns_secrets_and_requires_csrf(
    tmp_path: Path,
) -> None:
    path = tmp_path / ".env"
    path.write_text(
        "# preserved\nIMCODEX_QQ_CLIENT_SECRET=very-secret\nUNKNOWN=keep\n",
        encoding="utf-8",
    )
    app, _backend = _app(tmp_path)
    client = _local_client(app)

    response = client.get("/admin/api/config")
    body = response.json()

    assert response.status_code == 200
    assert body["csrfToken"] == "csrf-test"
    assert "very-secret" not in response.text
    assert "UNKNOWN" not in response.text
    qq = next(section for section in body["sections"] if section["id"] == "qq")
    secret = next(field for field in qq["fields"] if field["key"] == "IMCODEX_QQ_CLIENT_SECRET")
    assert secret["secretConfigured"] is True
    assert "value" not in secret

    payload = {
        "revision": body["revision"],
        "values": {"IMCODEX_HTTP_PORT": 8123},
        "secrets": {},
    }
    assert client.put("/admin/api/config", json=payload).status_code == 403
    assert (
        client.put(
            "/admin/api/config",
            json=payload,
            headers={
                "X-IMCodex-CSRF": "csrf-test",
                "Origin": "https://attacker.example",
            },
        ).status_code
        == 403
    )

    saved = client.put(
        "/admin/api/config",
        json=payload,
        headers={
            "X-IMCodex-CSRF": "csrf-test",
            "Origin": "http://127.0.0.1",
        },
    )
    assert saved.status_code == 200
    assert saved.json()["restartRequired"] is True
    assert client.get("/admin/api/config").json()["restartRequired"] is True
    contents = path.read_text(encoding="utf-8")
    assert "# preserved" in contents
    assert "UNKNOWN=keep" in contents
    assert "IMCODEX_HTTP_PORT=8123" in contents


def test_admin_config_api_rejects_stale_revision(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text("IMCODEX_HTTP_PORT=8000\n", encoding="utf-8")
    app, _backend = _app(tmp_path)
    client = _local_client(app)
    stale = client.get("/admin/api/config").json()["revision"]
    path.write_text("IMCODEX_HTTP_PORT=8001\n", encoding="utf-8")

    response = client.put(
        "/admin/api/config",
        json={
            "revision": stale,
            "values": {"IMCODEX_HTTP_PORT": 8123},
            "secrets": {},
        },
        headers={"X-IMCodex-CSRF": "csrf-test"},
    )

    assert response.status_code == 409
    assert path.read_text(encoding="utf-8") == "IMCODEX_HTTP_PORT=8001\n"


def test_admin_native_api_is_whitelisted_and_calls_typed_global_setters(
    tmp_path: Path,
) -> None:
    app, backend = _app(tmp_path)
    client = _local_client(app)

    response = client.get("/admin/api/native")

    assert response.status_code == 200
    assert response.json()["config"]["permissionMode"] == "full-access"
    assert response.json()["config"]["fast"] is False
    assert response.json()["models"][0]["serviceTiers"][0]["id"] == "priority"
    assert "must-not-leak" not in response.text
    assert "mcp_servers" not in response.text

    update = client.put(
        "/admin/api/native",
        json={"setting": "personality", "value": "default"},
        headers={"X-IMCodex-CSRF": "csrf-test"},
    )
    assert update.status_code == 200
    assert backend.calls == [("personality", None)]
    assert "filePath" not in update.text

    transition = client.put(
        "/admin/api/native",
        json={
            "setting": "preferences",
            "value": {"model": "gpt-test", "reasoningEffort": None, "fast": False},
        },
        headers={"X-IMCodex-CSRF": "csrf-test"},
    )
    assert transition.status_code == 200
    assert backend.calls[-1] == (
        "preferences",
        {"model": "gpt-test", "reasoningEffort": None, "fast": False},
    )

    rejected = client.put(
        "/admin/api/native",
        json={"setting": "native.call", "value": "thread/delete"},
        headers={"X-IMCodex-CSRF": "csrf-test"},
    )
    assert rejected.status_code == 422
