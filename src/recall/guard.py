"""Write guard (PreToolUse): file tools must not write into the Recall store.

The store changes only through recall_ingest, so the Gate checks every page write.
Shell writes (`exec`) are not blocked in v1; the Skill forbids them.
"""
from __future__ import annotations

import os
import re

GUARDED_TOOLS = frozenset({"write", "edit", "apply_patch", "multi_edit"})
PATH_KEYS = ("file_path", "path", "notebook_path", "target_file")
PATCH_RE = re.compile(r"^\*\*\* (?:Add|Update|Delete) File: (.+?)\s*$", re.MULTILINE)
BLOCK_MSG = "Recall store is managed. Use recall_ingest."


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _strings(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _strings(v)


def _paths(value) -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        for k in PATH_KEYS:
            v = value.get(k)
            if isinstance(v, str) and v.strip():
                found.append(v)
        for v in value.values():
            if isinstance(v, (dict, list, tuple)):
                found.extend(_paths(v))
    elif isinstance(value, (list, tuple)):
        for v in value:
            found.extend(_paths(v))
    return found


def _norm(p: str) -> str:
    # realpath folds `..`, mixed slashes and links; normcase folds case on Windows.
    return os.path.normcase(os.path.realpath(p))


def _inside(path: str, base: str, store_root: str) -> bool:
    p = path.strip().strip('"').strip("'")
    if not p:
        return False
    full = _norm(os.path.join(base, os.path.expanduser(p)))
    return full == store_root or full.startswith(store_root.rstrip(os.sep) + os.sep)


def decide(store_root, repo_root, payload: dict) -> str | None:
    """Block reason if a file tool in `payload` writes into the store at `store_root`, else None.

    Relative paths are resolved against `repo_root`.
    """
    tool = str(payload.get("tool_name") or "").lower()
    ti = payload.get("tool_input")
    if tool not in GUARDED_TOOLS or not isinstance(ti, dict):
        return None
    candidates = _paths(ti)
    for s in _strings(ti):
        candidates.extend(PATCH_RE.findall(s))
    base = str(repo_root)
    root = _norm(str(store_root))
    for c in candidates:
        if _inside(c, base, root):
            return BLOCK_MSG
    return None
