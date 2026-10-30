import pytest
from helpers import page
from recall.store import Meta

M = Meta("ingest", "s1")


@pytest.mark.parametrize("change,reason", [
    (page(sources=()), "provenance"),
    (page(sources=("missing.py",)), "provenance"),
    (page(body="key AKIA" + "A"*16), "secret"),
    (page(body="x " * 2000), "size"),
    (page(confidence="sure"), "confidence"),
])
def test_rejects(store, change, reason):
    res = store.ingest([change], M)
    assert res.accepted == [] and reason in res.rejected[0][1]
    assert "AKIA" not in (store.root / "log.md").read_text()


def test_near_duplicate_redirects(store):
    store.ingest([page()], M)
    res = store.ingest([page(path="pages/fact/how-to-test.md")], M)
    assert res.redirected == [("pages/fact/how-to-test.md", "pages/fact/build-and-test.md")]


def test_raw_and_code_ranges_are_valid_sources(store):
    raw = store.add_raw("s1", "final", "we use uv")
    assert store.ingest([page(sources=(raw, "pyproject.toml:1-2"))], M).accepted
