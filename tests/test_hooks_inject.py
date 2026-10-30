import json

from helpers import page
from recall.hooks import run
from recall.store import Meta


def ctx(r):
    return json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"]


def test_session_start_and_prompt(store):
    store.ingest([page()], Meta("ingest", "s0"))
    r = run("SessionStart", json.dumps({"session_id": "s1"}), str(store.repo_root))
    assert "# Recall index" in ctx(r)
    assert json.loads(r.stdout)["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    p = json.dumps({"session_id": "s1", "prompt": "how do I run pytest"})
    assert "Run `uv run pytest`" in ctx(run("UserPromptSubmit", p, str(store.repo_root)))
    assert run("UserPromptSubmit", p, str(store.repo_root)).stdout == ""


def test_not_git_repo(tmp_path):
    assert "Recall is off" in ctx(run("SessionStart", "{}", str(tmp_path)))
    assert run("UserPromptSubmit", "{}", str(tmp_path)).stdout == ""


def test_post_compact_state_reinjects_index(store):
    from recall.localindex import LocalIndex
    store.ingest([page()], Meta("ingest", "s0"))
    run("SessionStart", json.dumps({"session_id": "s1"}), str(store.repo_root))
    LocalIndex(store.repo_root).set_state("post_compact:s1", "1")
    text = ctx(run("UserPromptSubmit", json.dumps({"session_id": "s1", "prompt": "zzz"}), str(store.repo_root)))
    assert text.startswith("# Recall index")
    assert LocalIndex(store.repo_root).get_state("post_compact:s1") is None


def test_bad_stdin_never_raises(store):
    r = run("UserPromptSubmit", "not json", str(store.repo_root))
    assert r.exit_code == 0
