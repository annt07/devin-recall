"""Runtime: lazy open, fingerprint rebuild rule, page scan with Store.generation, error mapping."""
import json
import time

import pytest

from helpers import page

from recall.history import GitError, History, RefError
from recall.localindex import LocalIndex
from recall.runtime import HookResult, Runtime, run_hook, run_mcp, store_folder_correction
from recall.store import LockBusy, Meta, Store

P = "pages/fact/build-and-test.md"


@pytest.fixture
def counts(monkeypatch):
    c = {"git": 0, "rebuild": 0, "read": 0}
    run, rebuild, read = History._run, LocalIndex.rebuild, Store.read_page

    def _run(self, *a, **k):
        c["git"] += 1
        return run(self, *a, **k)

    def _rebuild(self, *a, **k):
        c["rebuild"] += 1
        return rebuild(self, *a, **k)

    def _read(self, *a, **k):
        c["read"] += 1
        return read(self, *a, **k)
    monkeypatch.setattr(History, "_run", _run)
    monkeypatch.setattr(LocalIndex, "rebuild", _rebuild)
    monkeypatch.setattr(Store, "read_page", _read)
    return c


def _reset(c):
    for k in c:
        c[k] = 0


def test_open_is_lazy(store, counts):
    rt = Runtime.open(store.repo_root)
    assert counts == {"git": 0, "rebuild": 0, "read": 0}
    assert rt.store.repo_root == store.repo_root


def test_index_rebuilds_only_on_change(store, counts):
    store.ingest([page()], Meta("ingest", "s1"))
    _reset(counts)
    Runtime.open(store.repo_root).index.search("pytest")
    Runtime.open(store.repo_root).index.search("pytest")
    assert counts["rebuild"] == 1


def test_mark_gives_no_rebuild(store, counts, monkeypatch):
    from recall import mcp_server as m
    monkeypatch.setenv("RECALL_REPO", str(store.repo_root))
    store.ingest([page()], Meta("ingest", "s1"))
    assert m.recall_search("pytest")[0]["path"] == P
    _reset(counts)
    m.recall_mark(P, "used")
    assert m.recall_search("pytest")[0]["path"] == P
    assert counts["rebuild"] == 0


def test_uncommitted_page_edit_is_found(store, counts):
    store.ingest([page()], Meta("ingest", "s1"))
    assert Runtime.open(store.repo_root).index.search("pytest")
    f = store.root / P
    f.write_text(f.read_text(encoding="utf-8") + "\nzebracorn hint\n", encoding="utf-8", newline="\n")
    _reset(counts)
    rt = Runtime.open(store.repo_root)
    assert [h["path"] for h in rt.index.search("zebracorn")] == [P]
    assert counts["rebuild"] == 1


def test_empty_index_rebuilds(store, counts):
    store.ingest([page()], Meta("ingest", "s1"))
    Runtime.open(store.repo_root).index.search("pytest")
    import sqlite3
    with sqlite3.connect(str(LocalIndex(store.repo_root).db)) as c:
        c.execute("DELETE FROM pages_fts")
    _reset(counts)
    assert Runtime.open(store.repo_root).index.search("pytest")
    assert counts["rebuild"] == 1


def test_pages_cached_and_refreshed_after_store_write(store, counts):
    store.ingest([page(), page("pages/fact/other.md", title="Other")], Meta("ingest", "s1"))
    rt = Runtime.open(store.repo_root)
    _reset(counts)
    first = rt.pages()
    assert [r for r, _, _ in first] == ["pages/fact/build-and-test.md", "pages/fact/other.md"]
    rt.pages()
    assert counts["read"] == 2
    g = rt.store.generation
    rt.store.add_raw("s1", "note", "something")
    assert rt.store.generation > g
    _reset(counts)
    rt.pages()
    assert counts["read"] == 2


@pytest.mark.parametrize("op", ["ingest", "set_status", "revert", "cleanup_raw", "write_index", "add_raw"])
def test_every_store_write_bumps_generation(store, op):
    import datetime as dt
    store.ingest([page()], Meta("ingest", "s1"))
    g = store.generation
    if op == "ingest":
        store.ingest([page(body="new")], Meta("ingest", "s1"))
    elif op == "set_status":
        store.set_status(P, "stale", dt.date.today())
    elif op == "revert":
        store.revert("HEAD", Meta("revert", "s1"))
    elif op == "cleanup_raw":
        (store.root / "raw" / "2000-01-01-s-note-00000000.md").write_text("x\n", encoding="utf-8")
        store.capture_commit(Meta("capture", "s"))
        g = store.generation
        store.cleanup_raw(dt.date.today())
    elif op == "write_index":
        store.write_index()
    else:
        store.add_raw("s1", "note", "text")
    assert store.generation > g


