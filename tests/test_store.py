from helpers import page
from recall.store import Meta


def test_ingest_writes_page_index_log_and_commit(store):
    res = store.ingest([page()], Meta("ingest", "s1", "Stop"))
    assert res.accepted == ["pages/fact/build-and-test.md"]
    front, body = store.read_page("pages/fact/build-and-test.md")
    assert front["status"] == "active" and "pyproject.toml" in front["anchors"]
    assert "[[pages/fact/build-and-test]] Build and test" in (store.root / "index.md").read_text()
    assert "ingest | pages/fact/build-and-test.md | session=s1" in (store.root / "log.md").read_text()
    e = store.history.log(op="ingest")[0]
    assert e.trailers["Recall-Session"] == "s1" and e.trailers["Recall-Trigger"] == "Stop"
    assert e.trailers["Recall-Pages"] == "pages/fact/build-and-test.md"


def test_public_file_operations(store):
    store.write_text("seed.json", "{}\n")
    assert (store.root / "seed.json").read_bytes() == b"{}\n"  # LF on every platform
    assert store.read_text("seed.json") == "{}\n"
    assert store.raw_files() == []
    rel = store.add_raw("s1", "note", "hello")
    assert store.raw_files() == [rel]
    store.ingest([page()], Meta("ingest", "s1"))
    (store.root / "index.md").write_text("broken", encoding="utf-8")
    store.write_index()
    assert "[[pages/fact/build-and-test]]" in store.read_text("index.md")


def test_append_log_without_session_and_truncated_session(store):
    from datetime import datetime
    when = datetime(2026, 1, 2, 3, 4)
    store.append_log("lint", "2 findings | 1 status change(s)", None, when=when)
    store.append_log("eval", "score=0.50", "x" * 80, when=when, session_max=60)
    log = store.read_text("log.md")
    assert log.endswith("\n## [2026-01-02 03:04] lint | 2 findings | 1 status change(s)\n"
                        f"\n## [2026-01-02 03:04] eval | score=0.50 | session={'x' * 60}\n")
