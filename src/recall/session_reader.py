"""Read-only adapter for the Devin CLI ``sessions.db``.

HARD RULE: Devin data is read-only. The database is opened only with the URI
``mode=ro`` and only ``SELECT`` and ``PRAGMA table_info`` statements run.
"""
from __future__ import annotations

import json
import sqlite3
import stat
import time
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from recall.markers import own_markers
from recall.paths import devin_base, log_error

COMPACT_MARKER = "You are continuing work from a previous conversation thread"
TEMP_MARKER = "/appdata/local/temp/"
POSIX_TEMP_PREFIXES = ("/tmp/", "/private/tmp/", "/var/folders/", "/private/var/folders/")
TOKEN_SCAN_ROWS = 50
_SESSION_COLS = ("id", "working_directory", "created_at", "last_activity_at", "title", "workspace_dirs")
_NODE_COLS = ("row_id", "session_id", "chat_message")


@dataclass
class Session:
    id: str
    working_directory: str
    created_at: datetime
    last_activity: datetime
    title: str
    workspace_dirs: list[str]


def default_db_path() -> Path:
    return devin_base() / "devin" / "cli" / "sessions.db"


def _is_temp(p: str) -> bool:
    """A normalized path in an OS temp folder (Windows %TEMP%, POSIX /tmp, macOS /var/folders)."""
    return TEMP_MARKER in p + "/" or any((p + "/").startswith(m) for m in POSIX_TEMP_PREFIXES)


def _own_injection(text: str) -> bool:
    """True if a message is text that Recall itself injected (never re-captured as a raw record)."""
    return any(m in text for m in _OWN_MARKERS)


_OWN_MARKERS = own_markers()


def _log_error(msg: str) -> None:
    log_error(None, f"session_reader: {msg}")


def _norm(p) -> str:
    s = str(p).replace("\\", "/").strip().lower()
    return s.rstrip("/") if len(s) > 1 else s


def _under(path: str, root: str) -> bool:
    return bool(path) and (path == root or path.startswith(root + "/"))


def _ts(v) -> datetime:
    try:
        x = float(v)
    except (TypeError, ValueError):
        try:
            dt = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            return datetime.fromtimestamp(0, timezone.utc)
    if x > 1e11:
        x /= 1000.0
    return datetime.fromtimestamp(x, timezone.utc)


def _dirs(v) -> list[str]:
    if not v:
        return []
    try:
        data = json.loads(v)
        if isinstance(data, list):
            return [str(x) for x in data if x]
    except (ValueError, TypeError):
        pass
    return [p.strip() for p in str(v).replace(";", "\n").splitlines() if p.strip()]


def _text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for it in content:
            if isinstance(it, str):
                parts.append(it)
            elif isinstance(it, dict) and isinstance(it.get("text"), str):
                parts.append(it["text"])
        return "\n".join(parts)
    return ""


def _meta(raw) -> dict:
    try:
        data = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


