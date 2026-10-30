"""Store: pages, raw records, index.md, log.md and the write lock."""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime
from importlib import resources
from pathlib import Path

import yaml

from recall import gate
from recall.history import History

TYPES = ("decision", "fact", "howto", "gotcha", "preference", "task")
STATUSES = ("active", "stale", "retired")
CONFIDENCE = ("high", "medium", "low")
FRONT_KEYS = ("type", "title", "summary", "status", "confidence", "sources", "anchors", "updated", "stale_since")
LOCK_STALE_S = 120.0
INIT_LOCK_WAIT_S = 5.0
RAW_MAX_CHARS = 20_000
RAW_KEEP_DAYS = 90
INIT_SENTINEL = "recall-initialized"
_SAFE_PATH = re.compile(r"^[\w./-]{1,200}$")


@dataclass
class PageChange:
    path: str
    front: dict
    body: str


@dataclass
class Meta:
    op: str
    session: str = ""
    trigger: str = "agent"


@dataclass
class IngestResult:
    accepted: list[str] = field(default_factory=list)
    rejected: list[tuple[str, str]] = field(default_factory=list)
    redirected: list[tuple[str, str]] = field(default_factory=list)
    commit: str | None = None


class LockBusy(Exception):
    pass


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def _append(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8", newline="\n") as f:
        f.write(text)


def _read(path: Path) -> str:
    with open(path, "r", encoding="utf-8") as f:  # universal newlines: CRLF -> LF
        return f.read()


def _one_line(s) -> str:
    return " ".join(str(s or "").split())


def _log_path(p: str) -> str:
    """A path that is safe to put in log.md (no secrets, no newlines)."""
    p = p if isinstance(p, str) else ""
    if not _SAFE_PATH.match(p):
        return "<invalid path>"
    return "<redacted path>" if any(rx.search(p) for _, rx in gate.SECRET_PATTERNS) else p


def _log_text(s) -> str:
    """One line of agent-supplied text for log.md, with secret-looking parts redacted."""
    s = _one_line(s)
    for _, rx in gate.SECRET_PATTERNS:
        s = rx.sub("<redacted>", s)
    return s


def _remove_if_stale(path: Path) -> bool:
    """Delete a lock file older than LOCK_STALE_S. True if the lock is gone (retry now).

    The file is stat-ed twice and only deleted if both looks agree, so a lock that another
    process re-created between the two looks is kept."""
    try:
        st1 = path.stat()
        if time.time() - st1.st_mtime <= LOCK_STALE_S:
            return False
        st2 = path.stat()
        if (st2.st_mtime_ns, st2.st_ino, st2.st_size) != (st1.st_mtime_ns, st1.st_ino, st1.st_size):
            return False
        path.unlink()
    except FileNotFoundError:
        pass
    return True


def raw_session_tag(session) -> str:
    """The session part of a raw record file name."""
    return re.sub(r"[^A-Za-z0-9_-]", "_", str(session or "nosession"))[:40] or "nosession"


def _raw_date(rel: str) -> date | None:
    m = re.match(r"^raw/(\d{4}-\d{2}-\d{2})-", rel)
    try:
        return date.fromisoformat(m.group(1)) if m else None
    except ValueError:
        return None


def code_source_path(src: str) -> str:
    """Strip an optional ':a-b' line range from a code source."""
    m = re.match(r"^(.*?):(\d+)-(\d+)$", src)
    return m.group(1) if m else src


def is_code_source(src: str) -> bool:
    return not (src.startswith("raw/") or src.startswith("commit:"))


class Store:
    def __init__(self, repo_root: Path):
        self.repo_root = Path(repo_root).resolve()
        self.root = self.repo_root / ".devin" / "recall"
        self.history = History(self.root, self.repo_root)
        # Write generation: every Store write increases it, so a cached page scan knows it is old.
        self.generation = 0

    def _write(self, path: Path, text: str) -> None:
        self.generation += 1
        _write(path, text)

    def _append(self, path: Path, text: str) -> None:
        self.generation += 1
        _append(path, text)

    # ---------- setup ----------
    def _initialized(self) -> bool:
        return all((self.root / n).exists() for n in (".git", "index.md", "log.md", "consumed.txt")) \
            and bool(self.history.log(limit=1))

    def _sentinel(self) -> Path:
        # Inside .git: never tracked, and gone if someone deletes the store's git folder.
        return self.root / ".git" / INIT_SENTINEL

    def _initialized_fast(self) -> bool:
        """File checks only (no git spawn): hooks call init() on every event."""
        return self._sentinel().is_file() and all(
            (self.root / n).exists() for n in ("index.md", "log.md", "consumed.txt"))

    def init(self) -> None:
        """Create the store if needed. Idempotent; runs under the store lock (may raise LockBusy)."""
        if self._initialized_fast():
            return
        if self._initialized():  # a store made before the sentinel existed
            self._mark_initialized()
            return
        self.root.mkdir(parents=True, exist_ok=True)
        with self.lock("init", wait=INIT_LOCK_WAIT_S):
            if not self._initialized():  # another process may have finished while we waited
                self._init_locked()
            self._mark_initialized()

    def _mark_initialized(self) -> None:
        try:
            _write(self._sentinel(), "")
        except OSError:
            pass  # only a speed-up; the slow check still works

    def _init_locked(self) -> None:
        (self.root / "pages").mkdir(parents=True, exist_ok=True)
        (self.root / "raw").mkdir(parents=True, exist_ok=True)
        self.history.init()
        tpl = resources.files("recall") / "templates"
        for name in ("SCHEMA.md", "eval.md"):
            if not (self.root / name).exists():
                self._write(self.root / name, tpl.joinpath(name).read_text(encoding="utf-8").replace("\r\n", "\n"))
        defaults = {"log.md": "# Recall log\n", ".gitignore": ".lock\n", "consumed.txt": ""}
        for name, text in defaults.items():
            if not (self.root / name).exists():
                self._write(self.root / name, text)
        for d in ("pages", "raw"):
            keep = self.root / d / ".gitkeep"
            if not any((self.root / d).iterdir()):
                _write(keep, "")
        if not (self.root / "index.md").exists():
            self.write_index()
        if not self.history.log(limit=1):
            self.history.commit(Meta("init", "", "install"), "init: recall store")

    # ---------- pages ----------
    def pages(self) -> list[str]:
        base = self.root / "pages"
        if not base.exists():
            return []
        return sorted(p.relative_to(self.root).as_posix() for p in base.glob("*/*.md"))

    def read_page(self, rel: str) -> tuple[dict, str]:
        text = _read(self.root / rel)
        if text.startswith("---\n"):
            end = text.find("\n---\n", 4)
            if end != -1:
                front = yaml.safe_load(text[4:end + 1]) or {}
                body = text[end + 5:]
                return front, body.strip("\n")
        return {}, text.strip("\n")

    def _render_page(self, front: dict, body: str) -> str:
        ordered = {k: front[k] for k in FRONT_KEYS if k in front}
        ordered.update({k: v for k, v in front.items() if k not in ordered})
        y = yaml.safe_dump(ordered, sort_keys=False, allow_unicode=True, default_flow_style=False)
        body = str(body).replace("\r\n", "\n").strip("\n")
        return f"---\n{y}---\n\n{body}\n"

    def _anchors(self, sources) -> tuple[dict, list[str]]:
        """({path: blob sha}, [code paths that could not be hashed])."""
        paths = list(dict.fromkeys(code_source_path(s) for s in sources if is_code_source(s)))
        shas = self.history.hash_objects([self.repo_root / p for p in paths])
        anchors = {p: shas[str(self.repo_root / p)] for p in paths if str(self.repo_root / p) in shas}
        return anchors, [p for p in paths if p not in anchors]

    def _previous_anchors(self, rel: str) -> dict | None:
        """Anchors of the existing page, or None if the page is new or unreadable."""
        try:
            if not (self.root / rel).is_file():
                return None
            prev = self.read_page(rel)[0].get("anchors")
        except (OSError, yaml.YAMLError):
            return None
        return prev if isinstance(prev, dict) else {}

    # ---------- ingest ----------
    def ingest(self, changes: list[PageChange], meta: Meta, consumed: list[str] = ()) -> IngestResult:
        res = IngestResult()
        with self.lock(meta.session):
            today = date.today().isoformat()
            for ch in changes:
                front = dict(ch.front or {})
                front.setdefault("status", "active")
                for k in ("anchors", "updated", "stale_since"):  # computed here or owned by lint
                    front.pop(k, None)
                ch = PageChange(ch.path, front, ch.body or "")
                v = gate.check(ch, self)
                if v.kind == "reject":
                    res.rejected.append((ch.path, v.reason))
                    continue
                if v.kind == "redirect":
                    res.redirected.append((ch.path, v.target))
                    continue
                front["sources"] = list(front.get("sources") or [])
                anchors, unknown = self._anchors(front["sources"])
                if unknown:
                    # Never write a page whose anchors silently lost a source: keep the old anchors
                    # of an existing page, reject a new page (or a source the old page did not anchor).
                    prev = self._previous_anchors(ch.path)
                    if prev is None or any(p not in prev for p in unknown):
                        res.rejected.append((ch.path, "anchors: cannot hash source"))
                        continue
                    anchors = {**anchors, **{p: prev[p] for p in unknown}}
                front["anchors"] = anchors
                front["updated"] = today
                front["stale_since"] = self._existing_stale_since(ch.path) if front["status"] == "stale" else None
                self._write(self.root / ch.path, self._render_page(front, ch.body))
                res.accepted.append(ch.path)
            valid = []
            for c in consumed or ():
                if self._raw_ok(c):
                    valid.append(c)
                elif c:
                    res.rejected.append((str(c), "consumed: not an existing raw record"))
            consumed = valid
            if consumed:
                self._append(self.root / "consumed.txt", "".join(f"{_one_line(c)}\n" for c in consumed))
            what = ", ".join(res.accepted) or ("consumed raw" if consumed else "no pages")
            stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
            lines = [f"\n## [{stamp}] {meta.op} | {what} | session={_log_text(meta.session) or '-'}\n"]
            lines += [f"rejected | {_log_path(p)}: {_log_text(r)}\n" for p, r in res.rejected]
            lines += [f"redirected | {_log_path(p)} -> {_log_path(t)}\n" for p, t in res.redirected]
            self._append(self.root / "log.md", "".join(lines))
            self.write_index()
            sources = sorted({s for p in res.accepted for s in (self.read_page(p)[0].get("sources") or [])})
            res.commit = self.history.commit(meta, f"{meta.op}: {what}", pages=res.accepted,
                                             sources=sources or list(consumed),
                                             paths=[*res.accepted, "index.md", "log.md", "consumed.txt"])
        return res

    def _existing_stale_since(self, rel: str):
        try:
            return self.read_page(rel)[0].get("stale_since") if (self.root / rel).is_file() else None
        except (OSError, yaml.YAMLError):
            return None

    def _raw_ok(self, rel) -> bool:
        return (isinstance(rel, str) and bool(re.fullmatch(r"raw/[\w.-]+\.md", rel))
                and (self.root / rel).is_file())

    def set_status(self, rel: str, status: str, today: date) -> None:
        """Change a page status in place. No commit, no lock: the caller holds the lock.

        Becoming stale sets `stale_since` (a no-op if already stale); leaving stale for
        active clears it; retired keeps it as a record of when the page went stale.
        """
        if status not in STATUSES:
            raise ValueError(f"status: must be one of {', '.join(STATUSES)}")
        front, body = self.read_page(rel)
        old = front.get("status", "active")
        if old == status:
            return
        front["status"] = status
        if status == "stale":
            front["stale_since"] = today.isoformat()
        elif status == "active":
            front["stale_since"] = None
        self._write(self.root / rel, self._render_page(front, body))

    # ---------- revert ----------
    def revert(self, ref: str, meta: Meta, page: str | None = None) -> str:
        """Undo a commit (or one page of it) as a NEW commit. History is never rewritten."""
        h = self.history
        with self.lock(meta.session):
            sha = h.resolve(ref)
            self.generation += 1  # git rewrites files in the working tree below
            # Untracked files (pending raw records) are fine; tracked edits are not.
            if h.status():
                raise RuntimeError("store has uncommitted changes; cannot revert")
            parent = h.parent(sha)
            if not parent:
                raise RuntimeError("cannot revert the root (init) commit")
            if page is not None:
                if not (_SAFE_PATH.match(page) and page.startswith("pages/") and page.endswith(".md")
                        and ".." not in page.split("/")):
                    raise ValueError(f"bad page path: {page!r}")
                h.restore_page(parent, page)
                pages = [page]
                touched = [page]
            else:
                touched = h.revert_commit(sha)
                if touched is None:
                    raise RuntimeError(f"revert of {ref} conflicts with later changes; aborted")
                pages = [p for p in touched if p.startswith("pages/")]
            self.write_index()
            stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
            self._append(self.root / "log.md", f"\n## [{stamp}] revert | {_one_line(ref)} -> {sha[:12]}"
                                         f"{' | ' + _log_path(page) if page else ''} | session={_log_text(meta.session) or '-'}\n")
            # Scoped: only what the revert touched; pending (untracked) raw records stay out.
            new = h.commit(meta, f"revert: {_one_line(ref)} ({sha[:12]})", pages=pages, sources=[f"commit:{sha}"],
                           paths=[*touched, "index.md", "log.md"])
            return new or h.resolve("HEAD")

    # ---------- raw records ----------
    def add_raw(self, session: str, kind: str, text: str, when: datetime | None = None) -> str | None:
        text = str(text or "").replace("\r\n", "\n").strip()
        if not text:
            return None
        if len(text) > RAW_MAX_CHARS:
            text = text[:RAW_MAX_CHARS] + "\n[truncated]"
        digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:8]
        raw_dir = self.root / "raw"
        if any(raw_dir.glob(f"*-{digest}.md")):
            return None
        sid = raw_session_tag(session)
        kind = re.sub(r"[^a-z0-9]", "", str(kind).lower())[:20] or "note"
        day = (when or datetime.now()).strftime("%Y-%m-%d")
        rel = f"raw/{day}-{sid}-{kind}-{digest}.md"
        self._write(self.root / rel, text + "\n")
        return rel

    def raw_files(self) -> list[str]:
        """All raw record files (store-relative), sorted; their names start with the date."""
        base = self.root / "raw"
        return sorted(p.relative_to(self.root).as_posix() for p in base.glob("*.md")) if base.exists() else []

    def pending_raw(self, scan=None) -> list[str]:
        """Raw records that no page cites and that are not listed in consumed.txt.

        `scan` is an optional callable returning [(rel, front, body)] (a shared page scan);
        without it the pages are read here."""
        raws = self.raw_files()
        if not raws:
            return []
        done: set[str] = set()
        cons = self.root / "consumed.txt"
        if cons.exists():
            done |= {ln.strip() for ln in _read(cons).splitlines() if ln.strip()}
        if scan is not None:
            for _, front, _ in scan():
                done |= {str(s) for s in (front.get("sources") or [])}
            return [r for r in raws if r not in done]
        for rel in self.pages():
            try:
                done |= {str(s) for s in (self.read_page(rel)[0].get("sources") or [])}
            except (OSError, yaml.YAMLError):
                continue
        return [r for r in raws if r not in done]

    def uncommitted_raw(self) -> list[str]:
        """Raw record files that are new or changed in the working tree (not yet committed)."""
        return [p for p in self.history.status(["raw"], include_untracked=True) if p.endswith(".md")]

    def cleanup_raw(self, today: date, wait: float = 1.0) -> str | None:
        """Remove raw records older than RAW_KEEP_DAYS (by their date prefix) with a `cleanup` commit.

        Pending ones are logged as "N raw not ingested". Git history keeps them. May raise LockBusy."""
        def old() -> list[str]:
            return [r for r in self.raw_files() if (d := _raw_date(r)) and (today - d).days > RAW_KEEP_DAYS]
        if not old():
            return None  # the common case: no lock, no git
        with self.lock("cleanup", wait=wait):
            gone = old()
            if not gone:
                return None
            pending = set(self.pending_raw())
            n = sum(r in pending for r in gone)
            self.generation += 1
            for r in gone:
                (self.root / r).unlink(missing_ok=True)
            self.append_log("cleanup", f"{n} raw not ingested", "")
            return self.history.commit(Meta("cleanup", "", "cleanup"),
                                       f"cleanup: {len(gone)} raw record(s) older than {RAW_KEEP_DAYS} days",
                                       sources=gone, paths=[*gone, "log.md"])

    def capture_commit(self, meta: Meta, wait: float = 1.0) -> str | None:
        """Commit new raw records. Returns None if nothing is new or the lock is busy."""
        try:
            with self.lock(meta.session, wait=wait):
                new = self.uncommitted_raw()
                if not new:
                    return None
                stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
                self._append(self.root / "log.md", f"\n## [{stamp}] capture | {len(new)} raw | "
                                             f"trigger={_log_text(meta.trigger)} | session={_log_text(meta.session) or '-'}\n")
                return self.history.commit(meta, f"capture: {len(new)} raw record(s)", sources=new,
                                           paths=[*new, "log.md"])
        except LockBusy:
            return None

    # ---------- log ----------
    def append_log(self, op: str, title: str, session: str | None, when: datetime | None = None,
                   session_max: int | None = None) -> None:
        """Append `## [<when>] <op> | <title> | session=<session>` to log.md. No lock: the caller holds it.

        All parts are redacted one-line text. `session=None` leaves out the session part;
        `session_max` cuts the redacted session to that many characters."""
        stamp = (when or datetime.now()).strftime("%Y-%m-%d %H:%M")
        line = f"\n## [{stamp}] {_log_text(op)} | {_log_text(title) or '-'}"
        if session is not None:
            sid = _log_text(session)
            line += f" | session={(sid[:session_max] if session_max is not None else sid) or '-'}"
        self._append(self.root / "log.md", line + "\n")

    # ---------- plain store files ----------
    def read_text(self, rel: str) -> str:
        """Text of a store file (store-relative path); CRLF reads as LF."""
        return _read(self.root / rel)

    def write_text(self, rel: str, text: str) -> None:
        """Write a store file (store-relative path) with LF line ends. No lock: the caller holds it."""
        self._write(self.root / rel, text)

    # ---------- index ----------
    def write_index(self) -> None:
        """Rebuild index.md from the pages. No lock: the caller holds it."""
        sections = {t: [] for t in TYPES}
        for rel in self.pages():
            front, _ = self.read_page(rel)
            status = front.get("status", "active")
            if status == "retired":
                continue
            t = rel.split("/")[1]
            if t not in sections:
                continue
            stale = " (stale)" if status == "stale" else ""
            sections[t].append(f"- [[{rel[:-3]}]] {_one_line(front.get('title'))}{stale}: "
                               f"{_one_line(front.get('summary'))}")
        out = ["# Recall index", ""]
        for t in TYPES:
            out.append(f"## {t}")
            out += sections[t]
            out.append("")
        self._write(self.root / "index.md", "\n".join(out))

    # ---------- lock ----------
    @contextmanager
    def lock(self, session: str, wait: float = 5.0):
        path = self.root / ".lock"
        deadline = time.monotonic() + wait
        while True:
            try:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                break
            except FileExistsError:
                try:
                    if _remove_if_stale(path):
                        continue
                except OSError:
                    pass
                if time.monotonic() >= deadline:
                    raise LockBusy(f"store lock busy: {path}")
                time.sleep(0.1)
        try:
            os.write(fd, json.dumps({"session": session, "t": time.time()}).encode("utf-8"))
        finally:
            os.close(fd)
        try:
            yield
        finally:
            try:
                path.unlink()
            except OSError:
                pass  # already gone or held by another process (Windows); goes stale after 120 s
