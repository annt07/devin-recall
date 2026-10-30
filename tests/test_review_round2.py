"""Second review round: spec gaps G1-G3, bugs B1-B13, portability P1-P6."""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from conftest import _git
from fixtures_sessions import make_db
from helpers import page

from recall import hooks
from recall.hooks import run
from recall.localindex import LocalIndex
from recall.store import Meta

PROJECT = Path(__file__).resolve().parents[1]
DAY_MS = 86_400_000


def _ctx(res) -> str:
    return json.loads(res.stdout)["hookSpecificOutput"]["additionalContext"]


def _status(store) -> str:
    return store.history._run("status", "--porcelain", "--untracked-files=all").stdout


# ---------- G1: pending count at SessionStart ----------
def test_session_start_injects_pending_count(store):
    store.add_raw("s1", "note", "a pending learning")
    ctx = _ctx(run("SessionStart", json.dumps({"session_id": "x"}), str(store.repo_root)))
    assert hooks.PENDING_MSG.format(n=1) in ctx
    assert ctx.count("Pending raw records:") == 1


def test_session_start_no_pending_line_when_zero(store):
    ctx = _ctx(run("SessionStart", json.dumps({"session_id": "x"}), str(store.repo_root)))
    assert "Pending raw records" not in ctx


# ---------- G2: code-mismatch note ----------
def test_code_mismatch_note(store):
    R = store.repo_root
    (R / "a.py").write_text("x = 1\n", encoding="utf-8")
    _git(R, "add", "a.py")
    _git(R, "commit", "-q", "-m", "a")
    old_head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=R, capture_output=True, text=True).stdout.strip()
    _git(R, "checkout", "-q", "-b", "feature")
    (R / "a.py").write_text("x = 2\n", encoding="utf-8")
    _git(R, "commit", "-q", "-am", "b")
    res = store.ingest([page(sources=("a.py",))], Meta("ingest", "s1"))
    assert res.accepted
    ctx = _ctx(run("SessionStart", "{}", str(R)))
    assert "Memory is newer" not in ctx  # HEAD is the memory commit itself
    _git(R, "checkout", "-q", old_head)  # older code: a.py changed back
    ctx = _ctx(run("SessionStart", "{}", str(R)))
    assert "Memory is newer than the checked-out code; verify stale pages:" in ctx
    assert "pages/fact/build-and-test.md" in ctx.split("Memory is newer", 1)[1]


# ---------- G3: 90-day raw cleanup ----------
def test_raw_cleanup_after_90_days(store):
    old_day = datetime.now() - timedelta(days=100)
    ingested = store.add_raw("s1", "note", "old but used", when=old_day)
    pending = store.add_raw("s1", "note", "old and never used", when=old_day)
    fresh = store.add_raw("s1", "note", "fresh one")
    store.capture_commit(Meta("capture", "s1"))
    store.ingest([], Meta("ingest", "s1"), consumed=[ingested])
    loose = store.add_raw("s2", "note", "old untracked", when=old_day)  # never committed
    from recall.maintenance import lint_mechanical
    lint_mechanical(store, LocalIndex(store.repo_root), date.today())
    for rel in (ingested, pending, loose):
        assert not (store.root / rel).exists()
    assert (store.root / fresh).exists()
    log = (store.root / "log.md").read_text(encoding="utf-8")
    assert "cleanup | 2 raw not ingested" in log
    e = store.history.log(op="cleanup", limit=1)
    assert e and e[0].trailers["Recall-Op"] == "cleanup"
    assert _status(store).strip() == ""  # deletions committed, nothing else left over


def test_raw_cleanup_runs_from_session_start(store):
    old = store.add_raw("s1", "note", "very old", when=datetime.now() - timedelta(days=91))
    run("SessionStart", "{}", str(store.repo_root))
    assert not (store.root / old).exists()


# ---------- B1: anchors when hashing fails ----------
def _break_batch_hash(monkeypatch, store, single_ok: bool):
    real = store.history._run

    def fake(*args, **kw):
        if args and args[0] == "hash-object" and ("--stdin-paths" in args or not single_ok):
            return subprocess.CompletedProcess(args, 1, "", "boom")
        return real(*args, **kw)
    monkeypatch.setattr(store.history, "_run", fake)


