# Recall: Automatic Per-Repo Memory for Devin

**Date:** 2026-10-30
**Status:** Approved by the user on 2026-10-30
**Research:** `docs/research/cross-session-memory-2026-10-30.md`, Karpathy "LLM Wiki" gist (442a6bf555914893e9891c11519de94f)

## Problem Statement

The user runs many Devin sessions in one project. Each new session starts with an empty context. When the context window is full, Devin compacts it, and compaction loses detail. Old sessions often contain decisions, commands and root causes that later sessions need again. The agent must find this information again, which increases token cost and decreases speed and quality. No mechanism exists that keeps this knowledge across sessions.

## Solution

Recall is a local, per-repo memory that Devin maintains automatically. Recall keeps a small wiki of linked Markdown topic pages. The agent compiles knowledge into these pages one time and keeps the pages current. The agent does not collect raw fragments.

- At session start, Recall injects a short index of the pages.
- On each prompt, Recall injects the pages that match the prompt.
- Before the session stops and before compaction, Recall makes the agent save durable learnings into the pages.
- After compaction, Recall injects the index again.
- A quality system rejects bad writes, marks stale pages, finds contradictions and measures if the memory helps.
- Each change is a git commit with metadata. The agent can show, compare or restore the memory at any point in time.
- In a repo with old sessions, Seed builds the first memory from the local Devin session records.

The user does not run commands. Hooks start all operations automatically. The agent uses the time-travel and quality operations through MCP tools when the conversation needs them (for example, when the user asks "what did we know last week?").

## User Stories

1. As a developer, I want each new session to start with the important project knowledge, so that I do not explain the project again.
2. As a developer, I want Recall to inject only the pages that are relevant to my prompt, so that the context stays small and cheap.
3. As a developer, I want the agent to save durable learnings before a session stops, so that the next session can use them.
4. As a developer, I want Recall to ingest the compaction summary after compaction, so that compaction does not lose learnings. A save before compaction is best-effort only.
5. As a developer, I want the index injected again after compaction, so that the agent keeps its project knowledge.
6. As a developer, I want new knowledge merged into existing pages, so that the memory does not fill with duplicates.
7. As a developer, I want each claim to cite its source, so that I can trust it and verify it. A source is a session record, a code location or a commit.
8. As a developer, I want writes that contain secrets (keys, tokens, passwords) rejected, so that the memory does not leak access.
9. As a developer, I want pages marked stale when the code that they cite changes, so that the agent does not trust old facts blindly.
10. As a developer, I want a periodic lint that finds contradictions, orphan pages and broken links, so that the memory stays healthy.
11. As a developer, I want Recall to track used pages and wrong pages, so that low-value pages lose rank and go away.
12. As a developer, I want a golden-question evaluation with a score, so that I can see if memory quality goes up or down.
13. As a developer, I want each memory change to record its session, trigger, branch and project commit, so that I can find its cause.
14. As a developer, I want the agent to show the memory at a past point, so that I can go back in time. A point is a date, a session, a project commit or the best eval score.
15. As a developer, I want the agent to compare two points in time, so that I can see what the memory learned or forgot.
16. As a developer, I want the agent to undo a bad change without rewriting history, so that I can always go forward again.
17. As a developer, I want a warning when the memory is newer than the checked-out code, so that the agent knows the risky pages.
18. As a developer, I want to read the wiki in Obsidian or VS Code, so that I can see what the agent knows.
19. As a developer, I want all data local and untracked by the project repo, so that nothing leaves my machine.
20. As a developer, I want no manual step in normal use, so that the memory works without my attention.
21. As a developer, I want Recall to build the first memory from the old sessions of the repo, so that existing knowledge is not lost.
22. As a developer, I want Seed to continue after an interruption, so that a long Seed completes over more sessions.

## Implementation Decisions

### Store layout

- The store folder is in the repo at the Devin config folder, sub-folder `recall`. The project repo does not track it (local git exclude). The store has its own git repository.
- `SCHEMA.md`: page types, page conventions, ingest workflow and lint workflow. The agent and the user change it over time.
- `index.md`: one line for each page (link, one-line summary, type, status). Budget: approximately 1.5k tokens.
- `log.md`: append-only. Each entry starts with `## [YYYY-MM-DD HH:MM] <op> | <title> | session=<id>`.
- `pages/<type>/<slug>.md`: topic pages. Types: decision, fact, howto, gotcha, preference, task. Budget: approximately 800 tokens for each page. Pages use `[[links]]`.
- `raw/<date>-<session>-<kind>.md`: immutable session records (compaction summary, last assistant message). Each record has a size cap.
- `eval.md`: golden questions with expected answers.
- The FTS5 search index and the usage counters (`used`, `wrong`) are in one SQLite file in the local application data folder. A hash of the repo path is its key. This file is the only location of the usage counters. Recall can rebuild the search index. The file is not in OneDrive.
- Precondition: the project folder is a git repository. If it is not, Recall stays off and the SessionStart hook injects one line that tells this.
- Lock: one lock file in the store (owner session id and time, stale after 2 minutes) serializes all store writes and commits. A hook that finds the lock held waits a maximum of 5 seconds, then skips its write and keeps it as pending.

