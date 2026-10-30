"""Context Gauge: estimate how full the session context is and ask for a save before compaction.

Reads Devin data only through the read-only SessionReader. The model is read on each call, so a
model switch has an effect on the next prompt. Only the observed limit (a scan of all sessions) is
cached per model in the LocalIndex state, for OBSERVED_TTL_S.
"""
from __future__ import annotations

import json
import os
import re
import statistics
import time
from importlib import resources
from pathlib import Path

from recall.markers import NEAR_MSG, URGENT_MSG
from recall.paths import default_config_base, devin_config_bases, local_base, log_error
from recall.session_state import SessionState

DEFAULT_LIMIT = 150_000
REGISTRY_FACTOR = 0.75
MIN_SAMPLES = 3
REPEAT_STEP = 0.10
OBSERVED_TTL_S = 86_400
_SUFFIX = re.compile(r"-(low|medium|high|xhigh|max)$")


# ---------- configuration ----------
def _read_json(p: Path) -> dict:
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def devin_config_path() -> Path:
    env = os.environ.get("RECALL_DEVIN_CONFIG")
    if env:
        return Path(env)
    for b in devin_config_bases():
        if (b / "devin" / "config.json").is_file():
            return b / "devin" / "config.json"
    return default_config_base() / "devin" / "config.json"


def load_devin_config() -> dict:
    """Devin user config (read only). Missing or broken file -> {}."""
    return _read_json(devin_config_path())


def _cfg_value(cfg: dict, dotted: str):
    if dotted in cfg:
        return cfg[dotted]
    cur = cfg
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _positive_int(v) -> int | None:
    if isinstance(v, bool):
        return None
    try:
        n = int(float(v))
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def _ratio_setting(env, key: str, default: float) -> float:
    try:
        return float(env.get(key) or default)
    except (TypeError, ValueError):
        return default


# ---------- model registry ----------
def load_registry() -> dict[str, int]:
    """Built-in registry, then the user `models.json` (adds or replaces entries)."""
    reg: dict[str, int] = {}
    try:
        reg.update(json.loads((resources.files("recall") / "templates" / "models.json").read_text("utf-8")))
    except (OSError, ValueError) as e:
        log_error(None, f"context_gauge: built-in registry: {e}")
    reg.update(_read_json(local_base() / "models.json"))
    return {str(k).lower(): n for k, v in reg.items() if (n := _positive_int(v))}


def base_model(name: str) -> str:
    """Drop the thinking-level suffix: it does not change the context window."""
    return _SUFFIX.sub("", (name or "").strip().lower())


def lookup(registry: dict[str, int], model: str) -> int | None:
    m = base_model(model)
    if not m:
        return None
    for cand in (m, m.removeprefix("claude-")):
        if cand in registry:
            return registry[cand]
    # e.g. "fusion-2026-x" -> "fusion": longest registry key that is a name prefix.
    keys = [k for k in registry if m.startswith(k + "-") or m.removeprefix("claude-").startswith(k + "-")]
    return registry[max(keys, key=len)] if keys else None


# ---------- gauge ----------
def _observed(reader, model: str, index=None) -> int | None:
    """Median compaction point of this model. With `index`, cached per base model for OBSERVED_TTL_S."""
    m = base_model(model)
    if not m:
        return None
    ss = SessionState(index) if index is not None else None
    if ss is not None:
        cached = ss.observed_limit(m)
        if cached is not None:
            value, at = cached
            if 0 <= time.time() - at < OBSERVED_TTL_S:
                return value or None
    samples = [t for mod, t in reader.compaction_samples() if base_model(mod) == m]
    found = int(statistics.median(samples)) if len(samples) >= MIN_SAMPLES else None
    if ss is not None:
        ss.set_observed_limit(m, found, time.time())
    return found


def _limit(reader, model: str, env, devin_config, registry, index=None) -> tuple[int, str]:
    if n := _positive_int(env.get("RECALL_CONTEXT_LIMIT")):
        return n, "env"
    if n := _positive_int(_cfg_value(devin_config or {}, "agent.compaction_threshold_tokens")):
        return n, "config"
    if n := _observed(reader, model, index):
        return n, "observed"
    if n := lookup(registry or {}, model):
        return int(n * REGISTRY_FACTOR), "registry"
    return DEFAULT_LIMIT, "default"


