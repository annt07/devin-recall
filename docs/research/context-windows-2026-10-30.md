# Devin CLI 3000.11.228: context windows, auto-compaction, model switches

Read-only research. DOCS = `%LOCALAPPDATA%\devin\cli\_versions\3000.11.228\share\devin\docs`.

## 1. Documented behaviour

| Claim | Source |
|---|---|
| `/compact` forces compaction. `/context` shows how much of the context window is used. | DOCS\reference\commands.mdx:556, 559; essential-commands.mdx:248 |
| `agent.compaction_threshold_tokens` in user config makes auto-compaction start earlier than the default, which is based on the context window. | DOCS\changelog\stable.mdx:194 |
| `PreCompact` / `PostCompact` hooks exist, with `manual` and `auto` matchers. The older `PostCompaction` hook still works. | stable.mdx:63; hooks\overview.mdx:68; hooks\lifecycle-hooks.mdx:220-240 |
| `PostCompaction` sends `summary` on stdin. | lifecycle-hooks.mdx:228; stable.mdx:689 |
| Long conversations are compacted earlier, in the background. | stable.mdx:593 |
| Adaptive model no longer compacts on every turn without need. | stable.mdx:862 |
| Steps from before a compaction can still be used for revert and fork. | stable.mdx:662 |
| Resume fixes after compaction (no dropped messages, repeated notices fixed). Auto-compaction is no longer shown in scrollback. | stable.mdx:984, 1150, 415 |
| Model switch: `/model` and its shortcuts work mid-turn and apply to the **next request**. | stable.mdx:296 |
| `Alt+Enter` in the picker switches the model for this session only. `Enter` saves it as the default. | stable.mdx:55 |
| `--model` together with `-c`/`-r` switches the model for the rest of a resumed conversation. | stable.mdx:209 |
| Refusal fallback can switch the model in the middle of a turn (`DEVIN_REFUSAL_FALLBACK`). | stable.mdx:198 |
| OTel has a `compaction` event. | DOCS\reference\otel.mdx:149 |
| Hook stdin carries only `session_id` and `prompt_id` plus event fields. It has **no model and no token count**. | hooks\overview.mdx:109-128; lifecycle-hooks.mdx:9 |

**Not documented anywhere:** the default threshold as a percentage, or what happens when you move from a large-window model to a small one. Nothing in the docs or changelog covers it.

## 2. Model configs (`%LOCALAPPDATA%\devin\cli\model_configs_v{5,6}.<hash>.bin`)

- The file is JSON: `{version, identity_digest, fetched_at_secs, payload}`. `payload` is base64 of a protobuf with no schema (repeated model messages).
- Inside each model message, field 1 is the display name and a string field holds the id. **Varint field 18 is the context window in tokens.** No field names are present in the protobuf.
- Examples from v6.5e4d52bc458e6330:

| Model | Field 18 (context window) |
|---|---|
| claude-sonnet-4-6 | 200000 |
| swe-1-6 | 200000 |
| swe-2-high / swe-2-max | 262000 |
| gpt-5-4-* | 272000 |
| gpt-5-4-mini-* | 400000 |
| claude-opus-5-5-low/medium, gpt-6-*, fusion-* | 1000000 |
| adaptive | (absent) |

- I found no compaction-threshold field in the payload. The server-side default ratio is not in this file.
- Inference: field number 18 could change between schema versions (v5 vs v6), so this is fragile.

## 3. sessions.db (`%APPDATA%\devin\cli\sessions.db`, mode=ro)

- `sessions.model` holds **one value per session**, the current or last model. Counts: claude-opus-5-5-low 283, claude-sonnet-4-6 131, swe-2-high 49, swe-1-6 13, fusion-… 5, adaptive 3, swe-2-max 3, '' 3.
- `sessions.metadata` keys: `total_credit_cost`, `total_acu_cost`, `response_dimensions`, `client_meta`. There is no context or model history.
- `message_nodes.metadata` keys: `summarized_from`, `num_tokens_preceding`, `is_system_prefix`, `extensions` (for example `compact/prior_node_ids`).
- `chat_message` keys: `message_id`, `role`, `content`, `metadata`, `tool_calls`, `tool_call_id`, `thinking`. **The model is not recorded per message**, so you cannot see model switches in the DB. I found 0 sessions with more than one model.
- Token drops: I flagged a drop where a value is less than 0.6× the previous one and the previous one is over 20k. There were 3118 drops, and nearly all had `summarized_from` set, so they are compaction points.
  - The pre-drop maximum clusters for claude-sonnet-4-6 (200k) were **about 154k–158k, or 77–79% of the window**. There are also many smaller drops in the 50k–125k range.
  - Caveat: I ordered rows by row_id across branches and subagents, so some drops are noise.
- Maximum `num_tokens_preceding` per session model:

| Model | Max tokens | Notes |
|---|---|---|
| opus-5-5-low | 796925 | |
| swe-2-high | 798166 | over its 262k window |
| fusion | 797887 | |
| sonnet-4-6 | 268405 | over its 200k window |
| swe-2-max | 261526 | |
| swe-1-6 | 170728 | |
| adaptive | 167005 | |

  - **Contradiction:** the swe-2-high and sonnet maxima are larger than the configured windows. Either the session ran on a 1M model earlier and `sessions.model` now shows the switched-to model, or the token counts include branches or subagents. This fits the large→small switch case. After a switch, history can be far over the new window until the next compaction check runs.
- Inference: auto-compaction seems to trigger at about 80% of the window, or earlier when the background pass runs.

## 4. config.json (`%APPDATA%\devin\config.json`)

- Relevant keys: `agent.model` (default for new sessions) and `agent.preferred_family_models.*`.
- `agent.compaction_threshold_tokens` is **not set**, so the default based on the window applies.

## 5. Can a hook get these values (read-only)?

| | Answer |
|---|---|
| (a) Model | Partly. `sessions.model` WHERE id = hook `session_id`. That the two ids are the same is assumed, not verified. The value reflects the latest switch only when it is persisted, and a session-only switch may not be. |
| (b) Window | Yes. Decode model_configs_v6 payload, find the message whose id equals the model, read varint field 18. `adaptive` has none. |
| (c) Tokens | Approximate. Latest `num_tokens_preceding` on the session's main chain (`sessions.main_chain_id` → walk `parent_node_id`). This lags by one message. |
| (d) Threshold | Only the user override `agent.compaction_threshold_tokens` is readable. The default ratio is not exposed. Observed ≈0.78–0.8 × window. |

## Recommended algorithm (hook: "near compaction?")

1. Read `session_id` from stdin. Open sessions.db with `?mode=ro`, retrying on lock.
2. Model = `sessions.model`. If it is empty or `adaptive`, use the smallest window among likely models (200000).
3. Window = field 18 from the newest model_configs_v6 file. If missing, fall back to a built-in table (200k / 262k / 272k / 400k / 1M), and if still unknown use 200000.
4. Threshold = min(`agent.compaction_threshold_tokens` if set, 0.75 × window). That is a little below the observed ~0.78 to allow for lag.
5. Tokens = `num_tokens_preceding` of the newest node on `main_chain_id` (ignore subagent chains), plus an estimate of the newest message (chars/4).
6. Near = tokens ≥ 0.9 × threshold. Over = tokens ≥ threshold, which also covers the case right after a large→small switch where tokens can already exceed the new window.
7. Every time the hook runs, recompute from the current model; never cache the window per session. Use `PreCompact`/`PostCompaction` events to reset any state.
