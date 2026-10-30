# Recall schema

Recall is the versioned wiki memory of this repository. Write pages only with the
`recall_ingest` tool. Never edit files in `.devin/recall/` with `write`, `edit` or a shell.

## Page types

Each page is `pages/<type>/<slug>.md`. The slug uses `a-z`, `0-9` and `-`.

- `decision`: a choice that was made, and why.
- `fact`: a durable fact about the code or the project.
- `howto`: steps to do a task.
- `gotcha`: a trap, and how to avoid it.
- `preference`: how the user wants the work done.
- `task`: open work that continues across sessions.

## Front matter

```yaml
type: fact            # equal to the folder
title: Build and test
summary: One line for index.md
status: active        # active | stale | retired
confidence: high      # high | medium | low
sources: [pyproject.toml]
anchors: {}           # set by ingest: git hash-object of each code source
updated: 2026-10-30   # set by ingest
stale_since: null
```

## Source forms

- `raw/<file>.md`: a raw record in this store.
- `commit:<sha>`: a commit in the code repository.
- `<repo path>` or `<repo path>:a-b`: a file (and optional line range) in the code repository.

Each page needs at least one source that exists.

## Rules

- One topic for each page. Edit the page in place; do not make a near-duplicate.
- Link related pages with `[[pages/<type>/<slug>]]`.
- A page is at most 800 tokens (about 3,200 characters). Split or condense.
- Never put secrets, tokens, passwords or keys in a page.

## Ingest workflow

1. Read `index.md` and the pending raw records.
2. For each durable learning, find the page for its topic with `recall_search`/`recall_get`.
3. Update that page, or make a new one. Cite sources.
4. Call `recall_ingest` with the pages, and list the raw records you consumed.
5. Fix each rejected or redirected page and ingest it again.

## Lint workflow

1. Run the mechanical lint (broken links, missing sources, stale anchors, orphans, size).
2. Mark pages whose cited code changed as `stale`, and verify them.
3. Merge duplicates, retire pages that are wrong or obsolete.
4. Ingest the fixes.
