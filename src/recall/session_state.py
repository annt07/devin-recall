"""SessionState: the only owner of LocalIndex state key names, their encodings and their cleanup.

Scope (spec, SessionState):
- session: start:, stop_block:, post_compact:, model:, gauge:, seen:, transcript:, and the injected
  rows of the session. Removed at SessionEnd, or by cleanup_stale when unseen for max_age_s.
- model: gauge_observed:<model>. Never removed (callers refresh after a TTL).
- store: lint_due, eval_due, last_semantic_lint, index_fingerprint. Never removed.
"""
from __future__ import annotations

import json

_START = "start:"
_STOP_BLOCK = "stop_block:"
_POST_COMPACT = "post_compact:"
_MODEL = "model:"
_GAUGE = "gauge:"
_SEEN = "seen:"
_TRANSCRIPT = "transcript:"
_OBSERVED = "gauge_observed:"
_LINT_DUE = "lint_due"
_EVAL_DUE = "eval_due"
_LAST_REVIEW = "last_semantic_lint"
_FINGERPRINT = "index_fingerprint"

SESSION_PREFIXES = (_START, _STOP_BLOCK, _POST_COMPACT, _MODEL, _GAUGE, _SEEN, _TRANSCRIPT)
MAX_AGE_S = 7 * 86400


def _float(v: str | None) -> float | None:
    try:
        return float(v) if v is not None else None
    except ValueError:
        return None


class SessionState:
    def __init__(self, index):
        self.index = index

    def _get(self, key: str) -> str | None:
        return self.index.get_state(key)

    def _set(self, key: str, value: str | None) -> None:
        self.index.set_state(key, value)

    # ---- session scope ----
    def start(self, sid: str) -> float | None:
        return _float(self._get(_START + sid))

    def set_start(self, sid: str, t: float) -> None:
        self._set(_START + sid, str(t))

    def ensure_start(self, sid: str, t: float) -> None:
        """Provisional start: write start:<sid> only when it is missing."""
        if self._get(_START + sid) is None:
            self.set_start(sid, t)

    def stop_block(self, sid: str) -> float | None:
        return _float(self._get(_STOP_BLOCK + sid))

    def set_stop_block(self, sid: str, t: float) -> None:
        self._set(_STOP_BLOCK + sid, str(t))

    def post_compact(self, sid: str) -> bool:
        return self._get(_POST_COMPACT + sid) is not None

    def set_post_compact(self, sid: str, t: float) -> None:
        self._set(_POST_COMPACT + sid, str(t))

    def clear_post_compact(self, sid: str) -> None:
        self._set(_POST_COMPACT + sid, None)

    def model(self, sid: str) -> str | None:
        return (self._get(_MODEL + sid) or "").strip() or None

    def set_model(self, sid: str, model: str) -> None:
        self._set(_MODEL + sid, model.strip())

    def gauge_tokens(self, sid: str) -> tuple[int, float] | None:
        """(tokens, time) of the last gauge message, or None."""
        parts = (self._get(_GAUGE + sid) or "").split()
        if len(parts) != 2:
            return None
        try:
            return int(float(parts[0])), float(parts[1])
        except ValueError:
            return None

    def set_gauge_tokens(self, sid: str, tokens: int, t: float) -> None:
        self._set(_GAUGE + sid, f"{tokens} {t}")

    def transcript(self, sid: str) -> tuple[int, int, object] | None:
        """Cached (size, mtime_ns, result) of the session transcript, or None."""
        try:
            d = json.loads(self._get(_TRANSCRIPT + sid) or "")
            return int(d["size"]), int(d["mtime_ns"]), d["result"]
        except (ValueError, TypeError, KeyError):
            return None

    def set_transcript(self, sid: str, size: int, mtime_ns: int, result) -> None:
        self._set(_TRANSCRIPT + sid, json.dumps({"size": int(size), "mtime_ns": int(mtime_ns),
                                                 "result": result}))

    def touch(self, sid: str, t: float) -> None:
        self._set(_SEEN + sid, str(t))

    def end_session(self, sid: str) -> None:
        self.index.delete_state([p + sid for p in SESSION_PREFIXES])
        self.index.delete_injected(sid)

    def cleanup_stale(self, now: float, max_age_s: float = MAX_AGE_S) -> None:
        """Remove sessions whose last-seen time (else start time) is older than max_age_s.
        A session with neither time is removed."""
        sids: set[str] = set()
        for p in SESSION_PREFIXES:
            sids.update(k[len(p):] for k in self.index.state_keys(p))
        for sid in sorted(sids):
            seen = _float(self._get(_SEEN + sid))
            t = seen if seen is not None else self.start(sid)
            if t is None or now - t > max_age_s:
                self.end_session(sid)

    # ---- model scope ----
    def observed_limit(self, model: str) -> tuple[int, float] | None:
        """(value, time) cached for a base model; value 0 means "no samples". None when absent."""
        parts = (self._get(_OBSERVED + model) or "").split()
        if len(parts) != 2:
            return None
        try:
            return int(parts[0]), float(parts[1])
        except ValueError:
            return 0, 0.0

    def set_observed_limit(self, model: str, value: int | None, t: float) -> None:
        self._set(_OBSERVED + model, f"{value or 0} {t}")

    # ---- store scope ----
    def last_review(self) -> float | None:
        return _float(self._get(_LAST_REVIEW))

    def set_last_review(self, t: float) -> None:
        self._set(_LAST_REVIEW, str(t))

    def lint_due(self) -> bool:
        return bool(self._get(_LINT_DUE))

    def set_lint_due(self, due: bool) -> None:
        self._set(_LINT_DUE, "1" if due else None)

    def eval_due(self) -> bool:
        return bool(self._get(_EVAL_DUE))

    def set_eval_due(self, due: bool) -> None:
        self._set(_EVAL_DUE, "1" if due else None)

    def fingerprint(self) -> str | None:
        return self._get(_FINGERPRINT)

    def set_fingerprint(self, fp: str) -> None:
        self._set(_FINGERPRINT, fp)
