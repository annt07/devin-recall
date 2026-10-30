# Recall Deepening Refactor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use godmode:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Deepen the Recall modules (Runtime, SessionState, Store/History interface, Maintenance, one-way dependencies) and fix the runtime flow costs, with no change of agent-visible behavior.

**Architecture:** see the spec. Both adapters go through Runtime. SessionState owns all state keys. History owns all git calls.

**Tech Stack:** Python 3.11+, uv, pytest.

**Spec:** `docs/godmode/specs/2026-10-30-recall-deepening-design.md`

## Global Constraints

- After each task, `uv run pytest -q` passes (172 tests at the start). Do not remove a test. Change a test only to use a new public operation.
- Key names stay byte-identical (spec, SessionState).
- MCP error text stays `"<ExceptionType>: <masked message>"`.
- No change to the store file format, commit trailers or hook configuration.
- Devin data is read-only. Windows/PowerShell, uv, one command per call, no git config changes. Stage explicit paths.

## Review Focus

1. A page file changed with no commit: search must show the change.
2. Two sessions share one LocalIndex: cleanup of one session must not remove keys of the other.
3. SessionStart lint retires a page: the injected index of the same call does not show it.
4. A session that never sends SessionEnd: its keys go away after 7 days.
5. An unchanged transcript on every prompt: no parse after the first.

## Tasks (in order; each one is a vertical slice that keeps the suite green)

### Task 1: Guard tests for the target structure (failing first)

**Files:** Create `tests/test_architecture.py`.
**Demo:** the AST tests show the current violations.
**Fails at base:** yes (private imports, hooks imports, state calls outside one module).

- [ ] Write AST tests. The scan root is `src/recall/**/*.py` only (tests and `__main__.py` are out of scope).
  - (a) only `hooks`, `mcp_server`, `cli` import `recall.hooks` / `recall.mcp_server`.
  - (b) no module imports a name with a leading `_` from another `recall` module, AND no module reads an attribute with a leading single `_` (not dunder) on any object except `self` and `cls` (catches `store._raw_files()`, `store.history._run(...)`).
  - (c) only `session_state` calls `get_state`, `set_state`, `state_keys`, `delete_state` or `delete_injected`.
  - Mark each with `pytest.mark.xfail(strict=True, reason="deepening task N")`, so the suite stays green. Remove xfail (a) in Task 2, (b) in Task 3, (c) in Task 5.
- [ ] Run `uv run pytest -q`. Expected: pass with 3 xfail. Commit.

### Task 2: One-way dependencies (markers leaf, guard plain decision)

**Files:** Create `src/recall/markers.py`. Change `session_reader.py`, `guard.py`, `hooks.py`, `context_gauge.py`. Tests: `tests/test_architecture.py`, `tests/test_guard.py`.
**Seam under test:** `guard.decide(store_root, repo_root, payload) -> str | None` (block reason or None); `markers` constants.
**Demo:** session_reader and guard import no adapter.
- [ ] Move the injected-text constants (index header, start hint, pending text, gauge text) to `markers.py`. hooks, context_gauge and session_reader import them from there.
- [ ] `guard.decide` returns the reason; hooks turns it into exit 2. Keep all guard tests (they go through `hooks.run`).
- [ ] Remove xfail (a). Full suite. Commit.

### Task 3: Store and History public interface

**Files:** Change `store.py`, `history.py`, `lint.py`, `semantic.py`, `evaluation.py`, `seed.py`. Tests: existing + `tests/test_architecture.py`.
**Seam under test:** `Store.append_log(op, title, session, when=None)`, `Store.raw_files()`, `Store.write_index()`, `Store.read_text(rel)`, `Store.write_text(rel, text)`; `History.status(paths)`, `History.untracked(paths)`, `History.restore_page(sha, page)`, `History.revert_commit(sha)`.
**Demo:** no private import across modules.
- [ ] Add the public operations (they wrap the existing private helpers; `_log_text` redaction stays inside Store). Move the git steps of `Store.revert` and `Store.uncommitted_raw` into the History operations.
- [ ] Change lint, semantic, evaluation and seed to use only public operations.
- [ ] Add tests for `History.revert_commit` conflict abort and `restore_page` through `Store.revert` (existing revert tests stay).
- [ ] Remove xfail (b). Full suite. Commit.

### Task 4a: SessionState module and LocalIndex key operations

**Files:** Create `src/recall/session_state.py`. Change `localindex.py` (add `state_keys(prefix) -> list[str]`, `delete_state(keys)`, `delete_injected(session)`). Test `tests/test_session_state.py`. No caller changes in this task.
**Demo:** the operations and the cleanup work in isolation.
- [ ] Tests first: each named operation reads and writes today's exact key names; `end_session` removes all session-scope keys and the injected rows of that session only; `cleanup_stale` removes a session unseen for 8 days, keeps a session seen 1 day ago, keeps `gauge_observed:` and store keys; a session with `start:` but no `seen:` uses the `start:` time; a session key with neither time is removed.
- [ ] Implement. Full suite. Commit.

### Task 4b: Move session-scope callers to SessionState

