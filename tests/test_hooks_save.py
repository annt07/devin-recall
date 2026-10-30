import json
import time

import pytest
from fixtures_sessions import make_db
from helpers import page

import recall.hooks as hooks
from recall.hooks import run
from recall.store import Meta


@pytest.fixture(autouse=True)
def _no_real_devin(tmp_path, monkeypatch):
    # Never let a test reach the real Devin sessions.db.
    monkeypatch.setenv("RECALL_DEVIN_DIR", str(tmp_path / "devin-home"))


def test_compaction_stop_and_drain(store):
    R = str(store.repo_root)
    run("SessionStart", json.dumps({"session_id": "s1"}), R)
    run("PostCompaction", json.dumps({"session_id": "s1", "summary": "We chose uv."}), R)
    [raw] = store.pending_raw()
    stop = json.dumps({"session_id": "s1", "last_assistant_message": "done"})
    out = json.loads(run("Stop", stop, R).stdout)
    assert out["decision"] == "block" and "recall_ingest" in out["reason"]
    assert run("Stop", json.dumps({"session_id": "s1", "stop_hook_active": True}), R).stdout == ""
    store.ingest([page(sources=(raw,))], Meta("ingest", "s1"), consumed=store.pending_raw())
    assert store.pending_raw() == [] and run("Stop", stop, R).stdout == ""
    assert store.history.log(op="capture")


def test_prompt_after_compaction_reinjects_index(store):
    R = str(store.repo_root)
    run("PostCompaction", json.dumps({"session_id": "s1", "summary": "x"}), R)
    out = run("UserPromptSubmit", json.dumps({"session_id": "s1", "prompt": "hi"}), R)
    text = json.loads(out.stdout)["hookSpecificOutput"]["additionalContext"]
    assert "# Recall index" in text and "Pending raw records: 1." in text


def test_pending_raw_cited_or_consumed(store):
    a = store.add_raw("s1", "note", "alpha")
    b = store.add_raw("s1", "note", "beta")
    assert store.add_raw("s1", "note", "alpha") is None and store.add_raw("s1", "note", "  ") is None
    assert store.pending_raw() == sorted([a, b])
    store.ingest([page(sources=(a,))], Meta("ingest", "s1"))
    assert store.pending_raw() == [b]
    store.ingest([], Meta("ingest", "s1"), consumed=[b])
    assert store.pending_raw() == []


def test_capture_commit_skips_when_locked_and_when_nothing_new(store):
    assert store.capture_commit(Meta("capture", "s1", "x")) is None
    store.add_raw("s1", "note", "gamma")
    with store.lock("other"):
        assert store.capture_commit(Meta("capture", "s1", "x")) is None
    assert store.capture_commit(Meta("capture", "s1", "x"))
    assert store.capture_commit(Meta("capture", "s1", "x")) is None


def test_stop_clean_young_session_does_not_block(store):
    R = str(store.repo_root)
    run("SessionStart", json.dumps({"session_id": "s1"}), R)
    assert run("Stop", json.dumps({"session_id": "s1", "last_assistant_message": "ok"}), R).stdout == ""


def test_stop_old_session_blocks_once_per_900s(store, monkeypatch):
    R = str(store.repo_root)
    now = [1_000_000.0]
    monkeypatch.setattr(hooks.time, "time", lambda: now[0])
    run("SessionStart", json.dumps({"session_id": "s1"}), R)
    now[0] += 601
    stop = json.dumps({"session_id": "s1", "last_assistant_message": "final answer"})
    assert json.loads(run("Stop", stop, R).stdout)["decision"] == "block"
    assert any("-final-" in p for p in store.pending_raw())
    now[0] += 899
    assert run("Stop", stop, R).stdout == ""
    now[0] += 2
    assert json.loads(run("Stop", stop, R).stdout)["decision"] == "block"


def test_stop_dirty_repo_blocks_but_store_does_not_count(store):
    R = str(store.repo_root)
    run("SessionStart", json.dumps({"session_id": "s1"}), R)
    # .devin/recall itself is untracked in the project repo: must not count as dirty.
    assert run("Stop", json.dumps({"session_id": "s1"}), R).stdout == ""
    (store.repo_root / "new.py").write_text("x = 1\n", encoding="utf-8")
    assert json.loads(run("Stop", json.dumps({"session_id": "s2"}), R).stdout)["decision"] == "block"


def test_hook_errors_never_raise(store, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("boom")
    monkeypatch.setattr(type(store), "pending_raw", boom)
    r = run("Stop", json.dumps({"session_id": "s1"}), str(store.repo_root))
    assert r.stdout == "" and r.exit_code == 0


def test_precompact_writes_probe(store, tmp_path):
    r = run("PreCompact", json.dumps({"session_id": "s1", "x": 1}), str(store.repo_root))
    assert r.stdout == ""
    from recall.paths import local_dir
    assert json.loads((local_dir(store.repo_root) / "probe-precompact.json").read_text(encoding="utf-8"))["x"] == 1


def test_session_end_captures_records(store, tmp_path, monkeypatch):
    home = tmp_path / "devin-home"
    (home / "devin" / "cli").mkdir(parents=True)
    ms = int(time.time() * 1000)
    make_db(home / "devin" / "cli" / "sessions.db", [{
        "id": "devin-1", "cwd": str(store.repo_root), "created": ms,
        "messages": [("user", "please add tests"), ("assistant", "Added the tests.")]}])
    run("SessionEnd", json.dumps({"session_id": "hook-sid"}), str(store.repo_root))
    entries = store.history.log(op="capture")
    assert entries and entries[0].trailers.get("Recall-Trigger") == "SessionEnd"
    kinds = {p.split("-")[-2] for p in store.pending_raw()}
    assert "final" in kinds
