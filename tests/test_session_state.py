"""SessionState: named operations over today's exact LocalIndex key names, and cleanup."""
import pytest

from recall.localindex import LocalIndex
from recall.session_state import SessionState

DAY = 86400.0


@pytest.fixture
def idx(tmp_path, monkeypatch):
    monkeypatch.setenv("RECALL_LOCAL_DIR", str(tmp_path / "local"))
    return LocalIndex(tmp_path / "repo")


@pytest.fixture
def st(idx):
    return SessionState(idx)


def test_start_key_and_encoding(st, idx):
    assert st.start("s1") is None
    st.set_start("s1", 100.5)
    assert idx.get_state("start:s1") == "100.5"
    assert st.start("s1") == 100.5


def test_ensure_start_writes_only_when_missing(st, idx):
    st.ensure_start("s1", 10.0)
    st.ensure_start("s1", 20.0)
    assert st.start("s1") == 10.0
    assert idx.get_state("start:s1") == "10.0"


def test_bad_float_reads_none(st, idx):
    idx.set_state("start:s1", "garbage")
    idx.set_state("stop_block:s1", "")
    assert st.start("s1") is None
    assert st.stop_block("s1") is None


def test_stop_block(st, idx):
    assert st.stop_block("s1") is None
    st.set_stop_block("s1", 5.0)
    assert idx.get_state("stop_block:s1") == "5.0"
    assert st.stop_block("s1") == 5.0


def test_post_compact_flag(st, idx):
    assert st.post_compact("s1") is False
    st.set_post_compact("s1", 7.0)
    assert idx.get_state("post_compact:s1") == "7.0"
    assert st.post_compact("s1") is True
    st.clear_post_compact("s1")
    assert idx.get_state("post_compact:s1") is None
    assert st.post_compact("s1") is False


def test_model(st, idx):
    assert st.model("s1") is None
    st.set_model("s1", "  claude-x ")
    assert idx.get_state("model:s1") == "claude-x"
    assert st.model("s1") == "claude-x"
    idx.set_state("model:s1", "   ")
    assert st.model("s1") is None


def test_gauge_tokens(st, idx):
    assert st.gauge_tokens("s1") is None
    st.set_gauge_tokens("s1", 1234, 9.5)
    assert idx.get_state("gauge:s1") == "1234 9.5"
    assert st.gauge_tokens("s1") == (1234, 9.5)
    idx.set_state("gauge:s1", "1234.0 9.5")  # today's reader accepts a float token count
    assert st.gauge_tokens("s1") == (1234, 9.5)
    idx.set_state("gauge:s1", "bad")
    assert st.gauge_tokens("s1") is None


def test_observed_limit(st, idx):
    assert st.observed_limit("claude") is None
    st.set_observed_limit("claude", 150000, 3.0)
    assert idx.get_state("gauge_observed:claude") == "150000 3.0"
    assert st.observed_limit("claude") == (150000, 3.0)
    st.set_observed_limit("claude", None, 4.0)  # "no samples" is cached as 0
    assert idx.get_state("gauge_observed:claude") == "0 4.0"
    assert st.observed_limit("claude") == (0, 4.0)
    idx.set_state("gauge_observed:claude", "x y")  # today's reader: value 0, time 0
    assert st.observed_limit("claude") == (0, 0.0)


def test_store_scope_keys(st, idx):
    assert st.last_review() is None
    st.set_last_review(11.0)
    assert idx.get_state("last_semantic_lint") == "11.0"
    assert st.last_review() == 11.0

    assert st.lint_due() is False
    st.set_lint_due(True)
    assert idx.get_state("lint_due") == "1"
    assert st.lint_due() is True
    st.set_lint_due(False)
    assert idx.get_state("lint_due") is None

    assert st.eval_due() is False
    st.set_eval_due(True)
    assert idx.get_state("eval_due") == "1"
    assert st.eval_due() is True
    st.set_eval_due(False)
    assert idx.get_state("eval_due") is None

    assert st.fingerprint() is None
    st.set_fingerprint("abc")
    assert idx.get_state("index_fingerprint") == "abc"
    assert st.fingerprint() == "abc"


