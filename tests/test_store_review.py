import os
import time

import pytest
from helpers import page

from recall.localindex import LocalIndex
from recall.paths import local_base, local_dir
from recall.store import LockBusy, Meta, Store

M = Meta("ingest", "s1")
P = "pages/fact/build-and-test.md"


def test_lock_busy_after_wait(store):
    with store.lock("a"):
        t0 = time.monotonic()
        with pytest.raises(LockBusy):
            with store.lock("b", wait=0.3):
                pass
        assert time.monotonic() - t0 >= 0.3
    assert not (store.root / ".lock").exists()


def test_stale_lock_recovered(store):
    lock = store.root / ".lock"
    lock.write_text("{}", encoding="utf-8")
    old = time.time() - 200
    os.utime(lock, (old, old))
    with store.lock("b", wait=0.2):
        assert lock.exists()
    assert not lock.exists()


def test_lock_release_tolerates_missing_file(store):
    with store.lock("a"):
        (store.root / ".lock").unlink()


def test_init_is_locked_and_idempotent(repo):
    s = Store(repo)
    s.root.mkdir(parents=True)
    (s.root / ".lock").write_text("{}", encoding="utf-8")
    with pytest.raises(LockBusy):
        s.init()
    (s.root / ".lock").unlink()
    s.init()
    s.init()
    assert len(s.history.log()) == 1


def test_secret_rejection_leaks_nowhere(store):
    secret = "ghp_" + "a" * 36
    res = store.ingest([page(body=f"token {secret}"),
                        page(path=f"pages/fact/{secret}.md", sources=(f"x{secret}.py",)),
                        page(path="pages/fact/sk-" + "a" * 24 + ".md", title="Other")],
                       Meta("ingest", f"s-{secret}"))
    assert len(res.rejected) == 3 and not res.accepted
    assert "sk-" + "a" * 24 not in (store.root / "log.md").read_text(encoding="utf-8")
    assert "secret" in res.rejected[0][1]
    assert secret not in (store.root / "log.md").read_text(encoding="utf-8")
    err = local_dir(store.repo_root) / "errors.log"
    assert not err.exists() or secret not in err.read_text(encoding="utf-8")
    base_err = local_base() / "errors.log"
    assert not base_err.exists() or secret not in base_err.read_text(encoding="utf-8")


def test_secret_in_summary_rejected(store):
    ch = page()
    ch.front["summary"] = "key AKIA" + "B" * 16
    res = store.ingest([ch], M)
    assert res.rejected and "secret" in res.rejected[0][1]


def test_reingest_same_path_updates_in_place(store):
    store.ingest([page()], M)
    res = store.ingest([page(body="Run `uv run pytest -q` now.")], M)
    assert res.accepted == [P] and res.redirected == []
    assert "-q" in store.read_page(P)[1] and store.pages() == [P]


def test_crlf_page_reread_and_search(store):
    store.ingest([page()], M)
    raw = (store.root / P).read_bytes().replace(b"\n", b"\r\n")
    (store.root / P).write_bytes(raw)
    front, body = store.read_page(P)
    assert front["title"] == "Build and test" and body == "Run `uv run pytest`."
    idx = LocalIndex(store.repo_root)
    idx.rebuild(store)
    assert [h["path"] for h in idx.search("pytest")] == [P]


def test_agent_stale_since_is_stripped(store):
    store.ingest([page(stale_since="2020-01-01")], M)
    assert store.read_page(P)[0]["stale_since"] is None


def test_consumed_entries_validated(store):
    raw = store.add_raw("s1", "final", "nothing durable")
    res = store.ingest([], Meta("ingest", "s1"), consumed=[raw, "raw/missing.md", "../etc/passwd", "raw/a\nb.md"])
    assert (store.root / "consumed.txt").read_text(encoding="utf-8") == f"{raw}\n"
    assert len(res.rejected) == 3 and all("consumed" in r for _, r in res.rejected)


def test_store_files_are_not_code_sources(store):
    res = store.ingest([page(sources=(".devin/recall/index.md",))], M)
    assert res.rejected and "provenance" in res.rejected[0][1]


def test_local_dir_case(monkeypatch, tmp_path):
    a, b = local_dir(tmp_path / "Repo"), local_dir(tmp_path / "repo")
    assert (a == b) == (os.name == "nt")
