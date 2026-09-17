# Sidegraph

> **Every agent session starts fresh. Your project should not.**

Sidegraph carries the project's accumulated decision map across sessions, so the next
agent approaches its task with the context a returning engineer has built over years:
why the code took its current shape, what was tried, and what the team learned.

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
years ago is never found.

Sidegraph keeps that half and serves it back. Specs say what should be true and code
says what was built; Sidegraph keeps the third line — *how one became the other*: the
decisions, the rejected alternatives, and the lessons learned by doing — knowledge
recoverable from neither the documents nor the code. Every record is bound into one
graph with your code and your own planning artifacts, so the memory knows what it
governs — and notices when it goes stale. And it reaches the agent at the moment of
work, **mistakes first, before the first grep** — so no mistake is paid for twice, and
an agent doesn't confidently re-propose the design your team already rejected.

**Deciding whether this is worth your team's time?** Read the
[engineering whitepaper](docs/whitepaper/index.md) first. It states the idea,
walks one real decision chain end to end, reports what running it showed
(including the corpus where memory cost 25.5% more and answered worse), and
gives a fit test you can apply to your own repository before installing anything.

```text
You: "refactor risk/fee_gate.py"

Injected into the agent's context — before it reads a single file:

  ## ⚠ Known mistakes & gotchas
  - [gotcha] HFT strategies require 0% maker/taker fees: assert_zero_fees()
    checks account fees up front and raises FeeGateError — the bot refuses
    to trade.

  ## Decisions
  - [adr] Stop levels ratchet monotonically: force_widen() is the only
    entry point allowed to widen an active stop.
    evidence: backtest showed ad-hoc re-widening added ~12% drawdown [internal backtest, 2026-03]

  ## Related
  ~ tried, reverted 2026-01: [adr] threshold-based fee checks
```

**Maximum relevant context before the first grep — and no mistake paid for twice.**

No vector database, no service, no API key: your team's **decision log as small text
records in the repo** — decisions, domain definitions, anchors — merging like code and
readable in the PR diff, plus a local MCP server and three hooks.

