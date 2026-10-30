"""History: the only module that runs git for the Recall store."""
from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

GIT_ID = ["-c", "user.name=Recall", "-c", "user.email=recall@localhost", "-c", "core.autocrlf=false"]
GIT_TIMEOUT_S = 30
TRAILER_KEYS = ("Recall-Op", "Recall-Session", "Recall-Trigger", "Recall-Repo-Commit",
                "Recall-Branch", "Recall-Sources", "Recall-Pages")


class RefError(Exception):
    pass


class GitError(Exception):
    pass


@dataclass
class Entry:
    sha: str
    date: str
    subject: str
    trailers: dict[str, str] = field(default_factory=dict)


def _clean(value) -> str:
    """One-line trailer value; '-' when empty."""
    if isinstance(value, (list, tuple)):
        value = ", ".join(str(v) for v in value)
    value = " ".join(str(value or "").split())
    return value or "-"


class History:
    def __init__(self, root: Path, repo_root: Path | None = None):
        self.root = Path(root)
        self.repo_root = Path(repo_root) if repo_root else None

    def _run(self, *args, input=None, env=None, cwd=None, check=True) -> subprocess.CompletedProcess:
        full_env = None
        if env:
            full_env = {**os.environ, **env}
        try:
            p = subprocess.run(["git", *GIT_ID, *args], cwd=cwd or self.root, capture_output=True, text=True,
                               encoding="utf-8", errors="replace", input=input, env=full_env,
                               timeout=GIT_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            raise GitError(f"git {args[0] if args else ''} timed out after {GIT_TIMEOUT_S}s") from None
        if check and p.returncode != 0:
            raise GitError(f"git {args[0] if args else ''} failed: {p.stderr.strip()}")
        return p

    def init(self) -> None:
        if not (self.root / ".git").exists():
            self._run("init", "-q", "-b", "main")

    def _repo_info(self) -> tuple[str, str]:
        if not self.repo_root:
            return "-", "-"
        p = self._run("rev-parse", "HEAD", "--abbrev-ref", "HEAD", cwd=self.repo_root, check=False)
        out = p.stdout.split()
        if p.returncode != 0 or len(out) != 2:
            return "-", "-"
        return out[0], out[1]

    def commit(self, meta, subject, pages=(), sources=(), when=None, eval_score=None,
               paths=None) -> str | None:
        """Commit the store. `paths` (store-relative) limits the commit to what the operation owns;
        None stages everything (init only). Returns None when nothing changed."""
        if paths is None:
            self._run("add", "-A")
            if not self._run("status", "--porcelain").stdout.strip():
                return None
            scope: list[str] = []
        else:
            scope = sorted({str(p) for p in paths if p and ((self.root / p).exists() or self._tracked(p))})
            if not scope:
                return None
            self._run("add", "-A", "--", *scope)
        repo_commit, branch = self._repo_info()
        values = [meta.op, meta.session, meta.trigger, repo_commit, branch, list(sources), list(pages)]
        lines = [f"{k}: {_clean(v)}" for k, v in zip(TRAILER_KEYS, values)]
        if eval_score is not None:
            lines.append(f"Recall-Eval: {_clean(eval_score)}")
        message = " ".join(str(subject).split()) + "\n\n" + "\n".join(lines) + "\n"
        env = None
        if when is not None:
            iso = when.isoformat() if isinstance(when, datetime) else str(when)
            env = {"GIT_AUTHOR_DATE": iso, "GIT_COMMITTER_DATE": iso}
        if not scope:
            self._run("commit", "-q", "-F", "-", input=message, env=env)
        else:
            # `--only` semantics: anything else staged earlier stays out of this commit.
            p = self._run("commit", "-q", "-F", "-", "--", *scope, input=message, env=env, check=False)
            if p.returncode != 0:
                # Nothing changed in scope (checked only on failure: saves a git call per commit).
                if self._run("diff", "--cached", "--quiet", "HEAD", "--", *scope, check=False).returncode == 0:
                    return None
                raise GitError(f"git commit failed: {(p.stderr or p.stdout).strip()}")
        return self._run("rev-parse", "HEAD").stdout.strip()

    def _tracked(self, path: str) -> bool:
        return self._run("ls-files", "--error-unmatch", "--", str(path), check=False).returncode == 0

    def log(self, op=None, session=None, page=None, since=None, limit=50) -> list[Entry]:
        args = ["log", "--format=%H%x1f%cI%x1f%s%x1f%(trailers:only,unfold)%x1e"]
        if since:
            args.append(f"--since={since}")
        if page:
            args += ["--follow", "--", page]
        p = self._run(*args, check=False)
        if p.returncode != 0:
            # An empty store (no commit yet) has no log; any other failure is a real error.
            if self._run("rev-parse", "--verify", "-q", "HEAD", check=False).returncode != 0:
                return []
            raise GitError(f"git log failed: {p.stderr.strip()}")
        out = p.stdout
        entries: list[Entry] = []
        for rec in out.split("\x1e"):
            rec = rec.strip("\r\n")
            if not rec.strip():
                continue
            parts = rec.split("\x1f")
            if len(parts) < 4:
                continue
            sha, date, subj, tr = parts[0].strip(), parts[1], parts[2], parts[3]
            trailers: dict[str, str] = {}
            for line in tr.splitlines():
                k, sep, v = line.partition(":")
                if sep and k.strip():
                    trailers[k.strip()] = v.strip()
            if op and trailers.get("Recall-Op") != op:
                continue
            if session and trailers.get("Recall-Session") != session:
                continue
            entries.append(Entry(sha, date, subj, trailers))
            if len(entries) >= limit:
                break
        return entries

    def hash_object(self, path: Path) -> str | None:
        """Blob hash of a working-tree file (used for page anchors)."""
        if not Path(path).is_file():
            return None
        return self.hash_objects([path]).get(str(path))

    def hash_objects(self, paths) -> dict[str, str]:
        """Blob hashes of several working-tree files in one git call: {str(path): sha}. Missing files are left out."""
        files = [str(p) for p in dict.fromkeys(paths) if Path(p).is_file()]
        if not files:
            return {}
        # --stdin-paths: no command-line length limit (Windows: 32k chars) for many anchors.
        cwd = self.repo_root or self.root
        try:
            p = self._run("hash-object", "--stdin-paths", input="".join(f + "\n" for f in files), cwd=cwd,
                          check=False)
            shas = p.stdout.split()
            if p.returncode == 0 and len(shas) == len(files):
                return dict(zip(files, shas))
        except GitError:
            pass
        # Batch failed (e.g. one unreadable file): hash one by one; files that still fail are left out.
        out: dict[str, str] = {}
        for f in files:
            try:
                p = self._run("hash-object", "--", f, cwd=cwd, check=False)
            except GitError:
                continue
            sha = p.stdout.strip()
            if p.returncode == 0 and re.fullmatch(r"[0-9a-f]{40,64}", sha):
                out[f] = sha
        return out

    def repo_is_ancestor(self, sha: str) -> bool | None:
        """True if code-repo commit `sha` is an ancestor of (or equal to) the repo HEAD; None if unknown."""
        if not re.fullmatch(r"[0-9a-fA-F]{4,64}", sha or ""):
            return None
        # One git call: exit 0 = ancestor, 1 = not an ancestor, anything else (unknown sha, no HEAD) = None.
        p = self._run("merge-base", "--is-ancestor", sha, "HEAD", cwd=self.repo_root or self.root, check=False)
        return {0: True, 1: False}.get(p.returncode)

    def repo_has_commit(self, sha: str) -> bool:
        """True if `sha` names a commit in the code repo."""
        if not re.fullmatch(r"[0-9a-fA-F]{4,64}", sha or ""):
            return False
        p = self._run("cat-file", "-e", f"{sha}^{{commit}}", cwd=self.repo_root or self.root, check=False)
        return p.returncode == 0

    def first_commit_time(self) -> float | None:
        """Commit time (epoch seconds) of the store's first commit; None when there is no commit."""
        p = self._run("log", "--max-parents=0", "--format=%ct", check=False)
        times = [float(t) for t in p.stdout.split() if t.isdigit()] if p.returncode == 0 else []
        return min(times) if times else None

    def _all(self) -> list[Entry]:
        return self.log(limit=10**9)

    def resolve(self, ref: str) -> str:
        ref = str(ref or "").strip()
        if not ref or ref.startswith("-") or "\n" in ref:
            raise RefError(f"unknown ref: {ref!r}")
        if ref.startswith("session:"):
            sid = ref[len("session:"):]
            for e in self._all():
                if sid and e.trailers.get("Recall-Session") == sid:
                    return e.sha
            raise RefError(f"no commit for {ref}")
        if ref.startswith("repo:"):
            want = ref[len("repo:"):].lower()
            if not re.fullmatch(r"[0-9a-f]{4,64}", want):
                raise RefError(f"bad repo sha: {ref}")
            for e in self._all():
                if e.trailers.get("Recall-Repo-Commit", "").lower().startswith(want):
                    return e.sha
            raise RefError(f"no commit for {ref}")
        if ref.startswith("before:"):
            m = re.fullmatch(r"before:([\w-]+)#([1-9]\d*)", ref)
            if not m:
                raise RefError(f"bad ref: {ref} (want before:<op>#<n>)")
            hits = [e for e in reversed(self._all()) if e.trailers.get("Recall-Op") == m.group(1)]
            n = int(m.group(2))
            if n > len(hits):
                raise RefError(f"no commit for {ref}")
            p = self._run("rev-parse", "--verify", "-q", f"{hits[n - 1].sha}^1^{{commit}}", check=False)
            if p.returncode != 0 or not p.stdout.strip():
                raise RefError(f"{ref} has no parent")
            return p.stdout.strip()
        if ref == "best-eval":
            best, best_score = None, None
            for e in self._all():  # newest first; ties keep the newest
                try:
                    score = float(e.trailers.get("Recall-Eval", ""))
                except ValueError:
                    continue
                if best_score is None or score > best_score:
                    best, best_score = e.sha, score
            if best is None:
                raise RefError("no commit with Recall-Eval")
            return best
        m = re.fullmatch(r"(\d{4}-\d{2}-\d{2})(?: (\d{2}:\d{2}(?::\d{2})?))?", ref)
        if m:
            text = f"{m.group(1)} {m.group(2) or '23:59:59'}"
            try:
                dt = datetime.fromisoformat(text)
            except ValueError:
                raise RefError(f"bad date: {ref}") from None
            # git's date parser cannot handle far-future years; pass a local-time epoch, clamped.
            ts = int(min(dt, datetime(2099, 12, 31, 23, 59, 59)).timestamp()) if dt.year > 1970 else 0
            p = self._run("rev-list", "-1", f"--before=@{ts}", "HEAD", check=False)
            if p.returncode != 0 or not p.stdout.strip():
                raise RefError(f"no commit before {ref}")
            return p.stdout.strip()
        p = self._run("rev-parse", "--verify", "-q", f"{ref}^{{commit}}", check=False)
        if p.returncode != 0 or not p.stdout.strip():
            raise RefError(f"unknown ref: {ref}")
        return p.stdout.strip()

    def has_path(self, sha: str, path: str) -> bool:
        return self._run("cat-file", "-e", f"{sha}:{path}", check=False).returncode == 0

    # ---------- working tree ----------
    def _porcelain(self, paths, untracked: str) -> list[tuple[str, str]]:
        """[(XY code, path)] of `git status --porcelain`, limited to `paths` (store-relative) if given."""
        args = ["status", "--porcelain", f"--untracked-files={untracked}"]
        if paths:
            args += ["--", *map(str, paths)]
        out = []
        for ln in self._run(*args).stdout.splitlines():
            p = ln[3:].strip().strip('"')
            if p:
                out.append((ln[:2], p))
        return out

    def status(self, paths=(), include_untracked: bool = False) -> list[str]:
        """Store paths that differ from HEAD (tracked only, unless `include_untracked`), sorted."""
        return sorted({p for _, p in self._porcelain(paths, "all" if include_untracked else "no")})

    def untracked(self, paths=()) -> list[str]:
        """Untracked store files under `paths`, sorted."""
        return sorted({p for code, p in self._porcelain(paths, "all") if code == "??"})

    def parent(self, sha: str) -> str | None:
        """First parent of commit `sha`, or None for the root commit."""
        p = self._run("rev-parse", "--verify", "-q", f"{sha}^1^{{commit}}", check=False)
        return p.stdout.strip() or None

    def restore_page(self, sha: str, page: str) -> None:
        """Set the working-tree file `page` to its content at commit `sha`; delete it if `sha` lacks it."""
        if self.has_path(sha, page):
            self._run("checkout", sha, "--", page)
        else:
            (self.root / page).unlink(missing_ok=True)

    def revert_commit(self, sha: str) -> list[str] | None:
        """Apply the inverse of commit `sha` to the index and working tree, without committing.

        Conflicts only in log.md / index.md (append-only / derived) keep our side; the caller rebuilds
        index.md. Any other conflict aborts and resets to HEAD and returns None. Otherwise returns the
        staged store paths."""
        if self._run("revert", "--no-commit", sha, check=False).returncode != 0:
            unmerged = self._run("diff", "--name-only", "--diff-filter=U").stdout.splitlines()
            if unmerged and set(unmerged) <= {"log.md", "index.md"}:
                self._run("checkout", "--ours", "--", *unmerged)
                self._run("add", "--", *unmerged)
            else:
                self._run("revert", "--abort", check=False)
                self._run("reset", "-q", "--hard", "HEAD", check=False)
                return None
        touched = [p.strip().strip('"') for p in
                   self._run("diff", "--cached", "--name-only", "HEAD").stdout.splitlines() if p.strip()]
        self._run("revert", "--quit", check=False)
        return touched

    def show(self, ref: str, path: str = "index.md") -> str:
        sha = self.resolve(ref)
        p = self._run("show", f"{sha}:{path}", check=False)
        if p.returncode != 0:
            raise RefError(f"{path} not in {ref}")
        return p.stdout

    def diff(self, a: str, b: str = "HEAD", page: str | None = None) -> str:
        args = ["diff", self.resolve(a), self.resolve(b)]
        if page:
            args += ["--", page]
        return self._run(*args).stdout
