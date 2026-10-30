# Recall

Recall is a local, automatic memory for one repo. Devin uses it across sessions and across compactions.

## Language

**Store**:
The memory folder of one repo, with its own git history.
_Avoid_: database, cache

**Page**:
One Markdown topic file in the Store. The agent edits it in place. It has a type: decision, fact, howto, gotcha, preference or task.
_Avoid_: item, note, entry

**Raw record**:
An immutable text from a session (compaction summary, final message, prompts). Pages cite raw records as sources.
_Avoid_: transcript, log entry

**Index**:
The catalog of all active pages, one line for each page. Recall injects it at session start.

**Log**:
The append-only, human-readable timeline of Store operations.

**Ingest**:
The operation that merges new knowledge from raw records into pages.
_Avoid_: save, capture

**Seed**:
The one-time ingest of the old sessions of a repo from the local Devin session data.
_Avoid_: backfill, import

**Gate**:
The write-time quality rules. A page change is accepted, rejected or redirected to an existing page.

**Lint**:
The health operation. Mechanical lint verifies anchors, links and freshness. Semantic lint finds contradictions and merge candidates.

**Eval**:
The golden-question test that gives a quality score for the Store.

**Anchor**:
A code location that a page cites, with the git hash of that code. A changed hash makes the page stale.

**History**:
The git commits of the Store, with Recall trailers. It permits views and reverts at any point in time.

**Ref**:
A point in History: a date, a session, a project commit, an operation or the best eval score.

**Runtime**:
The context of one hook call or one MCP tool call. It opens the Store and the search index and gives one page scan for that call.

**Session state**:
The local facts about one Devin session (start time, Stop block, compaction flag, model) and the maintenance schedule. It is not part of the Store.

**Maintenance**:
Lint, the semantic review and Eval, with their schedule.

## Relationships

- An **Ingest** reads one or more **Raw records** and changes one or more **Pages**.
- The **Gate** verifies each **Page** change before the **Store** accepts it.
- Each **Ingest**, **Lint** and revert makes one **History** commit.
- **Seed** is a series of **Ingests**, one for each old session.

## Flagged ambiguities

- "memory item" was the first unit. The design now uses **Page** and does not use atomic items.
