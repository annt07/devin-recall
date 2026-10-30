"""Eval: golden questions in eval.md, a score, and a commit that carries `Recall-Eval`."""
from __future__ import annotations

import re
import time
from typing import Callable

MAX_QUESTIONS = 10


def load_questions(store) -> list[tuple[str, str]]:
    """Parse `## Q: <question>` followed by `A: <answer>` from eval.md (max 10)."""
    p = store.root / "eval.md"
    if not p.exists():
        return []
    out: list[tuple[str, str]] = []
    question: str | None = None
    for line in p.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if s.startswith("## Q:"):
            question = s[5:].strip() or None
        elif s.startswith("A:") and question is not None:
            answer = s[2:].strip()
            if answer:
                out.append((question, answer))
                if len(out) >= MAX_QUESTIONS:
                    break
            question = None
    return out


def _norm(text) -> str:
    text = re.sub(r"[^\w\s]", " ", str(text or "").lower())
    return " ".join(text.split())


def run(store, answerer: Callable[[str], str]) -> float:
    """Ask each golden question; it passes if the expected answer is a substring of the answer."""
    qs = load_questions(store)
    if not qs:
        return 0.0
    passed = 0
    for q, expected in qs:
        exp = _norm(expected)
        if exp and exp in _norm(answerer(q)):
            passed += 1
    return passed / len(qs)


def record(store, score: float, session: str, state) -> str:
    """Log and commit the score with op `eval`, then mark the eval done in `state` (the caller's
    SessionState); returns the commit sha."""
    from recall.maintenance import mark_eval_done
    from recall.store import Meta
    score = min(max(float(score), 0.0), 1.0)
    text = f"{score:.2f}"
    meta = Meta("eval", str(session or ""), "agent")
    with store.lock(meta.session):
        store.append_log("eval", f"score={text}", session or "", session_max=60)
        sha = store.history.commit(meta, f"eval: score={text}", eval_score=text,
                                   paths=["log.md", "eval.md"])
        sha = sha or store.history.resolve("HEAD")
    mark_eval_done(state, time.time())
    return sha
