# Recall (devin-recall)

Recall is a tool that other people install and use in any repo. It is not a one-off script for this machine.

## Rules for all work

- Portable: no hard-coded user names, machine paths or OneDrive paths. Find Devin data and local data folders from environment variables (`APPDATA`, `LOCALAPPDATA`, `RECALL_DEVIN_DIR`, `RECALL_LOCAL_DIR`), with a fallback for macOS/Linux (`~/.local/share`, `~/.config/devin`).
- Installable: `uv tool install` or `uvx` from git must work. `recall install <repo>` must work in any git repo.
- Devin internal data is read-only (see the spec).
- Python 3.11+, uv, pytest. Test command: `uv run pytest -q`.

## Status (2026-10-30)

- Spec: `docs/godmode/specs/2026-10-30-recall-design.md`. Plan: `docs/godmode/plans/2026-10-30-recall.md`.
- Done: Tasks 0-13, two review rounds and their fixes (see `git log`).
- Deepening (`docs/godmode/plans/2026-10-30-recall-deepening.md`): Tasks 1-8 done. New modules `runtime.py`, `session_state.py`, `maintenance.py` (replaces `lint.py` and `semantic.py`), `markers.py`. 250 tests pass (`uv run pytest -q`, about 3 minutes; the slowest tests are git-heavy history/seed tests at 2-4.5 s each).
- Hook latency: a UserPromptSubmit runs in about 0.1 s in-process. The single `git rev-parse --show-toplevel` (about 80 ms on Windows) is most of it. `tests/test_flow.py::test_user_prompt_stays_in_latency_budget` guards it (< 1 s, 0 History git calls, at most 1 process).
- Task 0 results: `docs/research/hook-payloads-2026-10-30.md`. The Context Gauge reads `transcripts/<session_id>.json` first.
- Next: a first real install with `recall install <repo>` in a real project.
- Open (minor): `stale_since: null` is written on active pages.