class _TranscriptCache:
    """SessionState transcript cache of hook session `sid`, valid only for Devin session `devin_sid`."""

    def __init__(self, state: SessionState, sid: str, devin_sid: str):
        self.state, self.sid, self.devin_sid = state, sid, devin_sid

    def get(self):
        hit = self.state.transcript(self.sid)
        if hit is None:
            return None
        size, mtime_ns, wrapped = hit
        if not (isinstance(wrapped, dict) and wrapped.get("devin") == self.devin_sid):
            return None
        return size, mtime_ns, wrapped.get("status")

    def put(self, size: int, mtime_ns: int, result) -> None:
        self.state.set_transcript(self.sid, size, mtime_ns, {"devin": self.devin_sid, "status": result})


def context_status(reader, session_id: str, env, devin_config, registry, index=None,
                   cache_sid: str | None = None) -> dict:
    """{tokens, limit, ratio, source, model_source, token_source}. `tokens`/`ratio` are None when no
    token count exists.

    The transcript (`transcripts/<id>.json`, read-only) wins for model and tokens when usable. Without
    it, the model comes from the PreCompact state `model:<id>` in `index`, then sessions.db.
    `index` (a LocalIndex) also caches the observed limit per model; None disables the cache.
    With `index` and `cache_sid` (the hook session id), the transcript result is cached by file
    size and mtime_ns, so an unchanged transcript is not parsed again."""
    cache = (_TranscriptCache(SessionState(index), cache_sid, session_id)
             if index is not None and cache_sid else None)
    tr = (reader.transcript_status(session_id, cache=cache) if cache is not None
          else reader.transcript_status(session_id)) or ("", None)
    tokens, token_source = tr[1], "transcript"
    if tokens is None:
        tokens, token_source = reader.token_count(session_id), "sessions_db"
    if tokens is None:
        return {"tokens": None, "limit": None, "ratio": None, "source": None,
                "model_source": None, "token_source": None}
    model, model_source = tr[0], "transcript"
    if not model and index is not None:
        model, model_source = SessionState(index).model(session_id) or "", "precompact"
    if not model:
        model, model_source = reader.session_model(session_id), "sessions_db"
    limit, source = _limit(reader, model, env, devin_config, registry, index)
    return {"tokens": tokens, "limit": limit, "ratio": round(tokens / limit, 4), "source": source,
            "model_source": model_source, "token_source": token_source}


# ---------- UserPromptSubmit section ----------
def _ingested_since(store, since: float) -> bool:
    from datetime import datetime
    iso = datetime.fromtimestamp(int(since)).astimezone().isoformat()
    return bool(store.history.log(op="ingest", since=iso, limit=1))


def prompt_section(rt, payload) -> str:
    """Save request when the context is near or over its limit; '' otherwise. Never raises.
    `rt`: the Runtime (store, local index for the caches, session state)."""
    store = rt.store
    try:
        from recall.session_reader import SessionReader
        sid = str(payload.get("session_id") or "")
        reader = SessionReader()
        if not sid:
            return ""
        devin_sid = reader.resolve_session(sid, store.repo_root)  # one keyed lookup for a known id
        if not devin_sid:
            return ""
        env = os.environ
        st = context_status(reader, devin_sid, env, load_devin_config(), load_registry(), rt.local,
                            cache_sid=sid)
        if st["ratio"] is None:
            return ""
        near = _ratio_setting(env, "RECALL_NEAR_RATIO", 0.85)
        over = _ratio_setting(env, "RECALL_OVER_RATIO", 1.0)
        if st["ratio"] < near:
            return ""
        ss = rt.state
        prev = ss.gauge_tokens(sid)
        if prev is not None:
            last_tok, last_t = prev
            grew = st["tokens"] >= last_tok + REPEAT_STEP * st["limit"]
            if not (grew or st["tokens"] < last_tok or _ingested_since(store, last_t)):
                return ""
        ss.set_gauge_tokens(sid, st["tokens"], time.time())
        msg = URGENT_MSG if st["ratio"] >= over else NEAR_MSG
        return msg.format(pct=round(st["ratio"] * 100), tokens=st["tokens"], limit=st["limit"],
                          source=st["source"])
    except Exception as e:
        log_error(store.repo_root, e)
        return ""
