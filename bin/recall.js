#!/usr/bin/env node
// npx launcher for Recall: forwards every call to `uvx --from <source> recall`.
// RECALL_SOURCE overrides the package source (e.g. a git+https URL or a local path).
"use strict";

const { spawnSync } = require("node:child_process");

const SOURCE = process.env.RECALL_SOURCE || "git+ssh://git@github.com/annt07/devin-recall.git";

// Tells `recall install` to write `uvx --from <source> recall ...` into hook and
// MCP commands instead of the path of this ephemeral uvx environment.
const env = Object.assign({}, process.env, { RECALL_UVX_FROM: SOURCE });

const res = spawnSync("uvx", ["--from", SOURCE, "recall", ...process.argv.slice(2)], {
  stdio: "inherit",
  env,
  shell: false,
});

if (res.error) {
  if (res.error.code === "ENOENT") {
    console.error("recall: `uvx` was not found on PATH.");
    console.error("Install uv first: https://docs.astral.sh/uv/getting-started/installation/");
  } else {
    console.error(`recall: failed to start uvx: ${res.error.message}`);
  }
  process.exit(1);
}
process.exit(res.status === null || res.status === undefined ? 1 : res.status);
