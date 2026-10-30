import hashlib
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath

import pytest

from fixtures_sessions import make_db
from recall.session_reader import SessionReader, _ts

DAY = 86_400_000
NOW = int(time.time() * 1000)


def _sha(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("RECALL_LOCAL_DIR", str(tmp_path / "local"))
    # Pure paths (need not exist). pytest's tmp_path is itself under AppData\Local\Temp,
    # so the repo must live elsewhere to test the temp-folder exclusion.
    repo = PureWindowsPath(r"C:\work\My Repo")
    other = PureWindowsPath(r"C:\work\other")
    temp = PureWindowsPath(r"C:\Users\u\AppData\Local\Temp\x")
    msgs = [
        ("system", "You are continuing work from a previous conversation thread.\nSummary: we chose sqlite."),
        ("user", "<system-reminder>ignore me</system-reminder>"),
        ("user", "please add tests"),
        ("assistant", [{"text": "first answer"}]),
        ("user", [{"text": "and docs"}]),
        ("assistant", "final answer"),
        ("assistant", ""),
    ]
    rows = [
        {"id": "a", "cwd": str(repo).upper(), "created": NOW - 3 * DAY, "last": NOW - 3 * DAY, "messages": msgs},
        {"id": "b", "cwd": (repo / "sub").as_posix(), "created": NOW - 2 * DAY, "last": NOW - DAY},
        {"id": "c", "cwd": str(other), "created": NOW, "last": NOW},
        {"id": "d", "cwd": str(temp), "created": NOW, "last": NOW, "workspace_dirs": f'["{repo.as_posix()}"]'},
        {"id": "e", "cwd": str(repo), "created": NOW - 200 * DAY, "last": NOW - 200 * DAY},
        {"id": "f", "cwd": str(other), "created": (NOW - 5 * DAY) // 1000, "last": (NOW - 5 * DAY) // 1000,
         "workspace_dirs": f"{other}\n{repo}"},
        {"id": "g", "cwd": r"C:\work\My Repo2", "created": NOW, "last": NOW},
    ]
    db = make_db(tmp_path / "sessions.db", rows)
    return repo, db


def test_sessions_for_filters_and_orders(env):
    repo, db = env
    ids = [s.id for s in SessionReader(db).sessions_for(repo, since_days=90)]
    assert ids == ["b", "a", "f"]


def test_since_none_includes_old(env):
    repo, db = env
    ids = [s.id for s in SessionReader(db).sessions_for(repo, since_days=None)]
    assert ids == ["b", "a", "f", "e"]


def test_records(env):
    repo, db = env
    recs = SessionReader(db).records("a")
    assert [k for k, _ in recs] == ["compact", "prompts", "final"]
    assert recs[0][1] == "we chose sqlite."
    assert "please add tests" in recs[1][1] and "and docs" in recs[1][1]
    assert "ignore me" not in recs[1][1]
    assert recs[2][1] == "final answer"


def test_missing_column_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("RECALL_LOCAL_DIR", str(tmp_path / "local"))
    db = make_db(tmp_path / "s.db", [{"id": "a", "cwd": "x", "created": NOW, "messages": [("user", "hi")]}],
                 with_chat_message=False)
    assert SessionReader(db).records("a") == []
    assert (tmp_path / "local" / "errors.log").exists()


def test_missing_db_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("RECALL_LOCAL_DIR", str(tmp_path / "local"))
    r = SessionReader(tmp_path / "nope.db")
    assert r.sessions_for(tmp_path, None) == [] and r.records("a") == []
    assert not (tmp_path / "nope.db").exists()


def test_db_unchanged(env):
    repo, db = env
    before = _sha(db)
    r = SessionReader(db)
    r.sessions_for(repo, None)
    r.records("a")
    r.resolve_session("zzz", repo)
    assert _sha(db) == before
    assert not db.with_name(db.name + "-wal").exists()
    assert not db.with_name(db.name + "-journal").exists()


def test_resolve_session(env):
    repo, db = env
    r = SessionReader(db)
    assert r.resolve_session("unknown", repo) == "b"
    assert r.resolve_session("a", repo) == "a"


def test_default_path(monkeypatch, tmp_path):
    # A fake home: the real one may hold a Devin folder (the first existing candidate wins).
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("RECALL_DEVIN_DIR", str(tmp_path))
    assert SessionReader().db_path == tmp_path / "devin" / "cli" / "sessions.db"
    monkeypatch.delenv("RECALL_DEVIN_DIR")
    monkeypatch.setenv("APPDATA", str(tmp_path / "ad"))
    assert SessionReader().db_path == tmp_path / "ad" / "devin" / "cli" / "sessions.db"
    monkeypatch.delenv("APPDATA")
    assert SessionReader().db_path == Path.home() / ".config" / "devin" / "cli" / "sessions.db"


def test_records_skips_bad_chat_messages(tmp_path, monkeypatch):
    monkeypatch.setenv("RECALL_LOCAL_DIR", str(tmp_path / "local"))
    db = make_db(tmp_path / "s.db", [{"id": "a", "cwd": "x", "created": NOW, "messages": [("user", "hi")]}])
    con = sqlite3.connect(db)
    con.executemany("INSERT INTO message_nodes(session_id, chat_message) VALUES ('a', ?)",
                    [("not json",), ("[1, 2]",), (None,)])
    con.commit()
    con.close()
    assert SessionReader(db).records("a") == [("prompts", "hi")]


def test_sessions_for_null_working_directory(tmp_path, monkeypatch):
    monkeypatch.setenv("RECALL_LOCAL_DIR", str(tmp_path / "local"))
    repo = PureWindowsPath(r"C:\work\R")
    db = make_db(tmp_path / "s.db", [
        {"id": "n", "cwd": None, "created": NOW},
        {"id": "w", "cwd": None, "created": NOW, "workspace_dirs": f'["{repo.as_posix()}"]'}])
    assert [s.id for s in SessionReader(db).sessions_for(repo, None)] == ["w"]


def test_naive_iso_is_utc():
    assert _ts("2026-01-02T03:04:05") == datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    assert _ts("2026-01-02T03:04:05Z").tzinfo is not None


def test_select_rejects_non_select(tmp_path):
    with pytest.raises(ValueError):
        SessionReader(tmp_path / "x.db")._select("sessions", ("id",), "DELETE FROM sessions")