class SessionReader:
    def __init__(self, db_path: Path | str | None = None):
        self.db_path = Path(db_path) if db_path else default_db_path()

    # --- low level (read-only) -------------------------------------------
    def _connect(self) -> sqlite3.Connection | None:
        if not self.db_path.is_file():
            return None
        uri = self.db_path.resolve().as_uri() + "?mode=ro"
        for attempt in range(2):
            try:
                return sqlite3.connect(uri, uri=True)
            except sqlite3.OperationalError:
                if attempt == 0:
                    time.sleep(0.5)
        _log_error(f"cannot open {self.db_path} read-only")
        return None

    @staticmethod
    def _has_cols(con: sqlite3.Connection, table: str, cols) -> bool:
        have = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
        missing = [c for c in cols if c not in have]
        if missing:
            _log_error(f"schema: {table} missing {missing}")
        return not missing

    def _select(self, table: str, cols, sql: str, params=()) -> list[tuple]:
        if not sql.lstrip().upper().startswith("SELECT"):
            raise ValueError("session_reader runs SELECT statements only")
        con = self._connect()
        if con is None:
            return []
        try:
            if not self._has_cols(con, table, cols):
                return []
            return con.execute(sql, params).fetchall()
        except sqlite3.Error as e:
            _log_error(f"{type(e).__name__}: {e}")
            return []
        finally:
            con.close()

    # --- public seam -----------------------------------------------------
    def sessions_for(self, repo_root, since_days: int | None = 90) -> list[Session]:
        root = _norm(repo_root)
        rows = self._select("sessions", _SESSION_COLS,
                            "SELECT id, working_directory, created_at, last_activity_at, title, workspace_dirs"
                            " FROM sessions")
        cutoff = None if since_days is None else datetime.now(timezone.utc).timestamp() - since_days * 86400
        out = []
        for sid, cwd, created, last, title, wdirs in rows:
            ncwd = _norm(cwd or "")
            if _is_temp(ncwd) and not _is_temp(root):
                continue
            dirs = _dirs(wdirs)
            if not any(_under(_norm(d), root) for d in [cwd or "", *dirs]):
                continue
            s = Session(str(sid), cwd or "", _ts(created), _ts(last if last is not None else created),
                        title or "", dirs)
            if cutoff is not None and s.last_activity.timestamp() < cutoff:
                continue
            out.append(s)
        out.sort(key=lambda s: s.last_activity, reverse=True)
        return out

    def records(self, session_id: str) -> list[tuple[str, str]]:
        try:
            rows = self._select("message_nodes", _NODE_COLS,
                                "SELECT chat_message FROM message_nodes WHERE session_id = ? ORDER BY row_id",
                                (session_id,))
            compact, prompts, final = None, [], None
            for (raw,) in rows:
                try:
                    msg = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                if not isinstance(msg, dict):
                    continue
                role, text = msg.get("role"), _text(msg.get("content")).strip()
                if not text or _own_injection(text):
                    continue
                if role in ("system", "user") and COMPACT_MARKER in text and "Summary:" in text:
                    compact = text.split("Summary:", 1)[1].strip()
                elif role == "user" and not text.startswith("<system"):
                    prompts.append(text)
                elif role == "assistant":
                    final = text
            out = []
            if compact:
                out.append(("compact", compact))
            if prompts:
                out.append(("prompts", "\n\n".join(prompts)))
            if final:
                out.append(("final", final))
            return out
        except Exception:
            _log_error(traceback.format_exc())
            return []

    # --- context gauge (read-only) ---------------------------------------
    def session_model(self, session_id: str) -> str:
        rows = self._select("sessions", ("id", "model"), "SELECT model FROM sessions WHERE id = ?", (session_id,))
        return str(rows[0][0] or "") if rows else ""

    def token_count(self, session_id: str) -> int | None:
        """Newest ``metadata.num_tokens_preceding`` of the session, or None."""
        rows = self._select("message_nodes", ("row_id", "session_id", "metadata"),
                            "SELECT metadata FROM message_nodes WHERE session_id = ? ORDER BY row_id DESC LIMIT ?",
                            (session_id, TOKEN_SCAN_ROWS))
        for (raw,) in rows:
            n = _meta(raw).get("num_tokens_preceding")
            if isinstance(n, (int, float)) and not isinstance(n, bool) and n >= 0:
                return int(n)
        return None

    def transcript_status(self, session_id: str, cache=None) -> tuple[str, int | None] | None:
        """(model, prompt_tokens) from ``transcripts/<session_id>.json`` (ATIF, opened read-only), or
        None when the file is missing or unusable. Model: the last step with ``model_name``, else
        ``agent.model_name``. Tokens: that step's ``metrics.prompt_tokens``, else the last step's that has them.

        `cache` (optional) has ``get() -> (size, mtime_ns, result) | None`` and
        ``put(size, mtime_ns, result)``. When the file size and mtime_ns match the cached ones,
        the cached result is returned and the file is not parsed."""
        if not session_id or any(c in session_id for c in "/\\:") or session_id.startswith("."):
            return None
        p = self.db_path.parent / "transcripts" / f"{session_id}.json"
        try:
            st = p.stat()
        except OSError:
            return None
        if not stat.S_ISREG(st.st_mode):
            return None
        if cache is not None:
            hit = cache.get()
            if hit is not None and hit[0] == st.st_size and hit[1] == st.st_mtime_ns:
                r = hit[2]
                if r is None:
                    return None
                if isinstance(r, list) and len(r) == 2:
                    return str(r[0]), (None if r[1] is None else int(r[1]))
        result = self._parse_transcript(p, session_id)
        if cache is not None:
            cache.put(st.st_size, st.st_mtime_ns, None if result is None else list(result))
        return result

    def _parse_transcript(self, p: Path, session_id: str) -> tuple[str, int | None] | None:
        try:
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict) or not str(data.get("schema_version", "")).startswith("ATIF-"):
                raise ValueError(f"unknown transcript schema {str(data.get('schema_version'))[:40]!r}"
                                 if isinstance(data, dict) else "transcript is not an object")
            steps = data.get("steps")
            steps = [s for s in steps if isinstance(s, dict)] if isinstance(steps, list) else []

            def tok(s):
                m = s.get("metrics")
                n = m.get("prompt_tokens") if isinstance(m, dict) else None
                return int(n) if isinstance(n, (int, float)) and not isinstance(n, bool) and n >= 0 else None

            model, tokens = "", None
            for s in reversed(steps):
                if isinstance(s.get("model_name"), str) and s["model_name"].strip():
                    model, tokens = s["model_name"].strip(), tok(s)
                    break
            if not model:
                agent = data.get("agent")
                m = agent.get("model_name") if isinstance(agent, dict) else None
                model = m.strip() if isinstance(m, str) else ""
            if tokens is None:
                tokens = next((n for s in reversed(steps) if (n := tok(s)) is not None), None)
            if not model and tokens is None:
                raise ValueError("transcript has no model and no prompt_tokens")
            return model, tokens
        except Exception as e:
            _log_error(f"transcript {session_id}: {type(e).__name__}: {e}")
            return None

    def compaction_samples(self) -> list[tuple[str, int]]:
        """(session model, tokens just before a ``summarized_from`` node) for every past compaction."""
        models = dict(self._select("sessions", ("id", "model"), "SELECT id, model FROM sessions"))
        rows = self._select("message_nodes", ("row_id", "session_id", "metadata"),
                            "SELECT session_id, metadata FROM message_nodes ORDER BY session_id, row_id")
        out, prev_sid, prev_tok = [], None, None
        for sid, raw in rows:
            if sid != prev_sid:
                prev_sid, prev_tok = sid, None
            meta = _meta(raw)
            if meta.get("summarized_from") and prev_tok:
                out.append((str(models.get(sid) or ""), prev_tok))
            n = meta.get("num_tokens_preceding")
            if isinstance(n, (int, float)) and not isinstance(n, bool):
                prev_tok = int(n)
        return out

    def resolve_session(self, hook_sid: str, repo_root) -> str | None:
        """The Devin session id for a hook session id. A known id costs one keyed lookup; only an
        unknown id falls back to the newest session of the repo (a full scan)."""
        if hook_sid and self._select("sessions", ("id",), "SELECT id FROM sessions WHERE id = ?", (hook_sid,)):
            return hook_sid
        sessions = self.sessions_for(repo_root, None)
        return sessions[0].id if sessions else None
