"""Session-scope state through hooks.run: SessionEnd cleanup, stale cleanup at SessionStart, touch."""
import json
import time

import pytest

from recall.hooks import run
from recall.localindex import LocalIndex
from recall.session_state import SESSION_PREFIXES, SessionState


@pytest.fixture(autouse=True)
def _no_real_devin(tmp_path, monkeypatch):
    monkeypatch.setenv("RECALL_DEVIN_DIR", str(tmp_path / "devin-home"))


def _keys(idx, sid):
    return sorted(k for p in SESSION_PREFIXES for k in idx.state_keys(p) if k == p + sid)


def _fill(idx, sid, t):
    ss = SessionState(idx)
    ss.set_start(sid, t)
    ss.set_stop_block(sid, t)
    ss.set_post_compact(sid, t)
    ss.set_model(sid, "m")
    ss.set_gauge_tokens(sid, 10, t)
    ss.set_transcript(sid, 1, 2, ["m", 3])
    ss.touch(sid, t)
    assert idx.record_injected(sid, "pages/a.md")


def test_session_end_removes_only_that_session(store):
    R = str(store.repo_root)
    idx = LocalIndex(store.repo_root)
    now = time.time()
    _fill(idx, "A", now)
    _fill(idx, "B", now)
    run("SessionEnd", json.dumps({"session_id": "A"}), R)
    assert _keys(idx, "A") == []
    assert idx.record_injected("A", "pages/a.md")  # A's injected row was gone
    assert len(_keys(idx, "B")) == len(SESSION_PREFIXES)
    assert not idx.record_injected("B", "pages/a.md")  # B's injected row stays


def test_session_start_removes_stale_session(store):
    R = str(store.repo_root)
    idx = LocalIndex(store.repo_root)
    now = time.time()
    _fill(idx, "old", now - 8 * 86400)
    _fill(idx, "recent", now - 86400)
    SessionState(idx).set_observed_limit("fusion", 5, now - 8 * 86400)
    run("SessionStart", json.dumps({"session_id": "new"}), R)
    assert _keys(idx, "old") == []
    assert len(_keys(idx, "recent")) == len(SESSION_PREFIXES)
    assert SessionState(idx).observed_limit("fusion") == (5, now - 8 * 86400)
    assert SessionState(idx).start("new") is not None


def test_each_hook_call_touches_the_session(store):
    R = str(store.repo_root)
    idx = LocalIndex(store.repo_root)
    before = time.time()
    run("UserPromptSubmit", json.dumps({"session_id": "s1", "prompt": "hi"}), R)
    assert idx.get_state("seen:s1") is not None
    assert float(idx.get_state("seen:s1")) >= before - 1


def test_pre_tool_use_does_not_touch_the_session(store):
    """The write guard runs before every tool call: it must stay fast and write no state."""
    R = str(store.repo_root)
    idx = LocalIndex(store.repo_root)
    payload = {"session_id": "s1", "tool_name": "read", "tool_input": {"file_path": "x"}}
    run("PreToolUse", json.dumps(payload), R)
    assert SessionState(idx).start("s1") is None
    assert idx.get_state("seen:s1") is None