def test_hash_objects_falls_back_per_file(store, monkeypatch):
    target = store.repo_root / "pyproject.toml"
    want = store.history.hash_objects([target])[str(target)]
    _break_batch_hash(monkeypatch, store, single_ok=True)
    assert store.history.hash_objects([target]) == {str(target): want}


def test_lint_anchor_unknown_no_status_change(store, monkeypatch):
    store.ingest([page()], Meta("ingest", "s1"))
    n = len(store.history.log(limit=100))
    _break_batch_hash(monkeypatch, store, single_ok=False)
    from recall.maintenance import lint_mechanical
    f = lint_mechanical(store, LocalIndex(store.repo_root), date.today())
    assert [x.rule for x in f if x.path.endswith("build-and-test.md")] == ["anchor-unknown"]
    assert store.read_page("pages/fact/build-and-test.md")[0]["status"] == "active"
    assert len(store.history.log(limit=100)) == n


def test_ingest_unhashable_source(store, monkeypatch):
    store.ingest([page()], Meta("ingest", "s1"))
    before = store.read_page("pages/fact/build-and-test.md")[0]["anchors"]
    _break_batch_hash(monkeypatch, store, single_ok=False)
    res = store.ingest([page(body="Run `uv run pytest -q`.")], Meta("ingest", "s1"))
    assert res.accepted == ["pages/fact/build-and-test.md"]
    assert store.read_page("pages/fact/build-and-test.md")[0]["anchors"] == before
    res = store.ingest([page("pages/howto/new-one.md", title="Brand new zebra",
                             body="totally different words here")], Meta("ingest", "s1"))
    assert res.rejected == [("pages/howto/new-one.md", "anchors: cannot hash source")]
    assert not (store.root / "pages/howto/new-one.md").exists()


# ---------- B2: init fast path ----------
def test_init_fast_path_spawns_no_git(store, monkeypatch):
    from recall.store import Store
    Store(store.repo_root).init()  # first call may finish the slow path (writes the sentinel)

    def boom(*a, **k):
        raise AssertionError("git spawned")
    monkeypatch.setattr(subprocess, "run", boom)
    Store(store.repo_root).init()


# ---------- B3: gauge per-prompt cost ----------
def test_resolve_session_known_id_skips_scan(tmp_path, monkeypatch):
    from recall.session_reader import SessionReader
    now = int(time.time() * 1000)
    db = make_db(tmp_path / "s.db", [{"id": "abc", "cwd": "/elsewhere", "created": now}])
    r = SessionReader(db)
    monkeypatch.setattr(r, "sessions_for", lambda *a, **k: (_ for _ in ()).throw(AssertionError("scan")))
    assert r.resolve_session("abc", tmp_path) == "abc"


def test_token_count_is_limited(tmp_path, monkeypatch):
    from recall.session_reader import SessionReader
    db = tmp_path / "s.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE message_nodes(row_id INTEGER PRIMARY KEY, session_id TEXT, metadata TEXT)")
    con.execute("INSERT INTO message_nodes(session_id, metadata) VALUES ('a', ?)",
                (json.dumps({"num_tokens_preceding": 7}),))
    con.commit()
    con.close()
    r = SessionReader(db)
    seen = []
    real = r._select
    monkeypatch.setattr(r, "_select", lambda t, c, sql, p=(): seen.append(sql) or real(t, c, sql, p))
    assert r.token_count("a") == 7
    assert "LIMIT" in seen[0].upper()


# ---------- B4: revert scope ----------
def test_revert_does_not_commit_untracked_raw(store):
    store.ingest([page()], Meta("ingest", "s1"))
    raw = store.add_raw("s9", "note", "pending, untracked")
    store.revert("session:s1", Meta("revert", "s2"))
    assert f"?? {raw}" in _status(store)
    assert not (store.root / "pages/fact/build-and-test.md").exists()


