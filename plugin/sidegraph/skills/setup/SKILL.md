---
name: setup
description: Use when Sidegraph needs to be set up (or its setup finished/verified) in a repo — "set up sidegraph", "install sidegraph here", "get sidegraph working on this project", "настрой sidegraph", a fresh clone where memory should be turned on, or "why is my SessionStart map empty". Also reachable directly as /sidegraph:setup. The end-to-end first run: engine → graph → store → wiring check — then hands off to name-domains / import-adrs / record-decision for the first real memory.
---

# Set up Sidegraph

Bring a repo from zero to a working memory loop. One fact up front: **if this skill is
running, the Sidegraph plugin is already installed** — the MCP server and all three hooks
(`SessionStart`, `Stop`, `PreToolUse`) are wired and run via `uvx` straight from the
repository. What may still be missing: the engine, the graph, the store, and the first
memory. Work through the steps in order; every one is idempotent and safe to re-run.

## 1. Engine

```bash
graphify --version
```

Missing → install it, pinned to a known-good version: `uv tool install "graphifyy==0.9.6"`
(double-y package name; the CLI is `graphify` — see
[engine version pinning](../../../../docs/integrations/graphify.md#engine-version-pinning)
for the current known-good range). If the command is still not found afterwards, the
`uv tool` bin directory isn't on `PATH` — `uv tool update-shell`, new shell, retry.

## 2. Graph

```bash
graphify update .        # at the repo root
```

The LLM-free build — no API key. Produces `graphify-out/graph.json`, which Sidegraph reads
**read-only** (never writes). Works on code *and* markdown/ADR corpora, and on a non-git
folder too (sync falls back to a content-hash graph version — see
[non-git corpora](../../../../docs/integrations/graphify.md#non-git-and-doc-only-corpora)).

## 3. Store

```bash
uvx --from git+https://github.com/SantyagoSeaman/sidegraph.git@main sidegraph-init
```

Creates `.sidegraph/` — the repo-committed, file-per-record sidecar, plus its own
`.gitignore` keeping the derived `index.db` out of git — and reports the graph check.
Expected: `created store: .sidegraph` (or `already initialized: .sidegraph exists.` —
fine) and `found graph: graphify-out/graph.json`. `missing graph:` means step 2 didn't
run here. Commit `.sidegraph/` with the repo — the store travels with the code.

## 4. Verify the wiring

Call `list_domains()` — the cheapest read-only MCP tool. Any well-formed answer (an empty
`[]` on a fresh store is the expected one) proves the server is alive against this repo's
store. The `SessionStart` memory map appears at the start of the **next** session — a stub
on an empty store is expected, not broken. For the full seven-case proof (durability,
mistakes-first, refactor survival, ...), point the human at
[verifying your setup](../../../../docs/guides/verifying-your-setup.md).

## 5. Keep the graph fresh (recommend, don't run unasked)

Recommend `graphify hook install` — a post-commit hook that rebuilds the **code** graph on
every commit (doc/prose extraction still needs a manual `graphify update .` after doc
changes) — and chaining `sidegraph-sync` after Graphify's block in the same hook. Both
touch `.git/hooks/`, so confirm before installing. Details:
[keeping the graph fresh](../../../../docs/integrations/graphify.md#keeping-the-graph-fresh-git-hooks).

**Do NOT run `graphify claude install`.** The plugin already fronts the graph for Claude
Code (SessionStart map, read nudge, MCP); Graphify's own Claude wiring is redundant and
noisier — see
[why](../../../../docs/integrations/graphify.md#graphify-claude-install-is-redundant-with-the-sidegraph-plugin--skip-it).

## 6. First memory — hand off

In order of payoff:

1. **`sidegraph:name-domains`** — turn the graph's raw communities into a named table of
   contents; the moment one domain is accepted, the `SessionStart` TOC comes alive.
2. **`sidegraph:import-adrs`** — if the repo already has ADRs/specs/runbooks, seed the
   store from them instead of starting empty.
3. **`sidegraph:record-decision`** — capture the first gotcha by hand; then start a fresh
   session, ask about that area, and watch it come back first.

## Reset / cleanup

Everything Sidegraph owns in the repo is `.sidegraph/`; everything Graphify owns is
`graphify-out/`. Delete both to start over — source files are never touched.

## See also

- [`docs/getting-started/quickstart.md`](../../../../docs/getting-started/quickstart.md) —
  the same path in doc form, including the no-plugin manual wiring alternative.
- [`docs/reference/configuration.md`](../../../../docs/reference/configuration.md) —
  store/graph path resolution (`SIDEGRAPH_DIR`, `SIDEGRAPH_GRAPH`), the most common cause
  of "nothing shows up".
