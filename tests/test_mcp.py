import asyncio
import json

import pytest

from helpers import page

TOOLS = {"recall_search", "recall_get", "recall_ingest", "recall_mark", "recall_lint", "recall_eval",
         "recall_log", "recall_history", "recall_show_at", "recall_diff", "recall_revert"}
P = "pages/fact/build-and-test.md"


def _pg():
    ch = page()
    return {"path": ch.path, "front": ch.front, "body": ch.body}


@pytest.fixture
def mcp_repo(store, monkeypatch, tmp_path):
    monkeypatch.setenv("RECALL_REPO", str(store.repo_root))
    monkeypatch.chdir(tmp_path)  # works from any cwd
    return store


def test_list_tools_is_exactly_eleven():
    from recall.mcp_server import mcp
    names = {t.name for t in asyncio.run(mcp.list_tools())}
    assert names == TOOLS


def test_ingest_search_mark_show_get(mcp_repo):
    from recall import mcp_server as m
    res = m.recall_ingest(pages=[_pg()], session="s1")
    assert res["accepted"] == [P] and res["commit"]
    json.dumps(res)
    assert m.recall_search("pytest")[0]["path"] == P
    assert m.recall_mark(P, "used")["used"] == 1
    assert "uv run pytest" in m.recall_show_at("session:s1", P)["text"]
    got = m.recall_get(P)
    assert got["front"]["title"] == "Build and test" and "uv run pytest" in got["body"]
    assert "# Recall index" in m.recall_get("index.md")["text"]
    assert m.recall_log(op="ingest")[0]["trailers"]["Recall-Session"] == "s1"
    assert m.recall_history(P)
    assert P in m.recall_diff("before:ingest#1", page=P)["diff"]
    rev = m.recall_revert("session:s1", page=P, session="s2")
    assert rev["commit"] and m.recall_search("pytest") == []
    json.dumps([m.recall_log(), m.recall_lint()])


def test_errors_are_dicts(mcp_repo):
    from recall import mcp_server as m
    assert "error" in m.recall_show_at("session:nope")
    assert "error" in m.recall_get("../../secret.txt")
    assert "error" in m.recall_mark(P, "bogus")
    assert "error" in m.recall_ingest(pages=[{"bad": 1}])


def test_eval_questions_then_score(mcp_repo):
    from recall import mcp_server as m
    from recall.evaluation import load_questions
    from recall.localindex import LocalIndex
    (mcp_repo.root / "eval.md").write_text("## Q: How to test?\nA: uv run pytest\n", encoding="utf-8")
    assert load_questions(mcp_repo)
    q = m.recall_eval()
    assert q["questions"] == ["How to test?"]
    r = m.recall_eval(answers={"How to test?": "Run uv run pytest -q"}, session="s1")
    assert r["score"] == 1.0 and r["commit"]
    assert mcp_repo.history.log(op="eval")[0].trailers["Recall-Eval"] == "1.00"
    assert LocalIndex(mcp_repo.repo_root).get_state("last_semantic_lint")


def test_semantic_lint_resets_state(mcp_repo):
    from recall import mcp_server as m
    from recall.localindex import LocalIndex
    idx = LocalIndex(mcp_repo.repo_root)
    idx.set_state("lint_due", "1")
    res = m.recall_lint(semantic=True)
    assert "findings" in res and "semantic lint" in res["semantic_prompt"]
    assert idx.get_state("lint_due") is None and idx.get_state("last_semantic_lint")
    assert "semantic_prompt" not in m.recall_lint()


def test_cli_has_mcp_subcommand(monkeypatch):
    from recall import cli, mcp_server
    called = []
    monkeypatch.setattr(mcp_server.mcp, "run", lambda *a, **k: called.append(1))
    assert cli.main(["mcp"]) == 0 and called
