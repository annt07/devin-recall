"""Injected-text markers: every text Recall injects into the agent context (leaf module, no imports).

The Session Reader uses these texts to skip Recall's own injections when it captures raw records.
"""
from __future__ import annotations

INDEX_HEADER = "# Recall index"
START_HINT = "Use recall_search/recall_get for details. Save durable learnings only with recall_ingest."
OFF_MSG = "Recall is off: this folder is not a git repository."
MISMATCH_PREFIX = "Memory is newer than the checked-out code; verify stale pages:"
PENDING_MSG = "Pending raw records: {n}. Ingest them with recall_ingest."
STOP_BLOCK_MSG = ("Recall: save durable learnings with recall_ingest (cite or consume {n} pending raw records). "
                  "Then stop.")
NEAR_MSG = ("Recall: the context is {pct}% full ({tokens}/{limit} tokens, source {source}). "
            "Save durable learnings now with recall_ingest, before compaction.")
URGENT_MSG = ("URGENT Recall: the context is over its limit ({pct}%, {tokens}/{limit} tokens, source {source}). "
              "Compaction is imminent: save durable learnings with recall_ingest now.")


def own_markers() -> tuple[str, ...]:
    """Fixed prefixes of the injected texts (the part before the first format field)."""
    return tuple(m.split("{")[0] for m in (INDEX_HEADER, START_HINT, OFF_MSG, MISMATCH_PREFIX, PENDING_MSG,
                                           STOP_BLOCK_MSG, NEAR_MSG, URGENT_MSG))
