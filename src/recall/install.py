"""`recall install <repo>`: hooks, MCP config, Skill, git excludes and the store, in one idempotent step."""
from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
from importlib import resources
from pathlib import Path

from recall.store import Store

HOOK_EVENTS = ("SessionStart", "UserPromptSubmit", "Stop", "PreCompact", "PostCompaction", "SessionEnd",
               "PreToolUse")
WRITE_GUARD_MATCHER = "^(write|edit|multi_edit|apply_patch)$"
GIT_TIMEOUT_S = 30
SHORT_TIMEOUT = {"UserPromptSubmit", "PreToolUse"}
EXCLUDES = (".devin/recall/", ".devin/skills/recall/", ".devin/config.local.json", ".devin/mcp_config.local.json")
HOOK_MARK = " hook "  # with "recall" in the command, identifies a Recall hook entry


def _windows() -> bool:
    return os.name == "nt"


def _q(s: str) -> str:
    """Quote one argument for the shell that runs hook commands (cmd.exe on Windows, sh on POSIX)."""
    s = str(s)
    if _windows():
        # cmd.exe: double quotes stop splitting; % starts variable expansion, so double it. A `!` is
        # expanded under delayed expansion and cannot be escaped reliably inside quotes: refuse it.
        if "!" in s or '"' in s:
            raise RuntimeError(f"cannot quote this path for cmd.exe (contains ! or \"): {s}")
        return '"' + s.replace("%", "%%") + '"'
    return shlex.quote(s)


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=GIT_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"git {args[0] if args else ''} timed out after {GIT_TIMEOUT_S}s") from None


def _dev_project() -> Path | None:
    """The source checkout that holds this package, if Recall runs from one (editable / `uv run`)."""
    root = Path(__file__).resolve().parents[2]
    pp = root / "pyproject.toml"
    try:
        if pp.is_file() and 'name = "devin-recall"' in pp.read_text(encoding="utf-8"):
            return root
    except OSError:
        pass
    return None


def _launcher(tool_project) -> list[str]:
    """argv prefix that starts `recall` from a hook or MCP client.

    Always an absolute executable, resolved now: Devin may start hooks with a different PATH."""
    if tool_project is not None:
        uv = shutil.which("uv")
        if uv is None:
            raise RuntimeError("`uv` is not on PATH; it is needed for --tool-project")
        return [str(Path(uv).resolve()), "run", "--frozen", "--project", str(Path(tool_project).resolve()),
                "recall"]
    if _dev_project() is not None:
        return [sys.executable, "-m", "recall"]  # the checkout's own environment
    exe = shutil.which("recall")
    if exe is None:
        raise RuntimeError("`recall` is not on PATH; install it with `uv tool install` or pass tool_project")
    return [str(Path(exe).resolve())]


_LITERAL = frozenset({"run", "--frozen", "--project", "-m", "recall", "hook", "--repo", *HOOK_EVENTS})


def _cmd(argv: list[str]) -> str:
    """One shell command line; every argument except fixed literals is quoted."""
    return " ".join(a if i > 0 and a in _LITERAL else _q(a) for i, a in enumerate(argv))


def _load(path: Path) -> dict:
    if not path.is_file():
        return {}
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return {}
    data = json.loads(text)  # invalid JSON raises: never overwrite a file we cannot read
    if not isinstance(data, dict):
        raise RuntimeError(f"{path} is not a JSON object")
    return data


def _dump(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def _is_recall_entry(entry) -> bool:
    if not isinstance(entry, dict):
        return False
    hooks = entry.get("hooks")
    return isinstance(hooks, list) and any(
        isinstance(h, dict) and "recall" in str(h.get("command", "")) and HOOK_MARK in str(h.get("command", ""))
        and "--repo" in str(h.get("command", "")) for h in hooks)


def _merge_hooks(cfg: dict, launcher: list[str], repo: Path) -> dict:
    hooks = cfg.get("hooks")
    if not isinstance(hooks, dict):
        hooks = {}
    for ev in HOOK_EVENTS:
        cmd = _cmd([*launcher, "hook", ev, "--repo", str(repo)])
        entry: dict = {}
        if ev == "PreToolUse":
            entry["matcher"] = WRITE_GUARD_MATCHER
        entry["hooks"] = [{"type": "command", "command": cmd, "timeout": 5 if ev in SHORT_TIMEOUT else 30}]
        existing = hooks.get(ev)
        existing = [e for e in existing if not _is_recall_entry(e)] if isinstance(existing, list) else []
        hooks[ev] = [*existing, entry]
    cfg["hooks"] = hooks
    return cfg


def _merge_mcp(cfg: dict, launcher: list[str], repo: Path) -> dict:
    servers = cfg.get("mcpServers")
    if not isinstance(servers, dict):
        servers = {}
    servers["recall"] = {"command": launcher[0], "args": [*launcher[1:], "mcp"], "env": {"RECALL_REPO": str(repo)}}
    cfg["mcpServers"] = servers
    return cfg


def _add_excludes(repo: Path) -> None:
    p = _git(repo, "rev-parse", "--git-path", "info/exclude")
    if p.returncode != 0 or not p.stdout.strip():
        raise RuntimeError(f"cannot find the git exclude file: {(p.stderr or '').strip()}")
    path = Path(p.stdout.strip())
    if not path.is_absolute():
        path = repo / path
    lines = path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    missing = [e for e in EXCLUDES if e not in lines]
    if missing:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8", newline="\n") as f:
            if lines and path.read_text(encoding="utf-8")[-1:] != "\n":
                f.write("\n")
            f.write("".join(e + "\n" for e in missing))


def _copy_skill(repo: Path) -> None:
    src = resources.files("recall") / "templates" / "skill" / "SKILL.md"
    dst = repo / ".devin" / "skills" / "recall" / "SKILL.md"
    dst.parent.mkdir(parents=True, exist_ok=True)
    with open(dst, "w", encoding="utf-8", newline="\n") as f:
        f.write(src.read_text(encoding="utf-8"))


def install(repo_root, tool_project=None) -> dict:
    """Install Recall into a git repo. Idempotent. Raises RuntimeError before any write on a bad target."""
    p = _git(Path(repo_root), "rev-parse", "--show-toplevel")
    if p.returncode != 0 or not p.stdout.strip():
        raise RuntimeError(f"not a git repo: {repo_root}")
    repo = Path(p.stdout.strip()).resolve()
    tracked = _git(repo, "ls-files", "--", *EXCLUDES).stdout.split()
    if tracked:
        raise RuntimeError(f"refusing to install: these paths are tracked by git: {', '.join(tracked)}")
    launcher = _launcher(tool_project)
    cfg_path = repo / ".devin" / "config.local.json"
    mcp_path = repo / ".devin" / "mcp_config.local.json"
    cfg, mcp = _load(cfg_path), _load(mcp_path)  # parse both before writing either
    _add_excludes(repo)
    _dump(cfg_path, _merge_hooks(cfg, launcher, repo))
    _dump(mcp_path, _merge_mcp(mcp, launcher, repo))
    _copy_skill(repo)
    Store(repo).init()
    return {"repo": str(repo), "launcher": launcher}
