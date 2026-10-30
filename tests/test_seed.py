import json
import time
from datetime import datetime, timezone

from fixtures_sessions import make_db

from recall.hooks import run
from recall.localindex import LocalIndex

DAY_MS = 86_400_000


def _ctx(res) -> str:
    return json.loads(res.stdout)["hookSpecificOutput"]["additionalContext"]


def _setup(tmp_path, store, current="cur"):
    now = int(time.time() * 1000)
    repo = str(store.repo_root)
    rows = [{"id": f"s{i}", "cwd": repo, "created": now - (i + 1) * DAY_MS, "title": f"T{i}",
             "messages": [("user", f"prompt {i}"), ("assistant", f"answer {i}")]} for i in range(5)]
    rows.append({"id": "old", "cwd": repo, "created": now - 200 * DAY_MS,
                 "messages": [("user", "ancient")]})
    rows.append({"id": current, "cwd": repo, "created": now, "messages": [("user", "live now")]})
    db_dir = tmp_path / "devin-home" / "devin" / "cli"
    db_dir.mkdir(parents=True, exist_ok=True)
    make_db(db_dir / "sessions.db", rows)
    return {r["id"]: r for r in rows}


def _seed_commits(store):
    return [e for e in store.history.log(op="seed", limit=100)]


def test_seed_in_batches_backdated(tmp_path, store):
    rows = _setup(tmp_path, store)
    R = str(store.repo_root)
    ctx = _ctx(run("SessionStart", json.dumps({"session_id": "cur"}), R))
    assert "Seed: 3 sessions imported, 2 remaining" in ctx
    assert "recall_ingest" in ctx
    commits = _seed_commits(store)
    assert len(commits) == 3
    for e in commits:
        assert e.trailers["Recall-Trigger"] == "Seed"
        sid = e.trailers["Recall-Session"]
        assert sid in {"s0", "s1", "s2"}
        want = datetime.fromtimestamp(rows[sid]["created"] / 1000, timezone.utc)
        assert abs(datetime.fromisoformat(e.date).timestamp() - want.timestamp()) < 2
    state = json.loads((store.root / "seed.json").read_text(encoding="utf-8"))
    assert sorted(state["seen"]) == ["s0", "s1", "s2"]
    assert state["not_read"] == ["old"] and state["complete"] is False
    assert LocalIndex(store.repo_root).get_state("lint_due") is None

    ctx2 = _ctx(run("SessionStart", json.dumps({"session_id": "cur"}), R))
    assert "Seed: 2 sessions imported, 0 remaining" in ctx2
    state = json.loads((store.root / "seed.json").read_text(encoding="utf-8"))
    assert sorted(state["seen"]) == ["s0", "s1", "s2", "s3", "s4"] and state["complete"] is True
    assert LocalIndex(store.repo_root).get_state("lint_due") == "1"
    assert len(_seed_commits(store)) == 5

    ctx3 = _ctx(run("SessionStart", json.dumps({"session_id": "cur"}), R))
    assert "Seed:" not in ctx3
    assert len(_seed_commits(store)) == 5
    all_sessions = {e.trailers["Recall-Session"] for e in _seed_commits(store)}
    assert "cur" not in all_sessions and "old" not in all_sessions
    assert not any("live now" in p.read_text(encoding="utf-8") for p in (store.root / "raw").glob("*.md"))


def test_seed_lock_busy_keeps_state(tmp_path, store):
    from recall.seed import seed_batch
    from recall.session_reader import SessionReader
    _setup(tmp_path, store)
    idx = LocalIndex(store.repo_root)
    with store.lock("other"):
        res = seed_batch(store, SessionReader(), idx, "cur", wait=0.1)
    assert res["imported"] == 0 and res["busy"] is True
    assert not (store.root / "seed.json").exists()
    assert _seed_commits(store) == []


def test_seed_without_db_is_silent(store):
    ctx = _ctx(run("SessionStart", json.dumps({"session_id": "cur"}), str(store.repo_root)))
    assert "Seed:" not in ctx
