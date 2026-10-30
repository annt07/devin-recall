"""Devin plugin packaging: hooks.json, plugin.json, .mcp.json, the plugin SKILL.md,
the --via-plugin hook path and the recall_enable MCP tool."""
import json
import tomllib
from pathlib import Path

import pytest

from conftest import _git

PROJECT = Path(__file__).resolve().parents[1]
PLUGIN_JSON = PROJECT / ".devin-plugin" / "plugin.json"
HOOKS_JSON = PROJECT / "hooks.json"
MCP_JSON = PROJECT / ".mcp.json"
PLUGIN_SKILL = PROJECT / "skills" / "recall" / "SKILL.md"
PKG_JSON = PROJECT / "package.json"
BIN_JS = PROJECT / "bin" / "recall.js"


def _pyproject():
    return tomllib.loads((PROJECT / "pyproject.toml").read_text(encoding="utf-8"))


def _hook_commands(entries):
    return [h["command"] for e in entries for h in e["hooks"]]


# ---------- plugin manifest and component files ----------

def test_plugin_json_manifest():
    data = json.loads(PLUGIN_JSON.read_text(encoding="utf-8"))
    assert data["name"] == "recall"
    assert data["version"] == _pyproject()["project"]["version"]
    assert data["repository"] == "https://github.com/annt07/devin-recall"
    assert data.get("description")


def test_hooks_json_matches_install_events():
    """The plugin hook file covers exactly the events install.py writes, with the same
    matcher and timeouts, but with --via-plugin commands and no --repo."""
    from recall.install import HOOK_EVENTS, SHORT_TIMEOUT, WRITE_GUARD_MATCHER
    hooks = json.loads(HOOKS_JSON.read_text(encoding="utf-8"))
    assert set(hooks) == set(HOOK_EVENTS)
    for ev in HOOK_EVENTS:
        entries = hooks[ev]
        assert len(entries) == 1, ev
        cmds = _hook_commands(entries)
        assert len(cmds) == 1
        cmd = cmds[0]
        assert f"recall hook {ev}" in cmd
        assert "--via-plugin" in cmd
        assert "--repo" not in cmd
        assert "${PLUGIN_ROOT}" in cmd
        timeout = entries[0]["hooks"][0]["timeout"]
        assert timeout == (5 if ev in SHORT_TIMEOUT else 30), ev
        if ev == "PreToolUse":
            assert entries[0]["matcher"] == WRITE_GUARD_MATCHER
        else:
            assert "matcher" not in entries[0]


def test_mcp_json_server():
    data = json.loads(MCP_JSON.read_text(encoding="utf-8"))
    server = data["mcpServers"]["recall"]
    assert server["command"] == "uv"
    args = server["args"]
    assert args[:3] == ["run", "--frozen", "--quiet"]
    assert args[-1] == "mcp"
    assert "${PLUGIN_ROOT}" in " ".join(args)


def test_plugin_root_ships_no_rules():
    """The plugin root must not ship always-on rule files (Devin injects them everywhere);
    the repo's contributor rules live in .devin/global_rules.md instead."""
    for name in ("AGENTS.md", "AGENT.md", "agents.md", ".windsurfrules"):
        assert not (PROJECT / name).exists(), name
    assert (PROJECT / ".devin" / "global_rules.md").is_file()


def test_plugin_skill_is_template_byte_identical():
    from importlib import resources
    template = (resources.files("recall") / "templates" / "skill" / "SKILL.md").read_bytes()
    assert PLUGIN_SKILL.read_bytes() == template


def test_npx_package():
    data = json.loads(PKG_JSON.read_text(encoding="utf-8"))
    assert data["name"] == "devin-recall"
    assert data["private"] is True
    assert data["version"] == _pyproject()["project"]["version"]
    assert data["bin"] == {"recall": "bin/recall.js"}
    text = BIN_JS.read_text(encoding="utf-8")
    assert "uvx" in text and "RECALL_SOURCE" in text


# ---------- --via-plugin hook behavior ----------

def _ctx(result):
    return json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]


def test_via_plugin_no_store_is_silent_noop(repo):
    from recall.hooks import run
    for ev in ("UserPromptSubmit", "Stop", "PreToolUse", "PostCompaction", "PreCompact", "SessionEnd"):
        res = run(ev, '{"session_id": "p1"}', str(repo), via_plugin=True)
        assert res.stdout == "" and res.exit_code == 0, ev
    assert not (repo / ".devin" / "recall").exists()


def test_via_plugin_no_store_session_start_hint(repo):
    from recall.hooks import run
    res = run("SessionStart", '{"session_id": "p2"}', str(repo), via_plugin=True)
    assert "recall_enable" in _ctx(res)
    assert not (repo / ".devin" / "recall").exists()


