# Using the memory day to day

Sidegraph shows up in two places during a normal session: an automatic map at session start,
and an on-demand, task-scoped pull whenever you ask the agent something that touches specific
code or docs. Both render through the same `TaskContext`/`top_tier_map` machinery in
`src/sidegraph/retrieval.py`.

## What `SessionStart` injects

Every new session, the `sidegraph-session-start` hook runs a lazy sync (best-effort, see
[`guides/surviving-refactors.md`](surviving-refactors.md)) and injects a compact table of
contents as `additionalContext` — not a full dump of the store. Which of two renderers runs
depends entirely on whether the store has any **accepted domains** yet (see
[mind model](../concepts/mind-model.md) and
[retrieval: SessionStart TOC](../concepts/retrieval.md#sessionstart-toc)):

- **Once at least one domain has been ratified** (`sidegraph-domains bootstrap`/`add` →
  `sidegraph-ratify` — see [naming your domains](naming-your-domains.md)), `render_toc()`
  renders the real, domain-named table of contents: one line per accepted domain (title,
  one-line summary truncated to 100 characters, mistake count, subdomain count when
  non-zero). This is the normal steady-state view for any project whose mind model has names
  in it — most projects, after the first bootstrap/ratify pass.
- **Before any domain is ratified** — a fresh repo, or one that hasn't bootstrapped/ratified
  domains yet — `top_tier_map()` is the fallback: the same surrounding shape, but with a
  nameless `## Communities` section (up to the 8 largest Graphify communities by member
  count, labeled only by their highest-degree "god" node, e.g. `Trader — 42 entities
  (community 3)`; omitted entirely if no graph is present) instead of `## Domains`.

Whichever renderer ran, the `sidegraph-session-start` hook prepends the same **standing
search instruction**, once, ahead of the rest of the text (not inside either renderer — see
[`reference/hooks.md`](../reference/hooks.md#sidegraph-session-start)):

> When you need to find or understand code in this project, call get_task_context(seeds)
> before any grep or file search — decisions, gotchas and a domain map are indexed here.

Unlike the `PreToolUse` nudge (`Read`/`Grep` only, once per session — see below), this line is
unconditional and covers every search surface behaviorally, not just those two tools — bash
`grep`/`rg`/`find` and MCP structure-query tools included.

Both renderers also inject:

- **Initiatives** — every `Initiative` recorded in the store (name + description).
- **Global mistakes & constraints** — up to the 10 most recent `gotcha`/`lesson`/`constraint`
  decisions scoped `global`, newest first. This is the only place decisions appear at
  session start; everything task-scoped is deferred to `get_task_context`.

Finally, unless `$SIDEGRAPH_RATIFY_NUDGE == "off"`, the hook appends one more line — own
`try/except`, so a count failure never costs the map above it — whenever the pending-
ratification queue is non-empty (nothing is appended at zero). Verbatim, with the counts
substituted:

> Sidegraph: N record(s) awaiting ratification (X decisions, Y facts, Z domains; oldest D days) — review
> with the ratify MCP tool or sidegraph-ratify.

The count is proposed decisions, standalone proposed facts (a fact riding a proposed
decision's cascade is covered by it and never double-counted), and proposed domains — see
[`reference/hooks.md`](../reference/hooks.md#sidegraph-session-start) and
[`guides/capturing-decisions.md`](capturing-decisions.md) for what "ratification" means and
the `sidegraph:ratify-decisions` skill (which this line is meant to trigger) for the
in-session walkthrough.

With an empty store and no graph, this degrades to just the standing instruction plus the
header — that's expected, not a bug (see the never-crash contract in
[`reference/hooks.md`](../reference/hooks.md)).

## Asking task-scoped questions so `get_task_context` fires with the right seeds

The map above is deliberately thin. The real payoff is asking the agent something scoped to
what you're about to touch, so it calls `get_task_context(files=[...], entities=[...])` with
seeds that resolve to the right part of the graph:

> I'm about to edit `trader/exec.py`'s order-placement path. What should I know first?
>
> I'm refactoring the `Trader` class. Anything I should be careful about?

- A **file seed** (`files=["trader/exec.py"]`) pulls in every anchorable node in that file
  (code symbols or doc headings).
- An **entity seed** (`entities=[{"name": "Trader", "file_path": "trader/exec.py"}]`) resolves
  through the same name+file matching `add_decision`'s anchors use.

Vague, non-task questions ("what does this project do") don't give the tool anything to seed
on — be concrete about the file or symbol so the retrieval has something to resolve.

## Reading the output blocks

`get_task_context` renders up to five Markdown sections, in this order, each one skipped
entirely if empty:

| Block | What it means |
|---|---|
| `## ⚠ Known mistakes & gotchas` | `gotcha`/`lesson`/`constraint` decisions bound directly to your seed entities, newest first. Read this first — these are the "someone already stepped on this" warnings. |
| `## Decisions` | `adr` decisions bound directly to your seed entities, newest first — each with its own still-live supporting facts nested directly under it (see below). |
| `## Known facts` | `Fact` records bound to your seed/peripheral entities that aren't already shown inline under a decision above — standalone evidence, not tied to any one decision you can see (see below). |
| `## Structural map` | The budgeted structural subgraph around your seeds (`- name (file_type) [path:line]`) — a map, not memory. |
| `## Related` | Decisions reached indirectly: via a shared community, via a peripheral entity that showed up in the structural map, or scoped `global`; plus one-liner call-outs for superseded decisions on your seed entities (`~ tried, reverted 2026-01: ...`). |

If nothing resolves at all, the tool returns the literal string `No context found.`

Each decision line looks like:

```
- [gotcha] Use locks in Trader: lock around order placement (rejected: no lock, relied on GIL)
```

`[unratified]` is appended to the tag when the underlying decision's status is still
`proposed` — see below.

## Facts: inline evidence, and the `## Known facts` block

A `Fact` you attached to a decision (its own `facts` list — see
[`guides/capturing-decisions.md#facts-the-evidence-layer`](capturing-decisions.md#facts-the-evidence-layer))
rides that decision's line: the moment a decision is rendered in `## ⚠ Known mistakes &
gotchas`, `## Decisions`, or the community/peripheral/global entries under `## Related`, its
still-live supporting facts render right under it, one indented line each. The one exception:
the `~ tried, reverted 2026-01: ...` superseded one-liners also shown under `## Related` never
get evidence — a reverted decision's evidence isn't useful at that tight, unbudgeted-for-detail
tier.

```
- [adr] Wrap httpx with an explicit retry policy for the exchange client: ...
  evidence: httpx has no built-in retry — a transient 5xx is not retried automatically. [httpx docs, 'Timeouts and retries' section]
```

Same convention as a decision's own tag, just moved inside the word: a still-`proposed` fact
renders `  evidence [unratified]: ...` instead of `  evidence: ...`. And the mistakes-first
guarantee holds here too — evidence is spent from the exact same character budget as
decisions, strictly after them, so a fact can never eat the budget a **mistake** line needed;
that's the one hard guarantee. It's narrower than "never displaces any decision," though:
within `## Decisions`/`## Related`, an inline evidence line CAN still crowd out a
lower-ranked decision in the same bucket under budget pressure — ordinary same-bucket ranking
noise, not a violation of anything, and the mistakes block is never exposed to it.

A fact that ISN'T attached to (or doesn't yet support) any decision that made it into the
output above still surfaces, standalone, in its own `## Known facts` block — rendered right
after `## Decisions`, ahead of the structural map:

```
## Known facts
- fact: httpx has no built-in retry — a transient 5xx is not retried automatically. [httpx docs, 'Timeouts and retries' section]
```

Same tag, same rule, just on the `fact` word instead: `- fact [unratified]: ...` for one still
awaiting `sidegraph-ratify`.

## When `related`-via-community appears

A decision shows up under `## Related` instead of the direct blocks in two situations:

1. **Community fallback.** The decision itself is Tier-1-anchored to a Graphify community
   (`community:<id>`), and one of your seed nodes belongs to that same community — even
   though the decision isn't anchored to your exact seed entity. This is what keeps a
   decision alive and reachable when its leaf anchor degrades (ambiguous) or orphans, and
   it's also how a decision anchored to *ADR-A* can still surface while you're editing
   *ADR-B*, if both live in the same reference cluster.
2. **Peripheral entities.** Anything that shows up as a non-seed node in the budgeted
   structural subgraph (reachable from your seeds within the `structure_budget`) also
   contributes its own decisions to `## Related` — one hop out from what you asked about,
   not a full graph walk.

Global-scope decisions land in `## Related` too (after community/peripheral), so a
repo-wide constraint you already saw at session start can resurface here if it's relevant.

## Why an unratified draft still shows up (and what the tag means)

A `propose_decisions` draft writes with `status="proposed"` immediately (unless
`SIDEGRAPH_AUTO_ACCEPT=on`, in which case it lands `accepted` directly and carries no tag at
all — see
[`guides/capturing-decisions.md#4-auto-accept-opt-in`](capturing-decisions.md#4-auto-accept-opt-in))
— and while it is **inside the surfacing window** it is not hidden from retrieval. It appears in both
`get_task_context` and the raw `retrieve_decisions` MCP listing exactly like an accepted
decision, except every rendered line carries an explicit `[unratified]` tag:

```
- [gotcha] [unratified] Use locks in Trader: lock around order placement
```

The tag is the signal, not an exclusion: a proposed decision is genuinely useful to see
immediately (the session that just ended might be the most relevant context for the very
next session), but the tag tells you and the agent it hasn't been reviewed yet — treat it
as provisional until someone runs `sidegraph-ratify --accept` (see
[`guides/capturing-decisions.md`](capturing-decisions.md)). Once ratified, the tag is gone
and the line is indistinguishable from a manually recorded decision. A **dropped** proposal
(`--drop`) is rejected and excluded from every default listing, same as a superseded one.

**Two limits on that visibility** (both read-time policies — the store itself is untouched,
and neither ever removes a record):

- **The surfacing window.** A proposal older than `SIDEGRAPH_PROPOSAL_WINDOW_DAYS`
  (default 30; `0` disables) stops rendering as content on every surface. The point is the
  default state of a queue nobody reviews: an unreviewed draft should stop influencing
  sessions, not accumulate influence. It stays in `sidegraph-ratify`, stays in the
  SessionStart counter, and ratifying it later is the ordinary accept path.
- **Regulated mode.** `SIDEGRAPH_UNRATIFIED=off` withholds all proposed content
  regardless of age — for deployments where unreviewed text must never reach an agent.
  Pin it in the repository's committed `.claude/settings.json` `env` block so CI can
  assert it.

The SessionStart pending-ratification counter is exempt from both: it reports the queue's
size **and the oldest item's age** even when nothing in it renders, because a queue you
cannot see is exactly the one that rots. `sidegraph-doctor` adds the latency counterpart
(`time-to-ratify`) once records carry the ratifier stamp.
