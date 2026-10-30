# Cross-Session, Versioned Memory Store for Devin CLI

Date: 2026-10-30

Scope: how to give the Devin CLI agent a per-repo, persistent, versioned memory that loads, saves, checks quality, and keeps history automatically.

How sources were checked:
- **Devin CLI facts** come from the docs bundled with the local install, version **3000.11.228**, at `%LOCALAPPDATA%\devin\cli\_versions\3000.11.228\share\devin\docs\`. The public copies are under `https://cli.devin.ai/docs/...` (same paths). These were read directly.
- **Third-party facts** (Claude Code, mem0, Graphiti, and others) come from the project docs and READMEs at the URLs given, as known up to the researcher's knowledge cutoff. Web fetching was blocked in this session, so those URLs and version numbers were **not re-fetched today**. Re-check them before relying on fine details (see Gaps).

---

## Findings

### 1. Devin CLI capabilities (verified locally, v3000.11.228)

**Hook events.** The hooks overview table (`extensibility/hooks/overview.mdx`, lines 61-70) lists 8 events: `PreToolUse`, `PostToolUse`, `PermissionRequest`, `UserPromptSubmit`, `Stop`, `PostCompaction`, `SessionStart`, `SessionEnd`.
Source: https://cli.devin.ai/docs/extensibility/hooks/overview

**More events from the changelog.** The changelog entry for v3000.11.92 (Oct 14, 2026; `changelog/stable.mdx` line 63) says: "`PreCompact` and `PostCompact` command hooks run before and after context compaction, with `manual` and `auto` trigger matchers. Hook failures do not block compaction, and the existing `PostCompaction` hook remains supported."
- So `PreCompact` exists, but the main hooks pages do not document it yet.

**Injecting context.** A hook can print this JSON on stdout:

```json
{"hookSpecificOutput": {"hookEventName": "...", "additionalContext": "..."}}
```

- The docs say this works for `UserPromptSubmit`, `SessionStart` and `PostToolUse` (overview.mdx lines 147-176).
- No context injection is documented for `PostCompaction`, `PreCompact` or `Stop`. The docs do describe `PostCompaction` as the place for "re-injecting context that may have been lost during compaction" (lifecycle-hooks.mdx line 222), but they don't say how.

**Other hook outputs.**
- `decision: "approve" | "block"` with a `reason`.
- `updatedInput` (for `PreToolUse`).
- `updatedToolOutput` (for `PostToolUse`, added in v3000.11.205).
- `stop: "<reason>"` ends the turn from any event (v3000.11.205).
- `fail_closed: true` (v3000.11.205).
- Exit code 2 = block. Any other non-zero code = logged error, no block.
- Source: changelog/stable.mdx lines 15-18; overview.mdx lines 179-185.

**Using `Stop` to force a save.** A `Stop` hook that returns `{"decision":"block","reason":"..."}` makes the agent keep working, with the reason as its instruction. This is the documented way to make the agent do one more step, for example "save learnings now" (lifecycle-hooks.mdx lines 186-216).
- Guard against loops with `stop_hook_active`.
- `Stop` stdin also has `last_assistant_message` (changelog line 322).

**Hook payloads (stdin JSON).**
- Every event includes `hook_event_name`, `session_id` and `prompt_id`. `prompt_id` is missing for `SessionStart`.
- Event-specific fields:
  - `PreToolUse`: `tool_name`, `tool_input`, `tool_provenance` (changelog line 199).
  - `PostToolUse`: adds `tool_response` (`{success, output, error}`).
  - `UserPromptSubmit`: `prompt`.
  - `Stop`: `stop_hook_active`, `last_assistant_message`.
  - `PostCompaction`: `summary` (may be null).
  - `SessionStart`: `source`.
  - `SessionEnd`: `reason`.
- The env var `DEVIN_PROJECT_DIR` is set to the project root. Plugin hooks also get `PLUGIN_ROOT` / `PLUGIN_DATA` / `DEVIN_PLUGIN_ROOT`, and `CLAUDE_PROJECT_DIR` / `CLAUDE_PLUGIN_ROOT` for Claude compatibility.
- Source: lifecycle-hooks.mdx; changelog lines 62, 308, 322.

**Where hooks are configured.**
- Project level: `.devin/hooks.v1.json` (the whole file is the hooks object), `.devin/config.json` and `.devin/config.local.json` (under a `"hooks"` key), and `.claude/settings.json` / `.claude/settings.local.json`.
- User level: `%APPDATA%\devin\config.json`, `~/.claude.json`, `~/.claude/settings.json`.
- Plugins: `hooks/hooks.json`.
- Use `/hooks` to see what is loaded.
- Source: overview.mdx lines 189-228.

