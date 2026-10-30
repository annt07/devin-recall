"""LocalIndex: per-machine SQLite FTS5 search, usage counters, injected pages, state keys."""
from __future__ import annotations

import re
import sqlite3
from contextlib import closing
from pathlib import Path

from recall.paths import local_dir

_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS pages_fts USING fts5(path UNINDEXED, title, body, status UNINDEXED);
CREATE TABLE IF NOT EXISTS usage (path TEXT PRIMARY KEY, used INTEGER NOT NULL DEFAULT 0,
    wrong INTEGER NOT NULL DEFAULT 0, injected INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS injected (session TEXT NOT NULL, path TEXT NOT NULL, PRIMARY KEY (session, path));
CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT);
"""


class LocalIndex:
    def __init__(self, repo_root: Path):
        self.dir = local_dir(repo_root)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.db = self.dir / "index.sqlite"
        with closing(self._conn()) as c, c:
            c.executescript(_SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(str(self.db), timeout=5.0)

    def rebuild(self, store) -> None:
        rows = []
        for rel in store.pages():
            try:
                front, body = store.read_page(rel)
            except Exception:
                continue
            rows.append((rel, str(front.get("title") or ""), f"{front.get('summary') or ''}\n{body}",
                         str(front.get("status") or "active")))
        with closing(self._conn()) as c, c:
            c.execute("DELETE FROM pages_fts")
            c.executemany("INSERT INTO pages_fts(path, title, body, status) VALUES (?,?,?,?)", rows)

    def is_empty(self) -> bool:
        """True when the search index has no page rows."""
        with closing(self._conn()) as c:
            return c.execute("SELECT 1 FROM pages_fts LIMIT 1").fetchone() is None

    def search(self, query: str, k: int = 5, include_retired: bool = False) -> list[dict]:
        terms = re.findall(r"\w{3,}", str(query or ""))[:20]
        if not terms:
            return []
        match = " OR ".join('"' + t.replace('"', '""') + '"' for t in terms)
        sql = """
        WITH hits AS (SELECT path, title, status, bm25(pages_fts) AS score FROM pages_fts WHERE pages_fts MATCH ?)
        SELECT h.path, h.title, h.status,
               CASE WHEN COALESCE(u.injected,0) - COALESCE(u.used,0) >= 20 THEN h.score * 0.5 ELSE h.score END AS s
        FROM hits h LEFT JOIN usage u ON u.path = h.path
        WHERE (? OR h.status != 'retired')
        ORDER BY CASE WHEN h.status = 'active' THEN 0 ELSE 1 END, s
        LIMIT ?"""
        # bm25 is negative (lower = better); halving moves a score toward 0, i.e. ranks it lower.
        with closing(self._conn()) as c:
            rows = c.execute(sql, (match, 1 if include_retired else 0, int(k))).fetchall()
        return [{"path": p, "title": t, "status": s} for p, t, s, _ in rows]

    def mark(self, path: str, kind: str) -> None:
        if kind not in ("used", "wrong"):
            raise ValueError(f"unknown mark kind: {kind}")
        self._bump(path, kind)

    def _bump(self, path: str, col: str) -> None:
        with closing(self._conn()) as c, c:
            c.execute("INSERT OR IGNORE INTO usage(path) VALUES (?)", (path,))
            c.execute(f"UPDATE usage SET {col} = {col} + 1 WHERE path = ?", (path,))

    def usage(self, path: str) -> dict:
        with closing(self._conn()) as c:
            row = c.execute("SELECT used, wrong, injected FROM usage WHERE path = ?", (path,)).fetchone()
        used, wrong, injected = row or (0, 0, 0)
        return {"used": used, "wrong": wrong, "injected": injected}

    def record_injected(self, session: str, path: str) -> bool:
        with closing(self._conn()) as c, c:
            cur = c.execute("INSERT OR IGNORE INTO injected(session, path) VALUES (?,?)", (session, path))
            if cur.rowcount == 0:
                return False
            c.execute("INSERT OR IGNORE INTO usage(path) VALUES (?)", (path,))
            c.execute("UPDATE usage SET injected = injected + 1 WHERE path = ?", (path,))
        return True

    def get_state(self, key: str) -> str | None:
        with closing(self._conn()) as c:
            row = c.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def set_state(self, key: str, value: str | None) -> None:
        with closing(self._conn()) as c, c:
            if value is None:
                c.execute("DELETE FROM state WHERE key = ?", (key,))
            else:
                c.execute("INSERT OR REPLACE INTO state(key, value) VALUES (?,?)", (key, value))

    def state_keys(self, prefix: str) -> list[str]:
        """All state keys that start with `prefix` (exact byte prefix, no wildcards)."""
        with closing(self._conn()) as c:
            rows = c.execute("SELECT key FROM state WHERE substr(key, 1, ?) = ? ORDER BY key",
                             (len(prefix), prefix)).fetchall()
        return [r[0] for r in rows]

    def delete_state(self, keys) -> None:
        keys = [(k,) for k in keys]
        if not keys:
            return
        with closing(self._conn()) as c, c:
            c.executemany("DELETE FROM state WHERE key = ?", keys)

    def delete_injected(self, session: str) -> None:
        """Remove the injected rows of one session. Usage counters stay."""
        with closing(self._conn()) as c, c:
            c.execute("DELETE FROM injected WHERE session = ?", (session,))
