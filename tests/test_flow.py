"""Flow fixes: Stop reads before writes (ingest query last), start time after the SessionStart
sections, transcript cache by (size, mtime_ns), one index rebuild per SessionStart."""
import json
import os
import subprocess
import time

import pytest
from helpers import page

import recall.hooks as hooks
from recall.history import History
from recall.hooks import run
from recall.localindex import LocalIndex
from recall.runtime import Runtime
from recall.session_state import SessionState
from recall.store import Meta, Store

P = "pages/fact/build-and-test.md"


@pytest.fixture(autouse=True)
def _no_real_devin(tmp_path, monkeypatch):
    monkeypatch.setenv("RECALL_DEVIN_DIR", str(tmp_path / "devin-home"))
    monkeypatch.setenv("RECALL_DEVIN_CONFIG", str(tmp_path / "devin-config.json"))
    for k in ("RECALL_CONTEXT_LIMIT", "RECALL_NEAR_RATIO", "RECALL_OVER_RATIO"):
        monkeypatch.delenv(k, raising=False)


@pytest.fixture
def git_calls(monkeypatch):
    c = [0]
    orig = History._run

    def _run(self, *a, **k):
        c[0] += 1
        return orig(self, *a, **k)
    monkeypatch.setattr(History, "_run", _run)
    return c


def _state(store) -> SessionState:
    return SessionState(LocalIndex(store.repo_root))


# ---------- Stop ----------
def test_stop_hook_active_starts_no_git(store, git_calls):
    git_calls[0] = 0
    out = run("Stop", json.dumps({"session_id": "s1", "stop_hook_active": True}), str(store.repo_root))
    assert out.stdout == "" and git_calls[0] == 0


def test_stop_in_cooldown_starts_no_git(store, git_calls):
    _state(store).set_stop_block("s1", time.time() - 10)
    git_calls[0] = 0
    out = run("Stop", json.dumps({"session_id": "s1"}), str(store.repo_root))
    assert out.stdout == "" and git_calls[0] == 0


def _git_command(argv) -> str:
    """The git subcommand of a `git [-c k=v | -C dir]... <command> ...` argv ('' for non-git)."""
    if not argv or argv[0] != "git":
        return ""
    i = 1
    while i < len(argv) and argv[i] in ("-c", "-C"):
        i += 2
    return argv[i] if i < len(argv) else ""


@pytest.fixture
def git_argv(monkeypatch):
    """Record the git subcommand of every subprocess.run in recall.history and recall.hooks."""
    import recall.history as history_mod
    cmds: list[str] = []
    real = subprocess.run

    def recording(argv, *a, **k):
        cmds.append(_git_command(list(argv)))
        return real(argv, *a, **k)
    for mod in (history_mod, hooks):
        monkeypatch.setattr(mod.subprocess, "run", recording)
    return cmds


def test_stop_ingest_query_is_last_and_no_write_without_block(store, git_argv, monkeypatch):
    R = str(store.repo_root)
    ss = _state(store)
    ss.set_start("s1", time.time() - 5)
    store.ingest([page()], Meta("ingest", "s1"))  # saved in this session
    (store.repo_root / "new.txt").write_text("x", encoding="utf-8")  # project changes
    adds = []
    monkeypatch.setattr(Store, "add_raw", lambda self, *a, **k: adds.append(a))
    git_argv.clear()
    out = run("Stop", json.dumps({"session_id": "s1", "last_assistant_message": "done"}), R)
    assert out.stdout == ""
    assert "status" in git_argv and "log" in git_argv
    assert git_argv.index("status") < git_argv.index("log")  # the ingest query comes last
    assert git_argv[-1] == "log"
    assert adds == [] and ss.stop_block("s1") is None


def test_stop_clean_young_session_makes_no_ingest_query(store, git_argv):
    _state(store).set_start("s1", time.time() - 5)
    git_argv.clear()
    assert run("Stop", json.dumps({"session_id": "s1"}), str(store.repo_root)).stdout == ""
    assert "status" in git_argv and "log" not in git_argv


def test_stop_without_start_counts_any_ingest(store):
    # No session id: no start and no stop_block (since == 0). An existing ingest is a save: no block.
    store.ingest([page()], Meta("ingest", "s1"))
    (store.repo_root / "new.txt").write_text("x", encoding="utf-8")
    out = run("Stop", json.dumps({"last_assistant_message": "x"}), str(store.repo_root))
    assert out.stdout == ""


def test_stop_without_start_and_no_ingest_blocks(store):
    (store.repo_root / "new.txt").write_text("x", encoding="utf-8")
    out = run("Stop", json.dumps({"last_assistant_message": "x"}), str(store.repo_root))
    assert json.loads(out.stdout)["decision"] == "block"


def test_stop_seed_commit_does_not_count_as_save(store):
    R = str(store.repo_root)
    _state(store).set_start("s1", time.time() - 1200)  # old session
    store.ingest([page()], Meta("seed", "s1"))
    assert store.history.log(op="seed", limit=1)
    out = json.loads(run("Stop", json.dumps({"session_id": "s1", "last_assistant_message": "x"}), R).stdout)
    assert out["decision"] == "block"
    assert _state(store).stop_block("s1") is not None


# ---------- SessionStart start time ----------
def test_start_written_after_sections(store, monkeypatch):
    now = [1_000_000.0]
    monkeypatch.setattr(hooks.time, "time", lambda: now[0])

    def slow(rt, payload):
        now[0] += 100
        return ""
    monkeypatch.setattr(hooks, "SESSION_START_SECTIONS", [slow])
    run("SessionStart", json.dumps({"session_id": "s1"}), str(store.repo_root))
    assert _state(store).start("s1") == 1_000_100.0


