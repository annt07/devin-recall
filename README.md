# Recall

Recall gives Devin CLI an automatic, per-repo, versioned wiki memory. Hooks capture what happens in a session and inject relevant pages. The agent saves durable knowledge through the Recall MCP tools. The store is a separate git repo in `<repo>/.devin/recall/`.

## Install

Requires Python 3.11+, [uv](https://docs.astral.sh/uv/) and git. Works on Windows, macOS and Linux.

```
uv tool install git+<recall-repo-url>
recall install <path-to-your-git-repo>
```

`recall` must be on `PATH` when you run `recall install` (`uv tool update-shell`). The install writes the absolute path of the `recall` executable into the hook and MCP commands, so Devin does not need `PATH` later. Run `recall install` again if you move or reinstall the tool.

Without a tool install, from a checkout of this repo:

```
uv run recall install <path-to-your-git-repo>
```

Then the hooks run `<checkout>/.venv/.../python -m recall ...` (the absolute interpreter of the checkout). With `--tool-project <checkout>` they run `<absolute uv> run --frozen --project <checkout> recall ...` instead.

On Windows the hook commands are quoted for `cmd.exe` (`%` is doubled); a path with `!` is refused.

## What `recall install` does

- Adds Recall hooks to `<repo>/.devin/config.local.json` (SessionStart, UserPromptSubmit, Stop, PreCompact, PostCompaction, SessionEnd, and a PreToolUse write guard).
- Adds the `recall` MCP server to `<repo>/.devin/mcp_config.local.json`.
- Copies the agent Skill to `<repo>/.devin/skills/recall/SKILL.md`.
- Adds these paths to `.git/info/exclude` (local only, nothing is committed): `.devin/recall/`, `.devin/skills/recall/`, `.devin/config.local.json`, `.devin/mcp_config.local.json`.
- Creates the store.

It keeps existing entries in both JSON files, is safe to run again, and refuses to run if one of those paths is tracked by git.

## Data locations

- Store: `<repo>/.devin/recall/` (git repo).
- Local data (search index, error log): `RECALL_LOCAL_DIR` if set, else `%LOCALAPPDATA%\devin-recall`, else `$XDG_DATA_HOME/devin-recall`, else `~/.local/share/devin-recall`. `errors.log` is redacted with the Gate's secret patterns and rotated at 1 MB (`errors.log.1`).
- Devin data is read only. `RECALL_DEVIN_DIR` (parent of `devin/`) and `RECALL_DEVIN_CONFIG` (the config file) win if set. Else the first of these that has a `devin/` folder: `%APPDATA%`, `$XDG_CONFIG_HOME`, `~/Library/Application Support` (macOS), `~/.config`. If none exists yet: `%APPDATA%` or `$XDG_CONFIG_HOME` if set, else `~/.config`.

## Develop

```
uv run pytest -q
```
