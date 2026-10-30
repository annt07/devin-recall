import json
import time

import pytest

from recall import evaluation
from recall import maintenance as semantic
from recall.hooks import run
from recall.localindex import LocalIndex
from recall.runtime import Runtime


def _start(store):
    out = run("SessionStart", '{"session_id":"s1"}', str(store.repo_root)).stdout
    return json.loads(out)["hookSpecificOutput"]["additionalContext"]


def test_eval_score_and_ref(store):
    (store.root / "eval.md").write_text("# Golden questions\n\n## Q: How to test?\nA: uv run pytest\n\n## Q: Branch?\nA: main\n", encoding="utf-8")
    score = evaluation.run(store, lambda q: "Use UV run pytest." if "test" in q else "dev")
    assert score == 0.5
    sha = evaluation.record(store, score, "s1", Runtime.open(store.repo_root).state)
    assert store.history.resolve("best-eval") == sha
    assert store.history.log(limit=1)[0].trailers["Recall-Eval"] == "0.50"
    assert "eval | score=0.50" in (store.root / "log.md").read_text(encoding="utf-8")


def test_semantic_due(store):
    LocalIndex(store.repo_root).set_state("lint_due", "1")
    c = _start(store)
    assert "recall_lint(semantic=True)" in c and "recall_eval" in c


def test_semantic_not_due_after_recent_lint(store):
    LocalIndex(store.repo_root).set_state("last_semantic_lint", str(time.time()))
    assert "recall_lint(semantic=True)" not in _start(store)


def test_semantic_due_after_7_days(store):
    LocalIndex(store.repo_root).set_state("last_semantic_lint", str(time.time() - 8 * 86400))
    assert "recall_lint(semantic=True)" in _start(store)


def test_load_questions_max_10_and_empty(store):
    text = "".join(f"## Q: q{i}?\nA: a{i}\n\n" for i in range(12))
    (store.root / "eval.md").write_text(text, encoding="utf-8")
    qs = evaluation.load_questions(store)
    assert len(qs) == 10 and qs[0] == ("q0?", "a0")
    (store.root / "eval.md").write_text("# none\n", encoding="utf-8")
    assert evaluation.run(store, lambda q: "x") == 0.0


def test_record_clears_lint_due(store):
    rt = Runtime.open(store.repo_root)
    rt.state.set_lint_due(True)
    evaluation.record(store, 1.0, "s1", rt.state)
    assert rt.state.lint_due() is False and rt.state.last_review()
    assert semantic.review_section(rt, {}) == ""


def test_record_uses_caller_state_and_opens_no_index(store, monkeypatch):
    import recall.localindex as li
    from recall.session_state import SessionState
    state = SessionState(LocalIndex(store.repo_root))
    state.set_lint_due(True)
    monkeypatch.setattr(li, "LocalIndex", lambda *a, **k: pytest.fail("record opened a LocalIndex"))
    evaluation.record(store, 1.0, "s1", state)
    assert state.lint_due() is False and state.eval_due() is False
