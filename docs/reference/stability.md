# What is stable, and what is not

Sidegraph is pre-1.0. That is not a licence to change anything at any time — some of this
project's surfaces are load-bearing for anyone who adopts it, and a few genuinely are not
settled. This page says which is which, so you can tell what you are allowed to build on.

Read it before you write a script, a CI job, a prompt, or a tool against Sidegraph.

## The three tiers

| Tier | The promise |
|---|---|
| **Committed** | Will not break in 0.x without either an automatic migration or a deprecation window with the old form still working. Breaking changes are always a `CHANGELOG` entry. |
| **Provisional** | May gain fields and entries without notice. May change or lose them with a `CHANGELOG` entry — but no migration and no compatibility shim. Build on it, pin your version. |
| **Not a contract** | May change in any release, silently. Do not parse it, do not assert on it, do not build tooling against it. |

A surface moves **up** a tier when it gains a mechanical guard, never because it merely feels
settled. That rule is the lesson of a real drift: `sidegraph-bootstrap --resume` had a test
asserting its help text was accurate, so the help text stayed right — and the reference page
described a mode that had not existed for weeks, because no test looked there.

## Where each surface sits

### Committed

| Surface | What is fixed | What guards it |
|---|---|---|
| **The store on disk** — `.sidegraph/` layout, one JSON file per record, the `format` marker, `schema_version` | The current version is **0.6.0**. `0.2.0`/`0.3.0` stores migrate automatically on first open (original kept as `*.migrated-backup`, never deleted). `0.4.0`/`0.5.0` reload in place. A future bump does one of those two — it never orphans a store. | `sidegraph-verify` lints every canonical file against the write-path invariants; `--against <ref>` classifies every changed file against the store's own transition rules |
| **CLI commands, flags, exit codes** | 18 entry points, 77 flags. A flag is not removed without a deprecation window; documented exit codes keep their meaning | every parser option must appear in its `cli.md` section (`tests/test_docs_claims.py`) |
| **Environment variables** | The 13 in [`configuration.md`](configuration.md), and the store-path precedence between them. `SIDEGRAPH_DB` stays honoured as the documented deprecated form. `SIDEGRAPH_RATIFY_POLICY`'s name and its `manual` default are committed; its auto values and their gates are Provisional (below). | bidirectional test: the documented set must equal what `src/` actually reads |
| **Hook process contract** | Read one JSON payload on stdin, write one JSON object on stdout, never block the host, never crash it — any internal failure prints `{}` and exits normally | the hook tests, and a live host run per release |

The *payload schema* inside the hook contract is **Claude Code's**, not ours. If the host
changes it, we follow the host; that is the point of keeping the host seam thin.

### Provisional

| Surface | Why it is not committed |
|---|---|
| **The MCP tool set and return shapes** (24 tools) | Still moving, demonstrably. `ratify_decisions` was deprecated before 1.0 shipped. `anchors_orphaned` was added on 2026-08-03 and gained a `reason` field the same day, both for good reasons. Tool *names* are stable in practice; return dicts gain keys. The latest: `ratified_by`/`auto_ratify_error` joined all three propose result shapes — `null` under the default policy, so the key set grew even where nothing else did. |
| **Flow profiles** — the six names and their ingest globs | `spec-kit`'s glob reads `specs/*/plan.md`, while that flow writes its rejected alternatives to `research.md`, which nothing reads. Fixing that changes a glob. |
| **`doctor` finding codes** | Advisory lint; the set grows as checks are added. Guarded against the docs, not frozen. |
| **Auto-ratification policy** — `SIDEGRAPH_RATIFY_POLICY`'s `auto-low-risk`/`auto-all` values and eligibility gates, the `, auto-ratified N` CLI summary segment, and `sidegraph-doctor`'s `auto share`/`auto supersede rate` lines | New, and not yet run on a real corpus: the supersede-rate signal it relies on is unmeasured, so the gates may tighten. `manual` stays the default and changes nothing. |

### Not a contract

| Surface | Why |
|---|---|
| **The text retrieval renders** — `[unratified]` / `[drifted]` markers, mistakes-first line format, budget splits, the `SessionStart` map layout | It is unversioned prose written for a model to read, and the delivery layer is this project's most active open question: between two releases 130 commits apart, the share of sessions that never call retrieval fell from 43% to 26%, with no single change able to take the credit (whitepaper §8.3), and a quarter still skip it. Expect it to keep changing. |
| **Archive segment format** — `archive/<date>-<seq>-<hash>.jsonl` | Declared, implemented, and **never exercised**: `archive/` does not exist even in this project's own 173-decision store. A format nobody has run is not something to promise. |
| **`index.db`** — every table and column | Derived, gitignored, rebuilt from the canonical files whenever stale. It is a cache. Read the JSON. |

## Guarantees vs. the surfaces that express them

The distinction that matters most, and the one easiest to get backwards: **several behaviours
are committed while the surface expressing them is not.** The rendered line is free to
change; what it is telling you is not.

| Committed behaviour | Expressed through (not a contract) |
|---|---|
| **Mistakes-first.** A gotcha or lesson about the code in hand ranks ahead of ADRs and constraints. The one hard ranking guarantee. | the order of lines in `get_task_context` output |
| **Unratified memory cannot outrank ratified memory.** Proposed records are quarantined below all accepted memory on the five **delivery** surfaces: `get_task_context`, the thin queries, the `SessionStart` TOC, `drill_down`, and the `PreToolUse` nudge — and may stop rendering there entirely: outside the surfacing window (`SIDEGRAPH_PROPOSAL_WINDOW_DAYS`, default 30) or in regulated mode (`SIDEGRAPH_UNRATIFIED=off`). Withheld from rendering never means removed — the record stays in the store, in `sidegraph-ratify`, and in the SessionStart counter, which is exempt from both switches. `retrieve_decisions`/`list_facts` are raw listings, not ranked delivery — they are unranked and never tagged, but they are **not exempt from the policy**: since 2026-08-04 they apply the same surfacing window and regulated mode, so no agent-reachable read API returns unratified content that the two switches withhold. (Before that date they did return it — a reviewer correctly called the exemption a documented bypass of an advertised control.) | the `[unratified]` tag, where the block appears, and the window's default value |
| **Memory signals its own decay.** A record anchored to code that changed since capture is marked, not silently served as current. | the `[drifted]` tag |
| **Nothing leaves your machine.** No network calls, no remote telemetry. Optional local diagnostics live in the gitignored index and switch off with one variable. | `SIDEGRAPH_TELEMETRY` (the variable name is committed; the diagnostics tables are not) |

If you need one of these guarantees in a script, assert on the store or the MCP return value,
never on rendered text.

## The invariants underneath everything

These are not a tier. They are the premise — no version of Sidegraph trades them away:

1. **The engine's `graph.json` is read-only input.** Sidegraph never writes into it.
2. **The store is append-only.** No `Decision` is ever hard-deleted. A reversal closes the old
   record and appends a successor; the superseded record stays retrievable, because that
   history *is* the product.
3. **`schema_version` is stamped on the store**, always, from the first write.

## When we break something

- **Committed:** a `CHANGELOG` entry, plus a migration or a deprecation window. Never both
  absent.
- **Provisional:** a `CHANGELOG` entry.
- **Not a contract:** nothing. That is what the tier means.

Pin the version if any of this matters to you — `pip install 'sidegraph==X.Y.Z'`, or a commit
SHA in the `uvx --from git+…@<sha>` form. Pre-1.0, minor bumps may break provisional surfaces.
