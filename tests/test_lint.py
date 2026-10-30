import datetime as dt
import json

from helpers import page
from recall.hooks import run
from recall.localindex import LocalIndex
from recall.maintenance import format_findings, lint_mechanical
from recall.store import Meta

P = "pages/fact/build-and-test.md"


def test_stale_and_retire(store):
    store.ingest([page()], Meta("ingest", "s1")); idx = LocalIndex(store.repo_root)
    (store.repo_root / "pyproject.toml").write_text("[project]\nname='y'\n")
    f = lint_mechanical(store, idx, dt.date(2026, 10, 30))
    assert (P, "anchor-changed") in [(x.path, x.rule) for x in f]
    front = store.read_page(P)[0]
    assert front["status"] == "stale" and str(front["stale_since"]) == "2026-10-30"
    assert "(stale)" in (store.root / "index.md").read_text()
    assert store.history.log(op="lint", limit=1)
    lint_mechanical(store, idx, dt.date(2026, 11, 15))  # 16 days: still stale, stale_since kept
    front = store.read_page(P)[0]
    assert front["status"] == "stale" and str(front["stale_since"]) == "2026-10-30"
    lint_mechanical(store, idx, dt.date(2026, 12, 15))
    assert store.read_page(P)[0]["status"] == "retired"
    assert "build-and-test" not in (store.root / "index.md").read_text()
    ctx = json.loads(run("SessionStart", '{"session_id":"s9"}', str(store.repo_root)).stdout)
    assert "anchor" in ctx["hookSpecificOutput"]["additionalContext"]
    assert "Lint findings:" in ctx["hookSpecificOutput"]["additionalContext"]


def test_anchor_missing(store):
    store.ingest([page()], Meta("ingest", "s1")); idx = LocalIndex(store.repo_root)
    (store.repo_root / "pyproject.toml").unlink()
    f = lint_mechanical(store, idx, dt.date(2026, 10, 30))
    assert (P, "anchor-missing") in [(x.path, x.rule) for x in f]
    assert store.read_page(P)[0]["status"] == "stale"


def test_wrong_twice_retires(store):
    store.ingest([page()], Meta("ingest", "s1")); idx = LocalIndex(store.repo_root)
    idx.mark(P, "wrong")
    lint_mechanical(store, idx, dt.date(2026, 10, 30))
    assert store.read_page(P)[0]["status"] == "active"
    idx.mark(P, "wrong")
    lint_mechanical(store, idx, dt.date(2026, 10, 30))
    assert store.read_page(P)[0]["status"] == "retired"


def test_broken_link_orphan_unused(store):
    a = page(body="See [[pages/fact/nope]] and [[pages/howto/deploy]].")
    b = page("pages/howto/deploy.md", "Deploy steps", "Push the tag to release the wheel.")
    c = page("pages/gotcha/lonely.md", "Lonely gotcha", "Windows paths with spaces break shells.")
    store.ingest([a, b, c], Meta("ingest", "s1")); idx = LocalIndex(store.repo_root)
    for i in range(20):
        idx.record_injected(f"s{i}", "pages/howto/deploy.md")
    rules = {(x.path, x.rule) for x in lint_mechanical(store, idx, dt.date(2026, 10, 30))}
    assert (P, "broken-link") in rules
    assert ("pages/gotcha/lonely.md", "orphan") in rules
    assert ("pages/howto/deploy.md", "orphan") not in rules
    assert ("pages/howto/deploy.md", "unused") in rules
    assert all(r[1] != "anchor-changed" for r in rules)


def test_section_empty_and_capped(store):
    idx = LocalIndex(store.repo_root)
    today = dt.date.today()
    assert format_findings(lint_mechanical(store, idx, today)) == ""
    store.ingest([page(f"pages/fact/p{i}.md", f"Topic {i} unique{i}", f"Body word{i} alpha{i}.")
                  for i in range(12)], Meta("ingest", "s1"))
    text = format_findings(lint_mechanical(store, idx, today))
    lines = text.splitlines()
    assert lines[0] == "Lint findings:" and len(lines) == 11
    assert lines[1].startswith("- pages/fact/p0.md: orphan")


def test_no_lint_section_in_maintenance():
    from recall import maintenance
    assert not hasattr(maintenance, "lint_section")  # the SessionStart lint section lives in hooks


def test_lint_skips_status_change_when_locked(store):
    store.ingest([page()], Meta("ingest", "s1")); idx = LocalIndex(store.repo_root)
    (store.repo_root / "pyproject.toml").write_text("[project]\nname='y'\n")
    with store.lock("other"):
        f = lint_mechanical(store, idx, dt.date(2026, 10, 30), wait=0.1)
    assert (P, "anchor-changed") in [(x.path, x.rule) for x in f]
    assert store.read_page(P)[0]["status"] == "active"
