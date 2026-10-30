"""Runtime: the one place where an adapter opens a Store and a LocalIndex.

A Runtime lives for one hook call or one MCP tool call and is never reused. It is lazy: opening
it runs no git process and reads no page. The search index is rebuilt only when the page
fingerprint (sorted (path, mtime_ns, size) of all page files, from os.stat) differs from the one
saved at the last rebuild, or when the index has no rows. The page scan is cached with
`Store.generation`, so a Store write in the same call makes the next scan read again.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import traceback
from dataclasses import dataclass
from pathlib import Path

from recall.history import GitError, RefError
from recall.localindex import LocalIndex
from recall.paths import log_error
from recall.session_state import SessionState
from recall.store import LockBusy, Store

# MCP error mapping (unchanged): these give "<Type>: <masked message>", others an internal error.
EXPECTED = (RefError, LockBusy, GitError, ValueError, RuntimeError, KeyError, TypeError)
_SECRET_RE = re.compile(r"(?i)((?:token|secret|password|passwd|api[_-]?key|authorization|bearer)\s*[:=]?\s*)\S+")
_URL_CRED_RE = re.compile(r"(\w+://)[^/@\s]+@")


@dataclass
class HookResult:
    stdout: str = ""
    exit_code: int = 0
    stderr: str = ""


def store_folder_correction(path) -> Path:
    """The owner repo when `path` is a Recall store folder (.devin/recall) or inside one; else `path`.

    The store is its own git repo, so git (and a `.git` search) can land in it."""
    p = Path(path)
    for d in (p, *p.parents):
        if d.name.lower() == "recall" and d.parent.name.lower() == ".devin":
            return d.parent.parent
    return p


def safe_msg(exc: BaseException) -> str:
    msg = " ".join(str(exc).split())[:400]
    msg = _URL_CRED_RE.sub(r"\1***@", msg)
    return _SECRET_RE.sub(r"\1***", msg) or type(exc).__name__


def run_mcp(fn, log_root) -> dict:
    """Run one MCP tool body. Expected errors -> {"error": "<Type>: <masked>"}; others are logged
    (under `log_root()`, or the fallback folder when that fails) and return a generic error."""
    try:
        return fn()
    except EXPECTED as e:
        return {"error": f"{type(e).__name__}: {safe_msg(e)}"}
    except Exception as e:  # noqa: BLE001 - a tool never crashes the server
        try:
            log_error(log_root(), e)
        except Exception:
            log_error(None, e)
        return {"error": f"internal error ({type(e).__name__}); see errors.log"}


def run_hook(root, label: str, fn) -> HookResult:
    """Run one hook body. Any exception is logged with its traceback and gives an empty result
    (exit 0); only a result that the body returns can block (the guard's exit 2)."""
    try:
        return fn()
    except Exception as e:
        log_error(root, f"{label}: " + "".join(traceback.format_exception(type(e), e, e.__traceback__)))
        return HookResult()


class Runtime:
    def __init__(self, store: Store):
        self.store = store
        self._local: LocalIndex | None = None
        self._state: SessionState | None = None
        self._scan: list[tuple[str, dict, str]] | None = None
        self._scan_gen = -1

    @classmethod
    def open(cls, repo_root) -> "Runtime":
        """Open the store (file checks only when it exists; may raise LockBusy on first init)."""
        store = Store(repo_root)
        store.init()
        return cls(store)

    # ---------- local index and state ----------
    @property
    def local(self) -> LocalIndex:
        """The LocalIndex as it is (no freshness check): usage counters, injected rows, state."""
        if self._local is None:
            self._local = LocalIndex(self.store.repo_root)
        return self._local

    @property
    def state(self) -> SessionState:
        if self._state is None:
            self._state = SessionState(self.local)
        return self._state

    def begin_hook(self, sid: str, now: float) -> None:
        """A hook call of session `sid`: last-seen time, and a provisional start when missing."""
        self.state.touch(sid, now)
        self.state.ensure_start(sid, now)

    # ---------- pages ----------
    def pages(self) -> list[tuple[str, dict, str]]:
        """[(rel, front, body)] of all readable pages; read once per Store generation."""
        if self._scan is None or self._scan_gen != self.store.generation:
            gen = self.store.generation
            scan = []
            for rel in self.store.pages():
                try:
                    front, body = self.store.read_page(rel)
                except Exception:
                    continue
                if isinstance(front, dict):
                    scan.append((rel, front, body))
            self._scan, self._scan_gen = scan, gen
        return self._scan

    def _fingerprint(self) -> tuple[str, int]:
        """(hash of the sorted (path, mtime_ns, size) of all page files, number of pages). os.stat only."""
        rows = []
        for rel in self.store.pages():
            try:
                st = os.stat(self.store.root / rel)
            except OSError:
                continue
            rows.append([rel, st.st_mtime_ns, st.st_size])
        return hashlib.sha1(json.dumps(rows).encode("utf-8")).hexdigest(), len(rows)

    @property
    def index(self) -> LocalIndex:
        """The LocalIndex with a search index that matches the page files (rebuilt only on change)."""
        fp, n = self._fingerprint()  # before the scan: a change during the rebuild shows next time
        if fp != self.state.fingerprint() or (n and self.local.is_empty()):
            self.local.rebuild(_ScanStore(self))
            self.state.set_fingerprint(fp)
        return self.local


class _ScanStore:
    """What LocalIndex.rebuild needs, served from the Runtime page scan."""

    def __init__(self, rt: Runtime):
        self._pages = {rel: (front, body) for rel, front, body in rt.pages()}

    def pages(self) -> list[str]:
        return list(self._pages)

    def read_page(self, rel: str):
        return self._pages[rel]
