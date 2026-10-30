from recall.store import PageChange


def page(path="pages/fact/build-and-test.md", title="Build and test", body="Run `uv run pytest`.",
         sources=("pyproject.toml",), **front):
    return PageChange(path, {"type": path.split("/")[1], "title": title, "summary": title,
                             "confidence": "high", "sources": list(sources), **front}, body)
