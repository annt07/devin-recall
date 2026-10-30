import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from conftest import _git

PROJECT = Path(__file__).resolve().parents[1]
EVENTS = ["SessionStart", "UserPromptSubmit", "Stop", "PreCompact", "PostCompaction", "SessionEnd", "PreToolUse"]


def _hooks(repo):
    return json.loads((repo / ".devin" / "config.local.json").read_text(encoding="utf-8"))["hooks"]


def _commands(entries):
    return [h["command"] for e in entries for h in e["hooks"]]


def test_install_writes_hooks(repo):
    from recall.install import install
    install(repo, PROJECT)
    hooks = _hooks(repo)
    for ev in EVENTS:
        cmds = _commands(hooks[ev])
        assert len(cmds) == 1
        assert f"recall hook {ev} --repo" in cmds[0]
        assert f'--project "{PROJECT}"' in cmds[0] or "--project '" in cmds[0]
    assert hooks["PreToolUse"][0]["matcher"] == "^(write|edit|multi_edit|apply_patch)$"
    for ev in EVENTS[:-1]:
        assert "matcher" not in hooks[ev][0]
    assert hooks["UserPromptSubmit"][0]["hooks"][0]["timeout"] == 5
    assert hooks["PreToolUse"][0]["hooks"][0]["timeout"] == 5
    assert hooks["Stop"][0]["hooks"][0]["timeout"] == 30


def test_install_merges_and_is_idempotent(repo):
    from recall.install import install
    d = repo / ".devin"
    d.mkdir()
    foreign_hook = {"hooks": [{"type": "command", "command": "echo hi"}]}
    (d / "config.local.json").write_text(json.dumps({"other": 1, "hooks": {"Stop": [foreign_hook]}}), encoding="utf-8")
    (d / "mcp_config.local.json").write_text(json.dumps({"mcpServers": {"x": {"command": "x"}}, "keep": True}),
                                            encoding="utf-8")
    install(repo, PROJECT)
    install(repo, PROJECT)
    cfg = json.loads((d / "config.local.json").read_text(encoding="utf-8"))
    assert cfg["other"] == 1
    assert cfg["hooks"]["Stop"][0] == foreign_hook
    assert len(cfg["hooks"]["Stop"]) == 2
    for ev in EVENTS:
        assert sum("recall hook" in c for c in _commands(cfg["hooks"][ev])) == 1
    mcp = json.loads((d / "mcp_config.local.json").read_text(encoding="utf-8"))
    assert mcp["keep"] is True and mcp["mcpServers"]["x"] == {"command": "x"}
    rec = mcp["mcpServers"]["recall"]
    assert rec["env"]["RECALL_REPO"] == str(repo.resolve())
    assert rec["args"][-1] == "mcp"
    excl = (repo / ".git" / "info" / "exclude").read_text(encoding="utf-8").splitlines()
    for p in (".devin/recall/", ".devin/skills/recall/", ".devin/config.local.json", ".devin/mcp_config.local.json"):
        assert excl.count(p) == 1
    assert (d / "skills" / "recall" / "SKILL.md").read_text(encoding="utf-8").startswith("---")
    assert (d / "recall" / "index.md").is_file()


def test_install_refuses_tracked_files(repo):
    from recall.install import install
    d = repo / ".devin"
    d.mkdir()
    (d / "config.local.json").write_text("{}", encoding="utf-8")
    _git(repo, "add", "-f", ".devin/config.local.json")
    with pytest.raises(RuntimeError, match="tracked"):
        install(repo, PROJECT)
    assert json.loads((d / "config.local.json").read_text(encoding="utf-8")) == {}


def test_install_refuses_non_git(tmp_path):
    from recall.install import install
    with pytest.raises(RuntimeError):
        install(tmp_path, PROJECT)


def test_tool_mode_uses_absolute_recall(repo, monkeypatch, tmp_path):
    import recall.install as ins
    exe = str((tmp_path / "recall").resolve())
    monkeypatch.setattr(ins, "_dev_project", lambda: None)
    monkeypatch.setattr(ins.shutil, "which", lambda name: exe)
    ins.install(repo)
    cmd = _commands(_hooks(repo)["Stop"])[0]
    assert cmd.startswith(f"{ins._q(exe)} hook Stop --repo ")
    mcp = json.loads((repo / ".devin" / "mcp_config.local.json").read_text(encoding="utf-8"))
    assert mcp["mcpServers"]["recall"]["command"] == exe
    assert mcp["mcpServers"]["recall"]["args"] == ["mcp"]


def test_default_mode_in_dev_checkout_uses_this_python(repo):
    import sys
    from recall.install import install
    install(repo)
    cmd = _commands(_hooks(repo)["Stop"])[0]
    assert " -m recall hook Stop --repo " in cmd and "python" in cmd.lower()
    mcp = json.loads((repo / ".devin" / "mcp_config.local.json").read_text(encoding="utf-8"))
    assert mcp["mcpServers"]["recall"]["command"] == sys.executable


def test_quote_posix_and_windows(monkeypatch):
    import recall.install as ins
    monkeypatch.setattr(ins, "_windows", lambda: True)
    assert ins._q(r"C:\a b\c") == r'"C:\a b\c"'
    monkeypatch.setattr(ins, "_windows", lambda: False)
    assert ins._q("/a b/c") == "'/a b/c'"
    assert ins._q("/a/c") == "/a/c"


def test_cli_install(repo):
    from recall.cli import main
    assert main(["install", str(repo), "--tool-project", str(PROJECT)]) == 0
    assert (repo / ".devin" / "config.local.json").is_file()


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv not on PATH")
def test_e2e_real_hook_command(repo):
    from recall.install import install
    install(repo, PROJECT)
    env = {k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"}
    p = subprocess.run(["uv", "run", "--frozen", "--project", str(PROJECT), "recall", "hook", "SessionStart",
                        "--repo", str(repo)], input='{"session_id":"e2e"}', capture_output=True, text=True,
                       encoding="utf-8", env=env, timeout=120)
    assert p.returncode == 0, p.stderr
    assert "# Recall index" in p.stdout


def test_git_timeout_and_exclude_rev_parse_failure(monkeypatch, tmp_path):
    from recall import install as inst

    def slow(*a, **k):
        assert k['timeout'] == 30
        raise subprocess.TimeoutExpired(a[0], 30)
    monkeypatch.setattr(inst.subprocess, 'run', slow)
    with pytest.raises(RuntimeError, match='timed out'):
        inst._git(tmp_path, 'status')
    monkeypatch.setattr(inst, '_git', lambda *a: subprocess.CompletedProcess(a, 128, '', 'boom'))
    with pytest.raises(RuntimeError, match='exclude'):
        inst._add_excludes(tmp_path)
