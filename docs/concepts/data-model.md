# Data model

> **Start here:** Read the opening explanation, then follow only the detail needed for your current task. [Glossary](glossary.md).

The core record shapes are `Entity`, `Decision`, `Fact`, `AnchorBinding` and `Domain`. They are implemented in
[`src/sidegraph/schema.py`](../../src/sidegraph/schema.py), enforced in
[`src/sidegraph/store.py`](../../src/sidegraph/store.py). An `initiative:<label>` is a Tier-0 anchor that capture makes (see [anchoring](anchoring.md), and [Initiatives](#initiatives--a-tier-0-anchor-not-a-record-type)). This page is the
user-facing walk-through; see [decision memory](decision-memory.md) for why these fields
exist at all, and [mind model](mind-model.md) for what `Domain` is *for*.

For ordinary capture, use the MCP write tools rather than authoring these JSON rows by hand. For `add_decision`, supply the title, kind, context and choice, plus rejected alternatives and anchors when they apply. Sidegraph generates the record identity, timestamps, provenance and bindings. An anchor is a name with an optional repository-relative file path.

For example, the real scratch capture supplied:

```json
{
  "title": "Include the project in cache keys",
  "kind": "gotcha",
  "context": "Two projects can use the same resource name.",
  "choice": "Build cache keys from both project and resource name.",
  "rejected": "Using the resource name alone allows cross-project collisions.",
  "anchors": [{"name": "cache_key", "file_path": "cache.py"}, {"name": "cache.py", "file_path": "cache.py"}]
}
```

Use `gotcha` for a concrete trap, `lesson` for learned guidance, `constraint` for a limit that choices must respect, and `adr` for a design choice with its reasons. To reverse an accepted, open decision, supersede it with the replacement and its rationale. Keep the old row as history. For proposals or closed records, use the [status-sensitive recovery](../guides/surviving-refactors.md#reading-and-healing-stale-decisions-warnings).

## `Entity` — durable identity vs. the engine's shifting node ids

Graphify assigns code/doc symbols engine-native node ids that can change after a move or
graph rebuild. `Entity` is Sidegraph's own, stable identity that a
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

These tables describe the in-memory models. The `last_seen_*` fields, binding `status`,
and domain `communities` are derived index state, excluded from canonical JSON. See
[committed vs. derived](../reference/store-format.md#committed-vs-derived) for the on-disk
contract; the examples below illustrate model values rather than exact committed files.

## `Decision` — the memory

| Field | Type | Meaning |
|---|---|---|
| `id` | ULID | Record identity. |
| `title` | str | Short label. |
| `kind` | `adr` \| `lesson` \| `constraint` \| `gotcha` | What kind of memory this is. |
| `status` | `proposed` \| `accepted` \| `superseded` \| `rejected` \| `deprecated` | Lifecycle state. `proposed` = awaiting ratification (by a human, or by an auto-ratification policy where one is configured — `SIDEGRAPH_RATIFY_POLICY`) — it surfaces in retrieval tagged `[unratified]` and ranked last **while it is inside the surfacing window** (default 30 days; `SIDEGRAPH_PROPOSAL_WINDOW_DAYS`), and not at all in regulated mode (`SIDEGRAPH_UNRATIFIED=off`) — see [retrieval-in-sessions](../guides/retrieval-in-sessions.md#why-an-unratified-draft-still-shows-up-and-what-the-tag-means). Withheld from rendering never means removed: the record stays in the store, in the ratify queue and its counter, and is ratifiable at any time. `accepted` = live and untagged; `superseded` = closed by a successor; `rejected` = dropped at ratification; `deprecated` = retired by [`retire_decision`](../reference/mcp-tools.md#retire_decision) (a duplicate, or a decision whose subject is gone), closed with no successor and carrying `retired`. A `deprecated` decision is closed: retrieval, `get_task_context` and the hooks no longer serve it, and history views (`get_entity_history`, `retrieve_decisions(status="deprecated")`) still show it. |
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
| `retired` | `Retirement` \| `null` | Why a retired decision was closed, set only by `retire_decision` together with `accepted -> deprecated`; `null` on every other record. A `Retirement` holds `reason` (str, redacted) and `duplicate_of` (the ULID of the accepted decision that stays, or `null` when the subject is gone). Since 0.24.0; a store holding one needs every client on 0.24.0 or later ([mixed versions](../reference/stability.md#mixed-versions)). |

`Provenance`: `source` (an open `str`, not a fixed enum — for a `Decision` specifically,
shipped writers stamp `human` (`add_decision`/`supersede_decision` — the asking human was the
gate), `agent` (`propose_decisions`, the agent-initiated draft pipeline), `import`
(rationale-node import), or `doc-import` (`--docs` markdown import); `manual` and `bootstrap`
are the corresponding values for `Domain` provenance instead — see
[`Domain`](#domain--the-owned-abstraction) below), `ref`, `author`, `session_id`,
`graph_version`, `commit` — all optional except `source`, which records where the decision came from.
`commit` is a best-effort `git rev-parse HEAD` at write time, stamped on the add, supersede and
propose paths for decisions and facts; it is never backfilled, and `sidegraph-import` records
do not carry it.
`session_id` is best-effort attribution, not identity: it is the calling host's session when the
server could tell, and `null` otherwise (always `null` for CLI writes). A wrong or missing value
is not repaired in place; the store is append-only. See
[session attribution](../reference/hooks.md#session-attribution).

Invariants enforced on write (`store.add_decision`): `valid_to >= valid_from`; nothing is
ever hard-deleted; `retired` only on a `deprecated` decision; and the supersession invariant
below.

#### The supersession invariant

Decisions, facts and domains share live-head and pending-successor rules. Their closure
fields differ:

- **Only the live head can be superseded.** The target must be `accepted`. Decisions and facts must also be
  open (`valid_to` unset). Domains have no `valid_to` field. Superseding a `proposed` record is refused (`<id> is a pending proposal; amend
  it by dropping it and proposing again, or ratify it first`); superseding a closed one
  (`superseded`, `rejected`, `deprecated`, `dropped`) is refused and names its live successor
  when there is one (`<id> is no longer current; supersede its live successor <head id>
  instead`). The one exception: a `dropped` domain may still be superseded (the mass-drop
  recovery in the [naming guide](../guides/naming-your-domains.md#recovering-from-a-mass-drop)).
- **At most one pending successor per record.** While a `proposed` successor exists, any
  further successor (another proposal, or a direct accepted replacement that would strand it)
  is refused (`<id> already has a pending successor <P>; ratify or drop it first`). This
  holds for a `dropped` domain too.
- **Decision and fact closure.** A non-proposed successor closes its predecessor in the
  same write (`valid_to` set, `status` changed to `superseded`, at the successor's
  `valid_from`). This includes a rejected imported successor. A proposed successor leaves
  the accepted predecessor open until ratification accepts the successor. Dropping that
  proposal leaves the predecessor untouched.
- **Domain closure.** A proposed successor of an accepted domain waits for ratification,
  which marks the predecessor `superseded`. A dropped domain used for recovery becomes
  `superseded` in the supersession call, while its successor still awaits review. Neither
  transition writes `valid_to`, because domains have no temporal-validity field.
- **`ratify` re-checks decisions and facts for merges.** Two branches can each add a pending
  successor; after they merge, accepting a successor whose predecessor chain already has a live
  head (found through every branch of superseded records, however long) is refused
  (`<A> is already superseded by <X>; supersede that one instead`), and so is accepting one whose
  predecessor is still `proposed`. Ratifying a decision checks the facts it would accept first,
  before anything is written, and refuses when two of them supersede the same fact: drop one,
  then ratify the decision again.
  Accepting a domain whose slug another, unrelated accepted domain holds is refused too; a
  single pending same-slug revision of its accepted holder is no slug conflict, two revisions
  of one holder are.
- **A domain can be replaced by several live domains (a split).** The write path only
  supersedes a live head, so a split arrives with a merge of two branches that each replaced
  the same domain, or by hand. Both replacements can be accepted, in one `ratify` call or in
  two, and `ratify` says so: the answer for the part that completes the split reads
  `split: <old> is now replaced by 2 live domains: <a> (<id>), <b> (<id>); each keeps its own
  rules`, for every domain, covered by the domain snapshot or not (a covered one gets the
  snapshot's wording instead, which says what to refresh). `sidegraph-doctor` reports it as
  `domain-split`, an advisory that `--check` does not escalate; `sidegraph-verify` has no
  finding for it. A part counts only while its chain reaches an accepted domain, the rule the
  write side uses to find a live head, so a dropped part stops counting. To keep one
  replacement, drop the others with `sidegraph-ratify --drop <id>`.

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
| `status` | `proposed` \| `accepted` \| `superseded` \| `rejected` \| `deprecated` | Reuses `DecisionStatus`; `deprecated` is unused for facts (only a `Decision` is retired, by `retire_decision`). `proposed` surfaces in retrieval tagged `[unratified]` under the same window/regulated-mode limits as a proposed `Decision` (see its `status` row). |
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
  "statement": "HTTPX HTTPTransport retries connection establishment errors, not HTTP 5xx responses.",
  "source": "https://www.python-httpx.org/advanced/transports/",
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
| `relation` | `creates` \| `modifies` \| `affects` \| `deprecates` \| `considered` | Optional, default `"affects"`; additive, only rendered in retrieval output when non-default (the OKF export always shows it). What kind of relationship the decision has to this particular anchor — "decision X *deprecates* entity Y" changes how an agent should treat the entity, unlike the default "affects." Set per-anchor at capture (`anchors=[{"name": ..., "relation": ...}]`); never applies to the Tier-0 initiative binding, which is decision-level rather than per-anchor. |

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
| `slug` | str (kebab-case) | Unique among live (`proposed`/`accepted`) domains; the paired entity's name is `f"domain:{slug}"`. |
| `title` | str | Short label, non-empty. |
| `summary` | str | The WHY-IT-EXISTS prose — 1-3 sentences, **required**, non-empty. |
| `parent_id` | ULID \| `null` | Optional self-reference for a flexible hierarchy; acyclic, enforced on write. |
| `communities` | list[str] | Last-seen engine community ids covering this domain's members. Refreshed by `sidegraph-sync` on every accepted domain (REPLACE semantics — see [mind model](mind-model.md#how-domains-relate-to-engine-communities)), and immediately when a domain is accepted while a graph is readable; never touched by anything else. |
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

Store and record versions identify compatibility contracts. Use supported commands and coordinate client upgrades before raising a store requirement.

See the [field contract](../reference/store-format.md#schema_version--a-public-contract).

## Initiatives — a Tier-0 anchor, not a record type

An initiative groups decisions about a piece of work. It is an abstract anchor label rather than an independent record.

See the [field contract](../reference/store-format.md#initiatives--a-tier-0-anchor-not-a-record-type).

## `DriftCheck` — attributed auxiliary inspection

A drift check records an agent assertion that a decision still holds, the inspected revision and the rationale. It does not identify the inspector or rewrite the decision.

See the [field contract](../reference/store-format.md#driftcheck--attributed-auxiliary-inspection).

## `CheckedCodeFile` — inspected Git tree entry

Each checked file records the committed file identity inspected by a drift check.

See the [field contract](../reference/store-format.md#checkedcodefile--inspected-git-tree-entry).

## `DeclaredSectionFile` — declared sections of one checked file

A section-scoped inspection names the relevant sections of a Markdown file instead of treating every section as equally relevant.

See the [field contract](../reference/store-format.md#declaredsectionfile--declared-sections-of-one-checked-file).

## `DeclaredSection` — one declared or also-watched section

A declared section identifies content the inspection covers or also watches for changes.

See the [field contract](../reference/store-format.md#declaredsection--one-declared-or-also-watched-section).