def test_transcript_cache(st, idx):
    assert st.transcript("s1") is None
    st.set_transcript("s1", 100, 123456789, ["claude", 42])
    assert idx.get_state("transcript:s1") is not None
    assert st.transcript("s1") == (100, 123456789, ["claude", 42])
    st.set_transcript("s1", 0, 0, None)
    assert st.transcript("s1") == (0, 0, None)
    idx.set_state("transcript:s1", "{not json")
    assert st.transcript("s1") is None


def test_touch(st, idx):
    st.touch("s1", 50.0)
    assert idx.get_state("seen:s1") == "50.0"


def _fill(st, sid, t=1.0):
    st.set_start(sid, t)
    st.set_stop_block(sid, t)
    st.set_post_compact(sid, t)
    st.set_model(sid, "m")
    st.set_gauge_tokens(sid, 1, t)
    st.touch(sid, t)
    st.set_transcript(sid, 1, 1, None)


def _session_keys(idx, sid):
    return [k for k in idx.state_keys("") if k.split(":", 1)[-1] == sid and ":" in k]


def test_end_session_removes_only_that_session(st, idx):
    _fill(st, "a")
    _fill(st, "ab")  # prefix of nothing, but "a" is a prefix of "ab": must stay
    st.set_observed_limit("claude", 1, 1.0)
    st.set_lint_due(True)
    idx.record_injected("a", "pages/x.md")
    idx.record_injected("ab", "pages/x.md")
    st.end_session("a")
    assert _session_keys(idx, "a") == []
    assert len(_session_keys(idx, "ab")) == 7
    assert idx.get_state("gauge_observed:claude") is not None
    assert st.lint_due() is True
    assert idx.record_injected("a", "pages/x.md") is True  # row was gone
    assert idx.record_injected("ab", "pages/x.md") is False  # row stayed


def test_cleanup_stale(st, idx):
    now = 100 * DAY
    _fill(st, "old", now - 8 * DAY)
    _fill(st, "fresh", now - 1 * DAY)
    st.set_start("legacy_old", now - 8 * DAY)  # no seen: -> start: time
    st.set_start("legacy_new", now - 1 * DAY)
    st.set_model("orphan", "m")  # neither time -> removed
    st.set_observed_limit("claude", 1, 1.0)
    st.set_lint_due(True)
    st.set_eval_due(True)
    st.set_last_review(1.0)
    st.set_fingerprint("f")
    idx.record_injected("old", "p.md")
    idx.record_injected("fresh", "p.md")
    st.cleanup_stale(now)
    assert _session_keys(idx, "old") == []
    assert _session_keys(idx, "legacy_old") == []
    assert _session_keys(idx, "orphan") == []
    assert len(_session_keys(idx, "fresh")) == 7
    assert st.start("legacy_new") == now - 1 * DAY
    assert set(idx.state_keys("")) >= {"gauge_observed:claude", "lint_due", "eval_due",
                                       "last_semantic_lint", "index_fingerprint"}
    assert idx.record_injected("old", "p.md") is True
    assert idx.record_injected("fresh", "p.md") is False


def test_cleanup_seen_wins_over_start(st):
    now = 100 * DAY
    st.set_start("s", now - 30 * DAY)
    st.touch("s", now - 1 * DAY)
    st.cleanup_stale(now)
    assert st.start("s") == now - 30 * DAY


def test_cleanup_custom_max_age(st, idx):
    st.touch("s", 0.0)
    st.cleanup_stale(10.0, max_age_s=20.0)
    assert idx.get_state("seen:s") == "0.0"
    st.cleanup_stale(30.0, max_age_s=20.0)
    assert idx.get_state("seen:s") is None


def test_localindex_state_keys_prefix_is_literal(idx):
    idx.set_state("a%b", "1")
    idx.set_state("axb", "1")
    idx.set_state("a_c", "1")
    assert idx.state_keys("a%") == ["a%b"]
    assert idx.state_keys("a_") == ["a_c"]
    idx.delete_state(["a%b", "missing"])
    idx.delete_state([])
    assert idx.state_keys("a") == ["a_c", "axb"]
