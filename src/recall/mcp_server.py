"""MCP adapter: thin tools over Runtime (Store, History, LocalIndex), maintenance and eval."""
from __future__ import annotations

import functools
import json
import os
import re
import time
from datetime import date
from pathlib import Path

from mcp.server.mcpserver import MCPServer as _Server  # mcp 2.x (pinned <3 in pyproject)

from recall.runtime import Runtime, run_mcp, store_folder_correction

mcp = _Server("recall")

_TOP_FILES = ("index.md", "log.md", "SCHEMA.md", "eval.md")
# Raw records this MCP process returned through recall_get: one server lives for one Devin
# session, so the set is per-session. recall_ingest refuses to cite or consume unread raw.
_READ_RAW: set[str] = set()
_REL_RE = re.compile(r"(?:pages/[a-z]+/[\w.-]+\.md|raw/[\w.-]+\.md)")


def _repo_root() -> Path:
    """RECALL_REPO, else DEVIN_PROJECT_DIR, else the nearest folder with .git;
    a store folder resolves to its owner repo."""
    env = os.environ.get("RECALL_REPO") or os.environ.get("DEVIN_PROJECT_DIR")
    if env:
        return store_folder_correction(Path(env).resolve())
    here = Path.cwd().resolve()
    for d in (here, *here.parents):
        if (d / ".git").exists():
            return store_folder_correction(d)
    raise ValueError("no repo: set RECALL_REPO or run inside a git repo")


def _open() -> Runtime:
    return Runtime.open(_repo_root())


def _jsonable(value):
    return json.loads(json.dumps(value, default=str))


