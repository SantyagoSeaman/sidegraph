# Installation

Sidegraph has two parts: **Sidegraph itself** (the decision store + MCP server + hooks) and
**Graphify** (the code-graph engine it rents). Install both.

## Prerequisites

- **Python 3.13+** for Sidegraph (`.python-version` pins it).
- **Python 3.10+** for Graphify.
- [`uv`](https://docs.astral.sh/uv/) — used to install and run both.
- An MCP- and hooks-capable agent host: [Claude Code](claude-code-setup.md) or
  [OpenAI Codex CLI](codex-setup.md).

## Install Sidegraph

### Option A — PyPI (recommended for the package + CLIs)

`sidegraph` is a pure-Python package on PyPI. Install with pip or uv:

```bash
pip install sidegraph            # or: uv tool install sidegraph
```

That puts `sidegraph-mcp` and every `sidegraph-*` CLI on your PATH. Pin a version with
`pip install 'sidegraph==X.Y.Z'`. For an ephemeral run without installing,
`uvx --from sidegraph sidegraph-mcp` works — the entry-point name differs from the package
name, so `--from sidegraph` is required (a bare `uvx sidegraph-mcp` will not resolve).

### Option B — Claude Code plugin (recommended for Claude Code — auto-wires the hooks)

Inside a Claude Code session, in the repo you want memory over:

> **`@main` is a mutable ref.** Every `git+…@main` command on this page tracks the
> branch: what you install today is not what you installed yesterday, and a `uvx` cache
> refresh can change it under you. Fine for trying Sidegraph out; for anything you depend
> on — CI, a shared team setup, a pilot you intend to measure — replace `@main` with a
> commit SHA (`git+https://github.com/SantyagoSeaman/sidegraph@<sha>`) so the version is a
> decision you made rather than whatever HEAD happened to be. See [`reference/stability.md`](../reference/stability.md) for what each surface promises.

```
/plugin marketplace add SantyagoSeaman/sidegraph
/plugin install sidegraph@sidegraph
```

Installs the MCP server and all three hooks (`SessionStart`, `Stop`, `PreToolUse`)
automatically. The plugin's bundled config runs everything via `uvx --from
git+https://github.com/SantyagoSeaman/sidegraph.git@main`, so it builds straight from this
repository with `uv` — **no PyPI publish needed, works today**. See
[the plugin install path](../integrations/claude-code.md#plugin-install-path) for what the
bundled `.mcp.json`/`hooks.json` actually run, and the cwd-pinning details.

### Option C — uvx directly from git (latest / unreleased)

Want the latest unreleased build, prefer manual wiring, or need an entry point outside Claude
Code (e.g. `sidegraph-init` from a plain terminal)? Run it straight from the repository with
[`uvx`](https://docs.astral.sh/uv/guides/tools/) — no persistent install, no local checkout:

```bash
uvx --from git+https://github.com/SantyagoSeaman/sidegraph.git@main sidegraph-init
uvx --from git+https://github.com/SantyagoSeaman/sidegraph.git@main sidegraph-bootstrap --help
```

Every Sidegraph entry-point name differs from the source location, so any `uvx` invocation
must use `--from git+<url>@<ref> <entrypoint>` — a bare `uvx sidegraph-init` will not work.
`uv` caches the build after the first run, so repeat invocations are fast. Substitute
`git+ssh://git@github.com/...` for the same URL if you authenticate over SSH.

### Option D — from source (contributors)

```bash
git clone https://github.com/SantyagoSeaman/sidegraph.git   # or your local checkout
cd sidegraph
uv sync                 # installs into .venv
uv run pytest -q        # sanity check — all tests should pass
```

Invoke any entry point from another directory with `uv run --project`:

```bash
uv run --project /ABSOLUTE/PATH/TO/sidegraph sidegraph-mcp
```

Use `--project`, not `--directory` — `--directory` changes the working directory of the
invoked process, which breaks the relative `SIDEGRAPH_DIR` / `SIDEGRAPH_GRAPH` paths described
below.

**Troubleshooting**: if another project's virtualenv is already active in your shell (e.g.
you `source`d its `.venv/bin/activate`) when you run `uv run --project ...`, `uv` prints
`warning: VIRTUAL_ENV=... does not match the project environment path ... and will be
ignored` to stderr. That's `uv` itself, not Sidegraph, and it's harmless — the command still
runs against Sidegraph's own `.venv`. Silence it with `uv run --no-active --project ...` or by
deactivating the other venv first.


### Entry points

This table is the terminal reference. In day-to-day use you rarely type any of it: everyday
operations (recording, ratifying, syncing, domain work) run conversationally inside a
session through the MCP tools and plugin skills — see
[`reference/mcp-tools.md`](../reference/mcp-tools.md). The CLI commands below matter for
setup, scripted/CI use, and the two deliberately human-run jobs (`sidegraph-import`,
`sidegraph-compact`).

| Command | What it runs |
|---|---|
| `sidegraph-mcp` | The decision MCP server (stdio transport). |
| `sidegraph-session-start` | `SessionStart` hook — injects project memory. |
| `sidegraph-stop` | `Stop` hook — nudges the agent to distill decisions. |
| `sidegraph-pre-tool-use` | `PreToolUse` hook — redirects a blind `Read`/`Grep` toward `get_task_context`/`drill_down`. |
| `sidegraph-bootstrap` | Guided CLI — scan one of six supported ADR/spec profiles, preview and review candidates, write only after confirmation, verify anchors/host integration, and prove production retrieval. |
| `sidegraph-init` | CLI — bootstrap `.sidegraph/` in a repo: create the store, check for the graph, print the plugin install path (and the no-plugin `claude mcp add` alternative). |
| `sidegraph-ratify` | CLI — review/accept/drop proposed decisions and domains. |
| `sidegraph-sync` | CLI — re-anchor the store after a Graphify rebuild. |
| `sidegraph-import` | CLI — bootstrap decisions from Graphify rationale nodes (code docstrings, or ADR/SAD prose after a semantic pass), or from existing ADR/spec markdown directly (`--docs`). |
| `sidegraph-domains` | CLI — author Domain proposals: `bootstrap` from graph communities, or `add` manually. |
| `sidegraph-compact` | CLI — pack terminal-status (superseded/rejected/dropped) decisions and domains into an immutable archive segment. |
| `sidegraph-verify` | CLI — lint the store's canonical files against its write-path invariants (schema, validity windows, referential integrity, ULID uniqueness); `--against <git-ref>` additionally checks that every store file changed vs that ref was mutated legally (CI mode). |
| `sidegraph-doctor` | CLI — one-stop store health: composes `sidegraph-verify`'s strict lint with an advisory curation pass (stale proposals, dangling/degraded/orphaned records, never-surfaced decisions); `--check` also fails on advisory findings. |
| `sidegraph-viz` | CLI — render a read-only interactive HTML graph of the decision/fact store (nodes = decisions + facts + anchored entities; edges colored by anchor status; supersede + fact→decision links) plus a JSON sibling. |
| `sidegraph-export-okf` | CLI — project the store into a deterministic [Open Knowledge Format v0.1](https://github.com/GoogleCloudPlatform/knowledge-catalog/tree/main/okf) bundle any OKF-aware tool can read; strictly one-way, the store stays the source of truth. |
| `sidegraph-stats` | CLI — one screen of local usage statistics from the gitignored index (how often memory was asked for, how much of the code worked on has memory anchored to it, what the store holds, anchor health); read-only, never creates the index, `--json` for the same report as data. Reachable in a session as `/sidegraph:stats`. |
| `sidegraph-prepare-commit-msg` | `prepare-commit-msg` git hook — comments candidate `Sidegraph-Decision:` trailers into the commit message template for the human/agent to uncomment; never blocks or stalls `git commit`. See [`reference/git-bindings.md`](../reference/git-bindings.md). |
| `sidegraph-blame` | CLI — `git blame` a file, joined to the decisions/facts each hunk's commit carries (commit trailers + `provenance.commit`). See [`reference/git-bindings.md`](../reference/git-bindings.md). |

All of them read `SIDEGRAPH_GRAPH` (default `graphify-out/graph.json`) from the environment,
resolved relative to the process's working directory. Same for `SIDEGRAPH_DIR` — every command
(`sidegraph-mcp`, the three hooks, and every `cli.py` subcommand including `sidegraph-init`)
resolves the store directory through the same shared precedence: an explicit `--db` flag,
then `$SIDEGRAPH_DIR`, then the deprecated `$SIDEGRAPH_DB`, then an existing `.sidegraph/`,
then the default `.sidegraph` — see
[`reference/configuration.md`](../reference/configuration.md#store-path-resolution) for the
exact rules.

## Install Graphify

```bash
uv tool install "graphifyy==0.9.6"   # note the double-y package name; the CLI is `graphify`
graphify --version                    # sanity check
```

**Pin the version.** Graphify releases well ahead of Sidegraph's verified range, and the
engine seam is the one place an engine release can break Sidegraph. Known-good is
**0.9.6–0.9.8**; installing unpinned gets you whatever is current on PyPI, which is outside
that range and unverified. See
[engine version pinning](../integrations/graphify.md#engine-version-pinning) for how to
re-verify after an upgrade.

Fallback if you don't use `uv`: `pip install 'graphifyy==0.9.6'`.

`[mcp]` is an **optional** extra (`uv tool install "graphifyy[mcp]==0.9.6"`) that adds Graphify's
*own* MCP server — a deeper structure-query layer over the same `graph.json`. It coexists
fine with Sidegraph; Sidegraph only ever reads `graph.json` directly and doesn't need it. Add
`[pdf]` if your corpus includes PDFs (extras compose: `"graphifyy[mcp,pdf]"`).

See [`integrations/graphify.md`](../integrations/graphify.md) for how Sidegraph reads
Graphify's output, corpus types, and troubleshooting.

## Next

Continue to the [quickstart](quickstart.md) for the shortest path to a first captured and
retrieved decision. If the repository already contains ADRs or supported flow specs, use the
[Bootstrap existing rationale guide](bootstrap.md) for the preview-first path instead. Once
you've done that, run through
[verifying your setup](../guides/verifying-your-setup.md) — a nine-case checklist that
proves domain onboarding, domain management, durability, mistakes-first retrieval, quiet
capture, refactor survival, git-native merges, facts evidence and cascade, and your own usage statistics
actually work on your repo, each with an exact command and an observable
result.
