"""Maintenance interface: due checks are pure reads; the 7-day clock starts at the store's first commit."""
import time

import pytest

from recall import maintenance
from recall.localindex import LocalIndex
from recall.session_state import SessionState

DAY = 86400


@pytest.fixture
def counted(store, monkeypatch):
    """A SessionState whose LocalIndex counts every state write."""
    idx = LocalIndex(store.repo_root)
    writes = []
    real_set, real_del = idx.set_state, idx.delete_state
    monkeypatch.setattr(idx, "set_state", lambda k, v: (writes.append(k), real_set(k, v))[1])
    monkeypatch.setattr(idx, "delete_state", lambda ks: (writes.append(tuple(ks)), real_del(ks))[1])
    return SessionState(idx), writes


def test_review_due_new_store_false_and_no_write(store, counted):
    state, writes = counted
    assert maintenance.review_due(store, state, time.time()) is False
    assert writes == []
    assert state.last_review() is None


def test_review_due_clock_starts_at_first_commit(store, counted):
    state, writes = counted
    first = store.history.first_commit_time()
    assert first is not None and abs(first - time.time()) < 600
    assert maintenance.review_due(store, state, first + 6 * DAY) is False
    assert maintenance.review_due(store, state, first + 8 * DAY) is True
    assert writes == []


def test_review_due_empty_history_false(tmp_path):
    from recall.history import History
    h = History(tmp_path)
    h.init()
    assert h.first_commit_time() is None


def test_mark_review_done_resets_clock(store, counted):
    state, _ = counted
    now = time.time() + 8 * DAY
    state.set_lint_due(True)
    assert maintenance.review_due(store, state, now) is True
    maintenance.mark_review_done(state, now)
    assert maintenance.review_due(store, state, now) is False
    assert state.lint_due() is False and state.eval_due() is True and state.last_review() == now


def test_eval_due_pure_and_mark_eval_done(store, counted):
    state, writes = counted
    now = time.time()
    assert maintenance.eval_due(store, state, now) is True  # no eval commit yet
    state.set_eval_due(True)
    writes.clear()
    assert maintenance.eval_due(store, state, now) is True
    assert writes == []
    maintenance.mark_eval_done(state, now)
    assert state.eval_due() is False and state.lint_due() is False and state.last_review() == now


def test_review_section_sets_eval_due_only_when_shown(store, monkeypatch):
    from recall.runtime import Runtime
    rt = Runtime.open(store.repo_root)
    state, writes, real_set = rt.state, [], rt.local.set_state
    monkeypatch.setattr(rt.local, "set_state", lambda k, v: (writes.append(k), real_set(k, v))[1])
    assert maintenance.review_section(rt, {}) == ""
    assert writes == []
    state.set_lint_due(True)
    assert "recall_lint(semantic=True)" in maintenance.review_section(rt, {})
    assert state.eval_due() is True