Page front-matter:

```yaml
type: fact
title: Build and test commands
status: active        # active | stale | retired
confidence: high      # high | medium | low
sources: [raw/2026-10-30-7f3a-compact.md, "src/pyproject.toml"]
anchors: {"pyproject.toml": "<git blob hash>"}
updated: 2026-10-30
```

### Modules

- **Store** (deep core). Interface: read and write pages, raw records, index and log. It hides the file formats and the FTS5 index.
- **Gate** (write-time quality). Interface: `check(page_change) -> accept | reject(reason) | redirect(existing_page)`. Gate rules: schema, required provenance, secret scan, size budget and duplicate guard. The secret scan finds keys, tokens and passwords. V1 has no PHI/PII scan. For the duplicate guard, an FTS5 near-match makes the agent update the existing page.
- **Lint** (health). Interface: `lint_mechanical(store) -> findings`, `lint_semantic(store, llm) -> findings`.
  - Mechanical rules: anchors exist, anchor hashes are current, broken links, orphan pages, pages not in the index. A changed hash or a missing anchor file makes the status stale. The hook script runs these rules itself and injects the findings.
  - Semantic rules (an agent pass): contradictions, claims that newer raw records replace, merge candidates, confidence decay.
- **Eval**. Interface: `run(store, answerer) -> score`. The answerer is an adapter: a fake in tests and the agent in real use.
- **History** (the only module that calls git). Interface: `commit(op, meta)`, `log(filter)`, `resolve(ref) -> sha`, `snapshot(sha) -> path`, `diff(a, b)`, `blame(page)`, `revert(sha, page?)`.
- **Session Reader** (adapter). Interface: `sessions_for(repo_root) -> [session]`, `records(session) -> [raw record]`. It opens the Devin `sessions.db` read-only (`mode=ro`, one retry on a lock). It hides the internal Devin format. If the schema is not the expected schema, it stops safely and returns nothing.
  - Verified join (2026-10-30): the `summaries\history_<hex>.md` file names do not match `sessions.id`. The compaction summary text is in `message_nodes.chat_message` of the session, in the system message that starts "You are continuing work from a previous conversation thread". That message also points to the `history_<hex>.md` file. The `history_*.md` files are full pre-compaction dumps, and continuation threads contain the same content again.
  - Primary source: `sessions.db` only. The `history_*.md` files are an on-demand source for a gap, with a byte cap. Dedup uses the `<hex>` pointer from `chat_message`.
- **Seed**. Interface: `seed(store, reader) -> progress`. It builds the first memory from old sessions.
- **Hook adapter**. One entry point that gets the Devin hook JSON on stdin and writes the hook JSON on stdout.
- **MCP adapter**. A local stdio MCP server that gives Store, Lint, Eval and History to the agent as tools. It is the only write path for pages.
- **Skill**. Tells the agent when to search, what to save, how to write a page, and when to use the history tools.

### Hard rule: Devin data is read-only

