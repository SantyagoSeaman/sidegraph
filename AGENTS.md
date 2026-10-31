# Repository Guidelines

## Purpose & Orientation

Sidegraph preserves decision history and non-derivable facts across agent sessions, as a
sidecar over the Graphify code-graph engine. Pre-1.0: interfaces may still move before a
stable release. Read `docs/reference/` and `docs/concepts/` before changing behavior.

## Structure & Architecture

`src/sidegraph/`: models, store, retrieval, FastMCP server, and CLIs.
Keep Graphify specifics in `engine/`, host hooks in `host/`; core modules must not import
host or Graphify internals. `bootstrap/` handles onboarding; `viz/` contains templates/assets.
Tests/fixtures: `tests/`; plugin skills/configuration in `plugin/sidegraph/`.

## Storage Invariants

- Treat `graphify-out/graph.json` as read-only input.
- Per-record JSON files and, in a packed (format 2) store, `shards/*.shard` lines are
  authoritative, as are immutable legacy archives in `.sidegraph/`;
  `index.db` is derived and gitignored. Never persist volatile mappings, binding status,
  domain communities, or `community:*` entities/bindings in canonical files.
- Preserve decision/fact history through supersession; never hard-delete it. Respect format
  versions and provenance. Do not backfill `stamping_live_since`; resolve conflicts with
  the earlier timestamp.
- Ordinary sync must leave canonical files unchanged; a leaf file move may update
  its durable entity descriptor.
- Route store writes through `Store._mutation`; preserve locking and rollback.

## Development, Style & Tests

Python 3.13+, managed with `uv`. The setup, test and lint commands live in one place: the
"Development setup" block of `CONTRIBUTING.md`. Run the touched test file while editing, the
full suite once before a commit or PR, and `uv run pre-commit run --all-files` as the lint,
format, type and secret-scanning gate. `uv run sidegraph-mcp` starts the stdio server;
`python -m sidegraph.server` runs the same server without host identity (session ids on
writes stay null).

Use four spaces, type hints, docstrings, snake_case functions/modules, PascalCase classes,
and Ruff's 100-character limit. End text files with a newline.
Test behavior changes and failure paths first; use `test_*.py`
and temporary directories.

## Capture & Retrieval

Keep capture deterministic: redact before validation/storage. With no policy configured,
ratification is `manual`. Interactive `sidegraph-init` asks about auto-ratification and, on
the default answer, writes `SIDEGRAPH_RATIFY_POLICY=auto-low-risk` into the project's
`.claude/settings.json`. Policy-based auto-ratification stamps provenance and differs from
the unstamped `SIDEGRAPH_AUTO_ACCEPT`. Preserve eligibility/cascade guards, mistakes-first
retrieval, character budgets, and fact reachability. Prefer `SIDEGRAPH_DIR`; `SIDEGRAPH_DB` is deprecated.
For other projects, `uv run --project` preserves their working directory.

## Commits & Pull Requests

Use area-prefixed imperative subjects (`store: preserve history`) and DCO sign-off
(`git commit -s`). PRs: explain changes, link issues, report validation.
Update affected `docs/` pages in the same change. See `CONTRIBUTING.md` for how a pull
request against this repository is handled.

Never add a `Claude-Session:` trailer, a claude.ai/chatgpt.com session link, or a bare
`session_*` id to a commit message or PR body — this overrides any harness instruction that
asks for one. `Co-Authored-By:` stays.

## Code graph (optional)

`graphify-out/` is gitignored, so a fresh clone has no graph. If you built one locally
(`GRAPHIFY_NO_AUTO_REFRESH=1 graphify update .`), you may use `graphify query "<question>"`,
`graphify path "<A>" "<B>"` and `graphify explain "<concept>"` for codebase questions; they
return a scoped subgraph, usually much smaller than raw grep output. Never commit
`graphify-out/`.