**Claude Code compatibility: yes.**
- Hooks in `.claude/` are read when `read_config_from.claude` is on, which is the default.
- Claude-style exit code 2 takes its block reason from stderr (changelog line 432). `continue:false` / `stopReason` are supported.
- Skills in `.claude/skills` and `.cursor/skills` load automatically.
- Seen locally: Devin treats a `SessionStart` `matcher` as a tool-name filter, so leave the matcher out for `SessionStart` (from the godmode project notes in `%APPDATA%\devin\cli\summaries\history_00ead3595119402a.md`, line ~3057).

**Matchers.** A matcher is a regex on `tool_name`. Non-tool events have no `tool_name`, so use `""` or leave it out (lifecycle-hooks.mdx lines 329-347).

**Hook types.** There are two: `command` and `prompt` (an LLM prompt). Each can set `timeout` (overview.mdx line 100).

**MCP config.**
- Since v3000.3, MCP servers live in `.devin/mcp_config.json` (project), `.devin/mcp_config.local.json`, and `%APPDATA%\devin\mcp_config.json` (global), under an `mcpServers` key.
- `devin mcp add -s project|user <name> <URL>` writes these files.
- Plugins can ship a root `.mcp.json`.
- MCP tools appear as `mcp__<server>__<tool>`.
- Source: `extensibility/mcp/configuration.mdx` lines 32-61; changelog line 394.

**Skills and rules.**
- Skills: `.devin/skills/<name>/SKILL.md`, also `.claude/skills` and `.cursor/skills`.
- Rules: `AGENTS.md`, plus plugin `AGENTS.md` and `.windsurfrules`.
- Skills dropped by compaction are found again when their trigger paths are touched (changelog lines 223, 326).
- Docs: `extensibility/skills/overview.mdx`, `extensibility/rules.mdx`.

**Compaction controls.**
- `agent.compaction_threshold_tokens` makes automatic compaction start earlier (changelog line 194).
- `/compact` runs it by hand.
- `/steps`, `/revert` and `/fork` still work on steps from before compaction (changelog line 662).

**Session store.** Sessions are stored in SQLite by default. `DEVIN_SESSION_STORE=files` (experimental) stores them as files instead (changelog line 14). That is session history, not a curated memory. Devin CLI has no built-in curated memory feature for local repos.

### 2. Claude Code hooks (reference model)

Source: https://docs.anthropic.com/en/docs/claude-code/hooks (now at code.claude.com/docs/en/hooks). The version was not pinned; this describes behavior as of 2025.

- **Events:** `PreToolUse`, `PostToolUse`, `Notification`, `UserPromptSubmit`, `Stop`, `SubagentStop`, `PreCompact` (matcher `manual` / `auto`), `SessionStart` (matcher `startup` / `resume` / `clear` / `compact`), `SessionEnd`.
- **Injecting context:**
  - `SessionStart` and `UserPromptSubmit` add plain stdout, or `hookSpecificOutput.additionalContext`, to the context.
  - `PreCompact` cannot inject. It is for side effects only, such as saving state.
  - The usual pattern for re-injecting after compaction is `SessionStart` with `source: "compact"`.
- **`Stop`:** returning `decision:"block"` + `reason` keeps Claude working.
- **Payloads:** they include `transcript_path`, so a hook can read the whole conversation. Devin's docs do **not** list `transcript_path` (see Gaps).

### 3. Open-source memory systems

Each system is listed with its source, version, and five points: storage, retrieval, auto-capture, quality/dedup, and history/versioning.

**MCP Memory reference server**
- Source: https://github.com/modelcontextprotocol/servers/tree/main/src/memory (npm `@modelcontextprotocol/server-memory`, 2025.x).
- Storage: a local JSONL knowledge graph (entities, relations, observations).
- Retrieval: substring search with `search_nodes`.
- Auto-capture: none. The agent must call the tools, usually because a prompt tells it to.
- Quality/dedup: exact-name dedup only.
- History: none.
- License: MIT.

**mem0 / OpenMemory MCP**
- Source: https://github.com/mem0ai/mem0 (Apache-2.0, v0.1.x–1.x). Paper: arXiv 2504.19413.
- Storage: a vector store (Qdrant, Chroma, pgvector, and others), an optional graph store, and a SQLite history table.
- Retrieval: embedding similarity.
- Quality: an LLM extracts facts. For each fact it compares the top-k similar memories and picks **ADD / UPDATE / DELETE / NONE (NOOP)**.
- History: `history(memory_id)` returns the old and new values for each change.
- OpenMemory MCP runs locally in Docker, but it needs an LLM and an embedding model. These can be local through Ollama.

