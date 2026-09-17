# Data model

Sidegraph's store holds five record types — implemented in
[`src/sidegraph/schema.py`](../../src/sidegraph/schema.py), enforced in
[`src/sidegraph/store.py`](../../src/sidegraph/store.py) — plus a flat `Initiative`
container used as the Tier-0 anchor (see [anchoring](anchoring.md), and its own table at
the end of this page). This page is the
user-facing walk-through; see [decision memory](decision-memory.md) for why these fields
exist at all, and [mind model](mind-model.md) for what `Domain` is *for*.

## `Entity` — durable identity vs. the engine's shifting node ids

Graphify assigns each code/doc symbol a node id derived from its path, and that id shifts
every time the graph is rebuilt. `Entity` is Sidegraph's own, stable identity that a
`Decision` actually anchors to — `entity_id` never changes once minted; `last_seen_node_id`
is just a cached pointer onto the engine's current graph, refreshed on each rebuild by the
[sync job](anchoring.md#what-happens-on-rename-or-move).

| Field | Type | Meaning |
|---|---|---|
| `entity_id` | ULID | Immutable, minted once. The durable identity a `Decision` binds to. |
| `canonical_name` | str | Normalized name (see `canonicalize()` — lowercased, decoration stripped). |
| `kind` | `concrete` \| `abstract` | `concrete` = a real code/doc symbol; `abstract` = a community or initiative anchor. |
| `descriptor` | `{name, file_path?}` \| `null` | What `GraphifyReader.resolve()` needs to find the entity's current node. `null` for abstract entities. |
| `last_seen_node_id` | str \| `null` | Cached engine-native node id, refreshed each rebuild. |
| `last_seen_graph_version` | str \| `null` | The `graph_version` as of that same rebuild. |
| `last_seen_community` | str \| `null` | The Leiden community observed at that rebuild — the baseline sync uses to re-point Tier-1 bindings when Leiden renumbers communities. |

Entities are created **lazily** — only when a decision first references them — so the store
stays bounded to what's actually been decided about, not a mirror of the whole graph.

## `Decision` — the memory

| Field | Type | Meaning |
|---|---|---|
| `id` | ULID | Record identity. |
| `title` | str | Short label. |
| `kind` | `adr` \| `lesson` \| `constraint` \| `gotcha` | What kind of memory this is. |
| `status` | `proposed` \| `accepted` \| `superseded` \| `rejected` \| `deprecated` | Lifecycle state. `proposed` = awaiting ratification (by a human, or by an auto-ratification policy where one is configured — `SIDEGRAPH_RATIFY_POLICY`) — it surfaces in retrieval tagged `[unratified]` and ranked last **while it is inside the surfacing window** (default 30 days; `SIDEGRAPH_PROPOSAL_WINDOW_DAYS`), and not at all in regulated mode (`SIDEGRAPH_UNRATIFIED=off`) — see [retrieval-in-sessions](../guides/retrieval-in-sessions.md#why-an-unratified-draft-still-shows-up-and-what-the-tag-means). Withheld from rendering never means removed: the record stays in the store, in the ratify queue and its counter, and is ratifiable at any time. `accepted` = live and untagged; `superseded` = closed by a successor; `rejected` = dropped at ratification; `deprecated` is declared in the schema for future use. |
| `context` | str | Why this decision was needed. |
| `choice` | str | What was decided. |
| `rejected` | str \| `null` | What was tried/considered and abandoned, and why. |
| `consequences` | str \| `null` | Trade-offs accepted. |
| `valid_from` | datetime | Start of temporal validity. |
| `valid_to` | datetime \| `null` | End of temporal validity; `null` = still open. |
| `supersedes` | ULID \| `null` | The `Decision` this one replaces, if any. |
| `scope` | `repo` \| `module` \| `global` | How broadly this decision applies (`global` decisions surface everywhere, not just on anchored entities). |
| `layer` | `business` \| `technical` \| `null` | Optional filter axis for mixed corpora — "is this a business rule or a technical choice?" Additive, default `null`; set at capture (`add_decision(layer=...)` or a draft's `layer` field). |
| `provenance` | `Provenance` | Always present; see below. |
| `ratified_by` | str \| `null` | Who accepted it, stamped by the ratify transitions (best-effort `git config user.name`) — or `auto:<policy>` (e.g. `auto:auto-low-risk`) when an auto-ratification policy accepted it at write time. `null` = ratified before this field existed, or the identity was unavailable — never a guess. Additive since 2026-08-04; old files load unchanged. |
| `ratified_at` | datetime \| `null` | When it was accepted. With `valid_from` this gives queue latency, which `sidegraph-doctor` reports as `time-to-ratify`. |

`Provenance`: `source` (an open `str`, not a fixed enum — for a `Decision` specifically,
shipped writers stamp `human` (`add_decision`/`supersede_decision` — the asking human was the
gate), `agent` (`propose_decisions`, the agent-initiated draft pipeline), `import`
(rationale-node import), or `doc-import` (`--docs` markdown import); `manual` and `bootstrap`
are the corresponding values for `Domain` provenance instead — see
[`Domain`](#domain--the-owned-abstraction) below), `ref`, `author`, `session_id`,
`graph_version` — all optional except `source`, which records where the decision came from.

Invariants enforced on write (`store.add_decision`): `valid_to >= valid_from`; a decision that
supersedes another closes the predecessor (`valid_to` set, `status` flipped to `superseded`)
in the same write; nothing is ever hard-deleted.

### Example `Decision` (illustrative values)

```json
{
  "id": "01J8Z3QZ8N7K5V2X6R4T9W1B2C",
  "title": "Use ULIDs for record ids",
  "kind": "adr",
  "status": "accepted",
  "context": "Need a record-id scheme that sorts by creation time and needs no central coordination, since records can be written by agents, CI, or a human in any order.",
  "choice": "ULID (python-ulid) for every record id — lexicographically sortable, generated locally, no shared sequence.",
  "rejected": "Auto-incrementing integers — rejected: requires a single writer or a central sequence, which an append-only, git-mergeable store can't guarantee.",
  "consequences": "IDs are longer than integers; not a concern for a store nobody hand-types in bulk.",
  "valid_from": "2026-06-01T10:00:00Z",
  "valid_to": null,
  "supersedes": null,
  "scope": "repo",
  "provenance": {
    "source": "human",
    "ref": null,
    "author": "alex",
    "session_id": "sess-0192",
    "graph_version": "a1b2c3d:9f2e8b1c4d5a"
  }
}
```

## `Fact` — non-derivable knowledge that informed a decision

| Field | Type | Meaning |
|---|---|---|
| `id` | ULID | Record identity. |
| `statement` | str | The fact itself — 1-2 sentences, hard-compact. |
| `source` | str | Required epistemics: how it's known ("benchmark run 2026-07-09", "httpx docs", "trial-learned in session"). |
| `supports` | list[ULID] | `Decision` ids this fact informed. Empty = standalone (must instead carry an anchor to be reachable — see below). |
| `status` | `proposed` \| `accepted` \| `superseded` \| `rejected` \| `deprecated` | Reuses `DecisionStatus`; `deprecated` is unused for facts, same as for `Decision`. `proposed` surfaces in retrieval tagged `[unratified]` under the same window/regulated-mode limits as a proposed `Decision` (see its `status` row). |
| `valid_from` | datetime | Start of temporal validity. |
| `valid_to` | datetime \| `null` | End of temporal validity; `null` = still open. |
| `supersedes` | ULID \| `null` | The `Fact` this one replaces, if any — falsification is supersession, never an edit or a delete. |
| `provenance` | `Provenance` | Always present; `source` is `"human"` for `add_fact`/`supersede_fact` (the asking human was the gate) or `"agent"` for the `propose_decisions` pipeline (attached or standalone drafts). |
| `ratified_by` | str \| `null` | Who accepted it — stamped by the ratify transitions, including the cascade that rides a decision's verdict — or `auto:<policy>` (e.g. `auto:auto-low-risk`) when an auto-ratification policy accepted it at write time. `null` = ratified before this field existed, or identity unavailable; never a guess. |
| `ratified_at` | datetime \| `null` | When it was accepted. |

Deliberately absent, by the same Obvious-to-an-LLM test `Decision`'s fields pass: `kind`,
`scope`, `layer`, `confidence`, `title` — `source` already carries the epistemics, and
`statement` is short enough to be its own title.

Invariants enforced on write (`store.add_fact`): `valid_to >= valid_from`; a `superseded`
fact must have a successor; every id in `supports` must reference an existing `Decision`
(any status — facts inform decisions, the reverse link isn't a thing); `statement`/`source`
non-empty; provenance always present.

### The scope razor

A fact only belongs in the store if the code graph cannot derive it — Graphify can already
answer "what does the code do"; a `Fact` is for what it *can't* answer:

> Only facts the code graph cannot derive belong here: empirics (benchmarks, observed
> behavior), external constraints (API limits, library capabilities), trial-learned
> knowledge — never 'the code does X'.

### Attached vs. standalone

A `Fact` doesn't have to stand alone: it can `supports` one or more decisions (attached —
captured alongside the decision it informed, riding that decision's ratification verdict —
see [retrieval](retrieval.md) and the `sidegraph:ratify-decisions` skill for the cascade), or
carry its own `AnchorBinding`s directly to the entities it touches (standalone — needs at
least one anchor, or a `supports` id, or it's unreachable and the propose pipeline rejects
it). Either way it is its own record — independently falsifiable via `supersede_fact`,
independently anchored, and shareable across more than one decision.

### Example `Fact` (illustrative values)

```json
{
  "id": "01J8Z3R1M2N3P4Q5R6S7T8U9V0",
  "statement": "httpx has no built-in retry — a transient 5xx is not retried automatically.",
  "source": "httpx docs, section 'Timeouts and retries'",
  "supports": ["01J8Z3QZ8N7K5V2X6R4T9W1B2C"],
  "status": "accepted",
  "valid_from": "2026-07-09T14:00:00Z",
  "valid_to": null,
  "supersedes": null,
  "provenance": {
    "source": "human",
    "ref": null,
    "author": "alex",
    "session_id": null,
    "graph_version": "a1b2c3d:9f2e8b1c4d5a"
  }
}
```

## `AnchorBinding` — tiered link

A `Decision`/`Fact` does not embed its anchors; each is a separate `AnchorBinding` row
linking a record to an entity. One record can have several (multi-anchor) — see
[anchoring](anchoring.md) for how these are created and degraded.

| Field | Type | Meaning |
|---|---|---|
| `record_id` | ULID | The record (a `Decision` or a `Fact`) being anchored — renamed from `decision_id` when facts started sharing this same binding machinery; the two share one `(record_id, entity_id)` keyspace, so a decision id and a fact id never collide (ULIDs). |
| `entity_id` | ULID | The `Entity` it's anchored to. |
| `tier` | `0` \| `1` \| `2` | `0` = semantic/abstract (initiative, tag); `1` = group (community, or an accepted domain covering it); `2` = leaf (concrete entity). |
| `weight` | float, 0.0-1.0 | How central this anchor is to the decision (default `1.0`). |
| `status` | `live` \| `degraded` \| `orphaned` | Resolution health. Retrieval skips `orphaned` bindings and resolves the best `live` (or `degraded`) one. |
| `relation` | `creates` \| `modifies` \| `affects` \| `deprecates` \| `considered` | Optional, default `"affects"`; additive, only rendered when non-default. What kind of relationship the decision has to this particular anchor — "decision X *deprecates* entity Y" changes how an agent should treat the entity, unlike the default "affects." Set per-anchor at capture (`anchors=[{"name": ..., "relation": ...}]`); never applies to the Tier-0 initiative binding, which is decision-level rather than per-anchor. |

The store keys `anchor_bindings` on `(record_id, entity_id)` — a record (decision or fact) has
at most one binding per entity; re-anchoring or re-pointing overwrites
tier/status/weight/relation for that pair rather than adding a row. Degradation is always a
status flip, never a delete (see the
[store format reference](../reference/store-format.md)).

## `Domain` — the owned abstraction

`Domain` is a named, described area of the system — the abstraction layer the `SessionStart`
TOC and `drill_down` are built from (see [mind model](mind-model.md) for the full rationale).
Paired with an abstract `Entity` (`canonical_name=f"domain:{slug}"`, minted at acceptance) so
`AnchorBinding` machinery works unchanged: Tier-1 decisions bind to the domain entity exactly
like they'd bind to a `community:<id>` entity.

| Field | Type | Meaning |
|---|---|---|
| `domain_id` | ULID | Immutable, minted once. |
| `slug` | str (kebab-case) | Unique among non-superseded domains; the paired entity's name is `f"domain:{slug}"`. |
| `title` | str | Short label, non-empty. |
| `summary` | str | The WHY-IT-EXISTS prose — 1-3 sentences, **required**, non-empty. |
| `parent_id` | ULID \| `null` | Optional self-reference for a flexible hierarchy; acyclic, enforced on write. |
| `communities` | list[str] | Last-seen engine community ids covering this domain's members. Refreshed by `sidegraph-sync` on every accepted domain (REPLACE semantics — see [mind model](mind-model.md#how-domains-relate-to-engine-communities)); never touched by anything else. |
| `path_prefixes` | list[str] | Optional stabilizer/bootstrap membership rule — repo-relative path prefixes that count as "in this domain." Set at authoring time; **never refreshed by sync** (see [configuration](../reference/configuration.md#domain-sync-and-the-toc-cache)). |
| `status` | `proposed` \| `accepted` \| `superseded` \| `dropped` | Lifecycle state, mirroring `Decision.status` minus `rejected`/`deprecated`. |
| `supersedes` | ULID \| `null` | The `Domain` this one replaces, if any — description edits are a new row, like decisions. |
| `provenance` | `Provenance` | `source` is `bootstrap` \| `agent` \| `manual` depending on which of the [three authoring paths](mind-model.md#domain-lifecycle) produced it. |
| `seed_anchors` | list[`Descriptor`] | Committed authoring intent: "this domain includes the communities these entities currently live in." Unlike `communities` (a volatile last-seen snapshot, derived and never committed), these survive a fresh clone or rebuild because they anchor to durable entities rather than renumberable Leiden ids; `sidegraph-sync` re-resolves each to its current community every pass. |
| `ratified_by` | str \| `null` | Who accepted the domain, stamped at ratification (which also mints its paired `domain:<slug>` entity) — or `auto:<policy>` (e.g. `auto:auto-all` — the only policy that admits domains) when an auto-ratification policy accepted it at write time. `null` for domains accepted before the field existed. |
| `ratified_at` | datetime \| `null` | When it was accepted. |

Invariants enforced on write (`store.add_domain` / `store.supersede_domain`): slug uniqueness
among *live* (`proposed`/`accepted`) domains only — a `superseded` or `dropped` domain frees its
slug back up; `parent_id`, when set, must reference an existing domain and must not create a
cycle; `title`/`summary` non-empty (Pydantic-level).

### Example `Domain` (illustrative values)

```json
{
  "domain_id": "01J9A1B2C3D4E5F6G7H8J9K0L1",
  "slug": "risk-gate",
  "title": "Risk gate",
  "summary": "Pre-trade checks that block an order before it reaches the exchange — fee gates, position limits, kill switches.",
  "parent_id": null,
  "communities": ["19"],
  "path_prefixes": ["risk"],
  "status": "accepted",
  "supersedes": null,
  "provenance": {
    "source": "bootstrap",
    "ref": null,
    "author": "sidegraph-domains",
    "session_id": null,
    "graph_version": "a1b2c3d:9f2e8b1c4d5a"
  }
}
```

### Tags

`tag:<slug>` is not a record type of its own — it's a free-form label slugified (lowercase,
spaces → `-`, `[a-z0-9-]` only) into a durable, get-or-created abstract `Entity`, bound tier-0
via the same `AnchorBinding` machinery as an initiative. Many-to-many: a decision can carry
several tags, and `get_entity_history`/seeded retrieval find it via any of them. No lifecycle,
no ratification, no summary — see [mind model](mind-model.md#tags-vs-domains-vs-initiatives)
for how this differs from a `Domain`.

## `schema_version` — a public contract

`SCHEMA_VERSION` (currently `0.6.0` in `schema.py`) is stamped into the store's `meta` table
once, at creation, and checked for an **exact** match on the fast (digest-matches) freshness
path — a mismatch there normally raises immediately rather than silently reading mismatched
data. The one exception: a store stamped with a version in `_RELOADABLE_SCHEMA_VERSIONS`
(currently `0.4.0` and `0.5.0`, whose canonical files are fully forward-compatible) gets a full
index reload instead, re-stamping `schema_version` in the process. See
[store format](../reference/store-format.md#schema_version-and-exact-match-policy) for the
exact mechanics.

**The one migration path:** a legacy single-file SQLite store — either a bare `decisions.db`
or any store stamped `0.2.0`/`0.3.0` — is still readable. Opening it triggers a one-time,
fail-closed export **on open** (every row Pydantic-validated in full before any byte is
written; a garbled row raises and leaves the legacy file untouched) into the canonical
file-per-record layout, stamped at the running code's current `SCHEMA_VERSION`; the legacy
file is renamed to `<name>.migrated-backup` (never deleted) rather than removed. Both `0.2.0`
and `0.3.0` sources go through this same export, not a stamp-only rewrite — see
[store format: migration to 0.4.0](../reference/store-format.md#migration-to-040-from-02x-and-03x)
for the full four-step sequence. Any other mismatch (a version older than `0.2.0`, a
newer/future version, or an unrecognized string) is a hard rejection: use a fresh store.

## `Initiative` — the Tier-0 grouping container

Not a record type in its own right: a flat label a decision can be bound to at tier 0, so
"everything decided during the retry-hardening work" is retrievable as a set even when
those decisions touch unrelated code. Created implicitly at capture (a draft's
`initiative` field, or the git branch name when one is not given).

| Field | Type | Meaning |
|---|---|---|
| `id` | ULID | Record identity. |
| `name` | str | The label as written at capture — a branch name or a short phrase. |
| `description` | str \| `null` | Optional one-liner; usually absent for branch-derived initiatives. |
| `tags` | list[str] | Free-text tags, slugified into `tag:<slug>` entities like a decision's own. |
