"""Seed: import old Devin sessions of this repo as backdated raw records, a few per SessionStart.

State lives in ``<store>/seed.json``: ``{"seen": [], "not_read": [], "complete": false, "started": <ts>}``.
``seen`` are imported session ids, ``not_read`` are repo sessions older than the seed window.
Only sessions whose last activity is before ``started`` (the first seed run) are candidates:
newer sessions are captured live by SessionEnd.
"""
from __future__ import annotations

import json
import re
import time

from recall.history import GitError
from recall.paths import log_error
from recall.session_state import SessionState
from recall.store import LockBusy, Meta, raw_session_tag

SEED_DAYS = 90
BATCH = 3
SEED_LINE = "Seed: {n} sessions imported, {left} remaining"


def _load(store) -> dict:
    state: dict = {}
    if (store.root / "seed.json").exists():
        try:
            state = json.loads(store.read_text("seed.json"))
        except ValueError:
            state = {}
    if not isinstance(state, dict):
        state = {}
    state["seen"] = [str(x) for x in state.get("seen") or []]
    state["not_read"] = [str(x) for x in state.get("not_read") or []]
    state["complete"] = bool(state.get("complete"))
    return state


def _save(store, state: dict) -> None:
    store.write_text("seed.json", json.dumps(state, indent=2, ensure_ascii=False) + "\n")


def seed_batch(store, reader, index, current_session: str, batch: int = BATCH, wait: float = 1.0) -> dict:
    """Import up to `batch` unseen old sessions. Returns {imported, remaining, complete, busy}."""
    result = {"imported": 0, "remaining": 0, "complete": False, "busy": False}
    if not reader.db_path.is_file():
        return result  # no Devin data (yet): do nothing, try again next session
    state = _load(store)
    if state["complete"]:
        result["complete"] = True
        return result
    started = float(state.get("started") or time.time())
    exclude = {str(current_session or "")}
    recent = [s for s in reader.sessions_for(store.repo_root, SEED_DAYS)
              if s.id not in exclude and s.last_activity.timestamp() < started]
    recent_ids = {s.id for s in recent}
    older = [s.id for s in reader.sessions_for(store.repo_root, None)
             if s.id not in recent_ids and s.id not in exclude and s.last_activity.timestamp() < started]
    todo = [s for s in recent if s.id not in set(state["seen"])]  # newest first
    try:
        with store.lock(f"seed:{current_session}", wait=wait):
            state = _load(store)  # re-read under the lock: another session may have seeded meanwhile
            state["started"] = state.get("started") or started
            state["not_read"] = sorted(set(state["not_read"]) | set(older))
            todo = [s for s in todo if s.id not in set(state["seen"])]
            # add_raw dedups, so raw files of an earlier failed attempt are not returned as new; they are
            # still uncommitted and belong to their session's commit.
            leftover = store.uncommitted_raw() if todo[:batch] else []
            for s in todo[:batch]:
                new = {r for kind, text in reader.records(s.id)
                       if (r := store.add_raw(s.id, kind, text, when=s.last_activity))}
                mine = re.compile(rf"raw/\d{{4}}-\d{{2}}-\d{{2}}-{re.escape(raw_session_tag(s.id))}"
                                  r"-[a-z0-9]+-[0-9a-f]{8}\.md")
                new |= {r for r in leftover if mine.fullmatch(r)}
                new = sorted(new)
                state["seen"].append(s.id)
                _save(store, state)
                log_before = store.read_text("log.md") if (store.root / "log.md").exists() else ""
                store.append_log("seed", s.title, s.id, when=s.last_activity.astimezone())
                try:
                    store.history.commit(Meta("seed", s.id, "Seed"), f"seed: {s.id}", sources=new,
                                         when=s.last_activity, paths=[*new, "log.md", "seed.json"])
                except GitError as e:
                    # The log line and `seen` exist only with a commit: undo both, retry next time.
                    store.write_text("log.md", log_before)
                    state["seen"].remove(s.id)
                    _save(store, state)
                    log_error(store.repo_root, f"seed {s.id}: {e}")
                    break
                result["imported"] += 1
            left = [s for s in todo if s.id not in set(state["seen"])]
            result["remaining"] = len(left)
            if not left:
                state["complete"] = True
                _save(store, state)
                SessionState(index).set_lint_due(True)
            result["complete"] = state["complete"]
    except LockBusy:
        result["busy"] = True
        result["imported"] = 0
    return result


def session_start_section(rt, payload: dict) -> str:
    """SessionStart text for the seed (`rt`: the Runtime): the Seed line. (The pending-raw request
    is its own section.)"""
    from recall.session_reader import SessionReader
    res = seed_batch(rt.store, SessionReader(), rt.local, str(payload.get("session_id") or ""))
    if not res["imported"]:
        return ""
    return SEED_LINE.format(n=res["imported"], left=res["remaining"])