def test_session_start_without_session_id_writes_no_start(store):
    run("SessionStart", json.dumps({}), str(store.repo_root))
    assert LocalIndex(store.repo_root).state_keys("start:") == []


def test_first_stop_sets_provisional_start(store):
    before = time.time()
    run("Stop", json.dumps({"session_id": "s7"}), str(store.repo_root))
    t = _state(store).start("s7")
    assert t is not None and t >= before - 1


# ---------- transcript cache ----------
ATIF = {"schema_version": "ATIF-v1.7", "agent": {"model_name": "swe-1-6"},
        "steps": [{"source": "agent", "model_name": "swe-1-6", "metrics": {"prompt_tokens": 30_000}}]}


def _db(tmp_path, R):
    from test_context_gauge import make_db as gauge_db
    db = tmp_path / "devin-home" / "devin" / "cli" / "sessions.db"
    gauge_db(db, [{"id": "s1", "cwd": R, "model": "swe-1-6", "nodes": [(10, None)]}])
    t = db.parent / "transcripts" / "s1.json"
    t.parent.mkdir(parents=True, exist_ok=True)
    t.write_text(json.dumps(ATIF), "utf-8")
    return t


def test_transcript_parsed_once_while_unchanged(store, tmp_path, monkeypatch):
    import recall.session_reader as sr
    R = str(store.repo_root)
    t = _db(tmp_path, R)
    parses = [0]
    orig = sr.json.load

    def counting(f, *a, **k):
        if str(getattr(f, "name", "")).endswith("s1.json"):
            parses[0] += 1
        return orig(f, *a, **k)
    monkeypatch.setattr(sr.json, "load", counting)
    p = json.dumps({"session_id": "s1", "prompt": "hi"})
    run("UserPromptSubmit", p, R)
    run("UserPromptSubmit", p, R)
    assert parses[0] == 1
    # same size, new mtime -> parse again
    st = os.stat(t)
    os.utime(t, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    run("UserPromptSubmit", p, R)
    assert parses[0] == 2
    # new size -> parse again
    data = dict(ATIF, steps=[{"source": "agent", "model_name": "swe-1-6", "metrics": {"prompt_tokens": 31_000}}])
    t.write_text(json.dumps(data) + "   ", "utf-8")
    run("UserPromptSubmit", p, R)
    assert parses[0] == 3


def test_transcript_cache_returns_same_status(store, tmp_path):
    from recall.context_gauge import context_status
    from recall.session_reader import SessionReader
    R = str(store.repo_root)
    t = _db(tmp_path, R)
    idx = LocalIndex(store.repo_root)
    reader = SessionReader(t.parent.parent / "sessions.db")
    a = context_status(reader, "s1", {}, {}, {}, idx, cache_sid="s1")
    b = context_status(reader, "s1", {}, {}, {}, idx, cache_sid="s1")
    assert a == b and a["tokens"] == 30_000 and a["model_source"] == "transcript"
    assert SessionState(idx).transcript("s1") is not None


def test_user_prompt_stays_in_latency_budget(store, tmp_path, git_calls, monkeypatch):
    # Hook budget is 5 s; in-process a UserPromptSubmit is ~0.1 s (the one `git rev-parse` dominates).
    import subprocess
    procs = [0]
    orig = subprocess.Popen.__init__

    def counting(self, *a, **k):
        procs[0] += 1
        return orig(self, *a, **k)
    monkeypatch.setattr(subprocess.Popen, "__init__", counting)
    R = str(store.repo_root)
    _db(tmp_path, R)
    p = json.dumps({"session_id": "s1", "prompt": "hi"})
    run("UserPromptSubmit", p, R)
    procs[0] = git_calls[0] = 0
    t = time.perf_counter()
    run("UserPromptSubmit", p, R)
    assert time.perf_counter() - t < 1.0
    assert git_calls[0] == 0 and procs[0] <= 1  # only the hook's rev-parse --show-toplevel


# ---------- index rebuild count (E1, E2) ----------
@pytest.fixture
def rebuilds(monkeypatch):
    c = [0]
    orig = LocalIndex.rebuild

    def _rebuild(self, *a, **k):
        c[0] += 1
        return orig(self, *a, **k)
    monkeypatch.setattr(LocalIndex, "rebuild", _rebuild)
    return c


def test_session_start_lint_change_rebuilds_once(store, rebuilds):
    store.ingest([page()], Meta("ingest", "s1"))
    Runtime.open(store.repo_root).index  # noqa: B018 - saved fingerprint
    idx = LocalIndex(store.repo_root)
    idx.mark(P, "wrong")
    idx.mark(P, "wrong")
    rebuilds[0] = 0
    run("SessionStart", json.dumps({"session_id": "s2"}), str(store.repo_root))
    assert rebuilds[0] == 1
    assert Runtime.open(store.repo_root).index.search("pytest") == []
    assert rebuilds[0] == 1


def test_index_without_fingerprint_rebuilds_once(store, rebuilds):
    store.ingest([page()], Meta("ingest", "s1"))
    LocalIndex(store.repo_root).rebuild(store)
    assert not LocalIndex(store.repo_root).is_empty()
    assert _state(store).fingerprint() is None
    rebuilds[0] = 0
    assert Runtime.open(store.repo_root).index.search("pytest")
    assert rebuilds[0] == 1
    assert Runtime.open(store.repo_root).index.search("pytest")
    assert rebuilds[0] == 1
