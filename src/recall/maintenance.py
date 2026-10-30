"""Maintenance: mechanical lint (anchors, links, usage, page retirement), the due checks for the
semantic review and the eval, and the reset after they are done.

Due checks are pure reads. The review clock starts at the last review, or at the store's first
commit when no review exists. Eval scoring stays in `evaluation`.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import date, datetime

import yaml

# semantic review / eval schedule
REVIEW_MAX_INGESTS = 10
REVIEW_MAX_AGE_S = 7 * 86400
REVIEW_RAW_COUNT = 10
EVAL_MIN_GAP_S = 7 * 86400

STALE_RETIRE_DAYS = 30
WRONG_RETIRE = 2
UNUSED_GAP = 20
MAX_SECTION_LINES = 10
LINK_RE = re.compile(r"\[\[([^\[\]\n|]+?)(?:\|[^\[\]\n]*)?\]\]")


@dataclass
class Finding:
    path: str
    rule: str
    detail: str = ""


def _link_target(raw: str) -> str:
    t = raw.strip()
    return t if t.endswith(".md") else t + ".md"


def _as_date(v) -> date | None:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    try:
        return date.fromisoformat(str(v)[:10])
    except (TypeError, ValueError):
        return None


def _anchor_target(store, path: str):
    """The anchored file inside the repo, or None if it escapes the repo or is not a file."""
    try:
        target = (store.repo_root / path).resolve()
        target.relative_to(store.repo_root)
    except (ValueError, OSError):
        return None
    return target if target.is_file() else None


def _anchor_state(store, path: str, sha: str, hashes: dict[str, str]) -> str | None:
    """None if the anchor is intact, else the rule name. `hashes`: {str(target): current blob sha}."""
    target = _anchor_target(store, path)
    if target is None:
        return "anchor-missing"
    current = hashes.get(str(target))
    if current is None:
        return "anchor-unknown"  # the file exists but git could not hash it: no verdict
    return None if current == str(sha) else "anchor-changed"


def lint_mechanical(store, index, today: date, wait: float = 2.0, scan=None,
                    rebuild: bool = True) -> list[Finding]:
    """Check every page, apply status changes under the store lock, return the findings.

    Status changes (stale, retired) are skipped if the lock is busy; findings are still returned.
    `scan` is an optional callable returning [(rel, front, body)] of the readable pages (the
    Runtime page scan); a page file missing from it is reported as unreadable.
    `rebuild=False` skips the search index rebuild after status changes; the caller must then
    refresh the index itself (the Runtime index does it through the page fingerprint).
    """
    from recall.paths import log_error
    from recall.store import LockBusy, Meta

    try:
        store.cleanup_raw(today, wait=wait)  # 90-day raw-record cleanup rides along with every lint
    except LockBusy:
        pass
    except Exception as e:  # cleanup must never stop the lint
        log_error(store.repo_root, e)
    findings: list[Finding] = []
    changes: list[tuple[str, str]] = []  # (path, new status)
    pages = store.pages()
    existing = set(pages)
    loaded: dict[str, tuple[dict, str]] = {}
    if scan is not None:
        # The scan runs after the raw cleanup above (which bumps the Store generation).
        loaded = {rel: (front, body) for rel, front, body in scan() if rel in existing}
        findings += [Finding(rel, "unreadable", "front matter or file cannot be read")
                     for rel in pages if rel not in loaded]
    for rel in (pages if scan is None else ()):
        try:
            loaded[rel] = store.read_page(rel)
        except (OSError, yaml.YAMLError, UnicodeDecodeError):
            findings.append(Finding(rel, "unreadable", "front matter or file cannot be read"))
    inbound: dict[str, int] = {rel: 0 for rel in pages}
    for rel, (front, body) in loaded.items():
        for m in LINK_RE.finditer(body):
            target = _link_target(m.group(1))
            if target in existing:
                if target != rel:
                    inbound[target] += 1
            else:
                findings.append(Finding(rel, "broken-link", f"[[{m.group(1).strip()}]]"))
    targets = [t for front, _ in loaded.values() if isinstance(front.get("anchors"), dict)
               for path in front["anchors"] if (t := _anchor_target(store, str(path)))]
    hashes = store.history.hash_objects(targets)  # one git call for all anchors
    for rel, (front, body) in loaded.items():
        status = front.get("status", "active")
        anchors = front.get("anchors") or {}
        anchor_bad = False
        if isinstance(anchors, dict):
            for path, sha in anchors.items():
                rule = _anchor_state(store, str(path), sha, hashes)
                if rule:
                    anchor_bad = anchor_bad or rule != "anchor-unknown"
                    findings.append(Finding(rel, rule, str(path)))
        if status == "retired":
            continue  # only anchors and links are reported for retired pages
        use = index.usage(rel)
        new = status
        if use["wrong"] >= WRONG_RETIRE:
            new = "retired"
            findings.append(Finding(rel, "retired", f"marked wrong {use['wrong']} times"))
        elif status == "stale":
            since = _as_date(front.get("stale_since"))
            if since and (today - since).days > STALE_RETIRE_DAYS:
                new = "retired"
                findings.append(Finding(rel, "retired", f"stale since {since.isoformat()}"))
        elif anchor_bad:
            new = "stale"
        if new != status:
            changes.append((rel, new))
        if new == "retired":
            continue
        if len(pages) > 1 and inbound.get(rel, 0) == 0:
            findings.append(Finding(rel, "orphan", "no inbound link"))
        if use["injected"] - use["used"] >= UNUSED_GAP:
            findings.append(Finding(rel, "unused", f"injected {use['injected']}, used {use['used']}"))
    findings.sort(key=lambda f: (f.path, f.rule, f.detail))
    if changes:
        try:
            with store.lock("lint", wait=wait):
                for rel, status in changes:
                    store.set_status(rel, status, today)
                store.write_index()
                store.append_log("lint", f"{len(findings)} findings | {len(changes)} status change(s)", None)
                store.history.commit(Meta("lint", "", "lint"), f"lint: {len(findings)} findings",
                                     pages=[r for r, _ in changes],
                                     paths=[*(r for r, _ in changes), "index.md", "log.md"])
            if rebuild:
                index.rebuild(store)  # keep search status in step with the pages
        except LockBusy:
            pass
    return findings


def format_findings(findings: list[Finding], limit: int = MAX_SECTION_LINES) -> str:
    if not findings:
        return ""
    lines = ["Lint findings:"]
    lines += [f"- {f.path}: {f.rule} {f.detail}".rstrip() for f in findings[:limit]]
    return "\n".join(lines)


# ---------- semantic review and eval schedule ----------
def _iso(t: float) -> str:
    return datetime.fromtimestamp(int(t)).astimezone().isoformat()


def review_due(store, state, now: float) -> bool:
    """True when a semantic review is requested, 7+ days old, or 10+ ingests behind. No writes."""
    if state.lint_due():
        return True
    last = state.last_review()
    if last is None:
        last = store.history.first_commit_time()
        if last is None:
            return False  # no commit yet: the clock has not started
    if now - last > REVIEW_MAX_AGE_S:
        return True
    return len(store.history.log(op="ingest", since=_iso(last), limit=REVIEW_MAX_INGESTS + 1)) > REVIEW_MAX_INGESTS


def eval_due(store, state, now: float) -> bool:
    """An eval may be recorded if a review is due or just ran, or the last eval is 7+ days old. No writes."""
    if state.lint_due() or state.eval_due():
        return True
    return not store.history.log(op="eval", since=_iso(now - EVAL_MIN_GAP_S), limit=1)


def mark_review_done(state, now: float) -> None:
    """The semantic review request is handled; it ends with an eval, so allow one."""
    state.set_lint_due(False)
    state.set_last_review(now)
    state.set_eval_due(True)


def mark_eval_done(state, now: float) -> None:
    state.set_lint_due(False)
    state.set_eval_due(False)
    state.set_last_review(now)


def semantic_prompt(store) -> str:
    """Instructions for a semantic lint pass, followed by an eval."""
    raws = sorted(store.raw_files(), reverse=True)[:REVIEW_RAW_COUNT]  # names start with the date
    listed = "\n".join(f"- {r}" for r in raws) or "- (none)"
    return (
        "Recall semantic lint is due. Do it now, before other work:\n"
        "1. Read index.md (recall_get) and these newest raw records:\n"
        f"{listed}\n"
        "2. Find: contradictions between pages; claims that newer raw records replace; "
        "pages that should merge; low-confidence claims.\n"
        "3. Fix them with recall_ingest and cite sources (raw/<file>.md, commit:<sha> or repo paths). "
        "You can also run recall_lint(semantic=True) for the same instructions.\n"
        "4. Then call recall_eval to answer the golden questions in eval.md and record the score."
    )


def review_section(rt, payload) -> str:
    """SessionStart context section (`rt`: the Runtime): the semantic review request when due, else ""."""
    if not review_due(rt.store, rt.state, time.time()):
        return ""
    rt.state.set_eval_due(True)  # the request ends with recall_eval: let it record a score
    return semantic_prompt(rt.store)