Recall never writes, changes, locks for write, moves or removes Devin internal data. Devin internal data includes `sessions.db` and its `-wal` and `-shm` files, `summaries\`, `transcripts\` and all other files in the Devin application data folders. A change can corrupt the Devin work flow.

- The Session Reader opens `sessions.db` only with the SQLite URI `mode=ro`. It never runs a statement that writes, and it never runs a `PRAGMA` that changes the database.
- The Session Reader opens other Devin files only in read mode.
- Recall writes only in its store folder and in its own local data folder.
- A test verifies that Seed and the Session Reader do not change the fixture `sessions.db` (file hash before and after).

### Context Gauge (save before compaction)

The Session Reader gives `context_status(session) -> {tokens, limit, ratio, source}`. It finds the limit again on each call (no cache), so a model switch has an effect on the next prompt.

- `tokens`: the newest `metadata.num_tokens_preceding` in `message_nodes` for the session.
- `limit`, the first source that gives a value:
  1. `RECALL_CONTEXT_LIMIT` (environment).
  2. `agent.compaction_threshold_tokens` in the Devin user config.
  3. Observed limit: the median token count before past compactions (`summarized_from`) of the same model in the user's sessions (minimum 3 samples).
  4. Model registry window × 0.75.
  5. Default 150,000.
- Model registry: a JSON object `{"<model>": <context window tokens>}`. Recall ships a built-in registry. A user registry (`models.json` in the Recall local data folder) adds entries or replaces built-in entries. The lookup removes a thinking-level suffix (`-low`, `-medium`, `-high`, `-xhigh`, `-max`) from the model name, because the thinking level does not change the context window. The registry contains only model names and windows.
- Recall does not decode the internal Devin model-config files.
- UserPromptSubmit: if `ratio >= 0.85` (near) or `ratio >= 1.0` (over, for example after a switch from a large model to a small model), Recall injects a save request ("urgent" when over). Recall sends one request, then a new one only after an ingest or after 10% more of the limit. Both thresholds are settings. If no token count is available, Recall does nothing. The PostCompaction and Stop paths stay the fallback.
- Subagents: Recall does not track subagent context. If Task 0 shows that hooks run in subagents, the hooks do nothing there.

### Work split: mechanical and agent

Hooks run scripts. Scripts have no LLM. Thus:

- **Mechanical (the hook script does it):** raw record capture, FTS5 search and injection, Gate rules, mechanical lint, commits, locks, Seed selection of sessions.
- **Agent (the agent does it through MCP tools, after a request that a hook injects):** page writes (ingest), semantic lint, eval answers, Seed batches.

An agent operation can fail to occur, because the agent can ignore a request or the session can stop. The drain rule below makes sure that pending work occurs later.

**Drain rule:** a raw record is "pending" until an ingest cites it. SessionStart injects the count of pending raw records and a request to ingest them. The Stop hook blocks one time when the count of pending raw records is more than 0. Pending raw records older than 90 days go away with the normal raw-record cleanup, and the log records them as "not ingested".

### Hook flow (all automatic)

| Hook | Action |
|---|---|
| SessionStart | Write a session marker (session id, start time). Inject `index.md`, the code-mismatch note, the pending raw-record count and the mechanical lint findings. If more than 10 ingests or 7 days since the last semantic lint, add a semantic lint request. |
| UserPromptSubmit | FTS5 search on the prompt. Inject a maximum of 3 pages and approximately 2.5k tokens. Do not inject pages already injected in this session. If a post-compaction flag exists, inject the index again and clear the flag. Hook timeout: 5 seconds. |
| Stop | Save `last_assistant_message` as a raw record. Block one time with the reason "save durable learnings with recall_ingest" if one condition is true. Conditions: pending raw records exist, `git status --porcelain` shows changed files, or the session is older than 10 minutes with no ingest. `stop_hook_active` prevents a loop. "One time" means a maximum of one block in 15 minutes for each session, and no block after an ingest. |
| PreCompact | Best-effort only. The documents do not say that this hook can block or inject. The probe task records its real payload. The guaranteed path is PostCompaction. |
| PostCompaction | Save the `summary` as a raw record (pending). Set the post-compaction flag. The next UserPromptSubmit injects the index and an ingest request. |
| SessionEnd | Read the messages of this session from `sessions.db` through the Session Reader and save them as a raw record (pending). Commit. |
| PreToolUse (write guard) | Matcher `^(write\|edit\|apply_patch)$`. If the target path is in the store folder, block the tool call (exit code 2) with the reason "use recall_ingest". The probe task finds the path field in `tool_input`. |

The write guard is the only hook with a matcher. All other hook matchers stay empty. At SessionStart, if Seed is not complete, Recall adds a Seed request for the next batch.

### Seed

- Start: automatic, one time, in the first session that finds old sessions for the repo. Seed works in small batches. `log.md` records the progress, so Seed continues in the next session after an interruption.
- Repo match: a session is part of the repo when its `working_directory` or `workspace_dirs` is the repo root or a folder in it. The match ignores letter case and slash direction. Temporary folders do not match.
- Age limit: the last 90 days, newest first, in a token budget. Seed records older sessions as "not read".
- Source order for each session: (1) compaction summaries in `message_nodes`, (2) user prompts and final assistant messages. Seed reads tool outputs or a `history_*.md` file only for a gap.
- Each session becomes raw records, then an ingest through the Gate. Each ingest is one History commit with `Recall-Trigger: Seed` and the session time.
- Lint and Eval run one time at the end of Seed.

### MCP tools

V1 has 11 tools: `recall_search`, `recall_get`, `recall_ingest`, `recall_mark` (used / wrong), `recall_lint`, `recall_eval`, `recall_log`, `recall_history` (the changes of one page), `recall_show_at`, `recall_diff`, `recall_revert`.

V1 does not have `checkout`, `return`, `as_of`, `blame` or `stats`. A checkout changes the shared working tree, and that is dangerous with parallel sessions. `show_at`, `diff` and `revert` give the back-and-forth in time.

The agent calls these tools. The user never types a command. A CLI with the same operations exists only for tests and for the hook adapter.

### Commit metadata (git trailers)

`Recall-Op`, `Recall-Session`, `Recall-Trigger`, `Recall-Repo-Commit`, `Recall-Branch`, `Recall-Sources`, `Recall-Pages`, `Recall-Eval`.

A ref is one of: a date or time, `session:<id>`, `repo:<sha>`, `before:<op>#<n>`, `best-eval`, or a store commit sha.

