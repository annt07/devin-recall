import subprocess

import pytest
from helpers import page

from recall.history import GitError, History, RefError
from recall.store import Meta


def test_time_travel(store):
    h = store.history
    store.ingest([page()], Meta("ingest", "s1"))
    store.ingest([page(body="Run `uv run pytest -q`.")], Meta("ingest", "s2"))
    p = "pages/fact/build-and-test.md"
    assert "pytest -q" not in h.show("session:s1", p)
    assert "pytest -q" in h.show("HEAD", p) and "-q" in h.diff("session:s1", "HEAD", p)
    assert h.resolve("before:ingest#1") == h.log(op="init")[0].sha
    assert [e.trailers["Recall-Session"] for e in h.log(page=p)] == ["s2", "s1"]
    sha = store.revert("session:s2", Meta("revert", "s3"))
    assert "pytest -q" not in store.read_page(p)[1]
    store.revert(sha, Meta("revert", "s3"))
    assert "pytest -q" in store.read_page(p)[1]
    assert h.resolve("2999-01-01") == h.resolve("HEAD")


def test_ref_edge_cases(store):
    h = store.history
    store.ingest([page()], Meta("ingest", "s1"))
    for bad in ("session:nope", "before:ingest#2", "before:ingest#0", "before:ingest#x",
                "before:init#1", "best-eval", "1990-01-01", "repo:zzzz", "repo:", "nosuchref", "-q", ""):
        with pytest.raises(RefError):
            h.resolve(bad)
    repo_sha = h.log(op="ingest")[0].trailers["Recall-Repo-Commit"]
    assert h.resolve("repo:" + repo_sha[:7]) == h.log(op="ingest")[0].sha
    assert h.resolve("2999-01-01 10:00") == h.resolve("HEAD")


def test_revert_single_page_and_new_page(store):
    p = "pages/fact/build-and-test.md"
    store.ingest([page()], Meta("ingest", "s1"))
    store.revert("session:s1", Meta("revert", "s2"), page=p)
    assert p not in store.pages()
    assert "build-and-test" not in (store.root / "index.md").read_text(encoding="utf-8")
    log = store.history.log(limit=2)
    assert log[0].trailers["Recall-Op"] == "revert" and log[1].trailers["Recall-Session"] == "s1"


def test_revert_older_commit_keeps_later_pages(store):
    a, b = "pages/fact/build-and-test.md", "pages/fact/lint.md"
    store.ingest([page()], Meta("ingest", "s1"))
    store.ingest([page(path=b, title="Lint", body="Run ruff.")], Meta("ingest", "s2"))
    (store.root / "raw" / "pending.md").write_text("x\n", encoding="utf-8")  # untracked raw is fine
    store.revert("session:s1", Meta("revert", "s3"))
    assert store.pages() == [b]
    index = (store.root / "index.md").read_text(encoding="utf-8")
    assert "lint" in index and "build-and-test" not in index
    assert "revert | session:s1" in (store.root / "log.md").read_text(encoding="utf-8")
    with pytest.raises(Exception):
        store.revert("before:ingest#1", Meta("revert", "s3"))  # root commit


def test_revert_conflict_aborts_cleanly(store):
    p = "pages/fact/build-and-test.md"
    store.ingest([page()], Meta("ingest", "s1"))
    store.ingest([page(body="Two.")], Meta("ingest", "s2"))
    store.ingest([page(body="Three.")], Meta("ingest", "s3"))
    head = store.history.resolve("HEAD")
    with pytest.raises(Exception):
        store.revert("session:s2", Meta("revert", "s4"))
    assert store.history.resolve("HEAD") == head
    assert "Three." in store.read_page(p)[1]
    status = subprocess.run(["git", "status", "--porcelain"], cwd=store.root, capture_output=True, text=True)
    assert status.returncode == 0 and status.stdout.strip() == ""
    assert not (store.root / ".lock").exists()


def test_history_revert_commit_conflict_aborts(store):
    p = "pages/fact/build-and-test.md"
    store.ingest([page()], Meta("ingest", "s1"))
    store.ingest([page(body="Two.")], Meta("ingest", "s2"))
    store.ingest([page(body="Three.")], Meta("ingest", "s3"))
    h = store.history
    head = h.resolve("HEAD")
    assert h.revert_commit(h.resolve("session:s2")) is None
    assert h.resolve("HEAD") == head and h.status() == [] and "Three." in store.read_page(p)[1]


def test_history_revert_commit_stages_touched(store):
    p = "pages/fact/build-and-test.md"
    store.ingest([page()], Meta("ingest", "s1"))
    h = store.history
    touched = h.revert_commit(h.resolve("session:s1"))
    assert p in touched and p not in store.pages()
    h.commit(Meta("revert", "s2"), "revert", paths=touched)


def test_history_status_and_untracked(store):
    store.ingest([page()], Meta("ingest", "s1"))
    h = store.history
    assert h.status() == [] and h.untracked(["raw"]) == []
    (store.root / "raw" / "new.md").write_text("x\n", encoding="utf-8")
    (store.root / "pages" / "fact" / "build-and-test.md").write_text("changed\n", encoding="utf-8")
    assert h.status() == ["pages/fact/build-and-test.md"]
    assert h.untracked(["raw"]) == ["raw/new.md"]
    assert h.status(["raw"], include_untracked=True) == ["raw/new.md"]


def test_history_restore_page_and_store_revert_page(store):
    a, b = "pages/fact/build-and-test.md", "pages/fact/lint.md"
    store.ingest([page()], Meta("ingest", "s1"))
    first = store.history.resolve("HEAD")
    store.ingest([page(body="Two."), page(path=b, title="Lint", body="Run ruff.")], Meta("ingest", "s2"))
    store.history.restore_page(first, b)
    assert not (store.root / b).exists()
    store.history.restore_page(first, a)
    assert "Two." not in store.read_page(a)[1]
    store.history.restore_page("HEAD", a)
    store.history.restore_page("HEAD", b)
    store.revert("session:s2", Meta("revert", "s3"), page=a)
    assert "Two." not in store.read_page(a)[1] and b in store.pages()


def test_git_timeout_is_git_error(store, monkeypatch):
    def slow(*a, **kw):
        assert kw.get("timeout") == 30
        raise subprocess.TimeoutExpired(a[0], 30)
    monkeypatch.setattr(subprocess, "run", slow)
    with pytest.raises(GitError, match="timed out"):
        History(store.root).log()
