---
name: recall
description: Use the Recall wiki memory of this repo. Read it before you explore code, and save durable knowledge with the recall MCP tools.
---

# Recall

Recall is the versioned wiki memory of this repo. Hooks inject its index and matching pages. You read and write it only through the `recall_*` MCP tools.

## Read

- Read the injected `# Recall index` first.
- Call `recall_search` / `recall_get` before you explore the code again.
- For "what did we know at X", use `recall_show_at` and `recall_diff`.

## Write

- Save only durable knowledge, with one of the types: `decision`, `fact`, `howto`, `gotcha`, `preference`, `task`.
- One topic per page. Update the existing page; do not make a near-duplicate.
- Each page cites its sources: `raw/<file>.md`, a code path (`src/x.py` or `src/x.py:10-20`) or `commit:<sha>`.
- Never save secrets, tokens, passwords or personal data.
- Save with `recall_ingest`. For Seed raw records, pass `trigger="Seed"`.

## Rules

- Never edit `.devin/recall/` with file tools or with the shell. Use the MCP tools.
- When the Stop hook asks, cite every pending raw record in an ingest, or consume it if it holds nothing durable.
- Call `recall_mark(path, "used")` when a page helped, and `recall_mark(path, "wrong")` when it was wrong.
- To undo a bad change, use `recall_revert`.
