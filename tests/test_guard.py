import json
import os

import pytest

from recall.hooks import run


def _run(store, tool, ti):
    payload = {"tool_name": tool, "tool_input": ti}
    return run("PreToolUse", json.dumps(payload), str(store.repo_root))


@pytest.mark.parametrize("tool,ti,code", [
    ("write", {"file_path": ".devin/recall/pages/fact/x.md"}, 2),
    ("edit", {"path": "{root}/.devin/recall/index.md"}, 2),
    ("apply_patch", {"input": "*** Update File: .devin/recall/log.md\n"}, 2),
    ("write", {"file_path": "src/app.py"}, 0),
    ("exec", {"command": "ls"}, 0),
])
def test_guard(store, tool, ti, code):
    ti = {k: v.replace("{root}", str(store.repo_root)) for k, v in ti.items()}
    r = run("PreToolUse", json.dumps({"tool_name": tool, "tool_input": ti}), str(store.repo_root))
    assert r.exit_code == code and (code == 0 or "recall_ingest" in r.stderr)


def test_backslashes_and_case(store):
    p = str(store.repo_root / ".devin" / "recall" / "index.md").replace("/", "\\")
    if os.name == "nt":
        p = p.upper()
    assert _run(store, "write", {"file_path": p}).exit_code == 2


def test_forward_slash_absolute(store):
    p = str(store.repo_root).replace("\\", "/") + "/.devin/recall/x.md"
    assert _run(store, "edit", {"file_path": p}).exit_code == 2


def test_dotdot_into_store(store):
    assert _run(store, "write", {"file_path": "src/../.devin/recall/x.md"}).exit_code == 2


def test_dotdot_out_of_store(store):
    assert _run(store, "write", {"file_path": ".devin/recall/../other.md"}).exit_code == 0


def test_sibling_prefix_not_blocked(store):
    assert _run(store, "write", {"file_path": ".devin/recall2/x.md"}).exit_code == 0
    assert _run(store, "write", {"file_path": ".devin/other.md"}).exit_code == 0


def test_space_in_path_and_multi_patch(store):
    patch = ("*** Begin Patch\n*** Add File: docs/a b.md\n+x\n"
             "*** Delete File: .devin/recall/pages/fact/my page.md\n*** End Patch\n")
    r = _run(store, "apply_patch", {"patch": patch})
    assert r.exit_code == 2 and "recall_ingest" in r.stderr


def test_multi_edit_and_target_file(store):
    assert _run(store, "multi_edit", {"target_file": ".devin/recall/index.md"}).exit_code == 2
    assert _run(store, "write", {"notebook_path": ".devin/recall/n.ipynb"}).exit_code == 2


def test_store_root_itself_blocked(store):
    assert _run(store, "write", {"file_path": ".devin/recall"}).exit_code == 2


def test_bad_payload_permits(store):
    assert run("PreToolUse", "not json", str(store.repo_root)).exit_code == 0
    assert _run(store, "write", {"file_path": 5}).exit_code == 0
    r = run("PreToolUse", json.dumps({"tool_name": "write", "tool_input": "x"}), str(store.repo_root))
    assert r.exit_code == 0


def test_real_write_payload_shape(store):
    # Shape observed in the Task 0 probe (docs/research/hook-payloads-2026-10-30.md).
    for rel, code in ((".devin/recall/x.md", 2), ("src/ok.py", 0)):
        payload = {"hook_event_name": "PreToolUse", "session_id": "s", "prompt_id": "p", "tool_name": "write",
                   "tool_input": {"file_path": str(store.repo_root / rel), "content": "hi"},
                   "tool_use_id": "tu-1"}
        assert run("PreToolUse", json.dumps(payload), str(store.repo_root)).exit_code == code


def test_decide_returns_reason_or_none(store):
    from recall.guard import BLOCK_MSG, decide
    root, repo = store.root, store.repo_root
    assert decide(root, repo, {"tool_name": "write", "tool_input": {"file_path": ".devin/recall/x.md"}}) == BLOCK_MSG
    assert decide(root, repo, {"tool_name": "write", "tool_input": {"file_path": "src/ok.py"}}) is None
    assert decide(root, repo, {"tool_name": "exec", "tool_input": {"command": "ls"}}) is None


def test_markers_are_the_injected_texts():
    from recall import context_gauge, hooks, markers
    assert markers.INDEX_HEADER in markers.own_markers() and hooks.PENDING_MSG is markers.PENDING_MSG
    assert hooks.BLOCK_MSG is markers.STOP_BLOCK_MSG and context_gauge.NEAR_MSG is markers.NEAR_MSG
