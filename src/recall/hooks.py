"""Hook adapter: Devin starts `recall hook <event>`; this module turns stdin into context.

A hook never breaks Devin: `run` catches every exception, logs it to errors.log and exits 0.
Each call opens one Runtime (store, local index, session state, page scan).
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

from recall.context_gauge import prompt_section
from recall.maintenance import review_section
from recall.markers import MISMATCH_PREFIX, OFF_MSG, PENDING_MSG, START_HINT
from recall.markers import STOP_BLOCK_MSG as BLOCK_MSG
from recall.paths import log_error
from recall.runtime import HookResult, Runtime, run_hook, store_folder_correction
from recall.seed import session_start_section

MAX_PAGES = 3
MAX_CHARS = 10_000
INDEX_MAX_CHARS = 6_000
INDEX_TAIL = "... use recall_search for more"
STALE_WARN = "WARNING: stale, the cited code changed. Verify before use."
MISMATCH_MAX_PAGES = 10
# Ops whose commits record what the memory knows; their Recall-Repo-Commit dates the memory.
# (lint, cleanup, capture and eval commits made on an older checkout must not hide the mismatch.)
KNOWLEDGE_OPS = ("ingest", "revert", "seed")
TOPLEVEL_TIMEOUT_S = 2


def _context(event: str, text: str) -> HookResult:
    out = {"hookSpecificOutput": {"hookEventName": event, "additionalContext": text}}
    return HookResult(stdout=json.dumps(out, ensure_ascii=False))


def _toplevel(start) -> Path | None:
    try:
        p = subprocess.run(["git", "-C", str(start), "rev-parse", "--show-toplevel"],
                           capture_output=True, text=True, encoding="utf-8", timeout=TOPLEVEL_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError):
        return None
    out = (p.stdout or "").strip()
    return Path(out).resolve() if p.returncode == 0 and out else None


def _repo_root(repo: str | None) -> Path | None:
    root = _toplevel(repo or os.environ.get("DEVIN_PROJECT_DIR") or os.getcwd())
    if root is not None and (owner := store_folder_correction(root)) != root:
        # The store is its own git repo: resolve to the project that owns it.
        # If that folder is not itself a git toplevel, it is not a project repo.
        root = owner if _toplevel(owner) == owner else None
    return root


def _payload(stdin_text: str) -> dict:
    try:
        data = json.loads(stdin_text or "{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _index_text(store) -> str:
    """index.md for injection, capped at INDEX_MAX_CHARS (cut at a line end) plus a search hint."""
    p = store.root / "index.md"
    text = p.read_text(encoding="utf-8").strip() if p.exists() else ""
    if len(text) <= INDEX_MAX_CHARS:
        return text
    cut = text[:INDEX_MAX_CHARS]
    if "\n" in cut:
        cut = cut[:cut.rfind("\n")]
    return f"{cut.rstrip()}\n{INDEX_TAIL}"


def run(event: str, stdin_text: str, repo: str | None = None) -> HookResult:
    return run_hook(None, f"hook {event}", lambda: _resolved(event, stdin_text, repo))


def _resolved(event: str, stdin_text: str, repo: str | None) -> HookResult:
    root = _repo_root(repo)
    if root is None:
        return _context(event, OFF_MSG) if event == "SessionStart" else HookResult()
    return run_hook(root, f"hook {event}", lambda: _dispatch(event, stdin_text, root))


def _dispatch(event: str, stdin_text: str, root: Path) -> HookResult:
    from recall.store import LockBusy
    try:
        rt = Runtime.open(root)
    except LockBusy:
        return HookResult()  # another process holds the store; skip this event
    data = _payload(stdin_text)
    # PreToolUse runs before every tool call: no state write, keep the write guard fast.
    # SessionEnd removes the session keys, so it needs no provisional start.
    if event not in ("PreToolUse", "SessionEnd") and (sid := _session(data)):
        try:
            rt.begin_hook(sid, time.time())
        except Exception as e:  # the last-seen time must never stop the event
            log_error(root, e)
    handler = _HANDLERS.get(event)
    return handler(event, data, rt) if handler else HookResult()


def _session(data: dict) -> str:
    return str(data.get("session_id") or "")


def _session_start(event: str, data: dict, rt) -> HookResult:
    store, ss = rt.store, rt.state
    now = time.time()
    try:
        ss.cleanup_stale(now)  # sessions that never sent SessionEnd
    except Exception as e:  # cleanup must never hide the index
        log_error(store.repo_root, e)
    sections: list[str] = []
    # Sections run first: lint (and seed) can change page status, and the injected index and the
    # search index must show those changes in this same session.
    for section in SESSION_START_SECTIONS:  # each: (runtime, payload) -> str; "" means nothing
        try:
            text = section(rt, data)
        except Exception as e:  # one broken section never hides the index
            log_error(store.repo_root, e)
            continue
        if text:
            sections.append(text)
    # After the sections, so Seed and lint time is not session time (a provisional start exists).
    if sid := _session(data):
        ss.set_start(sid, time.time())
    rt.index  # noqa: B018 - make the search index match the pages (rebuilds only on change)
    return _context(event, "\n\n".join([f"{_index_text(store)}\n\n{START_HINT}", *sections]))


SESSION_START_SECTIONS: list = []
PROMPT_SECTIONS: list = []  # (runtime, payload) -> str; UserPromptSubmit appends non-empty results


def _user_prompt(event: str, data: dict, rt) -> HookResult:
    sid = _session(data)
    store, ss = rt.store, rt.state
    parts: list[str] = []
    if ss.post_compact(sid):
        parts.append(_index_text(store))
        ss.clear_post_compact(sid)
    used = sum(len(p) for p in parts)
    added = 0
    idx = rt.index  # a page edited without a commit is found too
    for hit in idx.search(str(data.get("prompt") or ""), k=10):
        if added >= MAX_PAGES:
            break
        try:
            front, body = store.read_page(hit["path"])
        except OSError:
            continue
        status = front.get("status", "active")
        if status == "retired":
            continue
        block = f"### {front.get('title') or hit['title']} ({hit['path']})\n"
        if status == "stale":
            block += STALE_WARN + "\n"
        block += body
        if used + len(block) > MAX_CHARS:
            continue
        if not idx.record_injected(sid, hit["path"]):
            continue
        parts.append(block)
        used += len(block)
        added += 1
    try:
        pending = len(store.pending_raw(scan=rt.pages))
    except Exception as e:  # never lose the injection over a counter
        log_error(store.repo_root, e)
        pending = 0
    if pending:
        parts.append(PENDING_MSG.format(n=pending))
    for section in PROMPT_SECTIONS:
        try:
            text = section(rt, data)
        except Exception as e:  # one section must never lose the others
            log_error(store.repo_root, e)
            text = ""
        if text:
            parts.append(text)
    if not parts:
        return HookResult()
    return _context(event, "\n\n".join(parts))


# ---------- save hooks ----------
BLOCK_COOLDOWN_S = 900
OLD_SESSION_S = 600


def _repo_dirty(root: Path) -> bool:
    try:
        p = subprocess.run(["git", "-C", str(root), "status", "--porcelain", "--untracked-files=all",
                            "--", ".", ":(exclude).devin/recall"],
                           capture_output=True, text=True, encoding="utf-8", timeout=15)
    except (OSError, subprocess.SubprocessError):
        return False
    return p.returncode == 0 and bool((p.stdout or "").strip())


def _post_compaction(event: str, data: dict, rt) -> HookResult:
    from recall.store import Meta
    sid, store = _session(data), rt.store
    store.add_raw(sid, "compact", str(data.get("summary") or ""))
    rt.state.set_post_compact(sid, time.time())
    store.capture_commit(Meta("capture", sid, event))
    return HookResult()


def _stop(event: str, data: dict, rt) -> HookResult:
    """Reads, cheapest first, all before any write: stop_hook_active, cooldown, pending raw count,
    session age, project changes, and last the git log query for an ingest. Only an ingest counts
    as a save (seed and revert do not). The final record and block time are written only on a block."""
    if data.get("stop_hook_active"):
        return HookResult()  # Devin is already continuing because of a block: never loop
    from datetime import datetime
    sid, store, ss = _session(data), rt.store, rt.state
    now = time.time()
    start = ss.start(sid) or 0.0
    last_block = ss.stop_block(sid) or 0.0
    if last_block and now - last_block < BLOCK_COOLDOWN_S:
        return HookResult()
    pending = store.pending_raw(scan=rt.pages)
    old = bool(start) and now - start > OLD_SESSION_S
    if not (pending or old or _repo_dirty(store.repo_root)):
        return HookResult()
    since = max(start, last_block)
    if since:
        iso = datetime.fromtimestamp(int(since)).astimezone().isoformat()
        saved = store.history.log(op="ingest", since=iso, limit=1)
    else:  # no start and no block time (no session id): any ingest counts as the save
        saved = store.history.log(op="ingest", limit=1)
    if saved:
        return HookResult()  # the agent already saved in this session (or since the last block)
    final = store.add_raw(sid, "final", str(data.get("last_assistant_message") or ""))
    if final and final not in pending:
        pending.append(final)
    ss.set_stop_block(sid, now)
    return HookResult(stdout=json.dumps({"decision": "block", "reason": BLOCK_MSG.format(n=len(pending))}))


def _pre_compact(event: str, data: dict, rt) -> HookResult:
    from recall.paths import local_dir, redact
    d = local_dir(rt.store.repo_root)
    d.mkdir(parents=True, exist_ok=True)
    with open(d / "probe-precompact.json", "w", encoding="utf-8", newline="\n") as f:
        f.write(redact(json.dumps(data, ensure_ascii=False, indent=2)) + "\n")
    model, sid = data.get("model"), _session(data)
    if isinstance(model, str) and model.strip() and sid:
        rt.state.set_model(sid, model)  # Context Gauge reads it
    return HookResult()


def _session_end(event: str, data: dict, rt) -> HookResult:
    from recall.store import Meta
    sid, store = _session(data), rt.store
    try:
        from recall.session_reader import SessionReader
        reader = SessionReader()
        devin_sid = reader.resolve_session(sid, store.repo_root)
        for kind, text in (reader.records(devin_sid) if devin_sid else []):
            store.add_raw(sid, kind, text)
    except Exception as e:  # reading Devin data must never stop the capture
        log_error(store.repo_root, e)
    store.capture_commit(Meta("capture", sid, "SessionEnd"))
    if sid:
        rt.state.end_session(sid)
    return HookResult()


def _pre_tool_use(event: str, data: dict, rt) -> HookResult:
    from recall.guard import decide
    reason = decide(rt.store.root, rt.store.repo_root, data)
    return HookResult(exit_code=2, stderr=reason) if reason else HookResult()


# ---------- sections: (runtime, payload) -> str ----------
def _lint_section(rt, payload) -> str:
    from datetime import date

    from recall.maintenance import format_findings, lint_mechanical
    # rebuild=False: rt.index after the sections sees the changed fingerprint and rebuilds once.
    return format_findings(lint_mechanical(rt.store, rt.local, date.today(), scan=rt.pages, rebuild=False))


def _mismatch_section(rt, payload) -> str:
    """Code-mismatch note: the memory was written on code that the checked-out HEAD does not contain."""
    store = rt.store
    sha = next((e.trailers.get("Recall-Repo-Commit", "") for e in store.history.log(limit=50)
                if e.trailers.get("Recall-Op") in KNOWLEDGE_OPS), "")
    if not sha or sha == "-" or store.history.repo_is_ancestor(sha) is not False:
        return ""  # no memory commit, commit unknown to this repo, or HEAD contains it
    stale = [rel for rel, front, _ in rt.pages() if front.get("status") == "stale"]
    listed = ", ".join(stale[:MISMATCH_MAX_PAGES]) or "(none marked stale yet)"
    more = f" (+{len(stale) - MISMATCH_MAX_PAGES} more)" if len(stale) > MISMATCH_MAX_PAGES else ""
    return f"{MISMATCH_PREFIX} {listed}{more}"


def _pending_section(rt, payload) -> str:
    n = len(rt.store.pending_raw(scan=rt.pages))
    return PENDING_MSG.format(n=n) if n else ""


_HANDLERS = {"SessionStart": _session_start, "UserPromptSubmit": _user_prompt,
             "PostCompaction": _post_compaction, "Stop": _stop, "PreCompact": _pre_compact,
             "SessionEnd": _session_end, "PreToolUse": _pre_tool_use}
# Order matters: lint (with the raw cleanup) and seed change pages and raw records first, so the
# mismatch note and the pending count describe the state after them.
SESSION_START_SECTIONS.extend([review_section, _lint_section, session_start_section, _mismatch_section,
                               _pending_section])
PROMPT_SECTIONS.append(prompt_section)
