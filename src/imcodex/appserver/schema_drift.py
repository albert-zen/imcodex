from __future__ import annotations

import json
import subprocess
import tempfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .protocol_map import REJECTED_SERVER_REQUEST_METHODS, SUPPORTED_SERVER_REQUEST_METHODS


JsonDict = dict[str, Any]
SchemaCommandRunner = Callable[..., subprocess.CompletedProcess[str]]


@dataclass(frozen=True, slots=True)
class ServerRequestSchemaDriftReport:
    schema_methods: frozenset[str]
    supported_methods: frozenset[str]
    rejected_methods: frozenset[str]
    missing_methods: frozenset[str]
    extra_bridge_methods: frozenset[str]
    command: tuple[str, ...] = ()
    unavailable_reason: str | None = None

    @property
    def covered_methods(self) -> frozenset[str]:
        return self.supported_methods | self.rejected_methods

    @property
    def ok(self) -> bool:
        return self.unavailable_reason is None and not self.missing_methods


def compare_server_request_methods(
    schema_methods: Iterable[str],
    *,
    supported_methods: Iterable[str] = SUPPORTED_SERVER_REQUEST_METHODS,
    rejected_methods: Iterable[str] = REJECTED_SERVER_REQUEST_METHODS,
    command: Iterable[str] = (),
    unavailable_reason: str | None = None,
) -> ServerRequestSchemaDriftReport:
    schema_method_set = frozenset(schema_methods)
    supported_method_set = frozenset(supported_methods)
    rejected_method_set = frozenset(rejected_methods)
    covered_methods = supported_method_set | rejected_method_set
    return ServerRequestSchemaDriftReport(
        schema_methods=schema_method_set,
        supported_methods=supported_method_set,
        rejected_methods=rejected_method_set,
        missing_methods=frozenset(schema_method_set - covered_methods),
        extra_bridge_methods=frozenset(covered_methods - schema_method_set),
        command=tuple(command),
        unavailable_reason=unavailable_reason,
    )


def check_generated_server_request_schema_drift(
    *,
    codex_bin: str = "codex",
    include_experimental: bool = False,
    timeout_s: float = 15.0,
    run: SchemaCommandRunner = subprocess.run,
) -> ServerRequestSchemaDriftReport:
    with tempfile.TemporaryDirectory(prefix="imcodex-appserver-schema-") as output_dir:
        command = [codex_bin, "app-server", "generate-json-schema", "--out", output_dir]
        if include_experimental:
            command.append("--experimental")
        try:
            run(
                command,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=timeout_s,
            )
        except FileNotFoundError as exc:
            return compare_server_request_methods(
                (),
                command=command,
                unavailable_reason=f"schema generation command is unavailable: {exc}",
            )
        except subprocess.TimeoutExpired as exc:
            return compare_server_request_methods(
                (),
                command=command,
                unavailable_reason=f"schema generation timed out after {timeout_s:.1f}s: {exc}",
            )
        except subprocess.CalledProcessError as exc:
            detail = str(exc.stderr or exc)
            return compare_server_request_methods(
                (),
                command=command,
                unavailable_reason=f"schema generation failed: {detail}",
            )
        try:
            schema = load_server_request_schema(output_dir)
        except (OSError, json.JSONDecodeError) as exc:
            return compare_server_request_methods(
                (),
                command=command,
                unavailable_reason=f"schema generation output could not be read: {exc}",
            )
        return compare_server_request_methods(
            extract_server_request_methods(schema),
            command=command,
        )


def load_server_request_schema(schema_dir: str | Path) -> JsonDict:
    path = Path(schema_dir)
    candidates = (
        path / "ServerRequest.json",
        path / "codex_app_server_protocol.schemas.json",
        path / "codex_app_server_protocol.v2.schemas.json",
    )
    for candidate in candidates:
        if candidate.exists():
            return json.loads(candidate.read_text(encoding="utf-8"))
    raise FileNotFoundError(f"generated app-server schema did not include ServerRequest.json under {path}")


def extract_server_request_methods(schema: JsonDict) -> frozenset[str]:
    request_schema = _find_server_request_schema(schema)
    if request_schema is None:
        return frozenset()
    methods: set[str] = set()
    _collect_method_literals(request_schema, schema, methods, seen=set())
    return frozenset(methods)


def _find_server_request_schema(schema: JsonDict) -> JsonDict | None:
    if schema.get("title") == "ServerRequest":
        return schema
    for container in _definition_containers(schema):
        candidate = container.get("ServerRequest")
        if isinstance(candidate, dict):
            return candidate
        for value in container.values():
            if isinstance(value, dict) and value.get("title") == "ServerRequest":
                return value
    return None


def _definition_containers(schema: JsonDict) -> list[JsonDict]:
    containers: list[JsonDict] = []
    for key in ("definitions", "$defs"):
        value = schema.get(key)
        if isinstance(value, dict):
            containers.append(value)
    components = schema.get("components")
    if isinstance(components, dict):
        schemas = components.get("schemas")
        if isinstance(schemas, dict):
            containers.append(schemas)
    return containers


def _collect_method_literals(schema: Any, root: JsonDict, methods: set[str], *, seen: set[int]) -> None:
    if not isinstance(schema, dict):
        return
    schema_id = id(schema)
    if schema_id in seen:
        return
    seen.add(schema_id)
    ref = schema.get("$ref")
    if isinstance(ref, str):
        resolved = _resolve_local_ref(root, ref)
        if resolved is not None:
            _collect_method_literals(resolved, root, methods, seen=seen)
        return

    properties = schema.get("properties")
    if isinstance(properties, dict):
        method_schema = properties.get("method")
        if isinstance(method_schema, dict):
            methods.update(_method_literals(method_schema, root, seen))

    for key in ("oneOf", "anyOf", "allOf"):
        variants = schema.get(key)
        if isinstance(variants, list):
            for variant in variants:
                _collect_method_literals(variant, root, methods, seen=seen)


def _method_literals(schema: JsonDict, root: JsonDict, seen: set[int]) -> set[str]:
    values: set[str] = set()
    ref = schema.get("$ref")
    if isinstance(ref, str):
        resolved = _resolve_local_ref(root, ref)
        if isinstance(resolved, dict):
            values.update(_method_literals(resolved, root, seen))
        return values
    const = schema.get("const")
    if isinstance(const, str):
        values.add(const)
    enum = schema.get("enum")
    if isinstance(enum, list):
        values.update(value for value in enum if isinstance(value, str))
    for key in ("oneOf", "anyOf", "allOf"):
        variants = schema.get(key)
        if isinstance(variants, list):
            for variant in variants:
                if isinstance(variant, dict):
                    variant_id = id(variant)
                    if variant_id in seen:
                        continue
                    seen.add(variant_id)
                    values.update(_method_literals(variant, root, seen))
    return values


def _resolve_local_ref(root: JsonDict, ref: str) -> JsonDict | None:
    if not ref.startswith("#/"):
        return None
    value: Any = root
    for raw_part in ref[2:].split("/"):
        part = raw_part.replace("~1", "/").replace("~0", "~")
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value if isinstance(value, dict) else None
