# Sidegraph

[![tests](https://img.shields.io/github/actions/workflow/status/SantyagoSeaman/sidegraph/ci.yml?label=tests)](https://github.com/SantyagoSeaman/sidegraph/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/sidegraph)](https://pypi.org/project/sidegraph/)
![Python 3.13+](https://img.shields.io/badge/python-3.13%2B-blue)
![License](https://img.shields.io/badge/license-Apache--2.0-green)
![MCP](https://img.shields.io/badge/MCP-server-8A2BE2)

> **Give an AI coding agent the mental model of a project that an experienced engineer carries — what exists,
> how it is connected, why it is built this way, and what was already tried — served task-aware, within budget,
> before the first grep; and make that why survive rebuilds, refactors, and time.**

Coding agents broke an old equilibrium: **code is now produced faster than anyone
accumulates the understanding of why it is the way it is.** The reasoning that shaped
each change happens once — inside a session — and is discarded with its context window.
You pay for the tokens, keep the diff, and throw away the judgment.

Half of a project's knowledge is derivable: what the code does, how it's connected — an
agent excavates that with grep, cheaper with every model generation. The half that
decides projects is not: **why it's built this way, what was tried and abandoned, which
constraint from outside the code forced the shape.** That information isn't in the
artifact at all. No future model will recover it, because it exists exactly once — at
decision time — and then evaporates: people leave, sessions end, the ticket from three
years ago is never found. Sidegraph keeps that half and serves it back.

**Deciding whether this is worth your team's time?** Read the
[engineering whitepaper](docs/whitepaper/index.md) first. It states the idea,
walks one real decision chain end to end, reports what running it showed, and
gives a fit test you can apply to your own repository before installing anything.

## Quickstart

Works cold: no existing ADRs required. No API key — the core loop is fully local
(one optional docs-analysis feature uses one; it's marked below).

```bash
# 1. Install the graph engine and build a graph over your repo (code or markdown)
uv tool install graphifyy                # double "y" — that's the PyPI name; CLI is `graphify`
cd /path/to/your/repo && graphify update .
```

`[mcp]` is an **optional** extra on `graphifyy` (`uv tool install "graphifyy[mcp]"`) — it adds
Graphify's *own* MCP server, a deeper structure-query layer over the same graph. Sidegraph
only ever reads `graph.json`, so the plain install above is all it needs.

```
# 2. Inside a Claude Code session in that repo: install the plugin — MCP server + all
#    three hooks, wired automatically.
/plugin marketplace add SantyagoSeaman/sidegraph
/plugin install sidegraph@sidegraph
```

```bash
# 3. Install the CLIs + MCP server, then bootstrap the store in your repo
#    (creates .sidegraph/, prints setup instructions)
uv tool install sidegraph        # from PyPI — puts sidegraph-init / sidegraph-mcp / … on PATH
sidegraph-init

# Prefer the latest unreleased build straight from git instead of PyPI? Swap step 3 for:
#   uvx --from git+https://github.com/SantyagoSeaman/sidegraph.git@main sidegraph-init
#   `@main` is a mutable ref — it moves under you. Pin a tag or a SHA for CI.
```

4. **Name your domains** — turns the graph's communities into a described table of
   contents. Tell your agent *"name my domains"* (or run `/sidegraph:name-domains`) and
   pick one of the 2–3 ready-made sets it proposes. CLI alternative for scripted/CI use:
   `sidegraph-domains bootstrap` + `sidegraph-ratify` —
   see [naming your domains](docs/guides/naming-your-domains.md).

Then record your first decision in a session — *"record a gotcha: … anchor it to
`<function or heading>` in `<file>`"* — and watch it come back at the top of the context
next time the agent works near that code.

Already have ADRs or design specs? `sidegraph-bootstrap --host claude-code` previews,
imports, anchors, and proves one record through retrieval — see the
[Bootstrap guide](docs/getting-started/bootstrap.md). Full setup (hooks, env vars, Codex,
and the source-checkout path for contributors):
[quickstart](docs/getting-started/quickstart.md) and
[installation](docs/getting-started/installation.md).

## Why

**For the team** — the senior engineer who never quits: what leaves with a person is not
code, it's the map of dead ends. Onboarding a new engineer and a new agent session is the
same problem, solved once. Settled questions stay settled — reopening one is a deliberate
supersede with a reason, not amnesia.

**For the project** — documentation that knows when it's stale: unlike a wiki, the memory
is anchored into the code and flags its own decay when the code moves on. Before an agent
edits, retrieval puts the decisions and dead ends already recorded for that code in front of
it, at whatever speed the agent works.

**For the process** — a sidecar, not a reform: it sits beside whatever spec/ADR flow you
already run, capture is a byproduct of ordinary sessions, and the single ritual is a
ratification gate. Provenance on every record (who decided, when, on what evidence) is a
ready audit trail for the era of agent-made decisions.

One honest boundary: this is not "cheaper agents in general." Memory pays off where it
replaces reading prose and where the answer isn't in the code at all; on a large monorepo
where two greps answer the question, it costs more than it saves.

Three kinds of tools circle this problem, and each misses it:

- **Agent memory** (mem0, Letta, Graphiti) remembers *conversations* — not decisions
  bound to code entities.
- **Code graphs and indexers** (Serena, Potpie, repo maps) know *what calls what* — not
  why it's built this way or what was learned the hard way.
- **ADR markdown** records the why — as prose in a folder nobody opens at the moment it
  matters, with no link to the code it concerns.

None of them can answer: **"which decisions touch *this* function — and how did they
evolve?"** Sidegraph is built for exactly that question.

|  | CLAUDE.md / AGENTS.md | Session memory tools | ADR markdown | Code-graph engines | OKF bundle | **Sidegraph** |
|---|---|---|---|---|---|---|
| Retrieved at the moment of need | ✗ whole-loaded, every session | partially | ✗ | ✓ structure only | partially — progressive disclosure | ✓ task-seeded, budgeted |
| Knows *what was tried and rejected* | ✗ | ✗ | sometimes | ✗ | ✗ | ✓ first-class `rejected` field |
| Anchored to the code it concerns | ✗ | ✗ | ✗ | ✓ | partially — concept links, not code | ✓ and survives refactors ([how](docs/guides/surviving-refactors.md)) |
| Temporal validity & supersession | ✗ edit-in-place | ✗ | sometimes a status header | ✗ | ✗ | ✓ append-only: `valid_from`/`valid_to`, `supersedes` chains |
| Human gate on what enters memory | ✓ | ✗ | ✓ | ✗ | ✓ curated like code | ✓ gated for `adr`/`constraint`/domains, auto for low-risk kinds |
| Lives in your repo, merges like code | ✓ | ✗ opaque store | ✓ | ✗ per-tool cache | ✓ | ✓ file-per-record log, ratified in the PR diff |
| Health is CI-gateable | ✗ | ✗ | ✗ | ✗ | ✓ `okf validate` | ✓ `sidegraph-verify` + `sidegraph-doctor` exit codes |

[OKF](https://cloud.google.com/blog/products/data-analytics/how-the-open-knowledge-format-can-improve-data-sharing/)
standardizes portable knowledge *bundles*, not decision memory; `sidegraph-export-okf`
projects the full store, history included, into an OKF v0.1 bundle any OKF consumer can read.

## How it works

```
 you work a session ──▶ Stop hook nudges the agent to distill durable decisions
                                │ propose_decisions / propose_domains (secrets redacted)
                                ▼
                        you ratify / drop  ──▶  append-only decision log,
                                                 committed with your repo
                                                        │
 next session ◀── SessionStart TOC            anchored to entities in the
 of named domains ◀── get_task_context ◀────── engine's graph (read-only);
 mistakes first        drill_down             re-anchored after refactors
 blind Read/Grep ──▶ nudged back to get_task_context (once per session)
```

Two layers age differently: the **structure** layer (the code graph — entities,
dependencies, communities: *the what*) and the **decision** layer on top (*the why*). The
graph is disposable — the engine regenerates it from source at any moment. The memory must
never be — so it lives in a separate store that nothing regenerates, and re-anchors itself
as the code moves. The unit of memory is an **entity, never a line of code**: functions,
classes, modules, document headings. Line numbers shift with every edit; entities persist
through them.

On top of both sit named **domains**: each is a described area — title, WHY-IT-EXISTS
summary, optional subdomains — so the agent's first read of a session is a table of
contents it can answer from, not a bare community listing. A domain's membership anchors
to durable entities, not volatile community ids, so it survives a fresh clone and a graph
rebuild. See [docs/concepts/mind-model.md](docs/concepts/mind-model.md).

## What gets stored

| Record | What it is |
|---|---|
| `Decision` | The memory: kind (`adr` / `lesson` / `constraint` / `gotcha`), context, choice, **rejected alternatives**, consequences, validity period, supersession chain, provenance |
| `Fact` | The evidence layer: compact, non-derivable knowledge — a benchmark, an external constraint, something trial-learned — that supports a decision or stands alone; razor: never "the code does X" |
| `Entity` | Durable identity for a code/doc entity — survives the engine's shifting node ids |
| `AnchorBinding` | The link between a decision and the entities it concerns — degrades gracefully on refactors, never guesses |
| `Domain` | A named, described area of the system (title + WHY-IT-EXISTS summary, optional subdomains) — what the `SessionStart` table of contents and `drill_down` are built from |

Append-only is a feature: a reversed decision is closed and superseded, never deleted —
*"tried before, abandoned because…"* stays retrievable via `get_entity_history`.

Facts follow the same append-only and ratification rules as decisions; ratifying or
dropping a decision carries its still-pending facts along in the same verdict. Details:
[docs/concepts/data-model.md](docs/concepts/data-model.md) and
[docs/guides/capturing-decisions.md#facts-the-evidence-layer](docs/guides/capturing-decisions.md#facts-the-evidence-layer).

## MCP tools

| Tool | What it does |
|---|---|
| `get_task_context` / `query_structure` / `query_decisions` | Task-seeded context under a char budget, **mistakes ranked first** — the full slice, or either half alone |
| `drill_down` | Walk one named domain: summary, subdomains, members, decisions |
| `list_domain_candidates` | Read-only, path-grouped domain candidates — the machine half of naming a project |
| `list_domains` | Read-only listing of every domain (optionally filtered by status), with member counts and parent/child lineage |
| `add_decision` / `supersede_decision` | Append / reverse a decision (nothing is ever deleted) |
| `add_fact` / `supersede_fact` | Append / falsify a non-derivable fact — evidence for a decision, or standalone |
| `find_entity` / `get_entity_history` | Which decisions *and facts* touch this entity, and how they evolved |
| `retrieve_decisions` / `list_facts` | List current decisions / current facts |
| `propose_decisions` / `propose_domains` / `add_domain` | Draft a decision (plus attached or standalone facts) or name a domain, from a session or by hand |
| `supersede_domain` | Lineage-correct rename/re-scope of a domain: closes the old, writes a `proposed` successor |
| `list_proposed` / `ratify` | The human gate (see `SIDEGRAPH_RATIFY_POLICY` in the configuration reference): review pending decisions, facts, *and* domains, accept/drop (a decision's verdict cascades to its still-pending facts) |
| `sync_anchors` | Diagnostic/heal MCP counterpart to `sidegraph-sync` — re-anchor against the current graph and return the rebind report as data |
| `verify_store` | Read-only integrity lint of the store's canonical files — the MCP counterpart to `sidegraph-verify` |
| `add_anchors` | Append bindings to an existing decision or fact — in-place re-anchoring for the `heal-anchors` triage flow |

CLIs: `sidegraph-bootstrap` (reviewed cold-start import and production proof),
`sidegraph-init` (initialize the store), `sidegraph-domains` (bootstrap/name domains),
`sidegraph-ratify` (gate drafts), `sidegraph-sync` (re-anchor after a rebuild),
`sidegraph-compact` (archive closed decisions/domains), `sidegraph-verify` (lint store
integrity; `--against <git-ref>` for CI). See
[docs/guides/ci-cd-maintenance.md](docs/guides/ci-cd-maintenance.md) for GitHub Actions
recipes built on `sidegraph-sync --check`/`sidegraph-verify`.
Reference: [docs/reference/](docs/reference/mcp-tools.md).

## Works on code and on docs

Anchor decisions to functions and classes — or to **headings in your architecture
markdown** (LLM-free graph build, non-git folders supported). `sidegraph-bootstrap` parses
existing ADRs and design specs into anchored decisions deterministically, no LLM.

An optional **semantic pass** (`graphify extract`, one API key, cached per file) goes a
layer deeper on documentation: prose becomes `concept` nodes and thematic clusters, giving
retrieval a richer graph to anchor against. Walkthrough:
[docs/guides/semantic-docs.md](docs/guides/semantic-docs.md).

## Trust & privacy

- **Everything is local.** Sidegraph reads your repo and the engine's `graph.json`
  (strictly read-only) and writes small human-readable JSON records inside your repo,
  plus a local, gitignored index it can always rebuild. **Nothing leaves your machine** —
  no network calls, no remote telemetry, no account. Sidegraph does keep local usage
  diagnostics in that gitignored index (which stored memory was shown, and which files a
  session touched afterwards) so you can see which memory was shown and which files
  those sessions then touched; they never travel, and `SIDEGRAPH_TELEMETRY=off` disables them.
- **Secrets don't enter memory.** Proposed decisions and facts pass redaction before they
  are stored. A ratification gate controls what the agent's drafts can persist: human for
  `adr`/`constraint` decisions and domains always, and, if you answer yes to `sidegraph-init`'s
  question (or set `SIDEGRAPH_RATIFY_POLICY` yourself), an auto-ratification stamp for lessons,
  gotchas, and standalone facts instead of a person's review.
- **Nothing is silently rewritten.** The store is append-only; every change of mind is
  recorded as a supersession with its reason.

## The engine underneath

Entity extraction and graph construction come from
[Graphify](https://github.com/safishamsi/graphify) (its LLM-free build covers both code
and markdown); Sidegraph never re-implements them or writes into the engine's output.
The engine is optional at runtime: without a graph, records anchor to file paths and
domains and retrieval still works, but symbol-level anchors, communities, and moved-code
resolution need it (see the [operations reference](docs/reference/operations.md#the-graph-dependency-stated-plainly)).
Everything the engine produces is derived and regenerated on every rebuild; everything
Sidegraph stores is deliberate, ratified, and permanent. **Own the memory, rent the graph.**

## Status

Pre-1.0: interfaces may still move. Not a code indexer, not general agent memory, not a
graph engine — decision memory over a rented graph, and nothing else.

## Documentation

[Getting started](docs/getting-started/installation.md) ·
[Concepts](docs/concepts/decision-memory.md) ·
[Guides](docs/guides/capturing-decisions.md) ·
[Reference](docs/reference/mcp-tools.md) ·
[Integrations](docs/integrations/graphify.md) (Graphify · Claude Code · Codex) ·
[Verify your setup](docs/guides/verifying-your-setup.md) ·
[Operations](docs/reference/operations.md) ·
[Pilot kit](docs/pilot-kit/README.md) ·
[Engineering whitepaper](docs/whitepaper/index.md)

## License

[Apache-2.0](LICENSE).
