# Mind model

Sidegraph's founding bet is that an agent should open a session already carrying a **mental
model** of the project — not re-derive one from a blind grep every time. That model has two
halves that come from different places and age differently:

- **WHAT exists and how it's connected** — the structural half — comes from the engine
  (Graphify's code graph: entities, edges, Leiden communities). It's disposable: the engine
  regenerates it from source on every rebuild, and Sidegraph never writes into it.
- **WHY it's built this way, and what was tried and abandoned** — the decision half — is what
  Sidegraph owns: the append-only `Decision` store, anchored to the structural half via
  `AnchorBinding` (see [anchoring](anchoring.md) and [decision memory](decision-memory.md)).

## The returning-engineer model

When an experienced engineer sits down to work, they do not begin by rediscovering the
repository. They bring a map accumulated across earlier tasks: the system's main areas, the
constraints behind them, the approaches that failed, and the consequences of earlier choices.
The current task brings one part of that map into focus.

Sidegraph carries that operating pattern across otherwise separate agent sessions. The durable
map lives with the project; the context window receives the relevant part. SessionStart gives
the agent its top-level orientation, task retrieval narrows the view, and file-level delivery
brings local decisions into focus. A new session can therefore continue from the project's
accumulated experience without loading its entire history at once.

Through the shipped v1 loop, the decision half was retrievable, but the structural half an
agent walked was still **nameless**: Leiden clusters (`community:19`) and god-node labels, with
no prose explaining what a cluster of files is *for*. The mind-model layer closes that gap:
**`Domain`** is a named, described area of the system — the abstraction layer the agent
answers a top-level question *from*, before it ever reads a leaf file.

## Top-down: TOC → drill_down → leaves

The read path now has three altitudes, and the point is to answer from the highest altitude
that suffices:

1. **`SessionStart` TOC** — the accepted domains, each with a title, a one-line WHY-IT-EXISTS
   summary, a mistake count, and a subdomain count (see
   [retrieval](retrieval.md#sessionstart-toc)). This is what an agent reads before touching
   anything — "what areas does this project have, and what should I already know about each."
   The mistake count is narrower than "mistakes in this area": it counts only decisions
   Tier-1-bound to the domain's own `domain:<slug>` entity (captured, or re-anchored, after
   the domain was ratified) — a gotcha anchored to a leaf entity that happens to live in the
   domain still surfaces in that entity's task context, just not in this count.
2. **`drill_down(domain_slug)`** — walk one domain: its full summary, its accepted subdomains,
   a capped sample of the code/doc entities that currently belong to it, and the decisions
   about it (mistakes first). Unlike the TOC's narrower mistake count, `drill_down`'s
   `decisions` list is the **union** of decisions tagged directly to the `domain:<slug>` entity,
   decisions anchored to any code/doc entity that lives in one of the domain's communities
   (a community-membership join, deduplicated), and decisions anchored to a whole *document*
   entity whose file_path is one of the domain's covered files — so a gotcha anchored to a leaf
   entity in the domain, or an imported ADR that anchored to a doc rather than the domain
   abstraction, *does* surface here even though it isn't counted in the TOC. The document branch
   matters specifically on doc corpora: `sidegraph-import --docs` anchors an ADR to the
   document's own file-level node, but Graphify clusters every doc file-level node into one hub
   community, so that node's community is essentially never among the domain's `communities`
   (those come from the document's *heading* nodes instead) — the community join alone misses
   it, but matching on file_path against the domain's covered headings closes the gap. It's
   scoped to whole-document anchors only, so a code corpus (where one file spans many
   communities) can't have a decision on an unrelated function in that file falsely surface just
   because the file is touched. This is the Axis-1 operation: go one level deeper into a named
   area without falling all the way to `get_task_context`'s leaf-level structural map.
3. **`get_task_context` / `query_structure` / `query_decisions`** — the seeded, budgeted leaf
   read path (unchanged from the shipped v1 loop), for when the agent already knows the
   files/entities it is about to touch.

An agent that only ever reaches for step 3 still gets a working tool — domains are additive,
not a prerequisite. But once domains exist, steps 1–2 let an agent answer "what does this
project look like" *without reading a single file*, which is the actual founding niche: a
queryable mental model loaded before the first grep, not an index that only pays off once you
already know what you're looking for.

## Domain lifecycle

A `Domain` is append-only, exactly like a `Decision`: it goes `proposed` → `accepted` (or
`dropped`), and revising its description is a **new row** with `supersedes` set, never an edit
in place — the slug and its paired abstract entity (`domain:<slug>`, minted at acceptance) stay
stable across a revision, so existing `AnchorBinding`s keep pointing at the same entity.

**The recommended way in: the naming skill.** For a fresh repo (or one with new unclaimed
communities after a big refactor), tell your agent *"name my domains"* — or run
`/sidegraph:name-domains` directly — and it walks you through 2–3 ready-made domain sets, at
different granularities, to pick from in one conversation, instead of hand-curating a raw
bootstrap listing. Naming is a **one-time onboarding** — domains are committed records, their
membership stays fresh automatically via sync, and re-running only ever surfaces genuinely new
areas (see [how often to run this](../guides/naming-your-domains.md#how-often-do-i-run-this-mostly-once)).
See the [naming guide](../guides/naming-your-domains.md) for the full flow;
[`sidegraph:manage-domains`](../../plugin/sidegraph/skills/manage-domains/SKILL.md) is the
companion skill for one-off adds/renames/drops afterward. Mechanically, the naming skill drives
path 1's candidate collection (via the read-only `list_domain_candidates` MCP tool) and path 2's
write below — the three raw authoring paths underneath it are:

**Three authoring paths, one gate:**

1. **Bootstrap** (`sidegraph-domains bootstrap`) — read the graph's own Leiden communities (and
   an optional `.graphify_labels.json` sidecar when a labeling pass has been run) and propose
   one `Domain` draft per community at or above a member-count threshold. Deterministic: a
   labeled community's slug/title/summary come from the label; an unlabeled one falls back to
   its god node's name, or a member digest ("N entities incl. a, b, c — around \<top file\>").
2. **Agent in-session** (`propose_domains` MCP tool) — mirrors `propose_decisions`: when an
   agent keeps navigating to a cluster of code with no name yet, it can propose a domain draft
   the same way it proposes a decision draft, from inside a session.
3. **Manual** (`add_domain` MCP tool / `sidegraph-domains add` CLI) — a human (or an agent
   asked to) names a domain directly, optionally nesting it under a `parent_slug`.

All three land `status=proposed`. **Every path passes through the same ratification gate** —
the unified `ratify` MCP tool (`ratify_decisions` is a deprecated alias, kept for one release)
and the `sidegraph-ratify` CLI, which list and accept/drop decisions *and* domains side by
side. By default no path bypasses human review, including bootstrap: a fresh repo can
propose dozens of domains from its community structure in one command, but nothing is named
until a human selects which of those drafts are worth keeping. The one exception is opt-in:
under `SIDEGRAPH_RATIFY_POLICY=auto-all`, `propose_domains` and `sidegraph-domains bootstrap`
ratify an eligible draft at write time through the same transition, stamped `auto:auto-all`;
manual adds and `supersede_domain` successors always wait for a human.

**Sync keeps the mapping fresh.** After a graph rebuild, `sidegraph-sync` (or the lazy
read-path trigger) recomputes each *accepted* domain's `communities` field — see
[below](#how-domains-relate-to-engine-communities) — so a domain's membership tracks the code
as it moves, without a human re-authoring anything. `path_prefixes` is never touched by sync;
only `communities` is (see [configuration](../reference/configuration.md#domain-sync-and-the-toc-cache)).

**Supersede to evolve a description.** Editing a domain's title/summary, or re-parenting it, is
the `supersede_domain` MCP tool (wrapping `store.supersede_domain(old_id, new_domain)`
directly): the old row flips to `superseded`, a new row (same slug or a new one, `supersedes`
set) replaces it — landing `status=proposed` like every other domain write, so it still needs
a `ratify` to go live. See the
[naming guide](../guides/naming-your-domains.md#recovering-from-a-mass-drop) for the one place
this matters in practice beyond an ordinary rename: freeing a community that a dropped domain
still claims (give the successor no matching `path_prefixes`/`seed_anchors` instead of
carrying the old rule forward).

**Listing.** `list_domains(status=None)` returns every domain in the store — optionally
filtered to one status — with membership/lineage counts (`member_count`, `seed_anchor_count`,
`parent_slug`/`child_slugs`); it's the general "show me all domains" answer `list_proposed`
(proposed-only) and `list_domain_candidates` (unclaimed-only) don't cover.

## How domains relate to engine communities

A `Domain.communities` field is a list of Leiden community ids — a **snapshot mapping**, not
an identity. Leiden renumbers community ids on every rebuild (the dogfood corpus saw
`149 → 19 → 256` across three rebuilds), so nothing durable can be *keyed* on a community id; a
`Domain`'s durable identity is its `slug` and its paired `domain:<slug>` entity. What used to
require re-pointing on **every decision's** Tier-1 binding individually now only needs repair
**once per domain**, at sync time: `sync._recompute_domain_communities` reruns the domain's own
membership rule against the current graph and writes the result back into `communities`.

That refresh uses **REPLACE semantics, not a union**, and it is worth being explicit about why,
so nobody "fixes" it into a union later: when a domain's `path_prefixes` match at least one
anchorable node in the *current* graph, the communities those matches sit in are direct,
current evidence — they fully replace whatever `communities` recorded before, rather than being
added to it. A blind union would let a domain that was once associated with some community keep
silently absorbing that community's decisions forever, even after the graph reorganizes and a
*different, unrelated* domain gets assigned that same now-reused id — Leiden ids are recycled
across rebuilds, so a union has no way to tell "still the same area" from "coincidentally the
same number now." Only when `path_prefixes` matches nothing today does the refresh fall back to
"survivors" — the subset of the previously recorded ids that still exist somewhere in the
current graph — and even then it never adds a *new*, unevidenced id. A domain that recomputes
to no communities *and* whose `path_prefixes` matched nothing is flagged **empty** in the sync
report and routed to a human (re-scope or supersede) rather than silently degrading.

The "never guess" discipline extends to *scope*, not just existence: a `path_prefixes` rule
that would newly claim more than 20% of all current communities is never written either (a
candidate that resolves to a single community is never capped, regardless of ratio — one
community can't swallow anything else) — the overbroad path contribution is dropped and the
domain is flagged **overbroad** in the sync report. If the domain also carries `seed_anchors`,
those are precise per-entity authoring and are always applied, so the domain keeps its
anchor-resolved membership; a domain with only the overbroad path keeps its previous
`communities` mapping unchanged. This applies to every
accepted domain's `path_prefixes` regardless of how it was authored (bootstrap-derived or
hand-written), and bootstrap's own proposal step applies a matching guard before a rule is even
proposed — see the [naming guide's guardrails](../guides/naming-your-domains.md#guardrails-against-an-over-broad-path-rule)
for the full mechanics.

Once a domain is accepted, new anchoring at capture time (`resolve_and_bind`) prefers it over
the bare `community:<id>` entity: if an anchor's current community is covered by an accepted
domain, the Tier-1 binding goes to the domain's `domain:<slug>` entity instead. Decisions bound
to the community before the domain existed are not migrated, but retrieval still surfaces them
under the community fallback — see
[retrieval](retrieval.md#bucket-c-domains-and-communities).

## Tags vs. domains vs. initiatives

Three ways to group decisions across entities exist now, and they answer different questions:

| | Question it answers | Shape | Lifecycle |
|---|---|---|---|
| **Tag** (`tag:<slug>`) | "which decisions carry this cross-cutting label?" | a bare slug, no prose | none — get-or-create, no ratification, no supersession |
| **Domain** | "what is this named area of the system, and what do I need to know about it?" | slug + title + required WHY-IT-EXISTS summary + optional parent/communities/path_prefixes | full record: proposed → accepted, append-only supersession to revise |
| **Initiative** | "which decisions belong to this piece of work?" | a name + optional description | flat container, no ratification gate of its own |

A tag is the cheapest of the three — free-form text slugified into a durable entity at capture
(`add_decision(tags=[...])`, or a draft's `tags` field), many-to-many, tier-0, no lifecycle to
manage. Reach for it when you want to say "these decisions are all about performance" without
describing *why performance work exists here* — that's what a domain's summary is for. A
domain is the only one of the three that is itself ratified content: it has a required summary,
it can be superseded, and it is what the TOC and `drill_down` are built from. An initiative
groups decisions around a unit of *work* (a branch, a project) rather than a unit of the
*system* — it has no summary field and nothing renders a description-first view of it the way
`drill_down` does for a domain.

## See also

- [Data model](data-model.md) — the `Domain` record's fields.
- [Anchoring](anchoring.md) — domain-aware Tier-1 binding.
- [Retrieval](retrieval.md) — the TOC, `drill_down`, and the budget fallback.
- [Naming your domains](../guides/naming-your-domains.md) — the practical bootstrap → ratify
  walkthrough.