def test_via_plugin_silent_outside_git(tmp_path):
    from recall.hooks import run
    res = run("SessionStart", "{}", str(tmp_path), via_plugin=True)
    assert res.stdout == "" and res.exit_code == 0


def test_via_plugin_noop_when_repo_install_present(repo):
    """A repo install owns the hooks: the plugin copy must never run twice."""
    from recall.hooks import run
    d = repo / ".devin"
    d.mkdir()
    cfg = {"hooks": {"SessionStart": [{"hooks": [
        {"type": "command", "command": "recall hook SessionStart --repo /x"}]}]}}
    (d / "config.local.json").write_text(json.dumps(cfg), encoding="utf-8")
    for ev in ("SessionStart", "UserPromptSubmit", "Stop"):
        res = run(ev, '{"session_id": "p3"}', str(repo), via_plugin=True)
        assert res.stdout == "", ev
    assert not (repo / ".devin" / "recall").exists()


def test_via_plugin_runs_with_store(repo, store):
    from recall.hooks import run
    res = run("SessionStart", '{"session_id": "p4"}', str(repo), via_plugin=True)
    assert "# Recall index" in _ctx(res)


def test_via_plugin_flag_cli(repo, monkeypatch, capsys):
    import io
    from recall.cli import main
    monkeypatch.setattr("sys.stdin", io.TextIOWrapper(io.BytesIO(b"{}"), encoding="utf-8"))
    # stdin wrapper needs .buffer for the CLI read
    class _Stdin:
        buffer = io.BytesIO(b"{}")
    monkeypatch.setattr("sys.stdin", _Stdin())
    assert main(["hook", "SessionStart", "--repo", str(repo), "--via-plugin"]) == 0
    out = capsys.readouterr().out
    assert "recall_enable" in out


# ---------- recall_enable ----------

def test_enable_creates_store_and_exclude(repo):
    from recall.install import enable
    res = enable(repo)
    assert res["repo"] == str(repo.resolve())
    assert (repo / ".devin" / "recall" / "index.md").is_file()
    excl = (repo / ".git" / "info" / "exclude").read_text(encoding="utf-8").splitlines()
    assert ".devin/recall/" in excl
    # No repo-install files: the plugin provides hooks, MCP config and the skill.
    assert not (repo / ".devin" / "config.local.json").exists()
    assert not (repo / ".devin" / "mcp_config.local.json").exists()
    assert not (repo / ".devin" / "skills" / "recall").exists()


def test_enable_is_idempotent(repo):
    from recall.install import enable
    enable(repo)
    res = enable(repo)
    excl = (repo / ".git" / "info" / "exclude").read_text(encoding="utf-8").splitlines()
    assert excl.count(".devin/recall/") == 1
    assert res["created"] is False


def test_enable_refuses_tracked_store(repo):
    from recall.install import enable
    d = repo / ".devin" / "recall"
    d.mkdir(parents=True)
    (d / "x.md").write_text("x", encoding="utf-8")
    _git(repo, "add", "-f", ".devin/recall/x.md")
    with pytest.raises(RuntimeError, match="tracked"):
        enable(repo)


def test_enable_refuses_non_git(tmp_path):
    from recall.install import enable
    with pytest.raises(RuntimeError):
        enable(tmp_path)


def test_mcp_recall_enable(repo, monkeypatch):
    monkeypatch.setenv("RECALL_REPO", str(repo))
    from recall.mcp_server import recall_enable
    res = recall_enable()
    assert res["repo"] == str(repo.resolve())
    assert (repo / ".devin" / "recall" / "index.md").is_file()


def test_mcp_repo_root_prefers_recall_repo(tmp_path, monkeypatch):
    from recall import mcp_server
    monkeypatch.setenv("RECALL_REPO", str(tmp_path))
    monkeypatch.setenv("DEVIN_PROJECT_DIR", str(tmp_path / "other"))
    assert mcp_server._repo_root() == tmp_path.resolve()


def test_mcp_repo_root_uses_devin_project_dir(repo, monkeypatch):
    from recall import mcp_server
    monkeypatch.delenv("RECALL_REPO", raising=False)
    monkeypatch.setenv("DEVIN_PROJECT_DIR", str(repo))
    assert mcp_server._repo_root() == repo.resolve()


# ---------- MCP: log.md and read-before-consume ----------

def _raw(store_root, name, text="raw text"):
    d = store_root / "raw"
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_text(text, encoding="utf-8")
    return f"raw/{name}"


@pytest.fixture
def mcp(repo, store, monkeypatch):
    import recall.mcp_server as ms
    monkeypatch.setenv("RECALL_REPO", str(repo))
    ms._READ_RAW.clear()
    return ms


def test_get_log_md(mcp, store):
    res = mcp.recall_get(path="log.md")
    assert res["path"] == "log.md" and "text" in res


