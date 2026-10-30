# Recall

Recall gives Devin CLI an automatic, per-repo, versioned wiki memory. Hooks capture what happens in a session and inject relevant pages. The agent saves durable knowledge through the Recall MCP tools. The store is a separate git repo in `<repo>/.devin/recall/`.

## Requirements

- Python 3.11+, [uv](https://docs.astral.sh/uv/) and git. Works on Windows, macOS and Linux.
- The npx path also needs Node.js 18+.
- The repository is private. Install commands pull it with your own git credentials (SSH key or credential manager).

## Install

Pick one of three paths.

### A) Devin plugin (recommended)

```
devin plugins install annt07/devin-recall
# or with a full git URL
devin plugins install https://github.com/annt07/devin-recall.git
```

This installs the plugin at user level. Its hooks, the `recall` MCP server and the `/recall:recall` skill then apply in every repo and on every machine where you sign in.

Recall stays off until you opt in per repo. In a repo without a store, the first SessionStart injects one line telling the agent how to enable it. Say "enable recall" — the agent calls the `recall_enable` MCP tool, which creates the store and adds `.devin/recall/` to `.git/info/exclude`. Nothing else is written.

### B) npx (no install, straight from GitHub)

```
npx github:annt07/devin-recall install <path-to-your-git-repo>
```

This runs `recall install` through `uvx` and writes `uvx --from git+ssh://git@github.com/annt07/devin-recall.git recall ...` into the hook and MCP commands, so they keep working even after the uvx cache is cleaned. To use a different source (for example an https URL or a local checkout), set `RECALL_SOURCE` first.

### C) uv tool

```
uv tool install git+ssh://git@github.com/annt07/devin-recall.git
recall install <path-to-your-git-repo>
```

`recall` must be on `PATH` when you run `recall install` (`uv tool update-shell`). The install writes the absolute path of the `recall` executable into the hook and MCP commands, so Devin does not need `PATH` later. Run `recall install` again if you move or reinstall the tool.

Without a tool install, from a checkout of this repo:

```
uv run recall install <path-to-your-git-repo>
```

Then the hooks run `<checkout>/.venv/.../python -m recall ...` (the absolute interpreter of the checkout). With `--tool-project <checkout>` they run `<absolute uv> run --frozen --project <checkout> recall ...` instead.

On Windows the hook commands are quoted for `cmd.exe` (`%` is doubled); a path with `!` is refused.

## What `recall install` does (paths B and C)

- Adds Recall hooks to `<repo>/.devin/config.local.json` (SessionStart, UserPromptSubmit, Stop, PreCompact, PostCompaction, SessionEnd, and a PreToolUse write guard).
- Adds the `recall` MCP server to `<repo>/.devin/mcp_config.local.json`.
- Copies the agent Skill to `<repo>/.devin/skills/recall/SKILL.md`.
- Adds these paths to `.git/info/exclude` (local only, nothing is committed): `.devin/recall/`, `.devin/skills/recall/`, `.devin/config.local.json`, `.devin/mcp_config.local.json`.
- Creates the store.

It keeps existing entries in both JSON files, is safe to run again, and refuses to run if one of those paths is tracked by git.

## What happens automatically

- **SessionStart** injects the memory index, runs lint and review checks, and seeds a few old Devin sessions per session until the backlog is done.
- **UserPromptSubmit** injects the pages that match your prompt, plus a context-fullness warning near the compaction limit.
- **Stop** blocks once when the session has unsaved learnings or pending raw records, and asks for a `recall_ingest`.
- **PreCompact / PostCompaction** record the model and the compaction summary, and re-inject the index afterwards.
- **PreToolUse** guards writes to `.devin/recall/` (use the MCP tools instead).
- **SessionEnd** captures the session transcript into raw records.

## MCP tools

`recall_search`, `recall_get`, `recall_ingest`, `recall_mark`, `recall_lint`, `recall_eval`, `recall_log`, `recall_history`, `recall_show_at`, `recall_diff`, `recall_revert`, `recall_enable`.

## Time travel and undo

The store is a git repo. `recall_show_at` shows the index or a page at any commit, date, session or `best-eval`. `recall_diff` diffs two refs. `recall_revert` undoes a commit (or one page of it) as a new commit; history is never rewritten.

## Uninstall

- **Plugin path:** `devin plugins remove recall`. The store stays on disk in each repo; delete `<repo>/.devin/recall/` per repo if you want it gone.
- **Repo install (paths B and C):** remove the Recall hook entries from `<repo>/.devin/config.local.json`, the `recall` server from `<repo>/.devin/mcp_config.local.json`, and `<repo>/.devin/skills/recall/`. Then delete the store.
- `uv tool uninstall devin-recall` removes the tool itself.

## Data locations

- Store: `<repo>/.devin/recall/` (git repo).
- Local data (search index, error log): `RECALL_LOCAL_DIR` if set, else `%LOCALAPPDATA%\devin-recall`, else `$XDG_DATA_HOME/devin-recall`, else `~/.local/share/devin-recall`. `errors.log` is redacted with the Gate's secret patterns and rotated at 1 MB (`errors.log.1`).
- Devin data is read only. `RECALL_DEVIN_DIR` (parent of `devin/`) and `RECALL_DEVIN_CONFIG` (the config file) win if set. Else the first of these that has a `devin/` folder: `%APPDATA%`, `$XDG_CONFIG_HOME`, `~/Library/Application Support` (macOS), `~/.config`. If none exists yet: `%APPDATA%` or `$XDG_CONFIG_HOME` if set, else `~/.config`.

## Troubleshooting

- **Hook timeouts:** hooks fail open — a slow or broken hook never blocks the session. Look in `errors.log` under the local data folder.
- **`uv` / `uvx` missing:** install uv from https://docs.astral.sh/uv/getting-started/installation/ and rerun.
- **Private repo:** the plugin and npx installs pull with your git credentials. Make sure `git clone` of the repo URL works first.
- **Plugin and repo install together:** the plugin hooks detect Recall entries in `<repo>/.devin/config.local.json` and become a no-op, so hooks never run twice. Devin also keeps the first `recall` MCP server definition it sees per name; the repo-level server wins over the plugin one when both exist.
- **Plugin hooks in a repo without a store:** everything is a silent no-op except SessionStart, which adds one "not enabled" line. `recall_enable` creates the store.

## Develop

```
uv run pytest -q
```
