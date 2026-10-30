# Research: Real Devin CLI hook payloads (Task 0 probe)

**Date:** 2026-10-30
**Version investigated:** Devin CLI 3000.11.228, Windows. One live session (`fascinated-outfit`, model `swe-2-medium`) in a probe git repo, with logging hooks for 8 events. Steps: write a file, run a subagent that reads it, `/compact`, one prompt, exit.

## Findings

### All registered events fire

12 records: SessionStart, UserPromptSubmit (2), PreToolUse (3), Stop (2), PreCompact, PostCompaction, PostCompact, SessionEnd. `DEVIN_PROJECT_DIR` is set for each event. Source: `recall-probe/payloads.jsonl`.

### stdin fields of each event

| Event | Fields (besides `hook_event_name`, `session_id`) |
|---|---|
| SessionStart | `source` (`startup`) |
| UserPromptSubmit | `prompt`, `prompt_id` |
| PreToolUse | `prompt_id`, `tool_name`, `tool_input`, `tool_use_id` |
| Stop | `prompt_id`, `stop_hook_active` (bool), `last_assistant_message` |
| PreCompact | `cwd`, `model`, `transcript_path`, `trigger` (`manual`), `turn_id` |
| PostCompact | the same fields as PreCompact |
| PostCompaction | `prompt_id`, `summary` |
| SessionEnd | `prompt_id`, `reason` (`prompt_input_exit`) |

### Session id

The hook `session_id` (`fascinated-outfit`) is equal to `sessions.id` in `sessions.db`. The fallback in `resolve_session` is not necessary in normal use.

### Write and read tool path key

`write` has `tool_input` keys `file_path`, `content`. `read` has `file_path`. The guard key list in `guard.py` includes `file_path`. `edit` was not observed.

### Subagents

The subagent tool call (`run_subagent`) fires PreToolUse. The tool call of the subagent (`read`) also fires PreToolUse, with the parent `session_id` and `prompt_id`. No SessionStart, Stop or SessionEnd fired for the subagent. Thus the write guard also protects the store from subagents, and subagents do not get Stop blocks or index injection.

### PreCompact gives the model and a transcript

PreCompact and PostCompact give `model` (`swe-2-medium`) and `transcript_path` (`%APPDATA%\devin\cli\transcripts\<session_id>.json`). The transcript is JSON, `schema_version` `ATIF-v1.7`, with keys `schema_version`, `session_id`, `agent` (`model_name`), `steps`, `final_metrics`. Agent steps have `model_name` and `metrics.prompt_tokens`. `final_metrics` has `total_prompt_tokens`. Only 68 transcript files exist for 490 sessions, so a transcript is not always present.

## Contradictions or Gaps

- The probe did not test if PreCompact can block or inject context. The design keeps PreCompact as best-effort.
- `edit` and `apply_patch` payloads were not observed.

## Conclusion

The design assumptions hold. Two improvements are possible: (1) the Context Gauge can read the current model and the last `prompt_tokens` from `transcripts/<session_id>.json` (read-only) when the file exists, which detects a model switch better than `sessions.model`; (2) PreCompact can record `model` for the gauge.