def test_tracked_hand_edit_blocks_revert(store):
    store.ingest([page()], Meta("ingest", "s1"))
    p = store.root / "pages/fact/build-and-test.md"
    p.write_text(p.read_text(encoding="utf-8") + "hand edit\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="uncommitted"):
        store.revert("session:s1", Meta("revert", "s2"))
    assert "hand edit" in p.read_text(encoding="utf-8")


# ---------- B5: seed retry ----------
def test_seed_retry_no_duplicate_log_and_commits_raw(tmp_path, store, monkeypatch):
    from recall.history import GitError
    from recall.seed import seed_batch
    from recall.session_reader import SessionReader
    now = int(time.time() * 1000)
    db_dir = tmp_path / "devin-home" / "devin" / "cli"
    db_dir.mkdir(parents=True)
    make_db(db_dir / "sessions.db", [{"id": "s0", "cwd": str(store.repo_root), "created": now - DAY_MS,
                                      "title": "T0", "messages": [("user", "p0"), ("assistant", "a0")]}])
    real = store.history.commit
    calls = {"n": 0}

    def flaky(meta, *a, **k):
        if meta.op == "seed" and calls["n"] == 0:
            calls["n"] += 1
            raise GitError("transient")
        return real(meta, *a, **k)
    monkeypatch.setattr(store.history, "commit", flaky)
    idx = LocalIndex(store.repo_root)
    assert seed_batch(store, SessionReader(), idx, "cur")["imported"] == 0
    assert "seed |" not in (store.root / "log.md").read_text(encoding="utf-8")
    assert seed_batch(store, SessionReader(), idx, "cur")["imported"] == 1
    assert (store.root / "log.md").read_text(encoding="utf-8").count("seed | T0") == 1
    assert "raw/" not in _status(store)  # the raw files of the failed attempt are committed now
    e = store.history.log(op="seed", limit=1)[0]
    assert e.trailers["Recall-Sources"].count("raw/") == 2


# ---------- B6: multi_edit matcher ----------
def test_matcher_includes_multi_edit():
    from recall.install import WRITE_GUARD_MATCHER
    import re
    assert re.match(WRITE_GUARD_MATCHER, "multi_edit")


# ---------- B7: index cap ----------
def test_index_injection_capped(store):
    (store.root / "index.md").write_text("# Recall index\n" + "x" * 9000, encoding="utf-8")
    text = hooks._index_text(store)
    assert len(text) <= hooks.INDEX_MAX_CHARS + 60
    assert text.endswith("... use recall_search for more")


# ---------- B8: eval rate cap ----------
def test_eval_rate_cap(store, monkeypatch):
    from recall import mcp_server as m
    monkeypatch.setenv("RECALL_REPO", str(store.repo_root))
    (store.root / "eval.md").write_text("## Q: How to test?\nA: uv run pytest\n", encoding="utf-8")
    ans = {"How to test?": "uv run pytest"}
    assert m.recall_eval(answers=ans)["score"] == 1.0
    assert m.recall_eval(answers=ans) == {"error": "eval not due"}
    assert m.recall_eval()["questions"]  # listing questions is always allowed
    LocalIndex(store.repo_root).set_state("lint_due", "1")
    assert "score" in m.recall_eval(answers=ans)


def test_eval_allowed_after_semantic_lint(store, monkeypatch):
    from recall import mcp_server as m
    monkeypatch.setenv("RECALL_REPO", str(store.repo_root))
    (store.root / "eval.md").write_text("## Q: How to test?\nA: uv run pytest\n", encoding="utf-8")
    ans = {"How to test?": "uv run pytest"}
    m.recall_eval(answers=ans)
    m.recall_lint(semantic=True)
    assert "score" in m.recall_eval(answers=ans)


def test_semantic_request_allows_eval(store, monkeypatch):
    from recall import evaluation, maintenance
    from recall.runtime import Runtime
    rt = Runtime.open(store.repo_root)
    evaluation.record(store, 1.0, "s", rt.state)  # a fresh eval: normally the next one is not due
    assert not maintenance.eval_due(store, rt.state, time.time())
    monkeypatch.setattr(maintenance, "review_due", lambda s, st, now: True)
    assert maintenance.review_section(rt, {})
    assert maintenance.eval_due(store, rt.state, time.time())


# ---------- B9: own markers skipped ----------
def test_records_skip_recall_injections(tmp_path):
    from recall.context_gauge import NEAR_MSG
    from recall.session_reader import SessionReader
    now = int(time.time() * 1000)
    msgs = [("user", "# Recall index\n\n## fact"), ("user", hooks.START_HINT),
            ("user", hooks.PENDING_MSG.format(n=3)),
            ("user", NEAR_MSG.format(pct=90, tokens=1, limit=2, source="env")),
            ("user", "real question")]
    db = make_db(tmp_path / "s.db", [{"id": "a", "cwd": "/x", "created": now, "messages": msgs}])
    recs = dict(SessionReader(db).records("a"))
    assert recs["prompts"] == "real question"


# ---------- B10 ----------
def test_toplevel_timeout_is_short(monkeypatch, tmp_path):
    seen = {}

    def fake(*a, **k):
        seen["t"] = k.get("timeout")
        return subprocess.CompletedProcess(a, 1, "", "")
    monkeypatch.setattr(hooks.subprocess, "run", fake)
    hooks._toplevel(tmp_path)
    assert seen["t"] == 2


# ---------- B11 ----------
def test_posix_temp_sessions_excluded(tmp_path):
    from recall.session_reader import SessionReader
    now = int(time.time() * 1000)
    rows = [{"id": "t1", "cwd": "/tmp/pytest-1/x", "created": now, "workspace_dirs": json.dumps(["/repo"])},
            {"id": "t2", "cwd": "/var/folders/ab/T/x", "created": now, "workspace_dirs": json.dumps(["/repo"])},
            {"id": "ok", "cwd": "/repo", "created": now}]
    db = make_db(tmp_path / "s.db", rows)
    assert [s.id for s in SessionReader(db).sessions_for("/repo")] == ["ok"]


# ---------- B12 ----------
def test_mcp_dependency_pinned_and_no_fallback():
    text = (PROJECT / "pyproject.toml").read_text(encoding="utf-8")
    assert '"mcp>=2.3,<3"' in text
    assert "fastmcp" not in (PROJECT / "src/recall/mcp_server.py").read_text(encoding="utf-8")


# ---------- B13 ----------
def test_errors_log_redacted_and_rotated(repo, monkeypatch):
    from recall import paths
    paths.log_error(repo, "oops token=ghp_" + "a" * 40 + " AKIAABCDEFGHIJKLMNOP")
    log = paths.local_dir(repo) / "errors.log"
    text = log.read_text(encoding="utf-8")
    assert "ghp_" + "a" * 40 not in text and "AKIAABCDEFGHIJKLMNOP" not in text
    monkeypatch.setattr(paths, "ERRORS_MAX_BYTES", 100)
    paths.log_error(repo, "x" * 200)
    paths.log_error(repo, "after rotation")
    assert log.with_name("errors.log.1").is_file()
    assert "after rotation" in log.read_text(encoding="utf-8")
    assert log.stat().st_size < 200


def test_probe_precompact_redacted(store):
    from recall.paths import local_dir
    run("PreCompact", json.dumps({"session_id": "s", "x": "password=hunter2hunter2"}), str(store.repo_root))
    text = (local_dir(store.repo_root) / "probe-precompact.json").read_text(encoding="utf-8")
    assert "hunter2hunter2" not in text and '"session_id": "s"' in text


# ---------- P1: absolute launcher, quoting ----------
def test_tool_mode_without_recall_on_path_fails(repo, monkeypatch):
    import recall.install as ins
    monkeypatch.setattr(ins, "_dev_project", lambda: None)
    monkeypatch.setattr(ins.shutil, "which", lambda name: None)
    with pytest.raises(RuntimeError, match="not on PATH"):
        ins.install(repo)
    assert not (repo / ".devin" / "config.local.json").exists()


def test_dev_checkout_uses_python_module(repo):
    import recall.install as ins
    res = ins.install(repo)
    assert res["launcher"] == [sys.executable, "-m", "recall"]


def test_windows_quoting_escapes_percent_and_bang(monkeypatch):
    import recall.install as ins
    monkeypatch.setattr(ins, "_windows", lambda: True)
    assert ins._q(r"C:\a%PATH%b") == r'"C:\a%%PATH%%b"'
    with pytest.raises(RuntimeError, match="!"):
        ins._q(r"C:\a!b")


def test_python_m_recall_runs():
    p = subprocess.run([sys.executable, "-m", "recall", "--help"], capture_output=True, text=True, timeout=60)
    assert p.returncode == 0 and "hook" in p.stdout


# ---------- P2: XDG / macOS ----------
def test_xdg_and_macos_candidates(monkeypatch, tmp_path):
    from recall import context_gauge, paths, session_reader
    for v in ("RECALL_LOCAL_DIR", "RECALL_DEVIN_DIR", "RECALL_DEVIN_CONFIG", "LOCALAPPDATA", "APPDATA",
              "XDG_DATA_HOME", "XDG_CONFIG_HOME"):
        monkeypatch.delenv(v, raising=False)
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    assert paths.local_base() == tmp_path / "data" / "devin-recall"
    mac = home / "Library" / "Application Support"
    (mac / "devin").mkdir(parents=True)
    (mac / "devin" / "config.json").write_text("{}", encoding="utf-8")
    assert session_reader.default_db_path() == mac / "devin" / "cli" / "sessions.db"
    assert context_gauge.devin_config_path() == mac / "devin" / "config.json"
    (tmp_path / "cfg" / "devin").mkdir(parents=True)
    (tmp_path / "cfg" / "devin" / "config.json").write_text("{}", encoding="utf-8")
    assert session_reader.default_db_path() == tmp_path / "cfg" / "devin" / "cli" / "sessions.db"
    assert context_gauge.devin_config_path() == tmp_path / "cfg" / "devin" / "config.json"


# ---------- P3 ----------
def test_install_git_has_timeout(monkeypatch, tmp_path):
    import recall.install as ins
    seen = {}

    def fake(*a, **k):
        seen["t"] = k.get("timeout")
        return subprocess.CompletedProcess(a, 1, "", "")
    monkeypatch.setattr(ins.subprocess, "run", fake)
    ins._git(tmp_path, "status")
    assert seen["t"] == 30
    with pytest.raises(RuntimeError):
        ins._add_excludes(tmp_path)


# ---------- P4 ----------
def test_stale_lock_not_removed_if_replaced(store, monkeypatch):
    from recall import store as st
    lock = store.root / ".lock"
    lock.write_text("{}", encoding="utf-8")
    old = time.time() - 1000
    os.utime(lock, (old, old))
    real_stat = Path.stat
    calls = {"n": 0}

    def stat(self, *a, **k):
        s = real_stat(self, *a, **k)
        if self == lock:
            calls["n"] += 1
            if calls["n"] == 2:  # someone replaced the lock between our two looks
                os.utime(lock, None)
                s = real_stat(self, *a, **k)
        return s
    monkeypatch.setattr(Path, "stat", stat)
    assert st._remove_if_stale(lock) is False
    assert lock.exists()


# ---------- P5 ----------
@pytest.mark.parametrize("text", ["AWS_SECRET_ACCESS_KEY=abcd1234efgh5678", "GITHUB_TOKEN: 0123456789abcdef",
                                  "STRIPE_API_KEY=sk_live_abcdefgh", "MY_SECRET='abcdefghij'"])
def test_env_style_secrets_rejected(store, text):
    from recall import gate
    v = gate.check(page(body=f"Set {text} first."), store)
    assert v.kind == "reject" and v.reason.startswith("secret")


# ---------- P6 ----------
def test_sqlite_uri_is_read_only(tmp_path):
    from recall.session_reader import SessionReader
    db = make_db(tmp_path / "with space%.db", [])
    con = SessionReader(db)._connect()
    try:
        with pytest.raises(sqlite3.OperationalError):
            con.execute("CREATE TABLE x(a)")
    finally:
        con.close()


# ---------- MCP over stdio ----------
def test_mcp_stdio_initialize_and_list_tools(store):
    import asyncio
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    async def go():
        env = {**os.environ, "RECALL_REPO": str(store.repo_root)}
        params = StdioServerParameters(command=sys.executable, args=["-m", "recall", "mcp"], env=env)
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                return {t.name for t in (await s.list_tools()).tools}
    names = asyncio.run(asyncio.wait_for(go(), 60))
    assert "recall_ingest" in names and len(names) == 11


# ---------- wheel contents ----------
@pytest.mark.slow
@pytest.mark.skipif(shutil.which("uv") is None, reason="uv not on PATH")
def test_wheel_contains_templates(tmp_path):
    p = subprocess.run(["uv", "build", "--wheel", "-o", str(tmp_path), str(PROJECT)],
                       capture_output=True, text=True, timeout=180)
    assert p.returncode == 0, p.stderr
    wheel = next(tmp_path.glob("*.whl"))
    names = set(zipfile.ZipFile(wheel).namelist())
    for f in ("SCHEMA.md", "eval.md", "models.json", "skill/SKILL.md"):
        assert f"recall/templates/{f}" in names
