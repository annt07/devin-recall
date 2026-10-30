"""Local index contention: WAL lets readers through, and a hook never waits on a busy index."""
import sqlite3
import time
from pathlib import Path


def _hold_write(db: Path):
    """A second connection holding a write transaction on `db`. Close+rollback to release."""
    c = sqlite3.connect(str(db), timeout=5)
    c.execute("BEGIN IMMEDIATE")
    c.execute("INSERT OR REPLACE INTO state(key, value) VALUES ('lock-test', '1')")
    return c


def test_wal_reader_not_blocked_by_writer(repo, store):
    from recall.localindex import LocalIndex
    idx = LocalIndex(repo)
    held = _hold_write(idx.db)
    try:
        # A reader through a fresh connection still answers while the writer holds its txn.
        assert idx.is_empty() in (True, False)
        assert idx.search("anything") == []
    finally:
        held.rollback()
        held.close()


def test_wal_mode_on_existing_db(repo, store):
    from recall.localindex import LocalIndex
    idx = LocalIndex(repo)
    held = _hold_write(idx.db)
    held.rollback()
    held.close()
    c = sqlite3.connect(str(idx.db))
    try:
        mode = c.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal"
    finally:
        c.close()


def test_hook_returns_empty_fast_on_locked_index(repo, store):
    from recall.hooks import run
    from recall.localindex import LocalIndex
    idx = LocalIndex(repo)
    held = _hold_write(idx.db)
    try:
        t0 = time.perf_counter()
        res = run("UserPromptSubmit", '{"session_id": "lock-1", "prompt": "hello"}', str(repo))
        dt = time.perf_counter() - t0
    finally:
        held.rollback()
        held.close()
    assert res.stdout == "" and res.exit_code == 0
    assert dt < 1.0, f"hook waited {dt:.2f}s on a busy index"
    log = idx.dir / "errors.log"
    assert log.is_file() and "database is locked" in log.read_text(encoding="utf-8")


def test_user_prompt_budget_with_locked_index(repo, store):
    """The regression guard for the 5s hook timeout: fail fast, stay in the hook budget."""
    from recall.hooks import run
    from recall.localindex import LocalIndex
    idx = LocalIndex(repo)
    held = _hold_write(idx.db)
    try:
        t0 = time.perf_counter()
        res = run("UserPromptSubmit", '{"session_id": "lock-2", "prompt": "hello"}', str(repo))
        dt = time.perf_counter() - t0
    finally:
        held.rollback()
        held.close()
    assert dt < 1.5, f"UserPromptSubmit took {dt:.2f}s with a locked index"
    assert res.exit_code == 0
