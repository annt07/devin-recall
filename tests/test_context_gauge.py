import hashlib
import json
import sqlite3

import pytest

from recall.hooks import run


@pytest.fixture(autouse=True)
def _no_real_devin(tmp_path, monkeypatch):
    # Never let a test reach the real Devin data or user config.
    monkeypatch.setenv("RECALL_DEVIN_DIR", str(tmp_path / "devin-home"))
    monkeypatch.setenv("RECALL_DEVIN_CONFIG", str(tmp_path / "devin-config.json"))
    monkeypatch.setenv("RECALL_LOCAL_DIR", str(tmp_path / "local"))
    for k in ("RECALL_CONTEXT_LIMIT", "RECALL_NEAR_RATIO", "RECALL_OVER_RATIO"):
        monkeypatch.delenv(k, raising=False)


def make_db(path, sessions):
    """sessions: {"id", "cwd", "model", "nodes": [(num_tokens, summarized_from_or_None)]}."""
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE sessions(id TEXT PRIMARY KEY, working_directory TEXT, created_at INTEGER,"
                " last_activity_at INTEGER, title TEXT, workspace_dirs TEXT, model TEXT)")
    con.execute("CREATE TABLE message_nodes(row_id INTEGER PRIMARY KEY, session_id TEXT, node_id TEXT,"
                " parent_node_id TEXT, chat_message TEXT, created_at INTEGER, metadata TEXT)")
    for s in sessions:
        con.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?,?)",
                    (s["id"], s.get("cwd", "C:/x"), 1, 1, "", None, s.get("model", "")))
        for i, (tok, summ) in enumerate(s.get("nodes", [])):
            meta = {} if tok is None else {"num_tokens_preceding": tok}
            if summ:
                meta["summarized_from"] = summ
            con.execute("INSERT INTO message_nodes(session_id, node_id, chat_message, created_at, metadata)"
                        " VALUES (?,?,?,?,?)",
                        (s["id"], f"{s['id']}-{i}", json.dumps({"role": "user", "content": "x"}), i,
                         json.dumps(meta)))
    con.commit()
    con.close()
    return path


def set_model(path, sid, model):
    con = sqlite3.connect(path)
    con.execute("UPDATE sessions SET model = ? WHERE id = ?", (model, sid))
    con.commit()
    con.close()


def add_node(path, sid, tok):
    con = sqlite3.connect(path)
    con.execute("INSERT INTO message_nodes(session_id, node_id, chat_message, created_at, metadata)"
                " VALUES (?,?,?,?,?)", (sid, "n", "{}", 99, json.dumps({"num_tokens_preceding": tok})))
    con.commit()
    con.close()


