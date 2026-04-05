from __future__ import annotations

import ast
from pathlib import Path


def test_layer_packages_exist() -> None:
    root = Path(__file__).resolve().parents[1] / "src" / "imcodex"
    assert (root / "bridge").is_dir()
    assert (root / "channels").is_dir()
    assert (root / "appserver").is_dir()


def test_layer_dependencies_only_flow_forward() -> None:
    root = Path(__file__).resolve().parents[1] / "src" / "imcodex"
    violations: list[str] = []
    for path in root.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        module_name = _module_name(root, path)
        for imported in _imports_for_module(path, module_name):
            if _is_disallowed_dependency(module_name, imported):
                violations.append(f"{module_name} -> {imported}")
    assert violations == []


def _module_name(root: Path, path: Path) -> str:
    relative = path.relative_to(root).with_suffix("")
    parts = ["imcodex", *relative.parts]
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _imports_for_module(path: Path, module_name: str) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            resolved = _resolve_import_from(module_name, node)
            if resolved:
                imports.add(resolved)
    return {name for name in imports if name.startswith("imcodex")}


def _resolve_import_from(module_name: str, node: ast.ImportFrom) -> str | None:
    if node.level == 0:
        return node.module
    module_parts = module_name.split(".")
    package_parts = module_parts[:-1]
    if node.level > len(package_parts):
        return node.module
    anchor = package_parts[: len(package_parts) - node.level + 1]
    if node.module:
        return ".".join([*anchor, node.module])
    return ".".join(anchor)


def _is_disallowed_dependency(module_name: str, imported: str) -> bool:
    if module_name.startswith("imcodex.appserver"):
        return imported.startswith("imcodex.bridge") or imported.startswith("imcodex.channels")
    if module_name.startswith("imcodex.bridge"):
        return imported.startswith("imcodex.channels")
    if module_name.startswith("imcodex.channels"):
        return imported.startswith("imcodex.bridge") or imported.startswith("imcodex.appserver")
    return False