[![tests](https://img.shields.io/github/actions/workflow/status/SantyagoSeaman/sidegraph/ci.yml?label=tests)](https://github.com/SantyagoSeaman/sidegraph/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/sidegraph)](https://pypi.org/project/sidegraph/)
![Python 3.13+](https://img.shields.io/badge/python-3.13%2B-blue)
![License](https://img.shields.io/badge/license-Apache--2.0-green)
![MCP](https://img.shields.io/badge/MCP-server-8A2BE2)

Green `tests` badge = the full suite (ruff · mypy · pytest) passing in CI on every push.
Install: **`pip install sidegraph`** (or `uv tool install sidegraph`) — a pure-Python package
(`sidegraph` on PyPI: the MCP server, the three hooks, and the `sidegraph-*` CLIs), no service,
no API key.

## Quickstart

Works cold: no existing ADRs required. No API key — the core loop is fully local
(one optional docs-analysis feature uses one; it's clearly marked below).

Already have ADRs or supported flow specifications? After building the graph, run
`sidegraph-bootstrap --host claude-code` to preview, review, anchor, and prove one record
through production retrieval. The 10–15 minute path is an explicitly unmeasured launch target;
see the [Bootstrap guide](docs/getting-started/bootstrap.md) for the six supported profiles,
host matrix, recovery contract, and reproducible dogfood path.

```bash
# 1. Install the graph engine and build a graph over your repo (code or markdown)
uv tool install graphifyy                # double "y" — that's the PyPI name; CLI is `graphify`
cd /path/to/your/repo && graphify update .
```

`[mcp]` is an **optional** extra on `graphifyy` (`uv tool install "graphifyy[mcp]"`) — it adds
Graphify's *own* MCP server, a deeper structure-query layer over the same graph. It works fine
installed alongside Sidegraph; Sidegraph itself only ever reads `graph.json`, so the plain
install above is all it needs.

```
# 2. Inside a Claude Code session in that repo: install the plugin — MCP server + all
#    three hooks, wired automatically. Builds straight from this repo via uv; no PyPI
#    publish needed.
/plugin marketplace add SantyagoSeaman/sidegraph
/plugin install sidegraph@sidegraph
```

> **`@main` is a mutable ref.** These commands track the branch — fine for trying Sidegraph out, but pin a commit SHA (`…/sidegraph@<sha>`) for CI, a shared team setup, or a pilot you intend to measure. See [docs/reference/stability.md](docs/reference/stability.md).

```bash
# 3. Install the CLIs + MCP server, then bootstrap the store in your repo
#    (creates .sidegraph/, prints setup instructions)
uv tool install sidegraph        # from PyPI — puts sidegraph-init / sidegraph-mcp / … on PATH
sidegraph-init

# Optional day-one seeding: import the rationale already sitting in your docstrings
sidegraph-import --dry-run

# Prefer the latest unreleased build straight from git instead of PyPI? Swap step 3 for:
#   uvx --from git+https://github.com/SantyagoSeaman/sidegraph.git@main sidegraph-init
```

4. **Name your domains** — turns the graph's communities into a described table of
   contents. Tell your agent *"name my domains"* (or run `/sidegraph:name-domains`) and
   pick one of the 2–3 ready-made sets it proposes — no long list to hand-curate. CLI
   alternative for scripted/CI use: `sidegraph-domains bootstrap` + `sidegraph-ratify` —
   see [naming your domains](docs/guides/naming-your-domains.md).

Then record your first decision in a session — *"record a gotcha: … anchor it to
`<function or heading>` in `<file>`"* — and watch it come back at the top of the context
next time the agent works near that code. The moment you accept a domain, `SessionStart`
starts answering from the top — a named table of contents instead of a bare community
listing. Full setup (hooks, env vars, Codex, and the source-checkout path for contributors):
[docs/getting-started/quickstart.md](docs/getting-started/quickstart.md) and
[docs/getting-started/installation.md](docs/getting-started/installation.md). Existing rationale:
[docs/getting-started/bootstrap.md](docs/getting-started/bootstrap.md).

**See it in action.** Sidegraph dogfoods itself: a
[`demo` branch](https://github.com/SantyagoSeaman/sidegraph/tree/demo) will carry Sidegraph's
own decision store — decisions and facts distilled from this project's design notes and
anchored to its real code graph. Once it ships, clone it (`git clone -b demo …`), run
`graphify update .`, and query the corpus to watch retrieval, supersession chains, and
mistakes-first ranking on a genuine project. The `public`/plugin branch stays lean — the store
ships only to `demo`, so installing the plugin never drags it along. **The `demo` branch ships
with a later release** — it does not exist yet, so the link above and the clone command do not
resolve today; see the
[Bootstrap guide's reproduce-the-dogfood-path section](docs/getting-started/bootstrap.md#reproduce-the-dogfood-path)
for the same note.

## Why

**For the team** — the senior engineer who never quits: what leaves with a person is not
code, it's the map of dead ends. Onboarding a new engineer and a new agent session is the
same problem, solved once. Settled questions stay settled — reopening one is a deliberate
supersede with a reason, not amnesia.

**For the project** — documentation that knows when it's stale: unlike a wiki, the memory
is anchored into the code and flags its own decay when the code moves on. Decisions are
made *in view of* prior decisions, so agent-speed production doesn't become agent-speed
architectural drift.

**For the business** — opex becomes an asset: today 100% of an agent's reasoning
amortizes to zero the moment the session ends. With Sidegraph every agent session leaves
a residue — decision capital that *compounds with project age* while everything else
(human memory, doc accuracy) decays. And it's the one investment model progress can't
commoditize: better models make derivable knowledge cheaper, not the non-derivable kind.

**For the process** — a sidecar, not a reform: it sits beside whatever spec/ADR flow you
already run, capture is a byproduct of ordinary sessions, and the single ritual is a
ratification gate — human by default, or a stamped auto-ratification policy where no human is
in the loop. Provenance on every record (who decided, when, on what evidence)
is a ready audit trail for the era of agent-made decisions.

One honest boundary, stated up front: this is not "cheaper agents in general." Memory
pays off where it replaces reading prose and where the answer isn't in the code at all;
on a large monorepo where two greps answer the question, it costs more than it saves.
What you buy is not speed — it's **owning your engineering judgment instead of renting it
back every session**. And writing decisions down is necessary but not sufficient: records
nothing surfaces at the moment of work simply go unread — delivery is the product.

Three kinds of tools circle this problem, and each misses it:

- **Agent memory** (mem0, Letta, Graphiti) remembers *conversations* — not decisions
  bound to code entities.
- **Code graphs and indexers** (Serena, Potpie, repo maps) know *what calls what* — not
  why it's built this way or what was learned the hard way.
- **ADR markdown** records the why — as prose in a folder nobody opens at the moment it
  matters, with no link to the code it concerns.

None of them can answer: **"which decisions touch *this* function — and how did they
evolve?"** Sidegraph is built for exactly that question: decision memory, anchored to a
real code graph, with temporal history.

|  | CLAUDE.md / AGENTS.md | Session memory tools | ADR markdown | Code-graph engines | OKF bundle | **Sidegraph** |
|---|---|---|---|---|---|---|
| Retrieved at the moment of need | ✗ whole-loaded, every session | partially | ✗ | ✓ structure only | partially — progressive disclosure | ✓ task-seeded, budgeted |
| Knows *what was tried and rejected* | ✗ | ✗ | sometimes | ✗ | ✗ | ✓ first-class `rejected` field |
| Anchored to the code it concerns | ✗ | ✗ | ✗ | ✓ | partially — concept links, not code | ✓ and survives refactors ([how](docs/guides/surviving-refactors.md)) |
| Temporal validity & supersession | ✗ edit-in-place | ✗ | sometimes a status header | ✗ | ✗ | ✓ append-only: `valid_from`/`valid_to`, `supersedes` chains |
| Human gate on what enters memory | ✓ | ✗ | ✓ | ✗ | ✓ curated like code | ✓ ratification loop |
| Lives in your repo, merges like code | ✓ | ✗ opaque store | ✓ | ✗ per-tool cache | ✓ | ✓ file-per-record log, ratified in the PR diff |
| Health is CI-gateable | ✗ | ✗ | ✗ | ✗ | ✓ `okf validate` | ✓ `sidegraph-verify` + `sidegraph-doctor` exit codes |

[OKF](https://cloud.google.com/blog/products/data-analytics/how-the-open-knowledge-format-can-improve-data-sharing/)
is complementary, not competing: it standardizes portable knowledge *bundles*, not decision
memory — and `sidegraph-export-okf` ships exactly that projection: the full store, history
included, as an OKF v0.1 bundle any OKF consumer can read.

The memory that matters most is what was tried, abandoned, and **why** — the mistake
you'd otherwise pay for twice. Sidegraph keeps it attached to the code and retrievable
long after everyone forgot.

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

Under the hood there are two layers that age differently: the **structure** layer
(the code graph — entities, dependencies, communities: *the what*) and the **decision**
layer on top (*the why*). The graph is disposable — the engine regenerates it from source at any moment. The
memory must never be — so it lives in a separate store that nothing regenerates, and
re-anchors itself as the code moves. The unit of memory is an **entity, never a line
of code**: functions, classes, modules, document headings. Line numbers shift with every
edit; entities persist through them.

A third piece sits on top of both: named **domains**. Rather than hand-curate a
200-line list of raw communities, you tell your agent *"name my domains"* and pick one of
2–3 ready-made sets it proposes (`/sidegraph:name-domains`); each domain is a described area
— title, WHY-IT-EXISTS summary, optional subdomains — so the agent's first read of a session
is a table of contents it can answer from, not a blind community listing. A domain's
membership anchors to durable entities, not volatile community ids, so it **survives a fresh
clone and a graph rebuild** — the mind-model layer is repo-committed team memory, same as the
decisions. See [docs/concepts/mind-model.md](docs/concepts/mind-model.md).

That is the whole design in one line: **a decision log that stays alive — anchored
precisely to code entities, durably to named domains, delivered mistakes-first before
the agent's first grep, and merging like code.**

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

Facts ride the same append-only/ratification rules as decisions, plus a cascade: ratifying
or dropping a decision carries every still-pending fact that supports it along in the same
call — one verdict, both records move. Retrieval renders a live fact as an inline
`evidence: <statement> [<source>]` line under the decision it supports, and a standalone one
in its own `## Known facts` block — never displacing a mistake line. Details:
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
| `list_proposed` / `ratify` | The human gate (by default — see `SIDEGRAPH_RATIFY_POLICY` in the configuration reference): review pending decisions, facts, *and* domains, accept/drop (dropping/accepting a decision cascades to its still-pending facts) |
| `sync_anchors` | Diagnostic/heal MCP counterpart to `sidegraph-sync` — re-anchor against the current graph and return the rebind report as data |
| `verify_store` | Read-only integrity lint of the store's canonical files — the MCP counterpart to `sidegraph-verify` |
| `add_anchors` | Append bindings to an existing decision or fact — in-place re-anchoring for the `heal-anchors` triage flow |

CLIs: `sidegraph-bootstrap` (reviewed cold-start import and production proof),
`sidegraph-init` (initialize the store), `sidegraph-domains` (bootstrap/name domains),
`sidegraph-import` (seed from existing rationale or ADR/spec markdown), `sidegraph-ratify`
(gate drafts), `sidegraph-sync` (re-anchor after a rebuild), `sidegraph-compact` (archive
closed decisions/domains), `sidegraph-verify` (lint store integrity; `--against <git-ref>`
for CI). See [docs/guides/ci-cd-maintenance.md](docs/guides/ci-cd-maintenance.md) for
GitHub Actions recipes built on `sidegraph-sync --check`/`sidegraph-verify`.
Reference: [docs/reference/](docs/reference/mcp-tools.md).

## Works on code and on docs

Anchor decisions to functions and classes — or to **headings in your architecture
markdown** (LLM-free graph build, non-git folders supported). `sidegraph-import`
seeds the store from rationale already sitting in your sources: docstrings with zero
extra setup, and — via `--docs` — your existing ADRs and design specs, parsed into
anchored decisions deterministically, no LLM.

An optional **semantic pass** (`graphify extract`, one API key, cached per file) goes a
layer deeper on documentation: prose becomes `concept` nodes and thematic clusters, and
import picks up rationale from the documents themselves. Walkthrough:
[docs/guides/semantic-docs.md](docs/guides/semantic-docs.md).

## Trust & privacy

- **Everything is local.** Sidegraph reads your repo and the engine's `graph.json`
  (strictly read-only) and writes small human-readable JSON records inside your repo,
  plus a local, gitignored index it can always rebuild. **Nothing leaves your machine** —
  no network calls, no remote telemetry, no account. Sidegraph does keep local usage
  diagnostics in that gitignored index (which stored memory was shown, and which files a
  session touched afterwards) so you can tell which memory is earning its keep; they never
  travel, and `SIDEGRAPH_TELEMETRY=off` disables them.
- **Secrets don't enter memory.** Proposed decisions and facts pass redaction before they
  are stored; a ratification gate — human by default, or an opt-in stamped policy — controls
  what the agent's drafts can persist.
- **Nothing is silently rewritten.** The store is append-only; every change of mind is
  recorded as a supersession with its reason.

## The engine underneath

Entity extraction and graph construction come from
[Graphify](https://github.com/safishamsi/graphify) (its LLM-free build covers both code
and markdown), and Sidegraph never re-implements them or writes into the engine's output.
The engine is optional at runtime: without a graph, records anchor to file paths and
domains and retrieval still works, but symbol-level anchors, communities, and moved-code
resolution need it (see the [operations reference](docs/reference/operations.md#the-graph-dependency-stated-plainly)). Everything the engine produces is derived
and regenerated on every rebuild; everything Sidegraph stores is deliberate, ratified,
and permanent. That split is the design: **own the memory, rent the graph.**

## Status

v0.1.0, on PyPI as [`sidegraph`](https://pypi.org/project/sidegraph/) (`pip install sidegraph`)
— also installable via the Claude Code plugin or directly from git (see Quickstart).
Published by a tag-triggered GitHub Actions workflow that gates on the full test suite
(trusted publishing, no stored token). Interfaces may still move before 1.0. The full loop — capture, ratification,
mistakes-first retrieval, refactor-surviving re-anchoring, semantic docs layer, the
mind-model layer (named domains, `SessionStart` table of contents, `drill_down`), and now the
facts layer (evidence attached to a decision or anchored standalone) — is exercised
end-to-end on real code and ADR corpora (a 4,700-node Python trading system and a 15-document
architecture corpus), with 2,339 tests as of this writing (a public checkout runs 2,196: the
four release-mechanics test files that read `tools/` aren't shipped, since `tools/` itself
isn't shipped, and 3 internal-corpus calibration tests skip — they need a private design
corpus not included here). Exact counts drift as tests are added; the `tests` badge above
tracks the suite passing, not a frozen number.
Honest boundaries: not a code indexer, not general agent memory, not a graph engine —
decision memory over a rented graph, and nothing else.

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
