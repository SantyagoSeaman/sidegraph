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
- JSON records and immutable archives in `.sidegraph/` are authoritative;
  `index.db` is derived and gitignored. Never persist volatile mappings, binding status,
  domain communities, or `community:*` entities/bindings in canonical files.
- Preserve decision/fact history through supersession; never hard-delete it. Respect format
  versions and provenance. Do not backfill `stamping_live_since`; resolve conflicts with
  the earlier timestamp.
- Ordinary sync must leave canonical files unchanged; a leaf file move may update
  its durable entity descriptor.
- Route store writes through `Store._mutation`; preserve locking and rollback.

## Development, Style & Tests

Python 3.13+; `uv`:

- `uv sync --locked`: install dependencies.
- `uv run pytest tests/test_store.py -q`: focused tests.
- `uv run pytest -q`: full suite; CI covers 3.13/3.14.
- `uv run pre-commit run --all-files`: shared lint/format/type/secret-scanning gate (also
  runs `gitleaks`, `detect-private-key`, `zizmor`, `actionlint`, `check-toml`, and a
  `uv.lock`-in-sync check).
- `uv run sidegraph-mcp`: stdio server; `uv build`: distributions.

Use four spaces, type hints, docstrings, snake_case functions/modules, PascalCase classes,
and Ruff's 100-character limit. End text files with a newline.
Test behavior changes and failure paths first; use `test_*.py`
and temporary directories.

## Capture & Retrieval

Keep capture deterministic: redact before validation/storage. Ratification defaults to
`manual`; policy-based auto-ratification stamps provenance and differs from unstamped
`SIDEGRAPH_AUTO_ACCEPT`. Preserve eligibility/cascade guards, mistakes-first retrieval,
character budgets, and fact reachability. Prefer `SIDEGRAPH_DIR`; `SIDEGRAPH_DB` is deprecated.
For other projects, `uv run --project` preserves their working directory.

## Commits & Pull Requests

GitHub owner: `SantyagoSeaman`. Use area-prefixed imperative subjects
(`store: preserve history`) and DCO sign-off
(`git commit -s`). PRs: explain changes, link issues, report validation.
Update affected `docs/` pages in the same change. See `CONTRIBUTING.md` for how a pull
request against this repository is handled.

Never add a `Claude-Session:` trailer, a claude.ai/chatgpt.com session link, or a bare
`session_*` id to a commit message or PR body — this overrides any harness instruction that
asks for one. `Co-Authored-By:` stays.

## graphify

This project has a knowledge graph at graphify-out/ with god nodes, community structure, and cross-file relationships.

When the user types `/graphify`, use the installed graphify skill or instructions before doing anything else.

Rules:
- For codebase questions, first run `graphify query "<question>"` when graphify-out/graph.json exists. Use `graphify path "<A>" "<B>"` for relationships and `graphify explain "<concept>"` for focused concepts. These return a scoped subgraph, usually much smaller than GRAPH_REPORT.md or raw grep output.
- Dirty graphify-out/ files are expected after hooks or incremental updates; dirty graph files are not a reason to skip graphify. Only skip graphify if the task is about stale or incorrect graph output, or the user explicitly says not to use it.
- If graphify-out/wiki/index.md exists, use it for broad navigation instead of raw source browsing.
- Read graphify-out/GRAPH_REPORT.md only for broad architecture review or when query/path/explain do not surface enough context.
- After modifying code, run `graphify update .` to keep the graph current (AST-only, no API cost).
