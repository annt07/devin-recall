# Recall Deepening Refactor

**Date:** 2026-10-30
**Status:** Draft for user review
**Source:** architecture review of 2026-10-30 (5 candidates and 8 flow issues). Decisions Q1-Q9 approved by the user ("go with recommendations").

## Problem Statement

The two adapters (hooks and MCP server) each open the Store and the LocalIndex, decide when to rebuild the search index and map errors. Their policies drift. Session state keys are raw strings in five modules, and nothing removes them. Four modules call private Store helpers. The maintenance schedule is spread over three modules. The Session Reader and the guard depend on the hook adapter. The runtime flow does unnecessary work in hooks with a 5-second limit.

## Solution

One refactor, in small steps that keep all tests green:

1. **Runtime** (new deep module): the only place that opens a Store and a LocalIndex for an adapter, decides when to rebuild the search index, gives one page scan for each call, and maps exceptions to adapter results. Both adapters use only Runtime.
2. **SessionState** (new deep module, used by Runtime and the maintenance module): owns all session-state key names, their encodings and their cleanup. No other module uses a state key string.
3. **Store and History interface**: callers use only public operations. Private helpers are not imported outside their module.
4. **Maintenance** (one module): mechanical lint, the due checks for semantic review and eval, and the reset after they are done. Due checks have no side effects. Eval scoring stays a separate helper.
5. **One-way dependencies**: injected-text markers are in a leaf module. The guard returns a plain decision. The Session Reader and the guard do not import the hook adapter.

## User Stories

1. As a developer, I want a search to cost one cheap check when no page changed, so that MCP calls are fast.
2. As a developer, I want each hook to read the pages one time, so that hooks stay inside 5 seconds.
3. As a developer, I want old session keys removed, so that local data does not grow without limit.
4. As a developer, I want the Stop hook to run its cheap checks first, so that most Stop calls start no git process.
5. As a developer, I want each prompt to parse the transcript only when it changed, so that prompts stay fast.
6. As a developer, I want the session age to exclude the time of Seed and lint, so that the 10-minute rule measures user work.
7. As a maintainer, I want both adapters to share one runtime policy, so that a fix applies to both.
8. As a maintainer, I want one module to own each concept, so that a change has one location.

## Implementation Decisions

### Runtime

- Interface (exact names in the plan): open a context for a repo, run an operation in that context with error mapping, get the shared page scan, get the search index in a fresh state.
- Rebuild rule (review 1, 2): Runtime makes a page fingerprint: the sorted list of `(path, mtime_ns, size)` of all page files, with `os.stat` only and no git process. It rebuilds the search index only when the fingerprint is different from the fingerprint saved at the last rebuild, or when the index has no rows. The fingerprint also catches page changes that have no commit. Usage counters are not in the fingerprint, because the search query reads them directly.
- Lazy work (review 1): Runtime does no fingerprint, no page scan and no git call when it opens. It does them only when an operation asks for the index or the pages.
- Page scan (review 3): the scan is lazy. Each Store write (ingest, set_status, revert, cleanup) makes the scan of that context invalid, so the next read gets the new pages. A context lives for one hook call or one MCP tool call and is never reused.
- Error mapping (review 12, plan review 4): the MCP error mapping does not change. The expected exception types stay the current set (`RefError`, `LockBusy`, `GitError`, `ValueError`, `RuntimeError`, `KeyError`, `TypeError`) with the text `"<ExceptionType>: <masked message>"`. Other exceptions keep the current text `"internal error (<Type>); see errors.log"`. Hooks give an empty result and exit 0, except the guard block (exit 2).
- Repo resolution (review 6): each adapter keeps its current resolution order. Hooks: explicit repo, `DEVIN_PROJECT_DIR`, working folder, with `git rev-parse --show-toplevel`. MCP: `RECALL_REPO`, then the nearest folder with `.git`. Both share one function for the store-folder correction. For MCP this is an intended bug fix: a `RECALL_REPO` or working folder inside `.devin/recall` now resolves to the owner repo (plan review 5).
- Page-scan freshness (plan review 6): Store has a write generation number. Each Store write increases it. Runtime keeps the page scan with the generation number and reads again when the number changed. No caller must call an explicit invalidate.

### SessionState