### Quality rules

- A write without provenance is rejected.
- A secret match rejects the write. PHI/PII is permitted in v1. The log records the rejection without the content.
- A stale page is injected with a warning.
- A page with 2 or more `wrong` marks, or stale for more than 30 days, gets status retired. A retired page is not in `index.md` and Recall does not inject it. Only `recall_get` and the history tools can show it. Git history keeps it.
- A page that is not used for N sessions (default 20) loses rank and becomes a lint merge or remove candidate.
- Eval runs only with the semantic lint, a maximum of 1 time in 7 days, with a maximum of 10 golden questions.
- The agent proposes golden questions when a page becomes stable. The user can edit `eval.md` at any time. No approval step.

### History rules

- One linear timeline. Branch names are trailers.
- Revert makes a new commit. No history rewrite and no force operation. Exception: a secret purge, and only after explicit user confirmation.
- Git history is kept forever. Raw records older than 90 days are removed from the working tree with a normal commit.
- When the project HEAD is behind the store's last `Recall-Repo-Commit`, SessionStart adds a note. Recall does not change the memory state automatically.

### Technology

Python, run with `uv`. Standard library `sqlite3` (FTS5) and `subprocess` git, plus the `mcp` SDK. No cloud service. No embeddings in v1.

## Testing Decisions

- Tests check external behavior only: files and commits in a temporary store, and hook stdout JSON. Tests do not verify internal functions.
- Seams under test (agreed in the design):
  1. Store + Gate interface, against a temporary folder.
  2. Lint mechanical, against fixture store folders. Lint semantic, with a fake LLM response.
  3. Eval runner, with a fake answerer adapter.
  4. History, against a temporary git repository.
  4a. Session Reader and Seed, against a small fixture `sessions.db` and fixture summary files. One test uses a changed schema and expects a safe stop.
  5. Hook adapter end to end: sample hook JSON on stdin, assert stdout JSON and store state.
  6. MCP adapter: one smoke test that lists the tools and calls `recall_search`.
- Prior art: none in this folder. Use `pytest` with `tmp_path`.

## Out of Scope

- Embeddings, vector search and `qmd`.
- Memory shared across repos or across team members.
- Separate memory branches for each project branch.
- A graphical interface. Obsidian or VS Code is optional for reading.
- Cloud or hosted memory services.
- A PHI/PII scan. The user postponed it. OneDrive sync of the memory folder, including raw records with full prompts and messages, is accepted for v1.
- Prompt-type hooks. The documents do not give their output contract.
- Folders that are not git repositories.

## Further Notes

- Observed local session data (2026-10-30, read-only): `%APPDATA%\devin\cli\sessions.db` (SQLite, tables `sessions`, `message_nodes`, `prompt_history`, `tool_call_state`), `summaries\history_*.md` and `transcripts\*.json`. This format is internal to Devin and can change.
- The PreCompact payload is not documented (added in CLI v3000.11.92). The first plan task is a probe that records the real payloads of all hooks. The probe also records PostCompact and the `tool_input` path field of the write and edit tools.
- No hook payload gives the context size. The Context Gauge reads it from `sessions.db` instead (research: `docs/research/context-windows-2026-10-30.md`).
- An independent senior review on 2026-10-30 gave 17 findings. This revision includes the fixes and the user decisions on the write guard, the MCP tool list, Eval and the save trigger.
- Context injection is documented only for SessionStart, UserPromptSubmit and PostToolUse. The design does not depend on injection from other hooks.
- The store is inside OneDrive. Git and file writes must retry on a lock. The SQLite index is outside OneDrive for this reason.