def _page(sources=None):
    return {"path": "pages/fact/x.md",
            "front": {"title": "x", "type": "fact", "summary": "s", "confidence": "high",
                      "sources": sources or ["src/a.py"]},
            "body": "body"}


def test_ingest_rejects_unread_consumed(mcp, store):
    raw = _raw(store.root, "r1.md")
    res = mcp.recall_ingest(pages=[_page()], session="s1", consumed=[raw])
    assert "unread raw records" in res["error"] and raw in res["error"]


def test_ingest_rejects_unread_cited(mcp, store):
    raw = _raw(store.root, "r2.md")
    res = mcp.recall_ingest(pages=[_page([raw])], session="s1")
    assert raw in res["error"]


def test_ingest_passes_after_recall_get(mcp, store):
    raw = _raw(store.root, "r3.md")
    mcp.recall_get(path=raw)
    res = mcp.recall_ingest(pages=[_page([raw])], session="s1", consumed=[raw])
    assert "error" not in res and res["accepted"] == ["pages/fact/x.md"]


def test_ingest_names_only_unread(mcp, store):
    read = _raw(store.root, "r4.md")
    unread = _raw(store.root, "r5.md")
    mcp.recall_get(path=read)
    res = mcp.recall_ingest(pages=[_page()], session="s1", consumed=[read, unread])
    assert unread in res["error"] and read not in res["error"]


def test_server_restart_clears_read_set(mcp, store):
    raw = _raw(store.root, "r6.md")
    mcp.recall_get(path=raw)
    mcp._READ_RAW.clear()  # a new MCP process starts empty
    res = mcp.recall_ingest(pages=[_page()], session="s1", consumed=[raw])
    assert "unread raw records" in res["error"]


def test_ingest_reject_makes_no_commit(mcp, store):
    from recall.history import History
    raw = _raw(store.root, "r7.md")
    before = History(store.root).log(limit=50)
    res = mcp.recall_ingest(pages=[_page([raw])], session="s1", consumed=[raw])
    assert "unread raw records" in res["error"]
    assert len(History(store.root).log(limit=50)) == len(before)


# ---------- uvx launcher env ----------

def test_launcher_uvx_source_tool_installs(repo, monkeypatch, tmp_path):
    """`recall install` under `uvx --from <src>` runs `uv tool install <src>` once and hooks the
    installed `recall` executable: uvx re-resolves a git source over the network on every call."""
    import subprocess

    import recall.install as ins
    monkeypatch.setattr(ins, "_dev_project", lambda: None)
    src = "git+ssh://git@example.com/o/r.git"
    monkeypatch.setenv("RECALL_UVX_FROM", src)
    uv = str((tmp_path / "uv").resolve())
    exe = str((tmp_path / "bin" / "recall").resolve())
    calls = []

    real_run = subprocess.run

    def fake_run(argv, **kw):
        if argv[0] == "git":
            return real_run(argv, **kw)
        calls.append(argv)
        if argv[1:3] == ["tool", "dir"]:
            return subprocess.CompletedProcess(argv, 0, str(tmp_path / "tools"), "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(ins.subprocess, "run", fake_run)
    monkeypatch.setattr(ins.shutil, "which", lambda name: {"uv": uv, "recall": exe}.get(name))
    ins.install(repo)
    assert calls[0][:4] == [uv, "tool", "install", "--force"] or calls[0][:3] == [uv, "tool", "install"]
    assert src in calls[0]
    hooks = json.loads((repo / ".devin" / "config.local.json").read_text(encoding="utf-8"))["hooks"]
    cmd = _hook_commands(hooks["Stop"])[0]
    assert cmd.startswith(f"{ins._q(exe)} hook Stop --repo ") and "--from" not in cmd
    mcp = json.loads((repo / ".devin" / "mcp_config.local.json").read_text(encoding="utf-8"))
    assert mcp["mcpServers"]["recall"]["command"] == exe
    assert mcp["mcpServers"]["recall"]["args"] == ["mcp"]


def test_launcher_uvx_source_install_failure(repo, monkeypatch, tmp_path):
    import subprocess

    import recall.install as ins
    monkeypatch.setattr(ins, "_dev_project", lambda: None)
    monkeypatch.setenv("RECALL_UVX_FROM", "git+ssh://git@example.com/o/r.git")
    uv = str((tmp_path / "uv").resolve())
    monkeypatch.setattr(ins.shutil, "which", lambda name: uv if name == "uv" else None)
    real_run = subprocess.run

    def fake_run(argv, **kw):
        if argv[0] == "git":
            return real_run(argv, **kw)
        return subprocess.CompletedProcess(argv, 1, "", "fetch failed")

    monkeypatch.setattr(ins.subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match="uv tool install"):
        ins.install(repo)
