# Sidegraph

A **decision / lessons layer** — durable team memory over an ADR/SAD corpus (and code)
that doesn't forget *why* things are the way they are as time passes and people move on.

**Thesis: own the memory, rent the graph.** Sidegraph owns an append-only, repo-committed
decision store and layers it as a **sidecar** over a mature code-graph engine
([Graphify](https://github.com/Graphify-Labs/graphify)) rather than re-building entity extraction
and graph construction. The edge over session-memory tools is durable anchoring + budgeted,
task-aware retrieval + decision memory riding on the engine's entity graph.

**Product continuity test:** Sidegraph exists to make separate agent sessions behave like a
continuing participant in the project. A core feature must preserve, organize, or deliver
accumulated project understanding. Document search by itself is not the product.

Sidegraph is pre-1.0 (v0.23.0) — the full loop (capture, ratification, mistakes-first
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
   that history *is* the product. A graph rebuild (`sidegraph-sync`) must not produce a git
   diff from regenerable state: it only ever touches derived, gitignored state (the local
   index, entity mappings, binding status, the TOC cache), never a committed record file
   (community labels are derived state, never committed). The one exception: a confirmed
   leaf-file move may update that entity's committed descriptor.
3. **The store format is a public contract.** Per-record JSON files (packed into shards in
   format 2), a stamped
   `schema_version`, and the append-only/merge rules are specified in
   [`docs/reference/store-format.md`](docs/reference/store-format.md) — that page is the
   source of truth for the on-disk layout, not this file.
4. **Keep the three seams clean** (see Architecture). The core never imports Graphify
   internals or host-specifics (Claude Code, Codex, …) directly.
5. **Obvious-to-an-LLM or cut it.** Every schema field and feature must be obviously useful
   to an LLM consuming the memory. If you can't justify it that way, drop it.

## Architecture — three seams

Keep these boundaries clean; they are what make the project OSS-friendly and churn-resistant.

Key modules only; `cli.py`, `bootstrap/`, `viz/` and the rest of `src/sidegraph/` sit
beside them.

```
src/sidegraph/
├── schema.py        # Pydantic models: Entity, Decision, Fact, AnchorBinding, Domain, Initiative
├── store.py         # owned append-only store (per-record JSON files, packed into shards by maintenance); enforces invariants on write
├── retrieval.py     # multi-altitude, budget-bounded, task-aware read path (mistakes first)
├── domain_snapshot.py  # committed domain-membership snapshot: format and matching rules
├── hot_index.py     # raw-sqlite handle on index.db for the hook hot path (no Store, no pydantic)
├── store_layout.py  # store constants and rules shared with the hot path (pydantic-free)
├── server.py        # FastMCP decision MCP — the tools agents call
├── engine/          # ── ENGINE SEAM ── the ONLY place Graphify specifics may live
│   ├── reader.py    #   GraphifyReader: reads graph.json (read-only input; no MCP)
│   └── rationale.py #   matching of Graphify rationale labels across its cut forms
└── host/            # ── HOST SEAM ── agent-host integration (SessionStart/Stop/PreToolUse/SubagentStart)
    ├── hooks.py     #   hook entry points; branch on host family (Claude Code, Codex, Muse, Grok Build)
    ├── mcp_entry.py #   `sidegraph-mcp` entry: server + session-binding middleware
    ├── session_identity.py  #   host session id from clientInfo, env, _meta
    ├── subagent.py  #   SubagentStart: the memory brief for a starting subagent
    ├── launch.py    #   the commit SessionStart resolved, which the hot-path hooks launch from
    ├── diagnostics.py  #   one bounded, content-free stderr signal per hook call
    └── claude_settings.py  #   the ratify-policy entry `sidegraph-init` writes
```

- **Portable core** (`schema`, `store`, `retrieval`, `server`) knows nothing about Graphify
  or any particular host. It speaks only `NodeRef` / `AbstractRef` / `descriptor`.
- **Engine seam** (`engine/`): a single thin `GraphifyReader` is the *only* module that
  imports Graphify specifics. A Graphify release breaks at most this one file. Pin the engine
  version; version the reader against it. Graphify is the reference (and only) adapter — do
  **not** build a second graph engine integration; just don't let the core reach past the
  reader.
- **Host seam** (`host/` and `plugin/sidegraph/`): everything host-specific lives here.
  Claude Code, Codex, Muse and Grok Build run the same hook entry points, which branch on the
  host family where payloads differ; OpenCode V2 gets MCP tools and shared skills. The
  portable core never imports `host/`.

See [`docs/concepts/data-model.md`](docs/concepts/data-model.md) for the record types
(`Entity`, `Decision`, `Fact`, `AnchorBinding`, `Domain`, `Initiative`) and
[`docs/concepts/anchoring.md`](docs/concepts/anchoring.md) /
[`docs/concepts/retrieval.md`](docs/concepts/retrieval.md) for how they're anchored and read.

## Stack & conventions

- **Python 3.13+** (`.python-version` pins it). Managed with `uv`.
- **Pydantic v2** for schema/validation. **FastMCP** for the MCP server. The store itself is
  **file-per-record JSON, optionally packed into shard files by maintenance (format 2)** (see
  [`docs/reference/store-format.md`](docs/reference/store-format.md))
  — no server, repo-committable, merges like code — with a derived, gitignored **SQLite**
  (stdlib `sqlite3`) index rebuilt from those files for fast queries. `python-ulid` for ids.
- **Type hints everywhere.** Public functions get docstrings explaining the invariant or
  design decision they implement, not just their parameters.
- **Tests:** `pytest`. Write the test first for schema invariants and store write-path rules —
  they are the contract. **Lint/format/types:** `pre-commit` (ruff + mypy + housekeeping
  hooks) — one definition of "lint", shared with CI; see `.pre-commit-config.yaml`.
- Match the surrounding code's idiom and comment density. Keep modules small and single-seam.

## Common commands

Setup, tests and lint: the "Development setup" block in
[`CONTRIBUTING.md`](CONTRIBUTING.md) is the one list (`uv sync --locked`, the test tiers,
`uv run pre-commit run --all-files`). CI runs that same pre-commit configuration as its lint
job, plus a history secret scan and, on ready pull requests, the full test matrix.

```bash
uv run sidegraph-mcp               # run the decision MCP server (stdio)
                                   # (`python -m sidegraph.server`: same server, no host identity)
```

## Working agreements

- **Read the relevant [`docs/reference/`](docs/reference/) and [`docs/concepts/`](docs/concepts/)
  pages before changing core behavior** — they document the invariants and trade-offs those
  modules already resolved; don't re-litigate them without reason.
- Prefer the smallest change that fixes or ships the thing at hand.
- Never introduce a second graph engine or a second host *seam in code* — those are
  explicitly out of scope; keep the seams clean enough that they'd be *possible* without a
  core rewrite.