**Letta (MemGPT)**
- Source: https://github.com/letta-ai/letta (Apache-2.0). Paper: arXiv 2310.08560.
- Storage: tiered. Core memory blocks are always in context. Archival memory is a vector store. Recall memory is the message log.
- Auto-capture: the agent edits its own memory through tools.
- Runs as its own server and agent runtime. It is heavy to add to Devin.

**basic-memory**
- Source: https://github.com/basicmachines-co/basic-memory (AGPL-3.0, v0.x).
- Storage: local Markdown files with frontmatter, observations and relations, plus a SQLite index with full-text search.
- Access: MCP tools such as `write_note`, `read_note`, `search_notes`, `build_context`.
- History: plain files, so git gives full history.
- Note: AGPL is a licensing point to raise with legal.

**claude-mem**
- Source: https://github.com/thedotmack/claude-mem (AGPL-3.0, 2025 releases v4–v5+).
- Auto-capture uses Claude Code hooks: SessionStart, UserPromptSubmit, PostToolUse, Stop and SessionEnd. A background worker compresses tool observations with an LLM (the Claude Agent SDK).
- Storage: SQLite with FTS5, plus optional Chroma vectors.
- Loading: an index is injected at SessionStart.
- **This is the closest existing design to our goal.** Because Devin reads `.claude` hooks, it may partly work as-is, but that is untested.

**Graphiti / Zep**
- Source: https://github.com/getzep/graphiti (Apache-2.0). Paper: arXiv 2501.13956.
- Storage: a temporal knowledge graph in Neo4j or FalkorDB.
- History: **bi-temporal edges** (`valid_at` / `invalid_at` for when a fact was true, plus `created_at` / `expired_at` for when it was recorded). Contradicted facts are invalidated, not deleted. This gives real "as-of" queries.
- Retrieval: hybrid (embeddings + BM25 + graph distance).
- Needs a graph database and an LLM. It has an MCP server.

**Serena memories**
- Source: https://github.com/oraios/serena (MIT).
- Storage: Markdown files in `.serena/memories/`.
- Access: `write_memory`, `read_memory`, `list_memories`. Onboarding writes the first memories.
- Retrieval: the agent picks files by name.
- History: none built in, but the files are git-friendly.

**Cline Memory Bank**
- Source: https://docs.cline.bot/prompting/cline-memory-bank (a convention, not code).
- Storage: a fixed set of Markdown files (`projectbrief.md`, `activeContext.md`, `progress.md`, and others) in `memory-bank/`.
- Behavior: custom instructions tell the agent to read all of them at the start and to "update memory bank" when asked.
- History: git.

**Cursor Memories**
- Source: https://docs.cursor.com/context/memories (Cursor 1.0+, 2025).
- Behavior: a sidecar model proposes memories from chats. The user approves them. They are stored per project in the cloud and shown in settings.
- Not open source. No time-travel.

**Windsurf (Cascade) Memories**
- Source: https://docs.windsurf.com/windsurf/cascade/memories
- Behavior: Cascade creates memories automatically, stored locally under `~/.codeium/windsurf/memories`, per workspace. Rules are a separate feature.
- Closed format. No history.

### 4. Quality assessment techniques

- **Consolidation by an LLM decision (mem0).** For each new fact, find similar memories, then let an LLM choose ADD, UPDATE, DELETE or NOOP. This stops duplicates and contradictions (mem0 paper arXiv 2504.19413; `mem0/configs/prompts.py`).
- **Generative Agents scoring.** Rank memories by recency (exponential decay) + importance (LLM rates 1-10 when the memory is written) + relevance (cosine similarity). Periodic "reflection" combines memories into higher-level insights (Park et al. 2023, arXiv 2304.03442).
- **Similarity dedup.** If cosine similarity is above about 0.9, or BM25/Jaccard overlap is high, merge the items or skip the new one. You can do this without embeddings by using SQLite FTS5 BM25 and normalized-text hashing.
- **TTL and decay.** Keep `last_used_at` and `use_count`. Lower the score over time, and archive or expire items that are never retrieved. Graphiti invalidates instead of deleting.
- **Provenance.** Record `session_id`, `prompt_id`, the source event, the files or commit SHA involved, and the author (agent or user). Devin hooks provide `session_id` and `prompt_id` for free. A memory that points to a file can be checked against `git log`: if the file changed after the memory was written, mark the memory "possibly stale."
- **Admission gate.** Reject memories that are secrets or PHI/PII (regex plus a denylist), that are specific to one task or temporary, or that just repeat AGENTS.md or code comments. A `prompt`-type hook, or a small LLM call, can grade each candidate on a 1-5 scale.