def digest(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()


@pytest.fixture
def db(tmp_path):
    return tmp_path / "devin-home" / "devin" / "cli" / "sessions.db"


def status(db, sid="s1", env=None, cfg=None, reg=None):
    from recall.context_gauge import context_status
    from recall.session_reader import SessionReader
    return context_status(SessionReader(db), sid, env or {}, cfg or {},
                          reg if reg is not None else {"opus-5-5": 1_000_000, "swe-1-6": 200_000})


def test_source_order(db):
    make_db(db, [{"id": "s1", "model": "claude-opus-5-5-low", "nodes": [(1000, None), (60_000, None)]}])
    h = digest(db)
    s = status(db, env={"RECALL_CONTEXT_LIMIT": "100000"}, cfg={"agent": {"compaction_threshold_tokens": 50}})
    assert s == {"tokens": 60_000, "limit": 100_000, "ratio": 0.6, "source": "env",
                 "model_source": "sessions_db", "token_source": "sessions_db"}
    assert status(db, cfg={"agent": {"compaction_threshold_tokens": 120_000}})["source"] == "config"
    assert status(db, cfg={"agent.compaction_threshold_tokens": 120_000})["limit"] == 120_000
    s = status(db)
    assert (s["source"], s["limit"]) == ("registry", 750_000)
    s = status(db, reg={})
    assert (s["source"], s["limit"]) == ("default", 150_000)
    assert digest(db) == h


def test_observed_needs_three_samples_same_model(db):
    hist = [(100, None), (500_000, None), (2000, "a"), (400_000, None), (3000, "b"), (600_000, None),
            (1000, "c")]
    make_db(db, [{"id": "s1", "model": "claude-opus-5-5-high", "nodes": [(10_000, None)]},
                 {"id": "old", "model": "claude-opus-5-5-low", "nodes": hist},
                 {"id": "other", "model": "swe-1-6", "nodes": [(9, None), (8, "x")] * 3}])
    s = status(db)
    assert (s["source"], s["limit"]) == ("observed", 500_000)
    make_db(db.with_name("b.db"), [{"id": "s1", "model": "claude-opus-5-5-high", "nodes": [(10, None)]},
                                    {"id": "old", "model": "claude-opus-5-5-low", "nodes": hist[:5]}])
    assert status(db.with_name("b.db"))["source"] == "registry"


def test_registry_suffix_and_user_override(tmp_path):
    from recall.context_gauge import load_registry, lookup
    reg = load_registry()
    assert lookup(reg, "opus-5-5-high") == lookup(reg, "opus-5-5-low") == 1_000_000
    assert lookup(reg, "claude-sonnet-4-6") == 200_000 and lookup(reg, "nope") is None
    (tmp_path / "local").mkdir()
    (tmp_path / "local" / "models.json").write_text(json.dumps({"opus-5-5": 400_000, "new-m": 5}), "utf-8")
    reg = load_registry()
    assert lookup(reg, "claude-opus-5-5-xhigh") == 400_000 and lookup(reg, "new-m-max") == 5


def test_model_switch_changes_limit(db):
    make_db(db, [{"id": "s1", "model": "claude-opus-5-5-low", "nodes": [(300_000, None)]}])
    assert status(db)["limit"] == 750_000
    set_model(db, "s1", "swe-1-6")
    s = status(db)
    assert s["limit"] == 150_000 and s["ratio"] == 2.0


def test_devin_config_loading(tmp_path, monkeypatch):
    from recall.context_gauge import load_devin_config
    assert load_devin_config() == {}
    (tmp_path / "devin-config.json").write_text('{"agent": {"compaction_threshold_tokens": 7}}', "utf-8")
    assert load_devin_config()["agent"]["compaction_threshold_tokens"] == 7
    (tmp_path / "devin-config.json").write_text("not json", "utf-8")
    assert load_devin_config() == {}


def _ctx(out):
    return json.loads(out.stdout)["hookSpecificOutput"]["additionalContext"] if out.stdout else ""


def test_hook_requests_once_then_urgent(store, db, monkeypatch):
    monkeypatch.setenv("RECALL_CONTEXT_LIMIT", "100000")
    R = str(store.repo_root)
    make_db(db, [{"id": "s1", "cwd": R, "model": "swe-1-6", "nodes": [(90_000, None)]}])
    h = digest(db)
    p = json.dumps({"session_id": "s1", "prompt": "hi"})
    first = _ctx(run("UserPromptSubmit", p, R))
    assert "recall_ingest" in first and "URGENT" not in first
    assert "recall_ingest" not in _ctx(run("UserPromptSubmit", p, R))
    add_node(db, "s1", 120_000)
    assert "URGENT" in _ctx(run("UserPromptSubmit", p, R))
    h = digest(db)  # add_node (the test) changed it; the hook must not
    run("UserPromptSubmit", p, R)
    assert digest(db) == h


def write_transcript(db, sid, data):
    d = db.parent / "transcripts"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{sid}.json"
    p.write_text(data if isinstance(data, str) else json.dumps(data), "utf-8")
    return p


ATIF = {"schema_version": "ATIF-v1.7", "session_id": "s1", "agent": {"model_name": "swe-1-6"},
        "steps": [{"source": "agent", "model_name": "claude-opus-5-5-low", "metrics": {"prompt_tokens": 10}},
                  {"source": "user", "message": "hi"},
                  {"source": "agent", "model_name": "swe-1-6", "metrics": {"prompt_tokens": 30_000}},
                  {"source": "user", "message": "again"}],
        "final_metrics": {"total_prompt_tokens": 1}}


def test_transcript_preferred_and_read_only(db):
    make_db(db, [{"id": "s1", "model": "claude-opus-5-5-low", "nodes": [(300_000, None)]}])
    t = write_transcript(db, "s1", ATIF)
    h = digest(t)
    s = status(db)
    assert (s["tokens"], s["limit"], s["model_source"], s["token_source"]) == (
        30_000, 150_000, "transcript", "transcript")
    assert digest(t) == h


def test_transcript_agent_model_fallback(db):
    make_db(db, [{"id": "s1", "model": "swe-1-6", "nodes": [(5, None)]}])
    write_transcript(db, "s1", {"schema_version": "ATIF-v1.7", "agent": {"model_name": "claude-opus-5-5-low"},
                                "steps": [{"source": "agent", "metrics": {"prompt_tokens": 40}}]})
    s = status(db)
    assert (s["tokens"], s["limit"], s["model_source"]) == (40, 750_000, "transcript")


@pytest.mark.parametrize("bad", ["not json", json.dumps({**ATIF, "schema_version": "OTHER-1"}),
                                 json.dumps({"schema_version": "ATIF-v1", "steps": "x"}), "[]"])
def test_bad_transcript_falls_back(db, bad):
    make_db(db, [{"id": "s1", "model": "claude-opus-5-5-low", "nodes": [(300_000, None)]}])
    write_transcript(db, "s1", bad)
    s = status(db)
    assert (s["tokens"], s["limit"], s["model_source"], s["token_source"]) == (
        300_000, 750_000, "sessions_db", "sessions_db")


def test_precompact_model_used_without_transcript(store, db):
    from recall.context_gauge import context_status
    from recall.localindex import LocalIndex
    from recall.session_reader import SessionReader
    R = str(store.repo_root)
    make_db(db, [{"id": "s1", "cwd": R, "model": "claude-opus-5-5-low", "nodes": [(300_000, None)]}])
    run("PreCompact", json.dumps({"hook_event_name": "PreCompact", "session_id": "s1", "cwd": R,
                                  "model": "swe-1-6", "transcript_path": "x", "trigger": "manual",
                                  "turn_id": "t"}), R)
    idx = LocalIndex(store.repo_root)
    assert idx.get_state("model:s1") == "swe-1-6"
    s = context_status(SessionReader(db), "s1", {}, {}, {"opus-5-5": 1_000_000, "swe-1-6": 200_000}, idx)
    assert (s["limit"], s["model_source"], s["token_source"]) == (150_000, "precompact", "sessions_db")


def test_hook_no_token_data_no_request(store, db, monkeypatch):
    monkeypatch.setenv("RECALL_CONTEXT_LIMIT", "100")
    R = str(store.repo_root)
    make_db(db, [{"id": "s1", "cwd": R, "model": "swe-1-6", "nodes": [(None, None)]}])
    assert "recall_ingest" not in _ctx(run("UserPromptSubmit", json.dumps({"session_id": "s1"}), R))
