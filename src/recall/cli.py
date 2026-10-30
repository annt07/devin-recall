"""Command line: `recall hook <event> --repo R`."""
from __future__ import annotations

import argparse
import sys


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="recall")
    sub = ap.add_subparsers(dest="cmd", required=True)
    h = sub.add_parser("hook", help="run a Devin hook")
    h.add_argument("event")
    h.add_argument("--repo", default=None)
    h.add_argument("--via-plugin", action="store_true",
                   help="hook call comes from the installed Devin plugin (opt-in per repo)")
    sub.add_parser("mcp", help="run the Recall MCP server (stdio); repo from RECALL_REPO or cwd")
    i = sub.add_parser("install", help="install Recall hooks, MCP config, Skill and store into a git repo")
    i.add_argument("repo")
    i.add_argument("--tool-project", default=None, help="run Recall with `uv run --project` from this checkout")
    args = ap.parse_args(argv)
    if args.cmd == "install":
        from recall.install import install
        try:
            res = install(args.repo, args.tool_project)
        except Exception as e:
            sys.stderr.write(f"recall install: {e}\n")
            return 1
        sys.stdout.write(f"Recall installed in {res['repo']} (runs: {' '.join(res['launcher'])})\n")
        return 0
    if args.cmd == "mcp":
        from recall.mcp_server import mcp
        mcp.run()
        return 0
    if args.cmd == "hook":
        from recall.hooks import run
        try:
            stdin_text = sys.stdin.buffer.read().decode("utf-8", errors="replace")
        except Exception:
            stdin_text = ""
        res = run(args.event, stdin_text, args.repo, via_plugin=args.via_plugin)
        if res.stdout:
            sys.stdout.buffer.write(res.stdout.encode("utf-8"))
            sys.stdout.buffer.flush()
        if res.stderr:
            sys.stderr.buffer.write(res.stderr.encode("utf-8"))
            sys.stderr.buffer.flush()
        return res.exit_code
    return 0


if __name__ == "__main__":
    sys.exit(main())
