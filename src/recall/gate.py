"""Gate: verifies each page write before the Store accepts it."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

if TYPE_CHECKING:
    from recall.store import PageChange, Store

PATH_RE = re.compile(r"^pages/(decision|fact|howto|gotcha|preference|task)/[a-z0-9][a-z0-9-]*\.md$")
RANGE_RE = re.compile(r"^(.+?):(\d+)-(\d+)$")
MAX_TOKENS = 800
DUP_THRESHOLD = 0.6
SECRET_PATTERNS = [
    ("aws-access-key", re.compile(r"AKIA[0-9A-Z]{16}")),
    ("github-token", re.compile(r"gh[pousr]_[A-Za-z0-9]{36,}")),
    ("gitlab-token", re.compile(r"glpat-[\w-]{20,}")),
    ("openai-key", re.compile(r"sk-[A-Za-z0-9]{20,}")),
    ("slack-token", re.compile(r"xox[baprs]-[\w-]{10,}")),
    ("private-key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("jwt", re.compile(r"eyJ[\w-]{10,}\.[\w-]{10,}\.[\w-]{10,}")),
    ("credential-assignment",
     re.compile(r"(?i)(password|passwd|secret|api[_-]?key|token)\s*[:=]\s*['\"]?[^\s'\"]{8,}")),
    ("env-secret-assignment",
     re.compile(r"\b(?:AWS_SECRET_ACCESS_KEY|GITHUB_TOKEN|[A-Z][A-Z0-9_]*_(?:API_KEY|TOKEN|SECRET))"
                r"\s*[:=]\s*['\"]?[^\s'\"]{8,}")),
]


@dataclass
class Verdict:
    kind: str  # accept | reject | redirect
    reason: str = ""
    target: str = ""


def _words(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]{3,}", text.lower()))


def _source_ok(src, store: "Store") -> bool:
    if not isinstance(src, str) or not src.strip() or "\n" in src or "\r" in src:
        return False
    if src.startswith("raw/"):
        name = src[4:]
        return bool(re.fullmatch(r"[\w.-]+\.md", name)) and (store.root / "raw" / name).is_file()
    if src.startswith("commit:"):
        return store.history.repo_has_commit(src[7:])
    m = RANGE_RE.match(src)
    rel = m.group(1) if m else src
    if m and int(m.group(2)) > int(m.group(3)):
        return False
    if Path(rel).is_absolute() or re.match(r"^[A-Za-z]:", rel):
        return False
    root = store.repo_root.resolve()
    try:
        target = (root / rel).resolve()
        target.relative_to(root)
    except (ValueError, OSError):
        return False
    try:
        target.relative_to(store.root.resolve())
        return False  # the store itself is never a code source
    except ValueError:
        pass
    return target.is_file()


def check(change: "PageChange", store: "Store") -> Verdict:
    front = change.front or {}
    m = PATH_RE.match(change.path or "")
    if not m:
        return Verdict("reject", "path: must match pages/<type>/<slug>.md")
    if front.get("type") != m.group(1):
        return Verdict("reject", "type: must equal the path folder")
    if not str(front.get("title") or "").strip():
        return Verdict("reject", "title: must not be empty")
    from recall.store import CONFIDENCE, STATUSES
    if front.get("status", "active") not in STATUSES:
        return Verdict("reject", f"status: must be one of {', '.join(STATUSES)}")
    if front.get("confidence") not in CONFIDENCE:
        return Verdict("reject", f"confidence: must be one of {', '.join(CONFIDENCE)}")
    sources = front.get("sources")
    if not isinstance(sources, list) or not sources:
        return Verdict("reject", "provenance: sources must be a non-empty list")
    for i, src in enumerate(sources):
        if not _source_ok(src, store):
            # Do not echo the source text: it is agent input and may hold secrets.
            return Verdict("reject", f"provenance: source #{i + 1} does not exist")
    body = str(change.body or "")
    try:
        y = yaml.safe_dump(front, sort_keys=False, allow_unicode=True)
    except yaml.YAMLError:
        return Verdict("reject", "front: not serializable")
    text = y + body
    if any(rx.search(change.path) for _, rx in SECRET_PATTERNS):
        return Verdict("reject", "secret: in path")
    for name, rx in SECRET_PATTERNS:
        if rx.search(text):
            return Verdict("reject", f"secret: {name}")
    if len(text) / 4 > MAX_TOKENS:
        return Verdict("reject", f"size: more than {MAX_TOKENS} tokens; split or condense")
    if change.path not in store.pages():
        words = _words(f"{front.get('title', '')} {body}")
        for rel in store.pages():
            f2, b2 = store.read_page(rel)
            if f2.get("status", "active") != "active":
                continue
            other = _words(f"{f2.get('title', '')} {b2}")
            union = words | other
            if union and len(words & other) / len(union) >= DUP_THRESHOLD:
                return Verdict("redirect", "near-duplicate: edit the existing page", rel)
    return Verdict("accept")