def test_session_start_retired_page_not_in_index(store):
    from recall.hooks import run
    store.ingest([page()], Meta("ingest", "s1"))
    idx = LocalIndex(store.repo_root)
    idx.mark(P, "wrong")
    idx.mark(P, "wrong")
    res = run("SessionStart", json.dumps({"session_id": "s2"}), str(store.repo_root))
    ctx = json.loads(res.stdout)["hookSpecificOutput"]["additionalContext"]
    assert "build-and-test" not in ctx.split("Use recall_search")[0]
    assert Runtime.open(store.repo_root).index.search("pytest") == []


def test_first_hook_call_sets_provisional_start(store):
    from recall.hooks import run
    from recall.session_state import SessionState
    before = time.time()
    run("UserPromptSubmit", json.dumps({"session_id": "s9", "prompt": "hi"}), str(store.repo_root))
    t = SessionState(LocalIndex(store.repo_root)).start("s9")
    assert t is not None and t >= before - 1


# ---------- error mapping (byte-identical to the MCP adapter before Runtime) ----------
@pytest.mark.parametrize("exc, text", [
    (RefError("no such ref: x"), "RefError: no such ref: x"),
    (LockBusy("store lock busy: p"), "LockBusy: store lock busy: p"),
    (GitError("git failed\n  token=abc"), "GitError: git failed token=***"),
    (ValueError("see https://u:pw@host/x"), "ValueError: see https://***@host/x"),
    (RuntimeError("cannot revert"), "RuntimeError: cannot revert"),
    (KeyError("k"), "KeyError: 'k'"),
    (TypeError(""), "TypeError: TypeError"),
])
def test_run_mcp_expected_errors(exc, text):
    def boom():
        raise exc
    assert run_mcp(boom, log_root=lambda: None) == {"error": text}


def test_run_mcp_unexpected_error_is_logged(tmp_path, monkeypatch):
    logged = []
    monkeypatch.setattr("recall.runtime.log_error", lambda root, e: logged.append((root, e)))

    def boom():
        raise OSError("secret path")
    assert run_mcp(boom, log_root=lambda: tmp_path) == {"error": "internal error (OSError); see errors.log"}
    assert logged and logged[0][0] == tmp_path


def test_run_mcp_unexpected_error_log_root_fails(monkeypatch):
    logged = []
    monkeypatch.setattr("recall.runtime.log_error", lambda root, e: logged.append(root))

    def no_root():
        raise ValueError("no repo")

    def boom():
        raise ZeroDivisionError()
    assert run_mcp(boom, log_root=no_root) == {"error": "internal error (ZeroDivisionError); see errors.log"}
    assert logged == [None]


def test_mcp_tool_error_text_unchanged(store, monkeypatch):
    from recall import mcp_server as m
    monkeypatch.setenv("RECALL_REPO", str(store.repo_root))
    assert m.recall_mark(P, "bogus") == {"error": 'ValueError: kind: must be "used" or "wrong"'}
    assert m.recall_get("../x") == {"error": "ValueError: bad path: '../x' (want pages/<type>/<slug>.md, "
                                             "raw/<file>.md or index.md, log.md, SCHEMA.md, eval.md)"}


def test_run_hook_maps_errors_to_empty_result(monkeypatch):
    logged = []
    monkeypatch.setattr("recall.runtime.log_error", lambda root, e: logged.append((root, e)))

    def boom():
        raise RuntimeError("x")
    assert run_hook(None, "hook Stop", boom) == HookResult()
    assert logged[0][1].startswith("hook Stop: Traceback")
    assert run_hook(None, "hook Stop", lambda: HookResult(exit_code=2, stderr="no")).exit_code == 2


# ---------- repo resolution ----------
def test_store_folder_correction(tmp_path):
    owner = tmp_path / "proj"
    assert store_folder_correction(owner / ".devin" / "recall") == owner
    assert store_folder_correction(owner / ".devin" / "Recall" / "pages") == owner
    assert store_folder_correction(owner) == owner


def test_mcp_recall_repo_inside_store_resolves_to_owner(store, monkeypatch):
    from recall import mcp_server as m
    monkeypatch.setenv("RECALL_REPO", str(store.root / "pages"))
    assert m._repo_root() == store.repo_root
    assert not (store.root / ".devin").exists()


def test_mcp_cwd_inside_store_resolves_to_owner(store, monkeypatch):
    from recall import mcp_server as m
    monkeypatch.delenv("RECALL_REPO", raising=False)
    monkeypatch.chdir(store.root)
    assert m._repo_root() == store.repo_root