**Files:** Change `hooks.py`, `context_gauge.py`, `seed.py`. (Maintenance-scope keys in `semantic.py`, `evaluation.py`, `mcp_server.py` move in Task 5.)
**Seam under test:** `SessionState(index)` with named operations: `start(sid)`, `set_start(sid, t)`, `ensure_start(sid, t)`, `stop_block`/`set_stop_block`, `post_compact`/`set_post_compact`/`clear_post_compact`, `model`/`set_model`, `gauge_tokens`/`set_gauge_tokens`, `observed_limit(model)`/`set_observed_limit(model, value, t)`, `last_review`/`set_last_review`, `lint_due`/`set_lint_due`, `eval_due`/`set_eval_due`, `fingerprint`/`set_fingerprint`, `transcript(sid)`/`set_transcript(sid, size, mtime_ns, result)`, `touch(sid, t)`, `end_session(sid)`, `cleanup_stale(now, max_age_s=7*86400)`.
**Demo:** SessionEnd removes the session keys and injected rows; a session unseen for 8 days is removed at the next SessionStart; another session stays.
- [ ] Tests first (through `hooks.run`): after SessionEnd of session A, A's keys and injected rows are gone and session B's keys stay (Review Focus 2); SessionStart removes a session unseen for 8 days.
- [ ] Replace the session-scope `get_state`/`set_state` calls in hooks, context_gauge and seed. Hooks call `touch` on each call with a session id, `end_session` at SessionEnd, `cleanup_stale` at SessionStart. Keep the current `start:` write position (Task 6 adds the provisional start, Task 7 moves the write after the sections).
- [ ] Full suite. Commit.

### Task 5: Maintenance

**Files:** Create `src/recall/maintenance.py` with the code of `lint.py` and `semantic.py`. Remove `lint.py` and `semantic.py` in this task. Change all importers: `hooks.py`, `mcp_server.py`, `evaluation.py` (scoring only), `tests/test_lint.py`, `tests/test_eval.py`, `tests/test_integration_fixes.py`, `tests/test_review_round2.py` (search for `recall.lint` and `recall.semantic` and change each one). Move the maintenance-scope state calls of `semantic.py`, `evaluation.py` and `mcp_server.py` to SessionState. Remove xfail (c). Test `tests/test_maintenance.py`.
**Seam under test:** `maintenance.lint_mechanical(...)` (same signature), `review_due(store, state, now) -> bool`, `eval_due(store, state, now) -> bool`, `mark_review_done(state, now)`, `mark_eval_done(state, now)`, `semantic_prompt(store)`, `session_start_section(...)`.
**Demo:** a due check makes no write; the 7-day clock starts at the store's first commit.
- [ ] Tests first: `review_due` on a new store returns False and writes nothing (count state writes); 8 days after the first commit returns True; after `mark_review_done` returns False.
- [ ] Move code; MCP `recall_lint(semantic=True)` and `recall_eval` use `mark_*`. Existing lint, eval and semantic tests keep passing (update imports only).
- [ ] Full suite. Commit.

### Task 6: Runtime (both adapters)

**Files:** Create `src/recall/runtime.py`. Change `hooks.py`, `mcp_server.py`. Test `tests/test_runtime.py`.
**Seam under test:** `Runtime.open(repo_root) -> Runtime` (lazy, no git, no scan); properties `store`, `index` (fresh: rebuild only on fingerprint change or empty index), `state` (SessionState); `pages() -> list[tuple[rel, front, body]]` (lazy; cached with `Store.generation`, read again when it changed); `run_mcp(fn) -> dict` and `run_hook(fn) -> HookResult` (error mapping exactly as today, see spec). Shared `store_folder_correction(path)`. Store gets `generation: int`, increased by every Store write (ingest, set_status, revert, cleanup_raw, write_index, add_raw). Sections receive the Runtime object.
**Demo:** a search with no page change does no rebuild; an uncommitted page edit is found by search.
- [ ] Tests first (counting with monkeypatch on `LocalIndex.rebuild` and `Store.read_page`): open does 0 git calls and 0 reads; two `index` uses with no change rebuild 1 time; `recall_mark` gives no rebuild (usage is not in the fingerprint); a page file edit (no commit) causes one rebuild and search finds the new text; `pages()` twice reads each page once; after a Store write the next `pages()` reads again; at SessionStart a page retired by lint is not in the injected index (Review Focus 3); MCP error text for each expected type and for an unexpected type is the same as today; MCP with `RECALL_REPO` inside `.devin/recall` resolves to the owner repo.
- [ ] Move both adapters to Runtime. Keep each adapter's repo resolution order, plus the shared store-folder correction. Runtime writes a provisional `start:` at the first hook call of a session if it is missing. lint, pending count and the index write use `Runtime.pages()` in a SessionStart call.
- [ ] Full suite. Commit.

### Task 7: Flow fixes

**Files:** Change `hooks.py`, `context_gauge.py`, `session_reader.py` (transcript read through a cache provided by SessionState). Test `tests/test_flow.py`.
**Demo:** Stop with `stop_hook_active` or in cooldown starts no git process; an unchanged transcript is parsed one time; session start time excludes Seed time.
- [ ] Tests first: count `History` git calls (monkeypatch `History._run` in the test) after repo resolution for an early-exit Stop (0; the hook's `rev-parse --show-toplevel` is exempt); a Stop that blocks writes the final record only when it blocks and runs the ingest query last; with a seed commit and no ingest, Stop still blocks; transcript parsed once for two prompts with an unchanged file, again after size or mtime changes; `start:<sid>` after SessionStart is later than a slow section (monkeypatch a section that advances a fake clock); a first hook call that is not SessionStart sets a provisional start.
- [ ] Implement. Full suite. Commit.

### Task 8: Final check

- [ ] `uv run pytest -q` and `uv run pytest -q --durations=10`. Report the time.
- [ ] Update `AGENTS.md` status and the Shared Interfaces section of `docs/godmode/plans/2026-10-30-recall.md` for the new modules. Commit.
