import shutil
import subprocess

import pytest


def _git(cwd, *args):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "core.autocrlf=false", *args],
                   cwd=cwd, check=True, capture_output=True)


@pytest.fixture(scope="session")
def _templates(tmp_path_factory):
    """Build one code repo and one initialized store per test run; tests get copies (git is slow)."""
    base = tmp_path_factory.mktemp("template")
    r = base / "my repo"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    (r / "pyproject.toml").write_text('[project]\nname = "demo"\n', encoding="utf-8", newline="\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "init")
    plain = base / "plain"
    shutil.copytree(r, plain)
    mp = pytest.MonkeyPatch()
    mp.setenv("RECALL_LOCAL_DIR", str(base / "local"))
    try:
        from recall.store import Store
        Store(r).init()
    finally:
        mp.undo()
    return {"repo": plain, "store": r / ".devin" / "recall"}


@pytest.fixture
def repo(tmp_path, monkeypatch, _templates):
    r = tmp_path / "my repo"
    shutil.copytree(_templates["repo"], r)
    monkeypatch.setenv("RECALL_LOCAL_DIR", str(tmp_path / "local"))
    return r


@pytest.fixture(autouse=True)
def _no_real_devin_db(tmp_path, monkeypatch):
    # Never let any test reach the real Devin sessions.db (SessionStart seeds from it).
    monkeypatch.setenv("RECALL_DEVIN_DIR", str(tmp_path / "devin-home"))
    monkeypatch.setenv("RECALL_DEVIN_CONFIG", str(tmp_path / "devin-config.json"))


@pytest.fixture(autouse=True)
def _short_init_lock_wait(monkeypatch):
    # Store.init waits up to 5 s for a busy lock; tests that hold the lock do not need the real wait.
    monkeypatch.setattr("recall.store.INIT_LOCK_WAIT_S", 0.2)


@pytest.fixture
def store(repo, _templates):
    """An initialized store (a copy of the template; same content as Store(repo).init())."""
    from recall.store import Store
    shutil.copytree(_templates["store"], repo / ".devin" / "recall")
    return Store(repo)