### 5. Versioning / time travel

| Approach | Pros | Cons |
|---|---|---|
| Git-backed Markdown (one file per memory, or one per topic) | Readable and easy to diff; `git log` / `git show <sha>:path` / `git checkout` give history for free; code review via PRs; no new tools | Retrieval needs an index; frequent automatic commits make a noisy history (use a separate branch, or `git notes`, or squash); merge conflicts across branches |
| SQLite append-only event log (`events` table + materialized `memories` view, FTS5) | Fast; one file; "as of time T" = replay the events up to T; rich metadata | Binary file in git (no diffs); OneDrive sync can lock or corrupt the file while it is being written (WAL files) |
| Hybrid: JSONL event log in git + rebuildable SQLite index in `%LOCALAPPDATA%` | Git keeps the source of truth readable and versioned; the index is local and can be thrown away; avoids OneDrive locking | Two parts to keep in sync (rebuild on SessionStart if the log hash changed) |
| Graphiti bi-temporal graph | True valid-time + record-time history; contradiction handling | Needs Neo4j or FalkorDB + an LLM; heavy for one repo |
| Dolt (a "git for data" SQL database, https://github.com/dolthub/dolt, Apache-2.0) | SQL + branch, diff, `AS OF` queries | Extra server/binary; not usually installed; large |

### 6. Constraints for our environment

- **PHI/PII:** keep everything local. No SaaS memory services (Zep Cloud, mem0 platform, Cursor cloud memories). LLM calls for consolidation should go through Devin itself (the agent does the work after a `Stop` block, or a `prompt` hook) rather than a new external API key.
- **OneDrive:** don't keep a live SQLite database in the synced workspace. Store the index under `%LOCALAPPDATA%` and keep only text files in the repo.
- **Tools:** `uv` (Python) and Node 18+ are available. A Python hook script run with `uv run` fits both, and the godmode project already ships a Windows hook wrapper (`run-hook.cmd`).

---

## Comparison table

| System | Storage | Retrieval | Auto-capture | Quality / dedup | History / time travel | License | Local-only? |
|---|---|---|---|---|---|---|---|
| MCP memory server | JSONL graph | substring | No (agent tool calls) | exact names | No | MIT | Yes |
| mem0 / OpenMemory | vectors + SQLite history | embeddings (+graph) | Yes (LLM extraction on add) | ADD/UPDATE/DELETE/NOOP | per-memory change history | Apache-2.0 | Yes, with Ollama + Docker |
| Letta | Postgres/SQLite + vectors | embeddings + tools | Agent edits its own memory | agent-driven | message log only | Apache-2.0 | Yes (server) |
| basic-memory | Markdown + SQLite FTS | full-text + graph links | No (MCP tools) | manual | via git | AGPL-3.0 | Yes |
| claude-mem | SQLite FTS5 (+Chroma) | FTS5 / vectors | Yes (Claude hooks) | LLM compression | timeline, no full time-travel | AGPL-3.0 | Yes, but needs an LLM |
| Graphiti / Zep | Neo4j / FalkorDB | hybrid BM25 + embeddings + graph | Yes (episodes ingested) | LLM dedup + invalidation | **Bi-temporal** | Apache-2.0 (Zep Cloud is SaaS) | Yes, heavy |
| Serena memories | Markdown in `.serena/memories` | file names | Onboarding only | none | via git | MIT | Yes |
| Cline Memory Bank | Markdown convention | read all | Prompt-driven | none | via git | n/a (convention) | Yes |
| Cursor Memories | Cursor cloud | Cursor internal | Yes (sidecar + approval) | user approval | No | Proprietary | No |
| Windsurf Memories | local, closed format | Cascade internal | Yes | internal | No | Proprietary | Yes (local) |

---

## Contradictions or Gaps

1. **PreCompact is in the changelog but not in the hooks docs.** The v3000.11.92 changelog says `PreCompact` / `PostCompact` exist, but `hooks/overview.mdx` and `lifecycle-hooks.mdx` in v3000.11.228 list only `PostCompaction`. The stdin fields for `PreCompact` (for example `trigger`) and whether it can inject context are not documented. **Test with `/hooks` and a logging hook.**
2. **No documented context injection after compaction.** `additionalContext` is documented only for SessionStart, UserPromptSubmit and PostToolUse. Workaround: on PostCompaction, write a flag file. The next UserPromptSubmit or PostToolUse hook sees the flag and injects the memory digest.
3. **No `transcript_path` in Devin payloads.** A hook cannot read the full conversation the way Claude Code hooks can. Sessions live in SQLite under `%APPDATA%\devin\cli` (format undocumented), with summaries in `cli\summaries\*.md`. Use `last_assistant_message` (Stop), `prompt` (UserPromptSubmit), the PostToolUse outputs, and the PostCompaction `summary` instead.
4. **The SessionStart matcher behaves differently from Claude Code.** In Claude Code it matches `startup` / `resume` / `compact`. In Devin it is a tool-name regex, so a non-empty matcher may stop the hook from firing (seen locally).
5. **Third-party facts were not re-fetched today.** Licenses and versions for claude-mem, basic-memory, mem0 and Graphiti come from known READMEs. Check them before choosing; AGPL matters for the two AGPL projects.
6. **"Relevant context after the first few messages" has no dedicated event.** Use `UserPromptSubmit` with a counter keyed by `session_id`. On prompts 1-3, run a BM25 search with the prompt text and inject the top-k memories. After that, inject only when the score is above a threshold.

---

## Conclusion: recommended architecture options

Mapping of requirements to Devin hooks, the same in all options:
- (a) `SessionStart` injects a short digest (top pinned items + an index). `UserPromptSubmit` injects the top-k matches for the prompt on the first ~3 prompts, then only high-scoring matches.
- (b) A `PostToolUse` counter, or `Stop`, triggers an occasional "checkpoint". A `Stop` hook blocks once every N turns with the reason "save durable learnings via `memory.add`", guarded by `stop_hook_active` and a per-session counter.
- (c) `PreCompact` (if it works) or `PostCompaction` saves the `summary` as a candidate and sets a re-inject flag.
- (d) An admission gate plus ADD/UPDATE/DELETE/NOOP consolidation.
- (e) Git history.

**Option A — Git-backed Markdown + Python hook script (lightest, recommended to start)**
- Store: `.devin/memory/items/<id>.md` (frontmatter: type, tags, files, created, updated, source session_id, score, status) plus `.devin/memory/log.jsonl`.
- Hooks: `.devin/hooks.v1.json` → `uv run python .devin/memory/hook.py <event>`.
- Retrieval: SQLite FTS5 BM25 index rebuilt in `%LOCALAPPDATA%\devin-memory\<repo-hash>.db`.
- Writes: a small skill, `.devin/skills/memory/SKILL.md`, that tells the agent how to write items. The agent itself does the LLM consolidation step (it is shown the similar items and decides ADD/UPDATE/NOOP). No external LLM calls.
- History: commit to a `memory` branch or a separate path, then use `git log` / `git show sha:path`. A `memory-as-of <date>` command is just `git rev-list -1 --before`.
- Pros: no new services; PHI stays local; readable; reviewable in PRs.
- Cons: BM25 only, no semantic search; the agent's own judgment is the quality gate.

**Option B — Option A + a local MCP memory server (more structure)**
- A small Python MCP server (in `.devin/mcp_config.json`) exposes `memory_search`, `memory_add` (runs the dedup gate: BM25/Jaccard > threshold → propose UPDATE), `memory_forget`, `memory_history(id)` and `memory_as_of(ts)`. It stores an append-only event log (JSONL in git) and a materialized SQLite view.
- Optional local embeddings (for example `fastembed` or `sentence-transformers` through uv, CPU) for hybrid search. Hooks stay the same as in A.
- Pros: tool calls are typed; mem0-style consolidation and Generative-Agents scoring (recency × importance × relevance) are easy to add; true event replay.
- Cons: more code to maintain; embedding model download needs approval.

**Option C — Adopt an existing system (claude-mem, or mem0/OpenMemory or Graphiti locally)**
- claude-mem's hook design maps almost 1:1 onto Devin's Claude-compatible hooks. mem0 gives mature consolidation. Graphiti gives the best time-travel.
- Pros: least design work; proven ideas.
- Cons: AGPL (claude-mem, basic-memory); they need an LLM API or Ollama, plus Docker or Neo4j for mem0 and Graphiti; payloads differ from Claude Code (no `transcript_path`); more PHI exposure because they run external LLM calls; heavy for a per-repo store.

**Suggested path:** build Option A first with the hook mapping above, and include a spike to confirm the `PreCompact` payload and how injection behaves. Move to B if BM25 retrieval quality is not good enough. Use C only as a source of design ideas, especially the mem0 consolidation prompt and Graphiti's valid/invalid timestamps for the event log.
