import datetime as dt
import json
import subprocess

from helpers import page
from test_seed import _seed_commits, _setup

from recall.history import GitError
from recall.hooks import run
from recall.maintenance import lint_mechanical
from recall.localindex import LocalIndex
from recall.store import Meta

P = "pages/fact/build-and-test.md"


def _ctx(res) -> str:
    return json.loads(res.stdout)["hookSpecificOutput"]["additionalContext"]


def _committed(store, ref="HEAD") -> set[str]:
    out = subprocess.run(["git", "show", "--name-only", "--format=", ref], cwd=store.root,
                         capture_output=True, text=True, check=True).stdout
    return {ln.strip() for ln in out.splitlines() if ln.strip()}


def _untracked(store) -> set[str]:
    out = subprocess.run(["git", "ls-files", "--others", "--exclude-standard"], cwd=store.root,
                         capture_output=True, text=True, check=True).stdout
    return {ln.strip() for ln in out.splitlines() if ln.strip()}


# ---------- 1. SessionStart order ----------
def test_page_retired_at_session_start_not_in_injected_index(store):
    store.ingest([page()], Meta("ingest", "s1"))
    idx = LocalIndex(store.repo_root)
    idx.mark(P, "wrong")
    idx.mark(P, "wrong")
    ctx = _ctx(run("SessionStart", json.dumps({"session_id": "s2"}), str(store.repo_root)))
    assert "retired" in ctx  # the lint section ran and retired the page
    index_part = ctx.split("Lint findings:")[0]
    assert "build-and-test" not in index_part.split("Use recall_search")[0]
    assert idx.search("pytest") == []  # search index rebuilt after the lint


# ---------- 2. commit scope ----------
def test_ingest_commit_leaves_pending_raw_untracked(store):
    raw = store.add_raw("s1", "final", "a pending record")
    (store.root / "stray.txt").write_text("not owned", encoding="utf-8")
    res = store.ingest([page()], Meta("ingest", "s1"))
    files = _committed(store, res.commit)
    assert files == {P, "index.md", "log.md"}
    assert {raw, "stray.txt"} <= _untracked(store)


def test_ingest_commit_includes_consumed(store):
    raw = store.add_raw("s1", "final", "nothing durable")
    store.capture_commit(Meta("capture", "s1", "Stop"))
    other = store.add_raw("s1", "final", "another pending one")
    res = store.ingest([], Meta("ingest", "s1"), consumed=[raw])
    assert _committed(store, res.commit) == {"log.md", "consumed.txt"}  # index.md unchanged
    assert other in _untracked(store)


def test_lint_commit_scoped(store):
    store.ingest([page()], Meta("ingest", "s1"))
    raw = store.add_raw("s1", "final", "pending during lint")
    (store.repo_root / "pyproject.toml").write_text("[project]\nname='y'\n")
    lint_mechanical(store, LocalIndex(store.repo_root), dt.date(2026, 10, 30))
    assert store.history.log(limit=1)[0].trailers["Recall-Op"] == "lint"
    assert _committed(store) <= {P, "index.md", "log.md"} and P in _committed(store)
    assert raw in _untracked(store)


def test_capture_commit_scoped(store):
    raw = store.add_raw("s1", "final", "captured")
    (store.root / "stray.txt").write_text("not owned", encoding="utf-8")
    sha = store.capture_commit(Meta("capture", "s1", "Stop"))
    assert _committed(store, sha) == {raw, "log.md"}
    assert "stray.txt" in _untracked(store)


def test_commit_paths_nothing_staged_returns_none(store):
    store.add_raw("s1", "final", "pending only")
    assert store.history.commit(Meta("lint"), "noop", paths=["index.md"]) is None


# ---------- 3. observed-limit cache ----------
class FakeReader:
    def __init__(self, model="opus-5-5-low"):
        self.model = model
        self.calls = 0

    def token_count(self, sid):
        return 1000

    def transcript_status(self, sid):
        return None

    def session_model(self, sid):
        return self.model

    def compaction_samples(self):
        self.calls += 1
        return [("opus-5-5-high", 400_000)] * 3


def test_observed_limit_cached_per_model_for_a_day(repo, monkeypatch):
    from recall import context_gauge as cg
    idx = LocalIndex(repo)
    reader = FakeReader()
    now = [1_000_000.0]
    monkeypatch.setattr(cg.time, "time", lambda: now[0])
    for _ in range(3):
        s = cg.context_status(reader, "s1", {}, {}, {}, index=idx)
        assert (s["source"], s["limit"]) == ("observed", 400_000)
    assert reader.calls == 1
    reader.model = "swe-1-6-high"  # another model: its own cache entry (no samples -> default)
    assert cg.context_status(reader, "s1", {}, {}, {}, index=idx)["source"] == "default"
    cg.context_status(reader, "s1", {}, {}, {}, index=idx)
    assert reader.calls == 2
    reader.model = "opus-5-5-max"
    now[0] += 86_400 + 1  # TTL over
    cg.context_status(reader, "s1", {}, {}, {}, index=idx)
    assert reader.calls == 3
    cg.context_status(reader, "s1", {}, {}, {})  # no index: no cache
    assert reader.calls == 4


# ---------- 4. seed robustness ----------
def test_seed_commit_failure_leaves_session_unseen(tmp_path, store, monkeypatch):
    from recall.seed import _load, seed_batch
    from recall.session_reader import SessionReader
    _setup(tmp_path, store)
    idx = LocalIndex(store.repo_root)
    real = store.history.commit

    def boom(*a, **k):
        raise GitError("disk full")
    monkeypatch.setattr(store.history, "commit", boom)
    res = seed_batch(store, SessionReader(), idx, "cur", batch=1)
    assert res["imported"] == 0
    assert _load(store)["seen"] == []
    monkeypatch.setattr(store.history, "commit", real)
    res = seed_batch(store, SessionReader(), idx, "cur", batch=1)
    assert res["imported"] == 1 and _load(store)["seen"] == ["s0"]
    assert len(_seed_commits(store)) == 1
    files = _committed(store)
    assert {"log.md", "seed.json"} <= files and all(f.startswith("raw/") or f in ("log.md", "seed.json")
                                                     for f in files)


# ---------- 5. append_log ----------
def test_append_log(store):
    store.append_log("seed", "My\ntitle", "s-1", when=dt.datetime(2026, 1, 2, 3, 4))
    text = (store.root / "log.md").read_text(encoding="utf-8")
    assert text.endswith("\n## [2026-01-02 03:04] seed | My title | session=s-1\n")


# ---------- 6. hook registrations ----------
def test_registrations_are_named_functions():
    from recall import hooks
    for fn in [*hooks.SESSION_START_SECTIONS, *hooks.PROMPT_SECTIONS, *hooks._HANDLERS.values()]:
        assert fn.__name__ != "<lambda>"
    assert len(hooks.SESSION_START_SECTIONS) == 5 and len(hooks.PROMPT_SECTIONS) == 1