def _tool(fn):
    """Register a tool; map known errors to {"error": ...}, log unknown ones without leaking details."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        return run_mcp(lambda: _jsonable(fn(*args, **kwargs)), log_root=_repo_root)
    mcp.tool()(wrapper)
    return wrapper


def _check_rel(path: str) -> str:
    path = str(path or "").replace("\\", "/").strip()
    if path in _TOP_FILES or (_REL_RE.fullmatch(path) and ".." not in path.split("/")):
        return path
    raise ValueError(f"bad path: {path!r} (want pages/<type>/<slug>.md, raw/<file>.md or {', '.join(_TOP_FILES)})")


def _check_page(path: str) -> str:
    path = _check_rel(path)
    if not path.startswith("pages/"):
        raise ValueError(f"not a page path: {path!r}")
    return path


def _entry(e) -> dict:
    return {"sha": e.sha, "date": e.date, "subject": e.subject, "trailers": e.trailers}


@_tool
def recall_search(query: str, k: int = 5, include_retired: bool = False) -> list[dict]:
    """Search the Recall wiki pages (FTS). Returns [{path, title, status}]."""
    return _open().index.search(query, k=max(1, min(int(k), 20)), include_retired=bool(include_retired))


@_tool
def recall_get(path: str) -> dict:
    """Read a page (also retired), a raw record, index.md, log.md, SCHEMA.md or eval.md."""
    store = _open().store
    rel = _check_rel(path)
    if not (store.root / rel).is_file():
        raise ValueError(f"not found: {rel}")
    if rel.startswith("raw/"):
        _READ_RAW.add(rel)
    if rel.startswith("pages/"):
        front, body = store.read_page(rel)
        return {"path": rel, "front": front, "body": body}
    return {"path": rel, "text": (store.root / rel).read_text(encoding="utf-8")}


@_tool
def recall_ingest(pages: list[dict], session: str = "", trigger: str = "agent",
                  consumed: list[str] | None = None) -> dict:
    """Write or update pages [{path, front, body}] through the Gate; `consumed` lists raw records used up."""
    from recall.store import Meta, PageChange
    if not isinstance(pages, list):
        raise ValueError("pages: must be a list of {path, front, body}")
    changes = []
    for p in pages:
        if not isinstance(p, dict) or not isinstance(p.get("path"), str) or not isinstance(p.get("front", {}), dict):
            raise ValueError("pages: each item needs path (str), front (dict) and body (str)")
        changes.append(PageChange(p["path"], dict(p.get("front") or {}), str(p.get("body") or "")))
    raw_refs = {str(c).replace("\\", "/").strip() for c in (consumed or [])}
    for p in pages:
        for src in (p.get("front") or {}).get("sources") or []:
            raw_refs.add(str(src).replace("\\", "/").strip())
    unread = sorted(r for r in raw_refs if r.startswith("raw/") and r not in _READ_RAW)
    if unread:
        raise ValueError(f"unread raw records: {', '.join(unread)}. Open them with recall_get first.")
    store = _open().store
    res = store.ingest(changes, Meta("ingest", str(session or ""), str(trigger or "agent")),
                       consumed=[str(c) for c in (consumed or [])])
    # No rebuild here: the next search sees the changed page fingerprint.
    return {"accepted": res.accepted, "rejected": [list(r) for r in res.rejected],
            "redirected": [list(r) for r in res.redirected], "commit": res.commit}


@_tool
def recall_mark(path: str, kind: str) -> dict:
    """Mark a page "used" (it helped) or "wrong" (it was wrong). Returns its usage counters."""
    rt = _open()
    store, idx = rt.store, rt.local  # usage counters are not in the fingerprint: no rebuild
    rel = _check_page(path)
    if kind not in ("used", "wrong"):
        raise ValueError('kind: must be "used" or "wrong"')
    if not (store.root / rel).is_file():
        raise ValueError(f"not found: {rel}")
    idx.mark(rel, kind)
    return {"path": rel, **idx.usage(rel)}


@_tool
def recall_lint(semantic: bool = False) -> dict:
    """Run the mechanical lint. With semantic=True also return the semantic lint instructions."""
    from recall import maintenance
    rt = _open()
    store = rt.store
    findings = maintenance.lint_mechanical(store, rt.local, date.today(), scan=rt.pages)
    out = {"findings": [{"path": f.path, "rule": f.rule, "detail": f.detail} for f in findings]}
    if semantic:
        out["semantic_prompt"] = maintenance.semantic_prompt(store)
        # The request is now handled; it ends with an eval (see the prompt).
        maintenance.mark_review_done(rt.state, time.time())
    return out


@_tool
def recall_eval(answers: dict[str, str] | None = None, session: str = "") -> dict:
    """Without answers: return the golden questions. With {question: answer}: score and record it
    (at most once in 7 days, unless a semantic lint is due or was just run)."""
    from recall import evaluation, maintenance
    rt = _open()
    store = rt.store
    qs = evaluation.load_questions(store)
    if not answers:
        return {"questions": [q for q, _ in qs]}
    if not isinstance(answers, dict):
        raise ValueError("answers: must be {question: answer}")
    if not qs:
        raise ValueError("eval.md has no golden questions")
    if not maintenance.eval_due(store, rt.state, time.time()):
        return {"error": "eval not due"}
    score = evaluation.run(store, lambda q: str(answers.get(q, "")))
    sha = evaluation.record(store, score, str(session or ""), rt.state)
    return {"score": round(score, 4), "total": len(qs), "commit": sha}


@_tool
def recall_log(op: str | None = None, session: str | None = None, since: str | None = None,
               limit: int = 20) -> list[dict]:
    """Store commits, newest first, filtered by op, session and since (git date)."""
    store = _open().store
    if since is not None and (str(since).startswith("-") or "\n" in str(since)):
        raise ValueError("bad since")
    return [_entry(e) for e in store.history.log(op=op, session=session, since=since,
                                                 limit=max(1, min(int(limit), 200)))]


@_tool
def recall_history(page: str) -> list[dict]:
    """Commits that touched one page, newest first."""
    store = _open().store
    return [_entry(e) for e in store.history.log(page=_check_page(page), limit=50)]


@_tool
def recall_show_at(ref: str, page: str | None = None) -> dict:
    """A file (default index.md) as it was at ref (sha, session:<id>, repo:<sha>, before:<op>#<n>,
    best-eval or a date)."""
    store = _open().store
    rel = _check_rel(page) if page else "index.md"
    return {"ref": ref, "sha": store.history.resolve(ref), "path": rel, "text": store.history.show(ref, rel)}


@_tool
def recall_diff(ref_a: str, ref_b: str = "HEAD", page: str | None = None) -> dict:
    """Diff of the store (or one file) between two refs."""
    store = _open().store
    rel = _check_rel(page) if page else None
    return {"a": ref_a, "b": ref_b, "diff": store.history.diff(ref_a, ref_b, rel)}


@_tool
def recall_enable() -> dict:
    """Create the Recall store in this repo (Devin plugin opt-in). Idempotent; also excludes
    `.devin/recall/` via .git/info/exclude. Refuses if that path is tracked by git."""
    from recall.install import enable
    return enable(_repo_root())


@_tool
def recall_revert(ref: str, page: str | None = None, session: str = "") -> dict:
    """Undo a commit (or one page of it) as a new commit."""
    from recall.store import Meta
    store = _open().store
    rel = _check_page(page) if page else None
    sha = store.revert(ref, Meta("revert", str(session or ""), "agent"), page=rel)
    return {"commit": sha}
