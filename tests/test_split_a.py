"""Split A gap tests: G2 mismatch note caps the stale page list."""
from recall import hooks


class _Entry:
    def __init__(self, op, sha):
        self.trailers = {"Recall-Op": op, "Recall-Repo-Commit": sha}


class _Hist:
    def log(self, limit=50):
        return [_Entry("lint", "aaa"), _Entry("ingest", "bbb")]

    def repo_is_ancestor(self, sha):
        assert sha == "bbb"  # lint commits do not date the memory
        return False


class _Store:
    history = _Hist()


class _Rt:
    """The Runtime parts the mismatch section uses: the store and the page scan."""
    store = _Store()

    def pages(self):
        return [(f"pages/fact/p{i:02}.md", {"status": "stale"}, "") for i in range(13)]


def test_mismatch_note_caps_stale_pages():
    text = hooks._mismatch_section(_Rt(), {})
    assert text.startswith(hooks.MISMATCH_PREFIX)
    assert "pages/fact/p09.md" in text and "pages/fact/p10.md" not in text
    assert text.endswith("(+3 more)")
