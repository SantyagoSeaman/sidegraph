# Retrieval

The read path is multi-altitude, budget-bounded, and task-aware: a cheap named map at session
start, a one-level-deeper walk on demand, and a precise, seeded pull when the agent is about to
do work. Implemented in [`src/sidegraph/retrieval.py`](../../src/sidegraph/retrieval.py); wired
into Claude Code via [`src/sidegraph/host/hooks.py`](../../src/sidegraph/host/hooks.py) and
exposed as MCP tools in [`src/sidegraph/server.py`](../../src/sidegraph/server.py). See
[mind model](mind-model.md) for why the altitudes exist at all.

## SessionStart TOC

Two renderers compete for the same `SessionStart` slot, and which one runs depends entirely on
whether the store has any accepted domains yet:

- **`render_toc(cache)`** — the real, domain-named table of contents (activated the moment the
  first domain is ratified — no sync required, see [below](#when-the-toc-goes-live)): one line
  per accepted domain (title, one-line summary truncated to 100 characters, mistake count,
  subdomain count when non-zero), then initiatives, then global mistakes. This is what "the
  mind model comes alive" means in practice. The per-domain mistake count
  (`_domain_mistake_count`) is narrower than "all mistakes in this area": it counts only
  decisions Tier-1-bound to the domain's own `domain:<slug>` entity, i.e. captured (or
  re-anchored) after the domain existed — a `gotcha`/`lesson`/`constraint` anchored only to a
  leaf entity within the domain still surfaces in that entity's `get_task_context` output, it
  just doesn't add to this count.
- **`top_tier_map(store, reader)`** — the legacy, nameless fallback: top communities by member
  count (labeled only by god-node name), initiatives, global mistakes. Runs on demand (no
  cache) whenever the store has zero accepted domains — a fresh repo, or one that hasn't
  bootstrapped/ratified any domains yet, sees exactly the same output as before this layer
  existed.

Neither renderer includes the standing search instruction itself — `host.hooks.session_start`
prepends it once, at the hook-assembly level, ahead of whichever renderer's text follows, so
`render_toc`/`top_tier_map` stay pure content formatters (see
[`reference/hooks.md`](../reference/hooks.md#sidegraph-session-start)). The instruction now
covers every search surface, not just `Read`/`Grep`: *"When you need to find or understand
code in this project, call `get_task_context(seeds)` before any grep or file search —
decisions, gotchas and a domain map are indexed here."* Both degrade gracefully — an empty
store still gets the instruction plus just the header. The `SessionStart` hook itself never
blocks startup: any failure, or a cache it can't parse, falls back to `top_tier_map` rather
than crashing; any failure in *that* prints `{}` and exits 0.

### When the TOC goes live

`build_toc(store, reader=None)` precomputes the cache from store content alone (domains,
initiatives, global mistakes — no graph needed) and is written to `store` meta under the
`toc_cache` key (see [configuration](../reference/configuration.md#domain-sync-and-the-toc-cache))
at two points:

1. **Every completed `sidegraph-sync` pass** (fresh or forced) — after the domain-community
   refresh, so the cache reflects that pass's own updates. **Also on a skipped
   (already-up-to-date) pass**, whenever at least one accepted domain exists: a content-only
   change such as `add_decision` bound to an existing domain entity never moves
   `graph_version`, so without this a lazy `sidegraph-sync` right after it would report
   "up to date" and leave the cache stale until an unrelated domain accept/drop happened to
   rebuild it.
2. **Every `ratify` call (MCP tool or `sidegraph-ratify` CLI) that actually accepted or dropped
   at least one domain** — immediately, without waiting for the next sync. This is what makes
   `bootstrap → ratify` visibly turn the TOC on in the very same session, even when no graph
   sync is needed.

A decisions-only ratify (no domain ids in the batch) does not rebuild the cache immediately.
It can change a rendered mistake count; the next completed or skipped sync refreshes that
count. Domain acceptance is the special case refreshed in the ratify operation itself.

## `drill_down(domain_slug)` — the Axis-1 operation

One level deeper than the TOC: `drill_down` returns a domain's full summary, its accepted
subdomains (title + one-line summary), a capped sample (20) of the code/doc entities currently
in it (`communities` ∪ `path_prefixes`, deduplicated), and the decisions bound to it —
mistakes first, same ranking convention as everything else. Works on a not-yet-ratified
(`proposed`) domain too; `status` in the result reflects that.

Unknown `domain_slug` never guesses a match: it returns `{"found": false, "candidates": [...]}`
with up to 10 currently-accepted slugs to retry with. No reader present degrades `members` to
an empty list with an explanatory `"note"` key — everything store-derived (`domain`,
`subdomains`, `decisions`) is unaffected.

## `get_task_context`: seeded, budgeted, mistakes-first

`get_task_context(seeds, store, reader, budget)` is the core UX: given what the agent is
about to touch, return a compact slice of structure *and* memory, memory ranked so the things
most likely to save it from a repeat mistake come first.

### Seeds

A `Seed` is either:
- a **file path** — resolves to every anchorable node in that file, or
- a **name (+ optional file_path)** — an entity ref, resolved the same way capture resolves
  anchors (see [anchoring](anchoring.md#descriptors-name--file)); an ambiguous ref keeps
  *all* candidates rather than guessing or dropping the seed.

Seeds are explicit — there is no semantic/embedding query matching; the caller (typically an
MCP tool call with `files=[...]` and/or `entities=[{"name", "file_path"}, ...]`) names exactly
what it's working on.

### Char-based budgets

`RetrievalBudget` is a hard split, in characters (`len(text)`, ≈4 chars/token — no tokenizer
dependency): `structure_chars` (default 4000) and `memory_chars` (default 6000). The split
exists so decision memory can never crowd out structural context, or vice versa — each half
fills independently and stops at its own cap. (`memory_chars` was raised from an original
2000 — see "Tiered rendering" below for why a bigger memory budget is what makes the tiered
render actually deliver on real ADR-scale content instead of dropping it.)

### Ranking buckets, and why mistakes come first

Decisions are gathered from seeds outward and ranked into ordered buckets. Each source list
(a seed entity, community/domain, peripheral entity, or global scope) is sorted by recency
before it is appended; the implementation does not perform a second global recency sort across
all sources in the same bucket.

| Bucket | Section | Contents |
|---|---|---|
| A | **Known mistakes & gotchas** | `gotcha` / `constraint` / `lesson` decisions bound to a seed entity |
| B | **Decisions** | `adr` decisions bound to a seed entity |
| C | **Related** | decisions on the seed's community *and*, when an accepted domain covers that community, the domain's own decisions too — plus peripheral (subgraph-neighbor) entities |
| D | **Related** | `scope: global` decisions |

### Bucket C: domains and communities

Bucket C does not choose *either* the domain *or* the community — it gathers both, and it
gathers **every** accepted domain that covers the seed community, not just one. For each seed
community, `store.find_domains_by_community(cid)` (plural — the singular
`find_domain_by_community` is only for anchoring's write-time Tier-1 pick) returns every
currently accepted domain claiming it; each one's paired `domain:<slug>` entity's valid
decisions are added *in addition to* whatever is still bound to the bare `community:<id>`
entity. Unioning all covering domains, not just the newest, matters because two accepted
domains can legitimately cover the same community at once (an "orphan window" between one
domain's acceptance and a later re-scope) — a decision tier-1-bound to an older covering domain
must still surface even after a newer domain also claims the community. This is on top of the
existing reason bucket C looks at the community at all: new anchoring prefers the domain once
one exists (see [anchoring](anchoring.md#domain-aware-tier-1)), but older decisions anchored
before any domain was ratified are still sitting on the `community:<id>` binding — dropping that
source would silently make them unretrievable. `add()` dedupes by decision id, so a decision
reachable through more than one of these sources renders once. A store with no accepted domains
covering any seed community sees byte-identical output to before this layer existed.

Bucket D's `scope: global` decisions are not currently settable through the MCP tools —
`add_decision`/`propose_decisions` always write `scope="repo"` (the `Decision` model
default); the global bucket only fills for decisions written with `scope="global"` by other
means (direct store access). See [`mcp-tools.md`](../reference/mcp-tools.md#add_decision).

Bucket A renders first because a gotcha or constraint is a **known trap for exactly this
code** — the whole point of the store is that an agent about to edit something sees "this
broke before, here's why" *before* it sees the architectural rationale or the general
structure. Superseded decisions on a seed entity are appended to Related as one-liners
(`~ tried, reverted 2026-01: ...`) rather than omitted — see [decision memory](decision-memory.md#append-only-as-a-feature).
Everything is deduplicated by decision id and filled greedily into `memory_chars`, buckets in
order (A, B, then C/D) — a decision that would overflow the *remaining* budget is simply not
added; since every bucket shares one running total, buckets A/B are always spent first, so
depth on the task's own seeds wins over breadth of merely-related decisions under pressure,
not the other way around.

### Facts: inline evidence, and the Known-facts bucket

A `Fact` rides the same `memory_chars` budget as decisions, spent strictly after them, in two
forms:

- **Inline evidence.** In buckets B-D, the moment an accepted decision line is placed
  (`rank_decisions.add()`), its live **accepted** supporting facts render immediately as
  adjacent `  evidence: <statement> [<source>]` lines. A live proposed supporting fact is
  quarantined in the final **Unratified proposals** section instead; a superseded fact does
  not render. Bucket A (mistakes) defers accepted evidence to a second pass
  instead of rendering it immediately — see the mistakes-budget guarantee below for why.
  Either way, evidence lines degrade/drop with their decision under budget pressure — a fact
  only ever renders next to a decision that itself made the cut.
- **The Known-facts bucket** (`## Known facts`, rendered right after `## Decisions`, ahead of
  the structural map). Accepted standalone facts — ones not already rendered inline under a
  decision above — bound to a seed or peripheral entity, walked in the same
  seed-then-peripheral order every other bucket uses, sorted by fact id for determinism.
  Proposed facts go to **Unratified proposals**. This bucket is populated only AFTER every
  decision bucket (A-D) and the superseded one-liners have already had first claim on the
  budget.

**The mistakes-budget guarantee: facts can never displace a mistake.** Bucket A is two-phase
specifically to protect this: phase 1 places every mistake decision's line, with NO evidence
interleaved, so an early mistake's evidence can never eat the budget a later mistake's own
decision line needed; only once every mistake line has its final placement does phase 2 walk
them in order and insert each one's evidence directly after it. Buckets B-D keep the ordinary
decision-then-its-evidence-then-next-decision order (the plan-level ruling that inline
evidence must never displace a mistake line only protects bucket A — evidence displacing a
lower-ranked ADR/related/global decision within its own bucket is accepted ranking noise, same
as any other same-bucket ordering effect). And because both the Known-facts bucket and every
inline evidence line are spent from the SAME cumulative counter, strictly after decisions in
program order, a fact can never displace a decision line it wasn't already entitled to run
ahead of by construction — this is what makes "mistakes ranked first" a guarantee, not a
best-effort ordering.

### Tiered rendering: direct entries get depth, related entries stay a pointer

Each rendered line also carries its own, smaller cap — but it is not uniform. A controlled
A/B re-evaluation found that an EARLIER uniform ~240-char clip (applied to every decision
regardless of relevance) made *delivered* completeness measurably worse right after the
import path was fixed to capture full ADR-scale sections (2000-char `context`/`choice`,
populated `consequences`): every decision — including the ones directly on-topic for the
task — was clipped down to its first ~2 table rows, `consequences` was never rendered at
all, and an off-topic decision's `rejected` snippet could repeat verbatim across every
unrelated question. The render is tiered instead:

- **Direct tier** (`TaskContext.mistakes`/`.decisions` — bucket A/B, decisions bound
  directly to a task's own seeds): a generous allowance — `choice` and `context` each
  clipped to ~1200 characters, plus `rejected` and `consequences` (when populated) each
  clipped to ~400 characters, rendered as `"(context: ...)"`/`"(rejected: ...)"`/
  `"(consequences: ...)"` suffixes.
- **Related tier** (`TaskContext.related` — bucket C/D and superseded one-liners): the
  original tight one-liner — `choice` clipped to ~240 characters, title + snippet only, no
  `context`/`rejected`/`consequences` at all, regardless of whether those fields are
  populated. This is also what stops an off-topic decision's `rejected`/`consequences` from
  repeating across unrelated questions — a related entry never renders them.

Every clip cuts at a word boundary with a trailing ellipsis, never mid-word. The per-field
clips are applied *before* the `memory_chars` check, not a bypass of it: a single
generously-rendered direct entry that still doesn't fit the remaining budget is retried once
at the tight tier before being dropped whole (degrade-before-drop — otherwise the
highest-priority entries were the ones an over-tight explicit budget silently dropped first,
inverting mistakes-first) — which is also why `memory_chars`'s default had to grow alongside
the generous tier (a real ADR-scale decision's fully-detailed line can run ~2400-2900
characters on its own).

`rejected`/`consequences` also don't clip blindly top-down. Real ADR `consequences` fields
are shaped `### Positive` then `### Negative`/`### Risks` — a plain top-down clip always
favored Positive (whatever a field happens to lead with) and never showed the accepted
costs. When a Negative/Risks/Trade-offs heading is recognizable, that portion gets first
claim on the field's clip budget and the remainder fills in with what came before it; a
`rejected` field built from multiple bold-pseudo-heading blocks (see
[importing decision-shaped markdown](../reference/cli.md#importing-decision-shaped-markdown---docs))
gets the same treatment so a second rejected alternative isn't silently starved by the
first. `drill_down` (an explicit, unbudgeted "go deeper" call) renders its decisions at the
direct tier too, for the same reason; `top_tier_map`/`build_toc`'s global-mistakes lines
stay at the tight tier deliberately — a TOC is a pointer to the real payload, not the
payload itself.

### The `[drifted]` marker: code changed after this record was captured

A direct-tier decision line (bucket A/B, and every `drill_down` decision line) can carry a
`[drifted]` tag in its tag cluster — `- [gotcha] [drifted] Title …` — meaning: at least one
file this record is anchored to has changed since the record's capture commit (`git diff
<provenance.commit>..HEAD`, the same detection as `sidegraph-doctor`'s `code-drift` check).
The record is still live — a drifted record is often still true — but its premise may have
been overtaken; verify against the current code before relying on it, and
`supersede_decision` it if it no longer holds. The full ULID such an action needs is
already on the line (the `(id: …)` suffix these tiers carry).

Whenever at least one marker renders, one fixed legend line is appended to the context
(and `drill_down` returns it as a separate `"legend"` key) explaining exactly that. The
related tight tier never carries the marker — no id, no supersede consumer there. Marker
freshness rides a store-meta cache refreshed by the SessionStart/Stop hooks
(`sync.refresh_code_drift_cache`, one bounded git scan); records superseded mid-session are
filtered out at read time, so a marker never outlives its record's liveness. Only
commit-stamped records (captured since provenance commit-stamping shipped) can ever be
flagged — imported/pre-wave records are invisible to this check, and `sidegraph-doctor`
counts them as "not checkable".

### Budget fallback: summaries instead of leaves

The structural-map half of `get_task_context` has its own budget (`structure_chars`) and its
own overflow behavior. Before this layer, the map simply stopped emitting leaf lines the
instant one didn't fit — the rest of the subgraph was silently dropped, a hard truncation. Now,
whatever leaves got dropped that way (`overflow_nodes`) get one more chance: their communities
are mapped to accepted domains (`store.find_domain_by_community`), and up to 5 one-line domain
summaries (`- [domain] <title>: <summary>`) are rendered into whatever budget is left over,
instead of wasting it on nothing. This is the "summaries instead of leaves" behavior — when the
budget is too tight to enumerate every file, the agent still gets *some* orientation, at the
domain level rather than none at all. A second trigger, walk saturation, cuts the BFS *walk*
itself before any line ever overflows — there `overflow_nodes` is empty, so the annotated areas
come instead from the task's own seed communities (the neighborhood's own named areas), not
literally the nodes that got cut, since the walk never got far enough to know what was
truncated. No accepted domain covers any candidate community (every store without ratified
domains, or a cut that happens to land outside a named area) → the fallback contributes nothing,
and the old truncation behavior is unchanged, byte-for-byte.

### Thin tools: the two halves, separately

`query_structure(seeds, budget_chars)` and `query_decisions(seeds, budget_chars)` are
`get_task_context` split down the middle — the structural-map half and the decision-memory half
(mistakes, decisions, facts, related), exposed as their own MCP tools for a cheap follow-up once the
caller already has one half and just needs the other. Both reuse the exact same seed-resolution
and ranking code as `get_task_context` — they're thinner call surfaces, not a different
algorithm. `query_structure` needs a reader (no graph, no structural map — it says so rather
than crashing); `query_decisions` degrades like `get_task_context` always has: named-seed
resolution needs a reader, but `scope: global` decisions still surface without one.

### Community fallback

`valid_decisions_for_entity` only returns decisions reachable through a `live` or `degraded`
binding — `orphaned` bindings are skipped, because that anchor no longer means anything.
When a decision's Tier-2 leaf has orphaned (a rename, a deleted symbol) but its Tier-1
community binding is still live, retrieval still surfaces it — under Related, keyed off the
seed's *current* community (looked up read-only via `find_abstract_entity`, never created at
read time). This is the payoff of [multi-anchoring](anchoring.md#multi-anchor-at-capture):
a decision doesn't vanish just because its most precise anchor did.

### Lazy sync on read

Before rendering, every retrieval-facing MCP tool — `get_task_context`, `query_structure`,
`query_decisions`, and `drill_down` — plus the `SessionStart` hook calls `maybe_sync(store,
reader)` — best-effort, wrapped so a sync failure degrades to un-synced retrieval rather than
erroring. `maybe_sync` is itself cheap in the common case: it compares the graph's current
version against the store's `last_synced_graph_version` meta stamp and no-ops if they match.
This is the self-healing mechanism described in
[anchoring](anchoring.md#what-happens-on-rename-or-move): a missed post-commit hook is simply
caught by the next read, and it's what keeps each accepted domain's `communities` field current
too (see [mind model](mind-model.md#how-domains-relate-to-engine-communities)).

### Rendering

`TaskContext.render()` emits, in order: **Known mistakes & gotchas**, **Decisions** (with any
accepted inline `evidence:` lines nested under the decision they support), **Known facts**
(accepted standalone facts — see above), **Structural map** (the budgeted subgraph around the
seeds, rendered as pointers — `- name (file_type) [file_path:line]`, never inlined code),
**Related**, then **Unratified proposals**.
Missing sections are omitted; an empty result renders `"No context found."`.

## See also

- [Mind model](mind-model.md) — why the TOC/drill_down altitudes exist and what a domain is.
- [Decision memory](decision-memory.md) — why mistakes-first retrieval is the point.
- [Anchoring](anchoring.md) — how entities, tiers, and binding status get there in the first place.
- [Store format](../reference/store-format.md) — the underlying read methods' contract.