- Operations: session start time, Stop block time, post-compaction flag, model from PreCompact, gauge request tokens, observed compaction limit for each model (with time), last semantic review time, review due flag, eval due flag, page fingerprint at last rebuild, transcript cache (size, change time, result), last-seen time of each session.
- Key names (review 5): SessionState keeps the existing key names byte for byte (`start:<sid>`, `stop_block:<sid>`, `post_compact:<sid>`, `model:<sid>`, `gauge:<sid>`, `gauge_observed:<model>`, `lint_due`, `eval_due`, `last_semantic_lint`). New keys: `seen:<sid>`, `index_fingerprint`, `transcript:<sid>`. No migration is necessary.
- Scope table (review 13):

| Scope | Keys | Cleanup |
|---|---|---|
| Session | `start:`, `stop_block:`, `post_compact:`, `model:`, `gauge:`, `seen:`, `transcript:`, and the `injected` rows of the session | at SessionEnd of that session, and at SessionStart for sessions with `seen:` older than 7 days |
| Model | `gauge_observed:<model>` | never (24 h refresh) |
| Store | `lint_due`, `eval_due`, `last_semantic_lint`, `index_fingerprint` | never |

- `seen:<sid>` is updated at each hook call that has a session id, except PreToolUse (to keep the write guard fast) and SessionEnd (it removes the keys). For a session with no `seen:` key (sessions from before this refactor), cleanup uses the `start:` time. A session key with neither time is removed (plan review 7).
- LocalIndex gets two storage operations (review 4): list the state keys with a prefix, and remove keys and the `injected` rows of a session. LocalIndex has no knowledge of key meaning.
- Provisional start (review 10): Runtime writes `start:<sid>` at the first hook call of a session if it is missing. SessionStart writes it again after its sections.

### Store and History

- New public Store operations for what callers use now: append a log line (with redaction), list raw records, write the index file, read and write a store text file (for seed).
- Review 7: the git steps of revert and of `uncommitted_raw` move into History as public operations: `status(paths)`, `restore_page(sha, page)`, `revert_commit(sha)` (with abort on conflict) and `untracked(paths)`. Store keeps the lock, the index write and the log line. Store no longer calls private History helpers.

### Maintenance

- Contains: mechanical lint, `review_due`, `eval_due`, `mark_review_done`, `mark_eval_done`, and the semantic review prompt.
- Due checks are pure reads. The 7-day clock starts at the last review, or at the store's first commit when no review exists.
- The MCP tools and the SessionStart section use only these operations.

### Flow fixes

- Stop: the reads occur in this order: `stop_hook_active`, cooldown, pending raw count, session age, project changes, and last the git log query for an ingest. All reads occur before any write (review 11). Recall writes the final message record and the block time only when it blocks. Only an ingest counts as a save. Seed and revert do not count.
- Session start time is written after the SessionStart sections run.
- Transcript cache in SessionState, keyed by session, with file size and change time.

## Testing Decisions

- All 172 current tests must pass after each step. A test that uses a private helper or a state key string changes to the public operation in the same step.
- New tests go through the Runtime interface, the SessionState interface and the Maintenance interface.
- Counting tests: a test counts page reads, index rebuilds, git calls and transcript parses with a counting fake or a monkeypatched function, and asserts the upper limits (1 page scan for each call, 0 rebuilds when the page fingerprint is unchanged, 0 History git calls after repo resolution for a Stop that exits early, 0 parses for an unchanged transcript).
- A dependency test (AST) asserts that no module except `hooks`, `mcp_server` and `cli` imports `recall.hooks` or `recall.mcp_server`. This includes the new modules `runtime`, `session_state`, `maintenance` and `markers`. It also asserts that no module imports a private name (leading `_`) from another Recall module.
- An AST test asserts that only `session_state` calls the LocalIndex state operations (review 8).
- The full suite runs after each step.

## Out of Scope

- New features. Behavior visible to the agent does not change, except the speed and the flow fixes above.
- Changes to the store file format, the commit trailers or the hook configuration.

## Further Notes

- An independent senior review (2026-10-30) gave 13 findings. This revision includes a decision for each finding (marked "review N").

- Rejected options: Runtime that also holds session state (fails the deletion test for SessionState); counting Seed or revert as a save (they add no learnings of the current session).
