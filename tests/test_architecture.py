"""AST guard tests for the module structure (deepening spec: one-way dependencies, public interfaces,
SessionState owns all state keys). Scan root: src/recall/**/*.py, without __main__.py."""
from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "recall"
ADAPTERS = {"hooks", "mcp_server"}
ADAPTER_USERS = {"hooks", "mcp_server", "cli"}
STATE_CALLS = {"get_state", "set_state", "state_keys", "delete_state", "delete_injected"}


def _modules() -> dict[str, ast.Module]:
    out = {}
    for p in sorted(SRC.rglob("*.py")):
        if p.name == "__main__.py":
            continue
        rel = p.relative_to(SRC).with_suffix("")
        name = ".".join(rel.parts)
        if name.endswith("__init__"):
            name = name[: -len("__init__")].rstrip(".") or "__init__"
        out[name] = ast.parse(p.read_text(encoding="utf-8"), filename=str(p))
    return out


def _imports(tree: ast.Module):
    """Yield (module, imported_name or None) for every recall import, including `from recall import x`."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name == "recall" or a.name.startswith("recall."):
                    yield a.name, None
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            if node.module == "recall":
                for a in node.names:
                    yield f"recall.{a.name}", None
            elif node.module.startswith("recall."):
                for a in node.names:
                    yield node.module, a.name


def test_modules_found():
    mods = _modules()
    assert "hooks" in mods and "guard" in mods and "__main__" not in mods


def _dynamic_imports(tree: ast.Module):
    """Yield the string literals passed to importlib.import_module(...) or __import__(...)."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        f = node.func
        called = f.attr if isinstance(f, ast.Attribute) else f.id if isinstance(f, ast.Name) else None
        arg = node.args[0]
        if called in ("import_module", "__import__") and isinstance(arg, ast.Constant) \
                and isinstance(arg.value, str):
            yield arg.value


def test_only_adapter_users_import_adapters():
    bad = []
    for name, tree in _modules().items():
        if name in ADAPTER_USERS:
            continue
        for mod, _ in _imports(tree):
            if mod.split(".", 1)[-1] in ADAPTERS:
                bad.append(f"{name} -> {mod}")
        for mod in _dynamic_imports(tree):
            if mod in {f"recall.{a}" for a in ADAPTERS}:
                bad.append(f"{name} -> dynamic {mod}")
    assert not bad, bad


def test_dynamic_import_detector():
    tree = ast.parse("import importlib\nimportlib.import_module('recall.hooks')\n__import__('recall.mcp_server')\n")
    assert list(_dynamic_imports(tree)) == ["recall.hooks", "recall.mcp_server"]


def _is_private(n: str) -> bool:
    return n.startswith("_") and not (n.startswith("__") and n.endswith("__"))


def _is_self(v) -> bool:
    return isinstance(v, ast.Name) and v.id in ("self", "cls")


def _private_access(tree: ast.Module):
    """Yield (lineno, name) for `.x_private` and getattr(obj, "_private") on anything but self/cls."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and _is_private(node.attr) and not _is_self(node.value):
            yield node.lineno, node.attr
        elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "getattr"
              and len(node.args) >= 2 and isinstance(node.args[1], ast.Constant)
              and isinstance(node.args[1].value, str) and _is_private(node.args[1].value)
              and not _is_self(node.args[0])):
            yield node.lineno, node.args[1].value


def test_no_private_access_across_modules():
    bad = []
    for name, tree in _modules().items():
        for mod, imported in _imports(tree):
            if imported and _is_private(imported):
                bad.append(f"{name}: from {mod} import {imported}")
        bad += [f"{name}:{line} .{attr}" for line, attr in _private_access(tree)]
    assert not bad, bad


def test_private_access_detector():
    tree = ast.parse("getattr(o, '_x')\ngetattr(self, '_y')\ngetattr(o, 'z')\no._w\nself._v\n")
    assert sorted(a for _, a in _private_access(tree)) == ["_w", "_x"]


def _state_refs(tree: ast.Module):
    """Yield (lineno, name) for every reference to a state operation: attribute or bare name."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in STATE_CALLS:
            yield node.lineno, node.attr
        elif isinstance(node, ast.Name) and node.id in STATE_CALLS:
            yield node.lineno, node.id


def test_only_session_state_touches_state():
    # Any reference counts (a call or e.g. `f = idx.get_state`). localindex defines the operations
    # (it is the storage module), so it is exempt with session_state.
    bad = []
    for name, tree in _modules().items():
        if name in ("session_state", "localindex"):
            continue
        bad += [f"{name}:{line} {ref}" for line, ref in _state_refs(tree)]
    assert not bad, bad


def test_state_ref_detector():
    tree = ast.parse("f = idx.get_state\nidx.set_state('k', 'v')\nx = other\n")
    assert sorted(r for _, r in _state_refs(tree)) == ["get_state", "set_state"]


SECTIONS = {"maintenance": "review_section", "context_gauge": "prompt_section",
            "seed": "session_start_section", "hooks": "_mismatch_section"}


def test_sections_take_runtime_and_open_nothing():
    bad = []
    mods = _modules()
    for mod, fn in SECTIONS.items():
        defs = [n for n in ast.walk(mods[mod]) if isinstance(n, ast.FunctionDef) and n.name == fn]
        assert len(defs) == 1, f"{mod}.{fn} missing"
        args = [a.arg for a in defs[0].args.args]
        if args[:1] != ["rt"]:
            bad.append(f"{mod}.{fn} args {args}")
        for node in ast.walk(defs[0]):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                    and node.func.id in ("SessionState", "LocalIndex"):
                bad.append(f"{mod}.{fn}:{node.lineno} {node.func.id}()")
    assert not bad, bad
