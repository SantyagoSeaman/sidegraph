# MCP tools reference

Twenty-four tools, all defined in [`src/sidegraph/server.py`](../../src/sidegraph/server.py) and
exposed by the `sidegraph-mcp` stdio server (`FastMCP("sidegraph")`). The server holds one
process-wide `Store` (path resolved via `$SIDEGRAPH_DIR`, same precedence the CLI and hooks
use — see [`configuration.md`](configuration.md#store-path-resolution)) and a best-effort
`GraphifyReader` loaded fresh per call from `SIDEGRAPH_GRAPH` — see
[`configuration.md`](configuration.md) for both. See
[mind model](../concepts/mind-model.md) for what `Domain`/`drill_down`/the thin tools are for.

## Summary

| Tool | Purpose | Typically called |
|---|---|---|
| [`add_decision`](#add_decision) | Write one decision directly, `status=accepted` | Mid-session, when a human/agent asks to record something now |
| [`supersede_decision`](#supersede_decision) | Close an old decision, write its replacement | When a past decision is explicitly reversed |
| [`add_fact`](#add_fact) | Write one non-derivable fact directly, `status=accepted` | Mid-session, when a human asks to record a benchmark/limit/trial-learned fact now |
| [`supersede_fact`](#supersede_fact) | Close an old fact, write its falsifying replacement | When a past fact is disproven or corrected |
| [`retrieve_decisions`](#retrieve_decisions) | List all decisions in the store | Ad hoc "what's in memory" queries, audits |
| [`list_facts`](#list_facts) | List all facts in the store, newest first | Ad hoc "what facts do we have" queries, audits |
| [`find_entity`](#find_entity) | Look up an entity_id by name (+ file_path) | Before `get_entity_history`, when you only have a name |
| [`get_entity_history`](#get_entity_history) | List every decision *and* fact anchored to one entity | Following the history of one specific component |
| [`get_task_context`](#get_task_context) | Budgeted, task-scoped merge of memory + structure | Before/while editing specific files or entities (the main read path) |
| [`query_structure`](#query_structure--query_decisions) | The structural-map half of `get_task_context`, alone | A cheap follow-up once you already have decision memory and just need the code map |
| [`query_decisions`](#query_structure--query_decisions) | The decision-memory half of `get_task_context`, alone | A cheap follow-up once you already have the structural map |
| [`drill_down`](#drill_down) | Walk one domain: summary, subdomains, members, decisions | After spotting a domain in the `SessionStart` TOC, to go one level deeper |
| [`list_domain_candidates`](#list_domain_candidates) | Read-only, path-grouped naming candidates over unclaimed communities | The machine half of the `name-domains` skill, before proposing any domain |
| [`list_domains`](#list_domains) | Full listing of every domain, any status, with lineage | "Show me all domains" — the `manage-domains` skill's general listing tool |
| [`propose_decisions`](#propose_decisions) | Deterministic write pipeline for distilled drafts, plus attached/standalone facts | End of session, from the `Stop`-hook nudge |
| [`propose_domains`](#propose_domains) | Deterministic write pipeline for domain drafts | End of session, when the agent recognized an unnamed area |
| [`add_domain`](#add_domain) | Manually author a Domain | Naming an area directly, human- or agent-initiated |
| [`supersede_domain`](#supersede_domain) | Close an old Domain, write its rename/re-scoped replacement | Renaming/re-scoping a domain, or freeing a mass-dropped community |
| [`list_proposed`](#list_proposed) | Human-readable listing of pending decisions, facts, *and* domains | Before ratifying, in a session or in PR review |
| [`ratify`](#ratify) | Accept/drop pending proposals of any kind (decisions, facts, domains) | Ratifying from within a session (CLI equivalent: `sidegraph-ratify`) |
| [`ratify_decisions`](#ratify_decisions-deprecated) | Deprecated alias for `ratify` | Existing callers only — prefer `ratify` |
| [`sync_anchors`](#sync_anchors) | Re-anchor the store against the current graph and report what happened | The diagnostic/heal path, after a `graphify update`; the `heal-anchors` skill's MCP-first path |
| [`verify_store`](#verify_store) | Integrity lint of the store's canonical files | CI or ad hoc — checking the store hasn't been hand-corrupted |
| [`add_anchors`](#add_anchors) | Append bindings to an EXISTING decision or fact | The `heal-anchors` triage flow — "code moved, decision still valid" |

## `add_decision`

```python
add_decision(
    title: str,
    kind: str,                        # "adr" | "lesson" | "constraint" | "gotcha"
    context: str,
    choice: str,
    rejected: str | None = None,
    consequences: str | None = None,
    author: str | None = None,
    session_id: str | None = None,
    anchors: list[dict] | None = None,  # [{"name": str, "file_path": str | None, "relation": str | None}, ...]
    initiative: str | None = None,
    tags: list[str] | str | None = None, # free text; redacted + slugified into tag:<slug> entities
    layer: str | None = None,           # "business" | "technical"
) -> dict
```

Appends a `Decision` with `status="accepted"` and `provenance.source="human"` (graph_version
stamped from the current reader, if any). Every text field (`title`/`context`/`choice`/
`rejected`/`consequences`, and tag text before slugification) is **redacted first** — the
same secret patterns as the propose/import pipelines; the scrubbed text is the only text
that reaches the repo-committed store, and the result's `redactions` counts the
replacements. Pattern coverage is measured, not assumed: against a seeded 14-class corpus
(`tests/test_redaction_seeded.py`) redaction catches 12 classes — private-key PEM blocks,
AWS `AKIA`, GitHub `ghp_`/PATs, Slack `xox`, `Bearer` tokens, `key = value` assignments,
JWTs, credentials embedded in URLs, Google `AIza`, `sk-` style keys, and card numbers
(16-digit candidates verified with Luhn, so invoice numbers and ULIDs are left alone). Two
classes are **deliberately** out of scope and pinned by test: e-mail addresses (PII, not
necessarily secret — blanket redaction would destroy legitimate provenance) and bare hex
tokens (they collide with commit SHAs and digests that records legitimately quote). This is
a defense, not a data-loss-prevention proof; run your own secret scanner over the store path
in CI as defense in depth. An all-secret tag redacts to `[REDACTED]` and is skipped, never minted as a
`tag:redacted` entity — same rule as `propose_decisions`. Each `anchors` entry is resolved against the
current graph and multi-anchored (leaf + domain/community, plus an initiative Tier-0 binding if
`initiative` is given) via `resolve_and_bind` — best-effort: with no graph present the
decision still writes, but the requested code anchors are skipped and both anchor feedback
lists are empty. An anchor's optional `relation` (one of
`creates`/`modifies`/`affects`/`deprecates`/`considered`, default `"affects"`) overrides the
default on that anchor's leaf + Tier-1 bindings only. The anchor list is validated *before
any write happens*: an invalid `relation`, a `name` or `file_path` that is not a string, or
a non-empty list in which no anchor has a `name` fails the whole call atomically rather
than leaving a half-anchored decision behind. In a list with at least one named anchor, an
anchor without a `name` is skipped.

`tags` are free-form labels, slugified (lowercase, spaces→`-`, `[a-z0-9-]` only) into durable
`tag:<slug>` abstract entities (tier-0, many-to-many — a decision can carry several, and
`get_entity_history` finds it via any of them, same as an initiative). A bare comma-separated
string is accepted too (`tags="perf, hot-path"` is equivalent to `tags=["perf", "hot-path"]`)
— agents routinely pass one, so both shapes work. `layer` optionally marks
the decision `"business"` or `"technical"` — a filter axis for mixed corpora. See
[data model](../concepts/data-model.md#tags) and
[mind model](../concepts/mind-model.md#tags-vs-domains-vs-initiatives).

**Returns:** `{"id": str, "status": str, "bindings": int, "entities": list[dict], "anchors_skipped":
list[dict], "anchors_orphaned": list[dict], "redactions": int}` — `status` is always `"accepted"` for this tool; `bindings` is the count of
`AnchorBinding` rows created (leaf/domain-or-community/initiative/tags, all counted together);
`entities` is one `{"entity_id": str, "canonical_name": str, "tier": int}` per binding, so a
caller can chain straight into [`find_entity`](#find_entity) or
[`get_entity_history`](#get_entity_history) without touching the store. `anchors_skipped` is
`[{"name": str, "reason": "ambiguous", "candidates": list[str]}, ...]` — the anchors whose name
matched more than one graph node (`candidates` capped at 5), so no precise Tier-2 leaf was
created for them; empty when every anchor resolved cleanly or no graph reader is present.

**`anchors_orphaned` is the one to act on.** It carries an entity summary plus a `reason`
(`{"entity_id", "canonical_name", "tier": 2, "reason": str}`) for every anchor that resolved
to **nothing**.
The leaf is still written — orphaned, never dropped — but it is dead on arrival: retrieval,
`drill_down` and the PreToolUse nudge all skip orphaned bindings, and no Tier-1 community
fallback is created either, so the record has **no delivery path at all** through that anchor.
`reason` says which fix applies — the causes are not interchangeable:

| `reason` | What happened | Fix |
|---|---|---|
| `file-not-in-graph` | the graph carries no node for that `file_path` at all | usually a **stale graph**: run `graphify update .` and re-anchor. Also a typo'd path, or a file type the engine doesn't index |
| `name-not-in-file` | the graph has that file; it has no such name | fix the **name** — `find_entity`/`query_structure` will say what is really there |
| `no-file-path` | a bare name that resolved to nothing | pass `file_path` |
| `no-graph` | no graph reader present at all (propose path only) | expected in a graph-less run; the anchor heals on the next sync once a graph exists |

Repair an already-written record with [`add_anchors`](#add_anchors) — bindings-only, no
duplicate record and no content-free supersession — rather than leaving memory that can never
be surfaced.

> Before this field existed, that case came back as `bindings: 1`, a normal-looking entity
> summary and an empty `anchors_skipped` — indistinguishable from success. On one measured
> corpus 29% of Tier-2 bindings were orphaned at birth and 11 of 35 decisions were
> unreachable through any live concrete binding, against 0–4% elsewhere. **9 of those 15
> orphans were `file-not-in-graph`** — the graph was a day older than the files the anchors
> named, and the names were fine, which is exactly why `reason` exists.

`Decision.scope` is not a parameter here (or on `propose_decisions`) — every decision written
through the MCP tools lands with the model default, `scope="repo"`. The `global` bucket
[`get_task_context`](#get_task_context) surfaces (and the `SessionStart` map's "global
mistakes") only fills from decisions written with `scope="global"` by other means (direct
store access) — there is currently no tool parameter to set it.

## `supersede_decision`

```python
supersede_decision(
    old_decision_id: str,
    title: str,
    kind: str,
    context: str,
    choice: str,
    rejected: str | None = None,
    consequences: str | None = None,
    anchors: list[dict] | None = None,  # [{"name": str, "file_path": str | None}, ...]
    session_id: str | None = None,
    author: str | None = None,
    source: str = "human",
) -> dict
```

Writes a replacement `Decision` (`status="accepted"`, provenance source defaults to
`"human"`) with
`supersedes=old_decision_id`. Under one serialized store mutation, the successor is written
first and the predecessor is then closed (`valid_to` set, `status` flipped to `superseded`).
The predecessor is never deleted. `session_id`, `author`, and `source` are stamped into the
successor's provenance; use `source="agent"` when an agent initiates the reversal.

Anchoring: pass `anchors` (same shape as `add_decision`'s) to `resolve_and_bind` the
replacement to ONLY those refs, exactly like `add_decision`. Omit `anchors` (the default) and
the replacement instead **inherits the predecessor's bindings verbatim** — same
`entity_id`/`tier`/`status`, copied via `Store.add_binding` — since a reversal concerns the
same entities the original decision did; task-seeded retrieval finds the successor everywhere
it found the predecessor. The two paths are exclusive: passing `anchors` replaces inheritance,
it never adds to it. Explicit `anchors` are validated exactly like `add_decision`'s, before the
successor is written or the predecessor closed.

**Returns:** `{"id": str, "supersedes": str, "bindings": int, "entities": list[dict],
"anchors_skipped": list[dict], "anchors_orphaned": list[dict], "redactions": int}` — same `bindings`/`entities`/
`anchors_skipped`/`anchors_orphaned`/`redactions` shapes as `add_decision`'s, reflecting whichever path
(explicit or inherited) produced the replacement's bindings (`anchors_skipped` is always
`[]` on the inherited path — inheritance never resolves against the graph). Text fields are
redacted exactly like `add_decision`'s.

## `add_fact`

```python
add_fact(
    statement: str,
    source: str,
    supports: list[str] | None = None,   # decision ids this fact informed; each must exist
    anchors: list[dict] | None = None,   # [{"name": str, "file_path": str | None, "relation": str | None}, ...]
    author: str | None = None,
    session_id: str | None = None,
) -> dict
```

Appends a `Fact` with `status="accepted"` and `provenance.source="human"` (graph_version
stamped from the current reader, if any) — the human-asked path, landing immediately with no
ratify hop, the same rationale `add_decision` uses. Only facts the code graph cannot derive
belong here: empirics (benchmarks, observed behavior), external constraints (API limits,
library capabilities), trial-learned knowledge — never 'the code does X'.

`statement` is the fact itself (1-2 sentences, hard-compact); `source` is the epistemics — how
it's known ("benchmark run 2026-07-09", "httpx docs"). `supports` is a list of decision ids
this fact informed; every id must already reference an existing `Decision` or the write raises
`ValueError` before anything is committed. An anchorless fact additionally needs at least one
supported decision that is still live (`proposed` or `accepted`); terminal-only support is
rejected as unreachable. `anchors` is the same `{"name", "file_path", "relation"?}` ref shape
`add_decision` takes, resolved against the current Graphify graph and multi-anchored (leaf +
community, best-effort) when a reader is present. **With no graph present, an anchor still gets
an ORPHANED Tier-2 leaf** — unlike `add_decision`, which silently *drops* anchors when there's
no reader — so a fact never writes unreachable; the binding heals once a graph exists.
`anchors` are validated exactly like `add_decision`'s, before anything is written.

Every text field (`statement`/`source`) is redacted first, same secret patterns as
`add_decision`'s.

**Returns:** `{"id": str, "statement": str, "status": str, "redactions": int, "entities":
list[dict], "anchors_skipped": list[dict], "anchors_orphaned": list[dict]}` — `status` is always `"accepted"` for this tool;
`entities` is one `{"entity_id": str, "canonical_name": str, "tier": int}` per binding
created; `anchors_skipped` is `[{"name": str, "reason": "ambiguous", "candidates":
list[str]}, ...]` (candidates capped at 5) — populated only when a graph is present and an
anchor's name matched more than one node, since there's nothing to be ambiguous against
otherwise.

## `supersede_fact`

```python
supersede_fact(
    old_fact_id: str,
    statement: str,
    source: str,
    supports: list[str] | None = None,   # defaults to the predecessor's own `supports`
    anchors: list[dict] | None = None,
    session_id: str | None = None,
    author: str | None = None,
) -> dict
```

Falsifies a fact: closes the predecessor (`valid_to` set, `status` flipped to `superseded`)
and writes a replacement (`status="accepted"`, `provenance.source="human"`) with
`supersedes=old_fact_id`, under one serialized store mutation with the successor written
first — the predecessor is never deleted,
it stays retrievable as "believed before, corrected because…". Raises `ValueError` if
`old_fact_id` doesn't resolve to an existing fact. Every text field is redacted exactly like
`add_fact`'s. `supports` defaults to the **predecessor's own** `supports` when omitted — a
superseding fact is assumed to inform the same decisions unless told otherwise.

Anchoring mirrors `supersede_decision`'s two exclusive paths: pass `anchors` to resolve and
bind the successor to ONLY those refs (same no-graph orphaned-leaf fallback `add_fact`
gives); omit `anchors` (the default) to **inherit the predecessor's bindings verbatim** —
same `entity_id`/`tier`/`weight`/`relation`/`status`, *including* any `orphaned` ones,
carried as-is. Passing `anchors` replaces inheritance; it never adds to it. Explicit
`anchors` are validated exactly like `add_decision`'s, before the successor is written or
the predecessor closed.

**Returns:** `{"id": str, "statement": str, "status": str, "redactions": int, "entities":
list[dict], "anchors_skipped": list[dict], "anchors_orphaned": list[dict], "supersedes": str}` — same shape as `add_fact`'s
plus `supersedes` (the predecessor's id); `anchors_skipped` is always `[]` on the inherited
path.

## `retrieve_decisions`

```python
retrieve_decisions(include_superseded: bool = False) -> list[dict]
```

Lists every decision in the store. By default excludes `status in {superseded, rejected}`;
pass `include_superseded=True` to see that history too (despite the name, this also brings
back `rejected`/dropped drafts). Results are ordered `gotcha`, `lesson`, then everything else
— **not** by recency; within a rank, order is whatever `iter_decisions()` returns.

**Returns:** a list of full `Decision.model_dump(mode="json")` dicts (all fields, including
`status`, so a `proposed` record is distinguishable from an `accepted` one here even though
the tool doesn't tag it `[unratified]` the way rendered text does).

**Proposal policy applies here too.** Unranked does not mean unfiltered: a `proposed` record
outside the surfacing window (`SIDEGRAPH_PROPOSAL_WINDOW_DAYS`, default 30) or any proposed
record in regulated mode (`SIDEGRAPH_UNRATIFIED=off`) is omitted from this listing, exactly as
it is from the rendered surfaces. Accepted records are never affected. This closes what a
2026-08-04 reviewer correctly identified as a documented bypass of an advertised control — an
agent could otherwise fetch through a raw tool precisely the content regulated mode exists to
withhold.

## `list_facts`

```python
list_facts(include_superseded: bool = False) -> list[dict]
```

The `retrieve_decisions` counterpart for the facts layer — previously a fact was only
reachable indirectly, via `get_entity_history` (which itself silently dropped facts until
the same wave that added this tool closed that gap too — see below). By default excludes
`status in {superseded, rejected}`; pass `include_superseded=True` to see that history too.
Facts carry no `kind`, so there is no mistakes-first ranking analogue — results are ordered
newest first (`valid_from` descending, `id` descending as a deterministic tiebreak).

**Returns:** a list of full `Fact.model_dump(mode="json")` dicts (all fields, including
`status`). The proposal policy applies here as it does to `retrieve_decisions`: a proposed
fact outside the surfacing window, or any proposed fact in regulated mode, is omitted.

## `find_entity`

```python
find_entity(name: str, file_path: str | None = None) -> dict
```

Looks up an `Entity.entity_id` by name — the tool that lets `get_entity_history` be reached
without reading the store directly. Tries an exact descriptor match (canonicalized `name` +
`file_path`) first; if that misses, falls back to a name-only scan across all entities.

- A single match (exact or name-only) is returned as found.
- No match at all: `{"found": false}`.
- Multiple name-only matches (the same name reused across files, with no `file_path` given
  to disambiguate): never guessed at — returned as `candidates` instead. Re-call with
  `file_path` set to one of them to resolve.

**Returns**, on a match: `{"found": true, "entity_id": str, "canonical_name": str,
"descriptor": {"name": str, "file_path": str | None} | None, "last_seen_node_id": str |
None, "bindings": [{"record_id": str, "record_type": "decision" | "fact", "tier": int,
"status": str}, ...]}` — `record_id` is the id of whichever bound *record* the binding
belongs to (a `Decision` or a `Fact`; `AnchorBinding.record_id` was renamed from
`decision_id` when facts started sharing the same anchoring machinery), and `record_type`
tells you which one it resolved to (via a `store.get_fact` probe) so a caller can dispatch
to `get_decision`/`get_fact` without guessing. On ambiguity:
`{"found": false, "candidates": [{"entity_id": str, "canonical_name": str, "file_path": str |
None}, ...]}`. On no match: `{"found": false}`.

## `get_entity_history`

```python
get_entity_history(entity_id: str) -> list[dict]
```

`entity_id` is the durable `Entity.entity_id` (a ULID) — **not** a decision id and not an
entity's display name; get one from `add_decision`/`supersede_decision`'s `entities` field or
from [`find_entity`](#find_entity). Returns every decision **and** fact with any binding to
that entity, newest (`valid_from`) first, regardless of binding status or decision/fact
status.

Per binding, the tool tries a decision lookup first, then a fact lookup (an unknown record
kind stays skipped, same as before). Previously this tool only ever called `get_decision`, so
a fact-only binding vanished from an entity's history with no trace — that gap is closed.

**Returns:** a list of full `Decision`/`Fact` dicts (as `retrieve_decisions`/`list_facts`
above), each with one additive key: `"record_type": "decision" | "fact"`, so a caller can
tell them apart without re-deriving it. Existing consumers keyed on the pre-existing fields
are unaffected.

## `get_task_context`

```python
get_task_context(
    files: list[str] | None = None,          # repo-relative paths
    entities: list[dict] | None = None,       # [{"name": str, "file_path": str | None}, ...]
    structure_budget: int = 4000,
    memory_budget: int = 6000,
    intent: str | None = None,               # optional label for what asked
) -> str
```

The main day-to-day read path. Runs a best-effort lazy `maybe_sync()` first (a sync failure
degrades to un-synced retrieval, never an error), resolves `files`/`entities` into seed nodes,
and returns a rendered Markdown string with up to six sections — accepted mistakes (with
accepted inline evidence facts), accepted decisions (with accepted inline evidence), accepted
known facts (standalone), structural map, related accepted memory, and unratified proposals —
under the given character budgets. See
[retrieval: facts](../concepts/retrieval.md#facts-inline-evidence-and-the-known-facts-bucket)
for the inline-vs-standalone split and the guarantee that facts never displace a mistake. The
structural map omits
pathless engine-artifact nodes (ones with no source file, e.g. `Any (code)`) entirely, since
they're dead weight against a budget meant to orient you within real files. When the structural
map's budget can't fit every leaf, the overflow is replaced with short accepted-domain summary
lines instead of a hard truncation — see
[retrieval: budget fallback](../concepts/retrieval.md#budget-fallback-summaries-instead-of-leaves).
Related decisions on a seed's community also include the decisions of **every** accepted domain
that currently covers that community (unioned, not just the newest one — more than one accepted
domain can cover the same community at once) — see
[retrieval: bucket C](../concepts/retrieval.md#bucket-c-domains-and-communities). See
[`guides/retrieval-in-sessions.md`](../guides/retrieval-in-sessions.md) for how to read the
output, and [`configuration.md`](configuration.md) for what the two budgets mean and whether
they're configurable beyond these call-time defaults.

`intent` is a label for what asked — a skill name, say. It is recorded for local statistics
only (see [`sidegraph-stats`](cli.md#sidegraph-stats)) and never affects what is returned.
The label `drill_down` is reserved for the server's own `drill_down` records and is not
recorded when passed here.

**Returns:** a Markdown string, or the literal `"No context found."` if nothing resolves.

## `query_structure` / `query_decisions`

```python
query_structure(
    files: list[str] | None = None,
    entities: list[dict] | None = None,
    budget_chars: int = 4000,
) -> str

query_decisions(
    files: list[str] | None = None,
    entities: list[dict] | None = None,
    budget_chars: int = 6000,
    intent: str | None = None,
) -> str
```

`get_task_context` split into its two halves, for a cheap follow-up once the caller already has
one half and just needs the other — same `files`/`entities` seed shape, same lazy-sync
behavior, same underlying ranking code (neither duplicates `get_task_context`'s logic).

- `query_structure` renders only the `## Structural map` block (with the same
  summaries-instead-of-leaves overflow fallback). No graph reader present → returns
  `"No structural context found."`/`"No graph reader available; structural map omitted."`
  rather than crashing — structure inherently needs the graph.
- `query_decisions` renders `## ⚠ Known mistakes & gotchas`, `## Decisions` (with inline
  `evidence:` lines), `## Known facts` (standalone facts), and `## Related` (no structural
  map) — internally it still walks the structural subgraph to resolve peripheral entities for
  bucket C, it just never renders the map itself. Degrades exactly like `get_task_context`:
  named-seed resolution needs a reader, but `scope: global` decisions still surface without
  one.

`query_decisions` takes the same optional `intent` as `get_task_context`: a label for what
asked, recorded for local statistics only, never affecting what is returned.

**Returns:** a Markdown string (or the tool-specific "nothing found" message above).

## `drill_down`

```python
drill_down(domain_slug: str) -> dict
```

Walk one [`Domain`](../concepts/mind-model.md): its WHY-IT-EXISTS summary, its accepted
subdomains (title + one-liner), a capped member sample (current `communities` ∪
`path_prefixes`, up to 20, deduplicated), and the decisions about it (mistakes first) — the
Axis-1 counterpart to the flat `SessionStart` TOC. The member sample is **pathful-only and
ranked**: a member with no `source_file` (an engine artifact with nothing to point a human at)
is dropped outright, and survivors are ordered members matching `path_prefixes` first, then each
covered community's god node, then everything else in the reader's own node order — so a big
domain's sample favors its most informative members rather than whatever happens to sort first.

`decisions` is the **union** of (a) decisions tagged directly to the domain (Tier-1-bound to its
`domain:<slug>` entity), (b) decisions anchored to any code/doc entity that lives in one of the
domain's `communities` — a community-membership join computed at call time, deduplicated by
decision id — and (c) decisions anchored to a whole **document** entity whose file_path is
covered by one of the domain's member nodes. This is what makes `drill_down` answer *"the
decisions about the code (or docs) in this area"*: imported (and most) decisions anchor to a
code/doc entity, not the domain abstraction, so without (b) a domain's own decisions never
surfaced under it. The join is bounded to the domain's own communities (a decision in a
different domain's community never leaks in) and applies the same status/validity filters as
the tagged path (superseded/rejected/expired decisions and orphaned bindings are excluded); it
is store-derived, so it needs no graph reader.

Branch (c) exists for **document corpora**: `sidegraph-import --docs` anchors an ADR/spec
decision to the document's *own* file-level node, but Graphify clusters every doc file-level
node into a single hub community — so that entity's community is essentially never among the
domain's `communities`, which come from the document's *heading* nodes (in per-document
communities) instead. Branch (b) alone therefore misses an imported ADR even when the domain
plainly covers that document via its headings. Branch (c) closes the gap by matching on
file_path instead of community: a decision anchored to a whole-document entity surfaces when
that document's file_path is one the domain already covers (i.e. the domain has at least one
member node — typically a heading — drawn from that same file). It is scoped to *whole-document*
anchors only, never a bare "the file is in a domain-covered path": a code entity's node is
never a document node regardless of which file it's in, so on a code corpus (where a single
file can span many communities) a decision anchored to an unrelated function in that file still
cannot surface just because the file is touched. Branch (c) needs a live graph reader (it
computes the domain's covered file_paths and classifies the anchor from current node data); with
no reader it is skipped and (a)+(b) are unaffected.
Runs the same lazy `maybe_sync()` as the other retrieval tools first.

Works on a `proposed` (not yet ratified) domain too — `status` in the result is the resolved
domain's own status (`proposed`/`accepted`/`dropped`; `find_domain_by_slug` never resolves to a
`superseded` row), since a caller may drill into a domain someone just proposed this session.

**Returns**, on a match: `{"found": true, "domain": {"slug": str, "title": str, "summary": str,
"parent_slug": str | None, "status": str}, "subdomains": [{"slug": str, "title": str,
"summary": str}, ...], "members": [str, ...], "decisions": [str, ...]}` — `members` and
`decisions` are pre-rendered lines, mistakes-first for `decisions`. `members` is `[]` with a
`"note"` key when no graph reader is present; everything else is store-derived and unaffected.

**Unknown `domain_slug`:** `{"found": false, "candidates": [str, ...]}` — up to 10
currently-accepted slugs to retry with (never a guess).

## `list_domain_candidates`

```python
list_domain_candidates(
    min_members: int = 5,
    paths: list[str] | None = None,
    limit: int = 100,
) -> dict
```

Read-only projection of the bootstrap candidate machinery — the machine half of the
[`sidegraph:name-domains`](../../plugin/sidegraph/skills/name-domains/SKILL.md) skill (see
[mind model](../concepts/mind-model.md#domain-lifecycle)). **Writes nothing, ever**; safe to
call repeatedly. Built on the same `collect_domain_candidates` selection
`sidegraph-domains bootstrap` writes from, with every one of bootstrap's guards already applied
(label-mismatch rejection, shared-dir/breadth veto on `path_prefixes`, claim skip, within-run
slug dedup, redact-before-slugify) — this tool always shows exactly what the CLI would propose.

Every not-yet-claimed community at or above `min_members` is a naming candidate, pre-grouped by
shared top-level path (a structure hint the agent is free to regroup, merge, or rename).
`min_members`/`paths` mirror `sidegraph-domains bootstrap`'s own knobs; the `name-domains`
skill's default call omits both and lets the agent narrow in conversation instead.

**`limit` defaults to 100** — the top 100 significant communities, in deterministic
community-id order (same order `sidegraph-domains bootstrap --limit` applies its own cap in).
On a monorepo-scale corpus the unbounded list is a ~276K-token dump (a real cross-project
finding: Apache Airflow returns 2,578 candidates at default settings); 100 is the measured
sweet spot (~10K tokens). Pass an explicit `limit=N` to widen it, or `limit=0` for the full,
unbounded list — the **"all" convention**, mirroring `sidegraph-domains bootstrap --limit 0`.
Re-running with the same default/explicit limit only ever proposes the same community-id-sorted
window; communities past it are never reached until you widen `limit` or narrow with
`min_members`/`paths`.

**Returns:** `{"graph_version": str | None, "total_candidates": int, "total_significant": int,
"truncated": bool, "already_claimed": int, "skipped": {"below_threshold": int, "filtered": int},
"groups": [{"path": str, "member_total": int, "candidates": [{"community": str,
"suggested_slug": str, "suggested_title": str, "members": int, "top_members": list[str] (<=3),
"top_file": str | None, "has_label": bool, "anchor": {"name": str, "file_path": str | None} |
None}, ...]}], "ungrouped": [...same candidate shape...]}`. `total_candidates` is how many
candidates THIS response actually includes (post-`limit`, post-`already_claimed`);
`total_significant` is the full significant-community count before `limit` truncated it AND
before the separate `already_claimed` skip — the two can differ even when `truncated` is
`false` (some of what `limit` let through was already claimed), but `truncated` specifically
means "the limit itself cut candidates you never even got to see," and a `"note"` key spells
out the widen/narrow options whenever that's the case. `anchor` is the community's god-node
resolved to a durable
name+file_path Descriptor — feed it back as a `Domain.seed_anchors` entry (via
`propose_domains`/`add_domain`) instead of the volatile `community` id, which does not
survive a fresh clone or graph rebuild. A candidate groups under its own derived
`path_prefixes` when it has one, else under a clear majority top-level directory among its
members — a candidate with neither lands in `ungrouped`, never silently dropped.
`already_claimed` counts communities excluded because a non-superseded domain (or a slug
collision) already claims them — never listed in `groups`/`ungrouped`. With no graph present,
returns the same all-empty shape (`truncated: false`, `total_significant: 0`) plus a `"note"`
key instead of erroring.

## `list_domains`

```python
list_domains(status: str | None = None) -> list[dict]
```

The full-listing counterpart to `list_proposed` (proposed-only) and `list_domain_candidates`
(unclaimed-only) — the tool that actually answers "show me all domains". `status`, when given,
filters to one of `"proposed"`/`"accepted"`/`"dropped"`/`"superseded"`; omitted (the default)
returns every domain regardless of status. Read-only — writes nothing, ever; safe to call
repeatedly.

**Returns:** a list sorted by `slug`, one dict per domain: `{"id": str, "slug": str, "title":
str, "summary": str, "status": str, "member_count": int, "path_prefixes": list[str],
"seed_anchor_count": int, "parent_slug": str | None, "child_slugs": list[str]}`.
`member_count` is `len(domain.communities)` — the current, engine-derived membership size (0
until the next `ratify`/`sidegraph-sync` resolves `path_prefixes`/`seed_anchors`, for a freshly
proposed domain). `parent_slug`/`child_slugs` reflect the FULL domain set regardless of the
`status` filter, so a filtered call still reports accurate lineage.

## `propose_decisions`

```python
propose_decisions(
    drafts: list[dict],
    session_id: str | None = None,
    author: str | None = None,
    facts: list[dict] | None = None,   # STANDALONE fact drafts — see below
) -> list[dict]
```

Each `drafts` entry is a `DraftDecision`: `{"title", "kind": adr|lesson|constraint|gotcha,
"context", "choice", "rejected"?, "consequences"?, "anchors": [{"name", "file_path",
"relation"?}], "initiative"?, "supersedes"?, "tags"? ([str], free text), "layer"?
("business"|"technical"), "facts"? ([DraftFact], attached — see below)}`. Runs the
deterministic pipeline in `capture.propose`: redact secrets (title/context/choice/rejected/
consequences **and** tag text) → dedup (conservative, same kind+canonicalized-title on a
shared anchor entity) → validate → write with `status="proposed"` → anchor best-effort
(per-anchor `relation` carried through) → bind an initiative if named or derivable from the
current git branch → bind tags → write each of the draft's own `facts` (attached — see
below). Per-draft failures don't abort the batch.

**Returns:** a list of `ProposeResult` dicts, one per draft, in the same order:
`{"status": "written" | "deduped" | "rejected", "decision_id": str | None, "reason": str |
None, "redactions": int, "anchors_skipped": list[dict], "anchors_orphaned": list[dict],
"facts": list[dict], "neighbors": list[dict], "ratified_by": str | None,
"auto_ratify_error": str | None}`.
`anchors_skipped` is `[{"name": str, "reason": "ambiguous", "candidates": list[str]}, ...]` —
anchors whose name matched more than one graph node (`candidates` capped at 5), so no precise
Tier-2 leaf was created for them; empty when every anchor resolved cleanly, there were no
anchors, or no graph reader was present. `anchors_orphaned` is `[{"entity_id",
"canonical_name", "tier": 2, "reason": str}, ...]` for anchors that resolved to nothing —
the leaf is still written, but dead on arrival for retrieval. `facts` is one
`ProposeFactResult` dict per entry in that draft's own `facts` list (empty when the draft
had none) — see below for the shape.
`neighbors` lists up to 3 live decisions already reachable via the draft's own anchors, newest
first. `ratified_by` is the `auto:<policy>` stamp when an
[auto-ratification policy](../guides/capturing-decisions.md#5-auto-ratification-policy-opt-in)
accepted the record at write time (`null` otherwise — always under the default `manual`);
`auto_ratify_error` is `null` unless an attempt failed. `status` keeps its write-action
meaning: an auto-ratified draft still reports `"written"`.

**Auto-accept:** when the `SIDEGRAPH_AUTO_ACCEPT` environment variable is `"on"` (read at
point of use in this tool shell; any other value, including unset, is off), every decision
draft, its attached facts, and every standalone fact land `status="accepted"` directly
instead of `"proposed"` — the pending-ratification queue is bypassed for this call.
`provenance.source` still stamps `"agent"` regardless, so history never lies about
authorship, only about whether a human reviewed it. **Domain drafts
(`propose_domains`) are never affected by this flag** — they always land `"proposed"` under
it. The stamped alternative is `SIDEGRAPH_RATIFY_POLICY` ([`configuration.md`](configuration.md));
when both are set, this flag wins. Opt-in, off by
default: this removes the store's only noise filter, so it's recommended for solo use, not
team stores — see
[`guides/capturing-decisions.md#4-auto-accept-opt-in`](../guides/capturing-decisions.md#4-auto-accept-opt-in)
and [`configuration.md`](configuration.md) for the env var itself.

### Facts: attached (`draft.facts`) vs standalone (the top-level `facts` param)

Two distinct ways a fact reaches `propose_decisions`, both running the same deterministic
`capture._propose_fact_one` pipeline (redact statement/source → reachability check → dedup
by canonicalized statement on a shared supported-decision or anchor entity → write
`status="proposed"` → anchor):

- **Attached** — a `DraftFact` inside `draft.facts` (i.e. nested in one of the `drafts`
  entries above), each `{"statement", "source", "anchors"? ([AnchorDraft]), "supports"?
  ([decision id, ...])}`. Runs immediately after that decision writes, inside `_propose_one`:
  `supports` always includes the decision just written (plus the draft's own `supports`),
  and a fact with no `anchors` of its own **inherits the decision's anchors** — but bindings
  are minted on the fact's own id, so it survives the decision independently. Its result
  lands in that draft's own `ProposeResult.facts` list, not the top-level list.
- **Standalone** — the top-level `facts` parameter, run through `capture.propose_facts`
  *after every decision draft has been processed*: `{"statement", "source", "anchors"?
  ([{"name", "file_path", "relation"?}]), "supports"? ([decision id, ...])}`. Neither
  `attached_to` nor inherited anchors apply here — a standalone draft must supply at least
  one `anchors` entry or one `supports` id itself, or it would be unreachable and is
  rejected with a reason (`status="rejected"`, no write). These results are appended to the
  overall returned list **after every decision draft's result**, in `facts` order — never
  interleaved with the decision results.

Either way, each result is a `ProposeFactResult`: `{"status": "written" | "deduped" |
"rejected", "fact_id": str | None, "reason": str | None, "redactions": int,
"anchors_skipped": list[dict], "anchors_orphaned": list[dict], "ratified_by": str | None,
"auto_ratify_error": str | None}` (same `anchors_skipped`/`anchors_orphaned` shape as
`ProposeResult`'s, same `ratified_by`/`auto_ratify_error` meaning). An attached fact accepted
through its decision's cascade carries the decision's own stamp.

## `propose_domains`

```python
propose_domains(
    drafts: list[dict],
    session_id: str | None = None,
    author: str | None = None,
) -> list[dict]
```

Each `drafts` entry is a `DraftDomain`: `{"slug", "title", "summary", "parent_slug"?,
"path_prefixes"?, "seed_anchors"?}`. Mirrors `propose_decisions` for the domain side — the agent
in-session authoring path (path 2 of 3, see
[mind model](../concepts/mind-model.md#domain-lifecycle)). `seed_anchors`
(`[{"name", "file_path"?}, ...]`) is a durable entity-anchor seed (mirrors `add_domain`'s own
`seed_anchors` param) for an agent-curated merge that has no single clean shared path prefix to
rely on — may be given alongside `path_prefixes`, in place of it, or omitted (default `[]`,
inert until a rule is added later). Unlike a raw community-id seed, `seed_anchors` survives a
fresh clone or a graph rebuild: `ratify` (immediately) and every later `sidegraph-sync` pass
resolve each anchor via `reader.resolve(desc).community`, so a domain's membership follows its
anchored entities rather than a Leiden id that gets renumbered on every rebuild. Runs the
deterministic pipeline in `capture.propose_domains`: redact secrets from title/summary → dedup
by slug (skip, never overwrite, when a non-superseded domain — proposed, accepted, *or
dropped* — already claims that slug) → resolve `parent_slug` if given (hard error if it doesn't
resolve to any domain) → write as `status="proposed"`. A human ratifies later via
[`ratify`](#ratify)/`sidegraph-ratify` — unless `SIDEGRAPH_RATIFY_POLICY=auto-all` ratifies an
eligible draft at write time (stamp `auto:auto-all`) and resolves its membership immediately.
When that membership step hits a problem, the domain stays accepted anyway, and
`auto_ratify_error` opens with `activation:` followed by one of two things: the error that
stopped membership from resolving, or a `path rule too broad` notice, which means the
`path_prefixes` claim was rejected and only the `seed_anchors` that resolve, if any, are
still applied. Per-draft failures don't abort the batch.

**Returns:** a list of `ProposeDomainResult` dicts, one per draft, in the same order:
`{"status": "proposed" | "skipped" | "rejected", "domain_id": str | None, "reason": str |
None, "redactions": int, "warnings": list[str], "ratified_by": str | None,
"auto_ratify_error": str | None}`. `status` keeps its write-action value even when
auto-ratified: an eligible domain still reports `"proposed"` here, never `"written"` —
`ratified_by` is what tells the two cases apart.

## `add_domain`

```python
add_domain(
    slug: str,
    title: str,
    summary: str,
    parent_slug: str | None = None,
    path_prefixes: list[str] | None = None,
    communities: list[str] | None = None,
    seed_anchors: list[dict] | None = None,
    author: str | None = "agent",
) -> dict
```

Manually author a `Domain` — the third of the three authoring paths (path 3). Always lands
`status="proposed"`: manual authoring is not an exception to the ratification gate —
`ratify`/`sidegraph-ratify` accepts it like any other draft.

`parent_slug`, when given, must resolve to an existing (non-superseded) domain via
`find_domain_by_slug`; anything else is a hard error (never guess a parent). `path_prefixes` and
`seed_anchors` (`[{"name", "file_path"?}, ...]`, durable entity anchors) seed the membership
rule the domain starts with — **only `communities`** is ever refreshed afterward, by `ratify`
(immediately) and `sidegraph-sync`, from those two rules; `path_prefixes`/`seed_anchors` are the
stabilizer inputs to that refresh and are never themselves rewritten (see
[configuration](configuration.md#domain-sync-and-the-toc-cache)). `communities` remains as a
separate, optional immediate seed for a caller that already knows current (volatile) community
ids and wants them visible before the next resolve pass.

**Returns:** `{"domain_id": str, "status": str}` (`status` is always `"proposed"`).

## `supersede_domain`

```python
supersede_domain(
    old_slug_or_id: str,
    new_slug: str,
    new_title: str,
    new_summary: str,
    path_prefixes: list[str] | None = None,
    seed_anchors: list[dict] | None = None,
    parent_slug: str | None = None,
    author: str | None = "agent",
) -> dict
```

The lineage-correct rename/re-scope path — wraps the existing `Store.supersede_domain`
primitive (previously reachable only via direct `Store` access, not any tool) as the fourth
domain-authoring surface. `old_slug_or_id` resolves either a `domain_id` or a `slug` (tries the
id lookup first, then `find_domain_by_slug`) — never a guess: one that resolves to nothing is a
hard error. `parent_slug`, when given, must resolve like `add_domain`'s. `path_prefixes`/
`seed_anchors` seed the SUCCESSOR's membership rule **from scratch** — nothing is inherited
from the predecessor; pass its values back explicitly to carry them over.

The predecessor is flipped to `superseded` immediately (append-only: the record stays, fully
retrievable, never deleted) in the same transaction that writes the successor. The successor
itself always lands `status="proposed"` — same rule `add_domain` always follows (and
`propose_domains` under the default policy): a human still calls
`ratify(accept=[...])` before the new name/scope is TOC-visible.

Raises (before anything is written) if: `old_slug_or_id` doesn't resolve to any domain;
`parent_slug` is given but doesn't resolve to any domain; or `new_slug` collides with some
OTHER still-live (proposed/accepted) domain (the predecessor itself is excluded from that
check, so the successor may reuse the same slug). This is also the direct fix for [recovering
from a mass-drop](../guides/naming-your-domains.md#recovering-from-a-mass-drop): supersede a
dropped domain with a successor that gets no matching `path_prefixes`/`seed_anchors`, and its
`communities` stays empty — freeing the community for a future `sidegraph-domains bootstrap`
pass to reconsider.

**Returns:** `{"domain_id": str, "status": str, "supersedes": str}` — `status` is always
`"proposed"`, `domain_id` is the successor's, `supersedes` is the predecessor's resolved
`domain_id`.

## `list_proposed`

```python
list_proposed() -> str
```

Renders every `status="proposed"` decision, fact, *and* domain human-readably, sectioned
"Decisions:", then "Facts:", then "Domains:" (each printed only when non-empty):

- **"Decisions:"** — one block per decision via `format_proposal` (id, kind, title,
  what/why, rejected, learned, provenance) **plus one indented `  evidence: <statement>
  [<source>]  (<id>)` line per still-proposed fact that decision's `facts`/`supports`
  attaches** (`store.facts_for_decision`) — a preview of the ratify cascade: accepting or
  dropping this decision carries those facts with it (see [`ratify`](#ratify) below).
- **"Facts:"** — standalone facts only, via `format_fact_proposal` (id, statement, source,
  `supports`, provenance): facts already nested under a decision above are excluded here,
  never double-listed.
- **"Domains:"** — a compact one-liner per domain: id, slug, title, first line of summary,
  **plus the domain's `path_prefixes`/`communities` membership rule** (`paths: ...;
  communities: ...`, each rendered as `(none)` rather than omitted when empty) — so an
  over-broad auto-derived rule is visible at this gate before it's accepted (records an
  auto-ratification policy accepted at write time never appear in this listing).

Returns the literal string `"No proposed decisions, facts, or domains pending
ratification."` if nothing is pending. Same renderer/sectioning the `sidegraph-ratify` CLI
uses with no flags.

**Returns:** a plain-text string.

## `ratify`

```python
ratify(accept: list[str] | None = None, drop: list[str] | None = None) -> dict[str, str]
```

Ratifies pending proposals of **any kind** — decisions, facts, and domains share one gate.
Each id in `accept`/`drop` is routed by lookup, decision first, then fact, then domain (never
guessed). `accept` always requires `proposed`, for any kind: a pending decision or fact flips
`proposed → accepted`; a pending domain flips `proposed → accepted` **and mints its paired
`domain:<slug>` abstract entity**.

`drop` requires `proposed` for a **decision or fact** (`→ rejected`, append-only, `valid_to`
set) but accepts `proposed` **or accepted** for a **domain** (`→ dropped`, no entity minted —
an entity already minted by a prior accept is left as-is): decisions and facts are memory
records with a deliberately narrow lifecycle, but domains are the owned abstraction layer, and
retiring an already-accepted one (e.g. to resolve a [cross-branch slug
conflict](store-format.md#merge-semantics-git-resolves-it-not-the-store) — two branches each
independently accepted a domain with the same slug, which `supersede_domain` cannot resolve on
its own) is a legitimate, append-only-safe operation.

**Cascade (facts ride their decision):** accepting or dropping an id that routes to a
**decision** also flips every still-`proposed` `Fact` whose `supports` names that decision, in
the same store transaction — the same end state as ratifying/dropping the fact directly. Each
cascaded fact gets its **own** entry in the returned dict too, alongside the decision's own:
`f"accepted (evidence of {decision_id})"` on accept, `f"dropped (evidence of {decision_id})"`
on drop — so nothing this call actually touched goes unreported, even though the caller only
named the decision id. Dropping a decision cascades a fact only when **every** decision named
in that fact's `supports` now has status `rejected` (a decision drop's own resulting status —
"dropped" is domain vocabulary only); a fact still supporting a live decision, or with an
empty `supports`, is left alone. A fact given directly in `accept`/`drop` (its own id, not via
a decision) is ratified/dropped the same way any standalone fact is — no cascade to report for
it. Order-independence:
`accept=[decision_id, fact_id]` and `accept=[fact_id, decision_id]` produce identical results
— every decision id in `accept` is processed in a first pass (so its cascade always sweeps a
nested fact before that fact's own turn), then everything else.

An id present in both lists is accepted; the drop result for it reads `"<accept-result> (drop
ignored)"`. An unknown id (not a pending decision, fact, or domain) gets `"error: unknown id
'<id>' (not a pending decision, fact, or domain)"`; any id not in an eligible status for the
requested action gets `"error: <ValueError message>"`. One bad id never aborts the rest of the
batch.

**Side effect:** whenever this call actually accepted or dropped at least one domain, it
immediately rebuilds the `SessionStart` TOC cache (`toc_cache` in store meta) — this is what
makes `bootstrap → ratify` turn the TOC on in the same session, without waiting for the next
`sidegraph-sync` pass. A decisions/facts-only call leaves the cache untouched.

**Returns:** `{id: "accepted" | "dropped" | "error: ..." | "<result> (drop ignored)" |
"accepted (evidence of <decision_id>)" | "dropped (evidence of <decision_id>)"}` — the last
two only ever appear for a fact id that rode a decision's cascade rather than being named
directly.

## `ratify_decisions` (deprecated)

```python
ratify_decisions(
    accept: list[str] | None = None, drop: list[str] | None = None
) -> dict[str, str]
```

**Deprecated alias for [`ratify`](#ratify), kept for one release.** Despite the name, it now
covers facts and domains too — behavior is identical to `ratify`, including the cascade and the
TOC-cache refresh. Prefer `ratify`; this alias exists only so existing callers written against
the old decisions-only name keep working.

## `sync_anchors`

```python
sync_anchors(force: bool = False) -> dict
```

Re-anchors the decision store against the current graph and returns exactly what happened —
the diagnostic/heal MCP counterpart to `sidegraph-sync`, and the MCP-first path the
[`heal-anchors`](../../plugin/sidegraph/skills/heal-anchors/SKILL.md) skill leads with.

**WRITES — this is not read-only.** It runs the same rebind pass `sidegraph-sync`/the lazy
`maybe_sync` (used by every retrieval tool) runs: every tracked entity's Tier-2 leaf binding
transitions (`live`/`degraded`/`orphaned`) per the deterministic resolve ladder, Tier-1
community bindings get re-pointed when Leiden renumbered, and every ACCEPTED domain's
`communities` are refreshed from its `path_prefixes`/`seed_anchors`. An entity's own
canonical *descriptor* is rewritten only on a `"moved"` rung (a unique, same-suffix name-only
match after the exact match missed, with the old `file_path` confirmed gone from disk **and**
that same move independently confirmed by COMMITTED git history at `HEAD` — a dirty-tree
hit that isn't yet committed reports `"moved_uncommitted"` instead and touches nothing; see
`SIDEGRAPH_TRUST_DIRTY_TREE` in [`configuration.md`](configuration.md) for the off-by-default
escape hatch); its node-id mapping (`last_seen_node_id`/`last_seen_community`/
`last_seen_graph_version`) updates on that same `"moved"` rung **and** on an exact-match
`"rebound"` rung (same `name`+`file_path` descriptor match as last sync, but the resolved
node id CHANGED since — see the
[rebind ladder](../guides/surviving-refactors.md#the-rebind-ladder)) — never guessed on
`"ambiguous"` or `"orphaned"`.

Unlike the silent lazy sync every retrieval tool runs (a sync failure there just degrades to
un-synced retrieval — nothing is ever reported), this tool never swallows a sync failure
quietly: call it after a `graphify update` when you want to **see** the rebind ladder's
outcomes.

Gated on `graph_version` vs. the store's last-synced stamp, same as `sidegraph-sync` — but
also reruns on its own, even when the version already matches, the first time it's called
after a canonical reload (`git pull`, merge, branch switch) leaves the store's volatile
state cold; `force=True` still forces an unconditional rerun (e.g. after hand-editing a
domain's `path_prefixes`).

**Returns:**

```python
{
    "synced": bool,                # False when the pass was skipped outright
    "from_version": str | None,
    "to_version": str,
    "counts": str,                 # report.counts() rendered as a string, e.g. "{'unchanged': 3}"; "" if nothing tracked
    "repointed": int,              # community bindings re-pointed, summed across all outcomes
    "outcomes": [{"status": str, "canonical_name": str, "detail": str}, ...],  # non-"unchanged"/"rebound" only
    "stale_decisions": [{"id": str, "title": str}, ...],
    "empty_domains": [{"slug": str, "title": str}, ...],
    "overbroad_domains": [{"slug": str, "title": str, "matched": int, "total": int}, ...],
    "slug_conflicts": [{"slug": str, "domain_ids": list[str]}, ...],
    "domains_refreshed": int,
    "domain_failures": [{"slug": str, "title": str, "error": str}, ...],
}
```

`synced` is `False` when the pass was skipped outright (`graph_version` unchanged, no
`force`, and no cold-reload flag pending) — when skipped, **every other field is an empty
default** (`outcomes: []`,
`counts: ""`, `repointed: 0`, `stale_decisions: []`, `empty_domains: []`,
`overbroad_domains: []`, `slug_conflicts: []`, `domains_refreshed: 0`, `domain_failures: []`)
from a fresh, un-run report — never the prior (possibly stale) one. Don't read a skipped pass
as "everything's clean"; pass `force=True` (or re-call after a real graph rebuild) to get an
actual report.

`outcomes` carries only entities worth a human's attention —
`moved`/`moved_uncommitted`/`ambiguous`/`orphaned`/
`error` — never the `unchanged`/`rebound` majority, the same filter `sidegraph-sync`'s own
printer applies; each non-`ok` outcome means:

- **`moved`** — informational, not actionable: a unique same-name match in a same-suffix
  file after the exact match missed, with the entity's old `file_path` confirmed gone from
  disk AND that move confirmed by committed git history. The anchor followed the code;
  nothing to do.
- **`moved_uncommitted`** — informational, not actionable (yet): same disk-level evidence as
  `moved`, but git's `HEAD` doesn't yet back it up (an uncommitted delete/rename/stash).
  Nothing is touched. Commit the move (or set `SIDEGRAPH_TRUST_DIRTY_TREE=on`) and re-sync.
- **`ambiguous`** — the name now matches more than one graph node; the leaf is `degraded`,
  not lost. Needs a human judgment call — see the `heal-anchors` skill.
- **`orphaned`** — no match at all for this entity. Check `stale_decisions` for whether this
  orphan took a whole decision down with it (every one of its Tier-2 leaves orphaned).
- **`error`** — the entity raised during rebind; `detail` carries the exception text. One bad
  entity never aborts the rest of the pass.

`domain_failures` is the domain-refresh analog of an `error` outcome: one entry per accepted
domain whose refresh itself raised (a malformed `seed_anchors` descriptor, a domain that
vanished mid-pass, a lock-contended write), isolated per domain so one broken domain never
costs any other domain its heal. It counts as an attention finding for `--check`/
`report_has_findings`, same as an `error` outcome — unlike `empty_domains`/`overbroad_domains`,
which stay informational.

`stale_decisions`/`empty_domains`/`overbroad_domains`/`slug_conflicts`/`domain_failures` mirror
`sidegraph-sync`'s own report fields exactly (see [`cli.md`](cli.md#sidegraph-sync) for each
one's precise meaning) — this tool is the same diagnostic, surfaced as data instead of
stdout text.

**With no Graphify graph present:** returns `{"synced": false, "error": "graph not readable
(<resolved path>)"}` instead of crashing — explanatory, not silent, since this tool *is* the
diagnostic path (contrast every other tool's best-effort, no-graph-present degrade, which
never surfaces an error at all).

## `verify_store`

```python
verify_store() -> dict
```

Lints the store's canonical files against the write-path invariants `store.py`'s write API
enforces at write time — the MCP counterpart to `sidegraph-verify` run *without*
`--against`. The snapshot checker itself only reads canonical files. The MCP wrapper first
opens the normal `Store`, so that open may create/rebuild `index.db` or run a supported legacy
migration; it does not mutate valid canonical record content merely to perform the lint.

Checks (snapshot layer, always everything): every hot record file parses against its schema;
`schema_version` is present and known; `valid_to >= valid_from`; a `superseded` record has a
successor (its `supersedes` chain resolves); every `supersedes` target exists; every binding
references an existing entity; every fact `supports` references an existing decision; ULIDs
are unique across hot files *and* archive segments (byte-identical archive-archive
duplicates from a sanctioned cross-branch `sidegraph-compact` merge are exempt — see
[`store-format.md#archive-segments-sidegraph-compact`](store-format.md#archive-segments-sidegraph-compact));
archive segments parse as JSONL; every hot record file is named `<its own internal id>.json`.

**Snapshot-only in v1** — this tool takes no git ref. A CI user who also wants the
transition layer (classify every store file changed vs a git ref against the store's own
write rules) runs `sidegraph-verify --against <git-ref>` on the command line instead — that
layer needs git plumbing this MCP surface deliberately doesn't carry. See
[`cli.md#sidegraph-verify`](cli.md#sidegraph-verify) for the full violation-code table this
tool shares with the CLI.

**Returns:** `{"clean": bool, "violations": [{"code": str, "path": str, "detail": str}, ...]}`
— `violations` is empty iff `clean` is `true`.

## `add_anchors`

```python
add_anchors(
    record_id: str,
    anchors: list[dict],   # [{"name": str, "file_path": str | None, "relation": str | None}, ...]
) -> dict
```

Appends bindings to an **existing** decision or fact — in-place re-anchoring for the
`heal-anchors` triage flow. Use this when triage (after `sync_anchors`) finds "code moved,
decision still valid": it heals an orphaned/stale anchor in place instead of forcing a
content-free `supersede_decision`/`supersede_fact`, which would pollute history with a
successor that says nothing new. Reach for `supersede_decision`/`supersede_fact` instead
when the CONTENT actually changed (the `choice`/`rejected`/`consequences` text), not just
where the code that decision is about now lives.

`anchors` is the same `{"name", "file_path"?, "relation"?}` ref shape every other anchoring
tool here takes. **This is bindings-only:** the decision/fact's own record file is never
rewritten — only `bindings/<record_id>.json` (and any newly minted `entities/<id>.json`)
changes — so append-only history and `sidegraph-verify --against`'s transition rules stay
intact (a record's content fields otherwise being immutable outside real status/`valid_to`
transitions).

Routing tries `record_id` as a decision, then as a fact; an id resolving to neither writes
nothing and returns `{"error": "unknown record '<id>'"}` (never a guess). Anchors are
validated *before* anything is written, exactly like `add_decision`/`add_fact`: an invalid
`relation`, a `name` or `file_path` that is not a string, or a non-empty list in which no
anchor has a `name` raises, and the whole call fails atomically rather than leaving a
half-anchored write behind.

Per anchor: resolved against the graph via the same `resolve_and_bind` ladder every other
anchoring tool here uses when a reader is present — an ambiguous name is reported, never
guessed, and creates no Tier-2 leaf; a resolved name lands a live Tier-2 leaf (+ Tier-1
domain/community). With no reader, or a name that resolves to nothing, the anchor still
binds — an orphaned Tier-2 leaf — so a re-anchor request is never silently dropped for lack
of a graph.

**Returns:** `{"record_id": str, "bound": list[dict], "orphaned": list[dict], "ambiguous":
list[dict]}` — `bound`/`orphaned` entries are `{"entity_id": str, "canonical_name": str,
"tier": 2}` entity summaries (same shape `add_decision`/`add_fact` return per binding):
`bound` for anchors that resolved to exactly one live graph node, `orphaned` for anchors
bound with no graph present or that resolved to nothing (never dropped either way).
`ambiguous` is `{"name": str, "reason": "ambiguous", "candidates": list[str]}` (capped at 5)
for anchor names that matched more than one graph node — no leaf created, the same
per-anchor feedback `add_decision`'s `anchors_skipped` gives.
