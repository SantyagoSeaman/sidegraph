# Anchoring

Anchoring is how a `Decision` attaches to the code or docs it's actually about, so retrieval
can surface it when an agent's task touches that entity. Implemented in
[`src/sidegraph/anchoring.py`](../../src/sidegraph/anchoring.py) (capture-time resolution) and
[`src/sidegraph/sync.py`](../../src/sidegraph/sync.py) (rebind after the graph changes).

## Descriptors: name + file

An anchor reference is a `Descriptor`: a `name` (symbol name or, for docs, a heading text or
the file name itself) and an optional `file_path`. This is deliberately minimal — current
Graphify releases (verified through 0.9.8) emit no signatures, qualified names, or
enclosing-module info, so `name` + `file_path` is all identity has to work with.
`canonicalize()` normalizes both sides of a match
(lowercase, strip a leading `.`, drop call-decoration like `()`), so `.foo()` in a decision's
anchor matches the graph node named `foo`.

## Lazy `Entity` creation

An `Entity` is created the first time a decision references it — via
`store.find_entity(name, file_path)` (dedup lookup) falling back to a fresh `Entity` — never
ahead of time. This keeps the store proportional to what's actually been decided about,
not a mirror of the whole graph. Abstract entities (communities, initiatives) are
get-or-created the same way, keyed by `canonical_name` (e.g. `community:19`,
`initiative:feature-aaa`) and reused across decisions. A `community:*` abstract entity never
gets a canonical file, though: Leiden renumbers communities on every rebuild, so community
identity is index-only, derived state — a snapshot label, not a durable one (see [store
format](../reference/store-format.md#community-bindings-are-derived-not-committed)).
`initiative:*`/`domain:*`/`tag:*` abstract entities are unaffected and get a committed file
the same way any other entity does.

## Multi-anchor at capture

`resolve_and_bind(record_id, ref, reader, store, initiative=None, relation=None)` resolves one
`Descriptor` against the current graph and can create up to three bindings for it in one call:

- **Tier-2 (leaf)** — the concrete entity itself, when the resolve either succeeded or failed
  outright (not when ambiguous).
- **Tier-1 (group)** — the entity's Leiden community, whenever one is known — see
  [domain-aware Tier-1](#domain-aware-tier-1) below for what actually gets bound once a domain
  has claimed that community.
- **Tier-0 (initiative)** — only when the decision names an owning initiative; always `live`.

`relation` (optional; `creates` \| `modifies` \| `affects` \| `deprecates` \| `considered`,
default `"affects"`) overrides the default on the Tier-2 and Tier-1 bindings created for *this*
anchor — it never applies to the Tier-0 initiative binding, which is decision-level rather than
per-anchor. See [data model](data-model.md#anchorbinding--tiered-link).

A decision typically ends up multi-anchored: a leaf plus its community (or domain), optionally
plus an initiative — so a single rebuild that only shifts the leaf still leaves the group anchor
live.

## Domain-aware Tier-1

Once a [`Domain`](../concepts/mind-model.md) has been ratified and covers an anchor's current
community, the Tier-1 binding goes to the domain's paired `domain:<slug>` entity **instead of**
the bare `community:<id>` entity — `store.find_domain_by_community(result.community)` is
checked first, and only falls through to the legacy `community:<id>` entity when no accepted
domain claims that community. A domain-covered Tier-1 binding is always `status="live"`: once a
domain has claimed the community, Tier-1 confidence comes from the domain's curation, not from
whether this particular leaf resolved cleanly (an ambiguous-but-shared-community leaf still
gets a `live` domain binding, where the legacy path would have marked it `degraded`).

A store without any ratified domains behaves *exactly* as before this existed — the
domain check is a no-op fallthrough until at least one domain is accepted and covers the
community in question. New memory automatically lands on durable, named abstractions once
domains are named; decisions anchored before that point keep their `community:<id>` binding
(not migrated), and retrieval still finds them via the [community
fallback](retrieval.md#bucket-c-domains-and-communities).

The two Tier-1 outcomes differ in durability, not just naming: a domain-covered binding points
at the domain's own `domain:<slug>` entity, which is a committed, human-named identity; the
bare `community:<id>` fallback points at a `community:*` entity that is index-only/derived
(snapshot labels, not identities — see [store
format](../reference/store-format.md#community-bindings-are-derived-not-committed)) and is
silently re-pointed, never written to git, whenever Leiden renumbers.

## Tier semantics

| Tier | Meaning | Created by |
|---|---|---|
| 0 | Semantic/abstract — an owning initiative, or a cross-cutting tag | Naming `initiative=` at capture, or a non-empty slug in `tags=[...]` |
| 1 | Group — the entity's Leiden community, or (once ratified) the accepted domain covering it | Whenever the resolved (or ambiguous-but-shared) node has a community |
| 2 | Leaf — the concrete code/doc entity itself | Whenever the name+file resolves uniquely, or fails outright |

## Graceful degradation ladder: resolved -> ambiguous -> orphaned

Sidegraph never guesses. `reader.resolve()` returns exactly one of three statuses, and each
drives different bindings:

| Resolve status | Tier-2 leaf | Tier-1 community | Tier-0 initiative |
|---|---|---|---|
| **resolved** (unique match) | created, `live` | created if the node has a community, `live` | created if named, `live` |
| **ambiguous** (multiple matches) | **none created** | created only if *all* candidates share one community, `degraded` | created if named, `live` |
| **unresolved** (no match) | created, `orphaned` | none (no community to fall back to) | created if named, `live` |

An ambiguous match with candidates spanning different communities gets **no binding at all**
beyond a possible initiative — Sidegraph would rather anchor nothing than anchor to a guess.

## Why multi-anchor matters for doc corpora

On a docs corpus (ADRs/SAD), both a heading and its containing file are anchorable nodes
(`file_type` in `{code, document, concept, rationale}` — an allowlist that also keeps
unanchorable types like `image` out, per the same never-guess principle; `concept` and
`rationale` are the two node types Graphify's semantic pass adds, see
[`guides/semantic-docs.md`](../guides/semantic-docs.md)). A heading rename orphans the
heading anchor — headings have no stable id of their own — but the file usually survives in
place. **Anchoring a doc decision to both the heading and the file** means the file anchor's
surviving community keeps the decision retrievable (via community fallback, see
[retrieval](retrieval.md#community-fallback)) even while the heading anchor is orphaned.
This is a recommended capture pattern for doc decisions, not a schema difference — a heading
node and a file node are both just nodes.

The same pattern applies to `concept` nodes from the semantic pass: a concept has no stable id
either, so **anchor concept + file together**, not the concept alone. Re-extracting an edited
file can rename or drop the concepts inside it — the same churn a code refactor causes to
symbol names — and the rebind ladder below handles it identically: exact match, then unique
name-only match, then orphaned. No special-casing for concepts versus code symbols.

## What happens on rename or move

The sync job (triggered lazily on read — see [retrieval](retrieval.md#lazy-sync-on-read))
re-resolves every concrete entity through a deterministic ladder (`rebind_entity` in
`sync.py`), gated on a graph-version check so it's a cheap no-op when nothing changed:

1. **Exact** — `resolve(name, file_path)` still resolves uniquely -> **rebound** (or
   **unchanged** if the node id didn't move); leaf bindings heal to `live`; the entity's
   community baseline is re-pointed if Leiden renumbered it (index-only — see [Community
   re-pointing](../guides/surviving-refactors.md#community-re-pointing)).
2. **Ambiguous** — leaf bindings flip to `degraded`; the node mapping is left untouched
   (never guess); community is re-pointed if a shared one is still resolvable.
3. **Moved** — exact fails but a name-only retry resolves uniquely, *and* the new file has
   the same suffix as the old `descriptor.file_path` (e.g. `.py` -> `.py`, `.md` -> `.md`),
   *and* the old `file_path` is confirmed gone from the checkout ->
   `descriptor.file_path` is updated to follow the file, leaf bindings heal to `live`.
   **Suffix guard:** a unique name-only hit whose file suffix *differs* from the old one
   (e.g. a vanished code symbol whose name happens to collide with a doc heading) is a
   **collision, not a move** — it is never adopted, and falls through to orphaned instead.
   Cross-type unique hits are not trusted. **Disk guard:** so is a hit whose old file is
   still there — a symbol renamed *inside* a surviving file is not a move, however unique
   the name-only hit elsewhere looks, and "can't verify" counts as "still there" (see [why
   `moved` checks the disk](../guides/surviving-refactors.md#why-moved-checks-the-disk)).
4. **Orphaned** — nothing resolves; leaf bindings flip to `orphaned`. Sync still tries to
   derive the entity's current community from its surviving file's nodes (useful when a
   symbol renamed but its file didn't move); that derivation only happens when the file's
   nodes agree on a single community — otherwise it abstains rather than guess.

All of these are status transitions on existing `AnchorBinding` rows or updates to `Entity`
mapping fields — nothing is ever deleted. A decision whose leaf anchors are *all* orphaned is
flagged "possibly stale" in the sync report, but stays retrievable through any surviving
Tier-1/Tier-0 binding.

## See also

- [Data model](data-model.md) — the `Entity` / `Decision` / `AnchorBinding` / `Domain` fields.
- [Mind model](mind-model.md) — why domains exist and how they relate to communities.
- [Retrieval](retrieval.md) — how tiers and binding status feed the read path.
- [Store format](../reference/store-format.md) — write-path invariants for bindings.
