import json
import subprocess

from recall import hooks
from recall.hooks import HookResult, run
from recall.paths import local_dir


def test_handler_exception_never_raises(store, monkeypatch):
    def boom(event, data, s):
        raise RuntimeError("kaboom")
    monkeypatch.setitem(hooks._HANDLERS, "SessionStart", boom)
    r = run("SessionStart", json.dumps({"session_id": "s1"}), str(store.repo_root))
    assert r == HookResult() and r.exit_code == 0
    log = (local_dir(store.repo_root) / "errors.log").read_text(encoding="utf-8")
    assert "kaboom" in log and "SessionStart" in log


def test_lock_busy_on_init_is_swallowed(repo):
    store_dir = repo / ".devin" / "recall"
    store_dir.mkdir(parents=True)
    (store_dir / ".lock").write_text("{}", encoding="utf-8")
    r = run("SessionStart", "{}", str(repo))
    assert r == HookResult()
    assert not (local_dir(repo) / "errors.log").exists()


def test_store_folder_resolves_to_owning_repo(store):
    r = run("SessionStart", json.dumps({"session_id": "s1"}), str(store.root))
    text = json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"]
    assert "# Recall index" in text
    assert not (store.root / ".devin").exists()


def test_store_folder_without_git_parent_is_off(tmp_path, monkeypatch):
    monkeypatch.setenv("RECALL_LOCAL_DIR", str(tmp_path / "local"))
    s = tmp_path / "proj" / ".devin" / "recall"
    s.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=s, check=True, capture_output=True)
    r = run("SessionStart", "{}", str(s))
    assert "Recall is off" in json.loads(r.stdout)["hookSpecificOutput"]["additionalContext"]
    assert not (s / ".devin").exists()
