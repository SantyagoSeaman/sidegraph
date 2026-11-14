# Task retrieval contract

> **Start here:** Use the section matching the command or payload you are checking. For everyday work, begin with the [documentation paths](../README.md). [Glossary](../concepts/glossary.md).

## `get_task_context`: seeded, budgeted, mistakes-first

`get_task_context(seeds, store, reader, budget)` is the core UX (the MCP tool of that name
takes the seeds as `files` and `entities`): given what the agent is about to touch, return a compact slice of structure *and* memory, memory ranked so the things
most likely to save it from a repeat mistake come first.

Within one retrieval or file-list call, descriptor resolution shares a lazy, operation-scoped
lookup of parsed entities. It preserves the existing exact-path, pathless adoption, ambiguity,
and winner rules while avoiding a full entity-table parse for each graph node. The lookup is
discarded after the call; a later call sees entity changes. This is a performance-only change:
seed selection, record ranking, budgets, and rendered output are unchanged.

### Seeds

A `Seed` is either:
- a **file path** — resolves to every anchorable node in that file, or
- a **name (+ optional file_path)** — an entity ref, resolved the same way capture resolves
  anchors (see [anchoring](../concepts/anchoring.md#descriptors-name--file)). An ambiguous ref with a file
  keeps *all* its candidates in that file. For the MCP tool `get_task_context`, an ambiguous ref
  with *no* file is reported and not read: expanding it would be a silent guess.
  `query_structure` and `query_decisions` still expand every candidate.

The MCP tool reads a seed that the graph does not hold as written before giving up on it: a
path with a stray `./`, a wrong prefix or a bare file name, a directory, and a `Type.member`
with no file. Every rewrite is a guess and is named as one in the reply, the seeds that resolved
as written come first, and a guess that could mean several things is listed and never read (see
[`get_task_context`](mcp-tools.md#seeds-are-read-tolerantly)). The rule is still
exact matching, never similarity. `query_structure` and `query_decisions` have no such ladder:
they take a seed exactly as written, so a loosely spelled path finds nothing there.

A seed reaches the store's entities two ways, and both count: a named seed that matches a
stored anchor name is that entity directly (whatever the graph calls the node, so a stored
`Type.member` is found), and every node a seed resolves to is mapped to the entity the store
last saw it as (`last_seen_node_id`), with the node's own label still looked up beside that.

Seeds are explicit — there is no semantic/embedding query matching; the caller (typically an
MCP tool call with `files=[...]` and/or `entities=[{"name", "file_path"}, ...]`) names exactly
what it's working on.

### Char-based budgets

`RetrievalBudget` is a hard split, in characters (`len(text)`, ≈4 chars/token — no tokenizer
dependency): `structure_chars` (default 4000) and `memory_chars` (default 6000). The split
sizes the graph walk independently of the memory render. `get_task_context` keeps that walk
for related records but omits the map; `query_structure` renders it under `structure_chars`.
(`memory_chars` was raised from an original 2000 — see "Tiered rendering" below for why a bigger memory budget is what makes the tiered
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

### Named symbols: what you named comes first

Buckets A and B put the records of a named symbol first. A seed entity is a named symbol
when an item of `entities` reached it and that item carries a `file_path`, or its name is
held by exactly one node of the graph. A name with no file that several nodes share names
nothing; its candidates are still read, as before. A direct record (an accepted decision
bound to a seed entity) that is bound to a named symbol is a named record, whatever else it
is bound to.

The placement order is: the mistake lines of the named records, their ADR lines, every other
direct mistake line, the evidence of the mistake lines, the evidence of the named ADRs, then
the other ADRs, each followed by its evidence. The sections do not change: all mistakes are
printed under **Known mistakes & gotchas** and all ADRs under **Decisions**, the named
records first in each. So a named symbol's ADR takes the budget before another symbol's
gotcha, and is printed below it. A call with no named symbol has no named record: every
direct mistake is placed before every direct ADR.

### Bucket C: domains and communities

It also takes every accepted domain that holds one of the seed nodes, so a domain holding
only part of a community still contributes.

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
one exists (see [anchoring](../concepts/anchoring.md#domain-aware-tier-1)), but older decisions anchored
before any domain was ratified are still sitting on the `community:<id>` binding — dropping that
source would silently make them unretrievable. `add()` dedupes by decision id, so a decision
reachable through more than one of these sources renders once. A store with no accepted domains
covering any seed community sees byte-identical output to before this layer existed.

Bucket D's `scope: global` decisions are not currently settable through the MCP tools —
`add_decision`/`propose_decisions` always write `scope="repo"` (the `Decision` model
default); the global bucket only fills for decisions written with `scope="global"` by other
means (direct store access). See [`mcp-tools.md`](mcp-tools.md#add_decision).

Bucket A renders first because a gotcha or constraint is a **known trap for exactly this
code** — the whole point of the store is that an agent about to edit something sees "this
broke before, here's why" *before* it sees the architectural rationale or the general
structure. Superseded decisions on a seed entity are appended to Related as one-liners
(`~ tried, reverted 2026-01: ...`) rather than omitted — see [decision memory](../concepts/decision-memory.md#append-only-as-a-feature).
Everything is deduplicated by decision id and filled greedily into `memory_chars`, buckets in
order (the named records, then A, B, then C/D) — a decision that would overflow the *remaining*
budget is simply not added; since every bucket shares one running total, buckets A/B are always
spent first, so depth on the task's own seeds wins over breadth of merely-related decisions
under pressure, not the other way around.

### Facts: inline evidence, and the Known-facts bucket

A `Fact` rides the same `memory_chars` budget as decisions, spent strictly after them, in two
forms:

- **Inline evidence.** In buckets B-D, the moment an accepted decision line is placed
  (`rank_decisions.add()`), its live **accepted** supporting facts render immediately as
  adjacent `  evidence: <statement> [<source>]` lines. A live proposed supporting fact is
  quarantined in the final **Unratified proposals** section instead; a superseded fact does
  not render. Bucket A (mistakes) defers accepted evidence to a second pass
  instead of rendering it immediately — see the mistakes-budget guarantee below for why.
  A named symbol's ADR is placed early, without its evidence; the evidence is inserted after
  its line once every mistake line and the mistakes' evidence are placed.
  Either way, evidence lines degrade/drop with their decision under budget pressure — a fact
  only ever renders next to a decision that itself made the cut.
- **The Known-facts bucket** (`## Known facts`, rendered right after `## Decisions`, ahead of
  `## Related`). Accepted standalone facts — ones not already rendered inline under a
  decision above — bound to a seed or peripheral entity, walked in the same
  seed-then-peripheral order every other bucket uses, sorted by fact id for determinism.
  Proposed facts go to **Unratified proposals**. This bucket is populated only AFTER every
  decision bucket (A-D) and the superseded one-liners have already had first claim on the
  budget.

Both fact-line forms clip the statement to 240 characters and the source to 300 characters,
at word boundaries where possible, appending `…` to a clipped part. If either part is clipped,
the line carries exactly one ` (clipped; full: list_facts(ids=["<id>"]))` note, naming the
call that returns the whole fact. The longer line spends the same shared character budget.
The same format appears in the SessionStart fallback map of a store with no accepted domain.

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
ahead of by construction — this is what makes the mistakes-budget guarantee hold by
construction, not as a best-effort ordering. It holds for a named symbol's records too: their
lines are placed first, their evidence only after every mistake line.

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
- **Related tier** (`TaskContext.related` — bucket C/D and superseded one-liners): a
  short one-liner with no `context`/`rejected`/`consequences` and no inline evidence,
  regardless of whether those fields are populated. This is also what stops an off-topic
  decision's `rejected`/`consequences` from repeating across unrelated questions — a
  related entry never renders them. Records reached through community, domain, peripheral
  or guessed-seed expansion show at most **5** lines, each with `choice` clipped to ~80
  characters and the record's `(id: …)` so it can be expanded with
  `retrieve_decisions(ids=[…])`; any further ones are counted in one closing
  `- (+N more related not shown: …)` line instead of rendered. Most of these records were
  measured off-task for the task at hand. Two kinds stay outside that cap and keep the
  plain one-liner (`choice` clipped to ~240 characters, no id): `scope: global` decisions,
  which are repo-wide rules, and the seed's own superseded one-liners, which are its history.

Every clip cuts at a word boundary with a trailing ellipsis. The one exception is a single unbroken token longer than the limit (a long identifier or URL), which is cut hard at the limit. The per-field
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
[importing decision-shaped markdown](cli.md#importing-decision-shaped-markdown---docs))
gets the same treatment so a second rejected alternative isn't silently starved by the
first. `drill_down` (an explicit "go deeper" call with no relevance budget; its output is capped by `budget_chars`) renders its decisions at the
direct tier too, for the same reason; `top_tier_map`/`build_toc`'s global-mistakes lines
stay at the tight tier deliberately — a TOC is a pointer to the real payload, not the
payload itself.

### The `[drifted]` marker: code changed after this record was captured

A direct-tier decision line (bucket A/B, and every `drill_down` decision line) can carry a
`[drifted]` tag in its tag cluster — `- [gotcha] [drifted] Title …` — meaning: at least one
file this record is anchored to has changed since the record's capture commit (`git diff
<provenance.commit>..HEAD`, the same detection as `sidegraph-doctor`'s `code-drift` check).
The record can still be relevant, but its premise may have changed. Verify the current
code before relying on it and
follow the [status-sensitive recovery](../guides/surviving-refactors.md#reading-and-healing-stale-decisions-warnings)
if it no longer holds. A proposal is not a supersession target. The full ULID a write needs is
already on the line (the `(id: …)` suffix these tiers carry).

Whenever at least one marker renders, one fixed legend line is appended to the context
(and `drill_down` returns it as a separate `"legend"` key) explaining exactly that. The
related tight tier never carries the marker, even on the capped lines that show an id. Marker
freshness rides a store-meta cache refreshed by the SessionStart/Stop hooks
(`sync.refresh_code_drift_cache`, one bounded git scan); records superseded mid-session are
filtered out at read time, so a marker never outlives its record's liveness. Only
commit-stamped records (captured since provenance commit-stamping shipped) can ever be
flagged — imported/pre-wave records are invisible to this check, and `sidegraph-doctor`
counts them as "not checkable".

### Budget fallback: summaries instead of leaves

The structural map rendered by `query_structure` has its own budget (`structure_chars`) and
its own overflow behavior. `get_task_context` does not render this block or its domain summaries.
Before this layer, the map simply stopped emitting leaf lines the instant one didn't fit — the rest of the subgraph was silently dropped, a hard truncation. Now,
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

### Thin tools: explicit map and memory

`query_structure(seeds, budget_chars)` and `query_decisions(seeds, budget_chars)` are
explicit map and memory reads (mistakes, decisions, facts, related), exposed as MCP tools for
a cheap follow-up. Both reuse the same seed-resolution
and ranking code as `get_task_context`, but not its [tolerant seed reading](#seeds): they match
seeds exactly as written. They're thinner call surfaces, not a different algorithm. `query_structure` needs a reader (no graph, no structural map — it says so rather
than crashing); `query_decisions` degrades like `get_task_context` always has: resolving a seed
through the graph needs a reader. Without one, a seed whose name and file match an anchor
descriptor already stored on a live binding still finds its decisions, and `scope: global`
decisions still surface.

### Community fallback

`valid_decisions_for_entity` only returns decisions reachable through a `live` or `degraded`
binding — `orphaned` bindings are skipped, because that anchor no longer means anything.
When a decision's Tier-2 leaf has orphaned (a rename, a deleted symbol) but its Tier-1
community binding is still live, retrieval still surfaces it — under Related, keyed off the
seed's *current* community (looked up read-only via `find_abstract_entity`, never created at
read time). This is the payoff of [multi-anchoring](../concepts/anchoring.md#multi-anchor-at-capture):
a decision doesn't vanish just because its most precise anchor did.

### Lazy sync on read

Before rendering, every retrieval-facing MCP tool — `get_task_context`, `query_structure`,
`query_decisions`, and `drill_down` — plus the `SessionStart` hook calls `maybe_sync(store,
reader)` — best-effort, wrapped so a sync failure degrades to un-synced retrieval rather than
erroring. A linked worktree that reads the main checkout's graph because it has none of its own
syncs it too, index-only: the derived state fills its cold index, and no tracked file is rewritten
(the moved rung abstains; see
[the Graphify integration](../integrations/graphify.md#linked-worktrees-read-the-main-checkouts-graph)).
`maybe_sync` is itself cheap in the common case: it compares the graph's current
version (plus the reader's resolver revision, so an upgrade that changes how names resolve reruns the pass once) against the store's `last_synced_graph_version` meta stamp and skips the full pass if they match (an entity remembered under
`pending_uncommitted_moves` is still re-verified once `HEAD` has moved).
This is the self-healing mechanism described in
[anchoring](../concepts/anchoring.md#what-happens-on-rename-or-move): a graph that was rebuilt, by the
[graph refresh hook](../integrations/graphify.md#keeping-the-graph-fresh-git-hooks) or by hand,
with no sync after it, is caught by the next read, and it's what keeps each accepted domain's
`communities` field current too (see
[mind model](../concepts/mind-model.md#how-domains-relate-to-engine-communities)). It cannot catch a graph
that was never rebuilt, because the version it compares is the file's own: that case is
reported separately, by comparing the graph's build commit with `HEAD` (see
[the Graphify integration](../integrations/graphify.md#keeping-the-graph-fresh-git-hooks)), and
only a rebuild fixes it.

### File applicability outside the graph

When an available graph does not contain an explicitly requested file, the memory read
also checks exact durable descriptor paths. Eligible decisions and facts can then arrive as
`[file-bound]`: declared applicability to that file, with symbol anchors the graph cannot
verify. This reads no neighboring path as an exact match and changes no anchor or binding.
Ordinary verified seed routes take precedence over the marker. Named-entity requests,
ambiguous/directory guesses and graph-present orphan handling keep their existing rules;
with no graph reader at all, this fallback is unavailable. The file still appears in the
not-in-graph diagnostic, and proposal policy, history, mistakes-first and budgets still apply.

### Rendering

`TaskContext.render()` emits, in order: **Known mistakes & gotchas**, **Decisions** (with any
accepted inline `evidence:` lines nested under the decision they support), **Known facts**
(accepted standalone facts — see above), **Structural map** (the budgeted subgraph around the
seeds, rendered as pointers — `- name (file_type) [file_path:line]`, never inlined code),
**Related**, then **Unratified proposals**.
Missing sections are omitted; an empty result renders `"No context found."`.
`get_task_context` calls `render_parts(include_structure=False)`: it omits the map, including
its domain-summary fallback, while leaving the graph walk and related records unchanged.
After the rendered text, `get_task_context` appends a `## Not in the code graph` block when a
seed path is not a file the graph holds, saying why: a file that exists but is missing from a stale graph
(rebuild it), from a graph with no committed change since its build (the file may be newer than
the build and uncommitted, or sit on a path or have a type the engine skips), a file that exists
only in another checkout of the repository (a sibling linked worktree: the sentence names the
worktree and branch, and says to start the session there to read its graph and records),
a well-formed path that is
not a file in any checkout (check the branch and the spelling), or a path that is not a
normalized repo-relative path (check it). In a linked worktree that borrows the
main checkout's graph, a file that exists only on the branch gets advice to build a graph in
this worktree: rebuilding the main checkout's graph cannot add it.
The rebuild advice for any other file names the main checkout. With no graph
at all and at least one seed, a `## No code graph` block names the path looked at and says to build
it (or, when the graph is there and cannot be read, that it is not readable). A file seed with
no current record of its own, and anchored files near it, gets a `## Nearest anchored` sentence
that names those files. When the answer
holds no decision memory at all, the records of the named files follow under
`## Nearest anchored records (not anchored to your files)`; see
[the nearest anchored records](mcp-tools.md#the-nearest-anchored-records).
When those records follow an empty primary lookup, the leading "No context found." becomes
"No record is anchored to the files you named; the records of the nearest anchored files follow."
If the hard cap removes every nearest record, the old lead and `## Why this is empty` heading
return, without the nearest-records block or its supersede hint. Telemetry and delivery claims
exclude the removed records.
`render()` itself produces none of these blocks.

## SessionStart TOC

Two renderers compete for the same `SessionStart` slot, and which one runs depends entirely on
whether the store has any accepted domains yet:

- **`render_toc(cache)`** — the real, domain-named table of contents (activated the moment the
  first domain is ratified — no sync required, see [below](#when-the-toc-goes-live)): one line
  per accepted domain (title, mistake count,
  subdomain count when non-zero), then global mistakes, then any proposed
  global mistakes under `## Unratified proposals`. This is what "the
  mind model comes alive" means in practice. The per-domain mistake count is what
  [`drill_down`](#drill_downdomain_slug--the-axis-1-operation) would serve for that domain:
  the accepted `gotcha`/`lesson`/`constraint` decisions in the union of those tagged to the
  domain's own `domain:<slug>` entity, those anchored to a code or doc entity that lives in one
  of its communities, and, when a graph reader is on hand, those anchored to a whole document
  it covers. Proposed and superseded records are not counted. A TOC built without a reader
  (the ratify and capture paths) leaves out the document branch until the next sync pass that
  has one.
- **`top_tier_map(store, reader)`** — the legacy, nameless fallback: top communities by member
  count (labeled only by god-node name), global mistakes, then up to 10 other
  accepted decisions under `## Recorded decisions`, up to 10 accepted facts under
  `## Known facts`, and the proposals inside the surfacing window under
  `## Unratified proposals`. The last three are what keep a store that has recorded its first
  decisions and named no domain yet from showing an agent only engine communities. Runs on
  demand (no cache) whenever the store has zero accepted domains — a fresh repo, or one that hasn't
  bootstrapped/ratified any domains yet, sees exactly the same output as before this layer
  existed.

Neither renderer includes the standing search instruction itself — `host.hooks.session_start`
prepends it once, at the hook-assembly level, ahead of whichever renderer's text follows, so
`render_toc`/`top_tier_map` stay pure content formatters (see
[`reference/hooks.md`](hooks.md#sidegraph-session-start)). The instruction now
covers every search surface, not just `Read`/`Grep`: *"When you need to find or understand
code in this project, call `get_task_context(files=[…])` with repo-relative paths before any
grep or file search — decisions, gotchas and a domain map are indexed here. If you know a
symbol's name and not its file, call `get_task_context(entities=[{"name": "…"}])` instead of
searching for the path. Before a non-trivial change, run the sidegraph check-plan skill if it
is available. If the tool is listed only by name, load it first."* A `Sidegraph X.Y.Z` line
follows it, so a session that launched an old install says so in its own context. Both
degrade gracefully — an empty
store still gets the instruction, the version line and the header. After the map the hook
appends the integrity checks' status lines (records awaiting ratification, code drift, a stale
or missing graph, uncommitted store files and the rest), and sends a notice to the person for
the problems that need one; the texts and the order are in the
[hooks reference](hooks.md#sidegraph-session-start) and
[troubleshooting](../guides/troubleshooting.md). The `SessionStart` hook itself never
blocks startup: any failure, or a cache it can't parse, falls back to `top_tier_map` rather
than crashing; any failure in *that* prints `{}` and exits 0.

### When the TOC goes live

`build_toc(store, reader=None)` precomputes the cache from the store (domains,
global mistakes; a graph reader is optional and only adds the document branch of the mistake
count) and is written to `store` meta under the
`toc_cache` key (see [configuration](configuration.md#domain-sync-and-the-toc-cache))
at two points:

1. **Every completed `sidegraph-sync` pass** (fresh or forced) — after the domain-community
   refresh, so the cache reflects that pass's own updates. **Also on a skipped
   (already-up-to-date) pass**, whenever at least one accepted domain exists and the store
   changed since the cache was built: a content-only change such as `add_decision` bound to an
   existing domain entity never moves `graph_version`, so without this a lazy
   `sidegraph-sync` right after it would report "up to date" and leave the cache stale until
   an unrelated domain accept/drop happened to rebuild it. Every retrieval call runs this
   pass, and a build counts every domain's decisions, so the cache records the store's
   canonical digest and an unchanged digest keeps it. A cache written without a graph reader
   carries no digest and is rebuilt once.
2. **Every `ratify` call (MCP tool or `sidegraph-ratify` CLI) that actually accepted or dropped
   at least one domain** — immediately, without waiting for the next sync. This is what makes
   `bootstrap → ratify` visibly turn the TOC on in the very same session, even when no graph
   sync is needed.

A decisions-only ratify (no domain ids in the batch) does not rebuild the cache immediately.
It can change a rendered mistake count; the next completed sync pass, or skipped one that
finds the store changed, refreshes that count, and so does the count after `add_decision`.
Domain acceptance is the special case refreshed in the ratify operation itself.

## `drill_down(domain_slug)` — the Axis-1 operation

One level deeper than the TOC: `drill_down` returns a domain's full summary, its accepted
subdomains (title + one-line summary), a capped sample (20) of the code/doc entities currently
in it (its members in the membership index each sync keeps, deduplicated), and the decisions bound to it —
mistakes first, same ranking convention as everything else. The decisions are cut at 25 (or
`limit`) and at `budget_chars` (default 24,000), and the cut ones come back as `omitted_ids`.
Works on a not-yet-ratified
(`proposed`) domain too; `status` in the result reflects that.

Unknown `domain_slug` never guesses a match: it returns `{"found": false, "candidates": [...]}`
with up to 10 currently-accepted slugs to retry with. No reader present degrades `members` to
an empty list with an explanatory `"note"` key. The other store-derived parts (`domain`,
`subdomains`) are unaffected, and so are the `decisions` found through the domain's own
bindings. A decision reachable only through the whole-document branch of the drill-down
(bound to a document whose heading the domain covers) needs the reader and drops out too.

## What `SessionStart` injects

Every new session, the `sidegraph-session-start` hook runs a lazy sync (best-effort, see
[`guides/surviving-refactors.md`](../guides/surviving-refactors.md)) and injects a compact table of
contents as `additionalContext` — not a full dump of the store. Which of two renderers runs
depends entirely on whether the store has any **accepted domains** yet (see
[mind model](../concepts/mind-model.md) and
[retrieval: SessionStart TOC](../concepts/retrieval.md#sessionstart-toc)).
On Grok Build the hook still runs, but its text is lost; the MCP server supplies the map
instead (the cached domain table of contents, or only the standing lines, never the
`## Communities` fallback below; see the [Grok Build guide](../integrations/grok.md)):

- **Once at least one domain has been ratified** (`sidegraph-domains bootstrap`/`add` →
  `sidegraph-ratify` — see [naming your domains](../guides/naming-your-domains.md)), `render_toc()`
  renders the real, domain-named table of contents: one line per accepted domain (title,
  mistake count, subdomain count when
  non-zero). This is the normal steady-state view for any project whose mind model has names
  in it — most projects, after the first bootstrap/ratify pass.
- **Before any domain is ratified** — a fresh repo, or one that hasn't bootstrapped/ratified
  domains yet — `top_tier_map()` is the fallback: the same surrounding shape, but with a
  nameless `## Communities` section (up to the 8 largest Graphify communities by member
  count, labeled only by their highest-degree "god" node, e.g. `Trader — 42 entities
  (community 3)`; omitted entirely if no graph is present) instead of `## Domains`.

Whichever renderer ran, the `sidegraph-session-start` hook prepends the same **standing
search instruction**, once, ahead of the rest of the text (not inside either renderer — see
[`reference/hooks.md`](hooks.md#sidegraph-session-start)):

> When you need to find or understand code in this project, call get_task_context(files=[…])
> with repo-relative paths before any grep or file search — decisions, gotchas and a domain map
> are indexed here. If you know a symbol's name and not its file, call
> get_task_context(entities=[{"name": "…"}]) instead of searching for the path. Before a
> non-trivial change, run the sidegraph check-plan skill if it is available. If the tool is
> listed only by name, load it first.

Unlike the records the `PreToolUse` hook delivers at the point of reading (see
[Memory without asking](#memory-without-asking) below), this line is unconditional and covers
every search surface behaviorally, not just calls that name a file with records — bash `find`
and MCP structure-query tools included.

The next line names the package the hooks run: `Sidegraph X.Y.Z`. A session that launched an old
install says so in its own context.

Both renderers also inject:

- **Global mistakes & constraints** — up to the 10 most recent `gotcha`/`lesson`/`constraint`
  decisions scoped `global`, newest first.
- **Unratified proposals** — proposed records inside the surfacing window, after everything
  accepted (see [proposal visibility](../guides/retrieval-in-sessions.md#why-an-unratified-draft-still-shows-up-and-what-the-tag-means)). The
  domain map lists proposed global mistakes only. The nameless map lists proposed decisions and
  facts, up to 10 of each.

The nameless map (no domain ratified yet) has two more sections: **Recorded decisions**, up to
the 10 most recent accepted decisions that no section above already shows, and **Known facts**,
up to 10 accepted facts. So the global mistakes are not the only decisions a session sees at
start. Everything task-scoped is deferred to `get_task_context`.

After the map come status lines for problems the integrity checks find. A line appears
while its problem exists. Problems needing a person can also produce a notice. The
[hooks reference](hooks.md#sidegraph-session-start) owns the check order and notice rules.
Use [troubleshooting](../guides/troubleshooting.md) to choose an action.

The line you will meet most is the ratification one. Unless `$SIDEGRAPH_RATIFY_NUDGE == "off"`, the
hook adds it whenever the pending-ratification queue is non-empty (nothing is appended at zero).
Verbatim, with the counts substituted:

> Sidegraph: N record(s) awaiting ratification (X decisions, Y facts, Z domains; oldest D days) — review
> with the ratify MCP tool or sidegraph-ratify.

The `; oldest D days` part is left out when only domains are pending.

The count is proposed decisions, standalone proposed facts (a fact riding a proposed
decision's cascade is covered by it and never double-counted), and proposed domains — see
[`reference/hooks.md`](hooks.md#sidegraph-session-start) and
[`guides/capturing-decisions.md`](../guides/capturing-decisions.md) for what "ratification" means and
the `sidegraph:ratify-decisions` skill (which this line is meant to trigger) for the
in-session walkthrough.

With an empty store and no graph, this degrades to the standing instruction, the version line,
the header and a line saying there is no code graph — that's expected, not a bug (see the
never-crash contract in [`reference/hooks.md`](hooks.md)).

## Memory without asking

The map above is thin on purpose, and an agent does not always call `get_task_context` before it
reads. Three more channels put records in front of an agent, or tell it that records exist, with
no query. All three read the store's index directly, so each costs a few tens of milliseconds.

- **At the point of reading.** The `PreToolUse` hook runs before a `Read`, `Edit`, `Write` or
  `Grep` call, and before a Bash line that runs `sed`, `grep`, `rg` or `cat`. When the call names
  a file that decisions are anchored to, the agent gets that file's top decisions, mistakes first,
  in the same call: two decisions, or three when the first two are mistakes, for at most three
  files per call. Each file is delivered once per agent, and an agent gets ten files in all; the
  count starts again for an agent after its context is compacted or cleared. A call that names no
  file with records gets nothing. `SIDEGRAPH_GREP_NUDGE=off` stops it.
- **In a subagent's brief.** On an `Agent` call (`Task` on older hosts), the hook reads the files
  the brief names, and the files named in a plan or notes document the brief names, and appends
  the decisions anchored to them to the brief: two per file, at most six lines. The subagent finds
  the block at the end of its first message. The parent does not see it, and its transcript keeps
  the prompt it wrote. `SIDEGRAPH_AGENT_BRIEF=off` stops it. In Claude Code's auto mode
  (`permission_mode: auto`) it leaves the call unchanged: that mode reviews a call before hooks run
  and refuses an input a hook changed. The subagent still gets the `SubagentStart` brief and the
  records of the files it reads.
- **At the start of a subagent.** A subagent starts without the session's context. The
  `SubagentStart` hook gives it the standing instruction and one sentence saying how many
  decisions are anchored to code, in how many files, and how many are recorded mistakes. It
  carries no records: the subagent asks `get_task_context` for the files it works on.
  `SIDEGRAPH_SUBAGENT_BRIEF=off` stops it.

The first two channels can deliver eligible facts as well as decisions, within the same record
caps. Facts keep their whole statement and source; fact lines longer than 512 characters are
withheld whole and remain available through `list_facts`. Within one answer or appended brief,
a later identical record may say `See above` with its kind, trust marker and ID under each
applicable file. The first copy remains complete for that surface. No state crosses calls or
agents, and saved characters do not buy extra records. Existing once-per-file claims remain;
use `get_task_context` for fuller decision context and `list_facts` for whole facts.
These two channels run on Claude Code and Grok Build (Sidegraph ships no `PreToolUse` hook for Codex or Muse).
`SubagentStart` delivers its brief on Claude Code, Codex and Muse; Grok Build runs it but does
not pass the output to the model. The wiring is in the [hooks reference](hooks.md#sidegraph-pre-tool-use).

## Per-agent MCP sent state

For Codex requests with an announced thread id, `get_task_context` (including nearest
records), `query_decisions` and filtered `retrieve_decisions` share a sent set in derived
`index.db` meta rows, `mcp_sent:<session>:<agent or ->:<record id>`. Only non-proposed full
record bodies that survive the final output cap qualify: detailed, non-degraded decisions
in the mistakes/decisions buckets, or full listing records. Previously sent bodies become
in-place `[sent earlier]` lines, without accepted inline evidence, or compact
`sent_earlier: true` listing rows; `ids=[...]` always requests full records. The freed budget
can admit further records. Related lines, facts and proposals are never claimed. State is
independent of telemetry; a read or batch-claim failure renders the usual full-body answer
and claims nothing new. Claude, Grok and unknown callers remain inexact and use no sent
state. SessionStart expires rows after 30 days and clears the main agent's rows on
compact/clear, including Codex's canonical rollout and bare-thread aliases, before the
60-second duplicate-event gate. Other sessions and subagents retain their rows.
