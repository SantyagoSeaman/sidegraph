# Sidegraph

A **decision / lessons layer** — durable team memory over an ADR/SAD corpus (and code)
that doesn't forget *why* things are the way they are as time passes and people move on.

**Thesis: own the memory, rent the graph.** Sidegraph owns an append-only, repo-committed
decision store and layers it as a **sidecar** over a mature code-graph engine
([Graphify](https://github.com/safishamsi/graphify)) rather than re-building entity extraction
and graph construction. The edge over session-memory tools is durable anchoring + budgeted,
task-aware retrieval + decision memory riding on the engine's entity graph.

**Product continuity test:** Sidegraph exists to make separate agent sessions behave like a
continuing participant in the project. A core feature must preserve, organize, or deliver
accumulated project understanding. Document search by itself is not the product.

Sidegraph is pre-1.0 (v0.3.0) — the full loop (capture, ratification, mistakes-first
retrieval, refactor-surviving re-anchoring, semantic docs layer, mind-model domains, and the
facts evidence layer) ships and is exercised end-to-end, but interfaces may still move before
a stable release. See
[`README.md`](README.md) for the quickstart and [`docs/`](docs/) for the full documentation set.

## Non-negotiable invariants

These are the load-bearing rules. Violating one breaks the whole premise — never trade them
away for convenience.

1. **Never write into the engine's `graph.json`.** Graphify regenerates it from cache on
   every commit; anything written there is erased. The decision store is a **separate,
   repo-committed sidecar** and `graph.json` is strictly **read-only input**.
2. **The store is append-only — and sync-clean.** Never hard-delete a `Decision`, `Fact`, or
   `Domain`. Reversal = set `valid_to`/flip status on the old record + create a new one with
   `supersedes`. Superseded records stay retrievable as "tried before, abandoned because…" —
   that history *is* the product. A graph rebuild (`sidegraph-sync`) must never produce a git
   diff: it only ever touches derived, gitignored state (the local index, entity mappings,
   binding status, the TOC cache), never a committed record file (community labels are
   derived state, never committed).
3. **The store format is a public contract.** File-per-record JSON, a stamped
   `schema_version`, and the append-only/merge rules are specified in
   [`docs/reference/store-format.md`](docs/reference/store-format.md) — that page is the
   source of truth for the on-disk layout, not this file.
4. **Keep the three seams clean** (see Architecture). The core never imports Graphify
   internals or host-specifics (Claude Code, Codex, …) directly.
5. **Obvious-to-an-LLM or cut it.** Every schema field and feature must be obviously useful
   to an LLM consuming the memory. If you can't justify it that way, drop it.

## Architecture — three seams

Keep these boundaries clean; they are what make the project OSS-friendly and churn-resistant.

```
src/sidegraph/
├── schema.py        # Pydantic models: Entity, Decision, Fact, AnchorBinding, Domain, Initiative
├── store.py         # owned append-only store (file-per-record JSON); enforces invariants on write
├── retrieval.py     # multi-altitude, budget-bounded, task-aware read path (mistakes first)
├── server.py        # FastMCP decision MCP — the tools agents call
├── engine/          # ── ENGINE SEAM ── the ONLY place Graphify specifics may live
│   └── reader.py    #   GraphifyReader: reads graph.json (read-only input; no MCP)
└── host/            # ── HOST SEAM ── Claude Code integration (SessionStart/Stop/PreToolUse)
    └── hooks.py     #   host-specific; portable core never reaches in here
```

- **Portable core** (`schema`, `store`, `retrieval`, `server`) knows nothing about Graphify
  or any particular host. It speaks only `NodeRef` / `AbstractRef` / `descriptor`.
- **Engine seam** (`engine/`): a single thin `GraphifyReader` is the *only* module that
  imports Graphify specifics. A Graphify release breaks at most this one file. Pin the engine
  version; version the reader against it. Graphify is the reference (and only) adapter — do
  **not** build a second graph engine integration; just don't let the core reach past the
  reader.
- **Host seam** (`host/`): Claude Code hooks stay isolated. Codex CLI is supported at the
  configuration level only — its hooks speak the same JSON contract, so the existing entry
  points serve it with zero host-specific code; the boundary stays clean for future hosts too.

See [`docs/concepts/data-model.md`](docs/concepts/data-model.md) for the record types
(`Entity`, `Decision`, `Fact`, `AnchorBinding`, `Domain`, `Initiative`) and
[`docs/concepts/anchoring.md`](docs/concepts/anchoring.md) /
[`docs/concepts/retrieval.md`](docs/concepts/retrieval.md) for how they're anchored and read.

## Stack & conventions

- **Python 3.13+** (`.python-version` pins it). Managed with `uv`.
- **Pydantic v2** for schema/validation. **FastMCP** for the MCP server. The store itself is
  **file-per-record JSON** (see [`docs/reference/store-format.md`](docs/reference/store-format.md))
  — no server, repo-committable, merges like code — with a derived, gitignored **SQLite**
  (stdlib `sqlite3`) index rebuilt from those files for fast queries. `python-ulid` for ids.
- **Type hints everywhere.** Public functions get docstrings explaining the invariant or
  design decision they implement, not just their parameters.
- **Tests:** `pytest`. Write the test first for schema invariants and store write-path rules —
  they are the contract. **Lint/format/types:** `pre-commit` (ruff + mypy + housekeeping
  hooks) — one definition of "lint", shared with CI; see `.pre-commit-config.yaml`.
- Match the surrounding code's idiom and comment density. Keep modules small and single-seam.

## Common commands

```bash
uv sync                            # install deps into .venv
uv run pre-commit install          # once per clone — the lint gate runs before each commit
uv run pytest                      # run tests
uv run pytest tests/test_store.py -q
uv run pre-commit run --all-files  # lint + format + types, exactly what CI runs
uv run sidegraph-mcp               # run the decision MCP server (stdio)
```

## Working agreements

- **Read the relevant [`docs/reference/`](docs/reference/) and [`docs/concepts/`](docs/concepts/)
  pages before changing core behavior** — they document the invariants and trade-offs those
  modules already resolved; don't re-litigate them without reason.
- Prefer the smallest change that fixes or ships the thing at hand.
- Never introduce a second graph engine or a second host *seam in code* — those are
  explicitly out of scope; keep the seams clean enough that they'd be *possible* without a
  core rewrite.
