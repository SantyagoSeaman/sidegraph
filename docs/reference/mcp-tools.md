# MCP tools reference

Twenty-four tools, all defined in [`src/sidegraph/server.py`](../../src/sidegraph/server.py) and
exposed by the `sidegraph-mcp` stdio server (`FastMCP("sidegraph")`). The server holds one
process-wide `Store` (path resolved via `$SIDEGRAPH_DIR`, same precedence the CLI and hooks
use — see [`configuration.md`](configuration.md#store-path-resolution)) and a best-effort
`GraphifyReader` loaded fresh per call from `SIDEGRAPH_GRAPH` — see
[`configuration.md`](configuration.md) for both. See
[mind model](../concepts/mind-model.md) for what `Domain`/`drill_down`/the thin tools are for.
`sidegraph-mcp --help` (or `-h`) prints a short usage text and exits without starting the server.

## Summary

| Tool | Purpose | Typically called |
|---|---|---|
| [`add_decision`](#add_decision) | Write one decision directly, `status=accepted` | Mid-session, when a human/agent asks to record something now |
| [`supersede_decision`](#supersede_decision) | Close an old decision, write its replacement | When a past decision is explicitly reversed |
| [`add_fact`](#add_fact) | Write one non-derivable fact directly, `status=accepted` | Mid-session, when a human asks to record a benchmark/limit/trial-learned fact now |
| [`supersede_fact`](#supersede_fact) | Close an old fact, write its falsifying replacement | When a past fact is disproven or corrected |
| [`retrieve_decisions`](#retrieve_decisions) | Filter decisions by status, kind, file, text or id (bounded) | Ad hoc "what's in memory" queries, audits |
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

## Safe write diagnostics

Malformed proposal drafts and logical write arguments report schema fields, bounded
indices and fixed error codes without repeating input values or custom validator text.
The formatter shows at most 10 errors, 8 path components and 1,024 characters; unknown
fields/codes use fixed placeholders. Validation semantics and per-item draft isolation
are unchanged. A malformed outer signature still rejects the whole call.

Raw admission runs before signature coercion. All logical MCP writes, including
`ratify`, `ratify_decisions`, `sync_anchors` and `add_anchors`, apply the finite input
shape, string and node policy described below. Unknown write parameter names are omitted
from errors while valid parameter help remains. Read-tool aliases/help are unchanged.
The server rejects unsupported tool registrations rather than executing an unguarded
write. The isolated FastMCP compatibility adapter requires the tested argument-only
core-schema shape (locked FastMCP 3.4.2 / Pydantic 2.13.4); it validates no function body.

An incomplete store operation can report `rejected` after canonical replacement. Follow
its may-have-written warning: do not repeat the proposal in the same session; reopen the
store and check `list_proposed` in the next session. Generic store exceptions, including
`ValueError`, use this conservative remedy even when a particular refusal preceded a write:
the error class alone cannot prove atomicity. Known controlled validation errors retain
safe field/code help. A failure after a successful record write keeps its write-action status and reports a controlled stage/recovery message.

Write exception guards prevent raw validation/generic causes in framework WARNING/ERROR
logs. FastMCP DEBUG argument tracing occurs before middleware and remains outside this
protection. Read/filesystem diagnostics, successful descriptors and paths, record IDs
and ratify result keys keep their usual semantics; historical logs/data are unchanged.
See [security policy](../../SECURITY.md#write-diagnostics-and-logging).

## Capture and direct-write resource limits

`propose_decisions`, `propose_domains`, and direct add/supersede decision, fact and domain
writes check inputs before redaction, additional model validation, anchor validation or
canonical mutation. These fixed limits include tags, metadata, descriptors, mapping keys
and nested facts:

| Resource | Limit |
|---|---|
| Each string field | 256 KiB UTF-8 |
| Admitted strings in one agent or direct write request | 4 MiB UTF-8 |
| Agent decision/fact/domain drafts, including attached facts | 100 |
| Structure depth per draft | 32 |
| Visited structure nodes per draft | 4,096 |
| Admitted structure nodes in one agent or direct write request | 65,536 |

`propose_decisions(drafts=..., facts=...)` uses one shared preflight and budget for both
halves. An attached fact counts toward the draft limit and is inspected with its parent.
A field/structure violation anywhere in that decision rejects the whole decision before
its parent or facts write; valid top-level siblings retain normal behavior. Each invalid
item returns a static rejected result and consumes neither admitted-byte nor admitted-node
request budgets; rejected drafts still count toward the 100-draft ceiling. Cycles and
strings that cannot encode as UTF-8 are item violations too. A node is each scalar/container
visit, including mapping keys; root depth is zero and a leaf at depth 32 is allowed.
A request-wide draft, admitted-byte or admitted-node violation raises `InputLimitError`
before any canonical write. Direct-write violations likewise fail before a write; no input
is silently truncated and there is no disabling environment switch. Policy errors do not
echo input keys, values or secrets. Request metadata violations reject the whole request.

Raw Python inputs use strings, strict UTF-8 bytes/bytearray, `None`, booleans, `int`/`float`,
mappings, lists/tuples and stored Pydantic model fields. Draft batches and metadata
sequences must be lists/tuples. Other opaque values, including sets, deques, dict views,
iterators/generators and non-string Enum objects, are rejected without consuming them even
when Pydantic would otherwise coerce them. Only Enum subclasses of `str` retain their
measured base-string behavior; numeric and binary Enum subclasses are rejected before
conversion. Numeric enums can become strings in Pydantic string fields, unlike plain numbers. An unsupported nested value rejects its containing draft; unsupported
request metadata or batch shape rejects the whole request. These are data checks, not a
sandbox for caller-defined Python methods.

Python callers of `propose`, `propose_facts` and `propose_domains` receive the same checks
when calling each independently, including existing Pydantic models. The reusable `redact`
helper has no global field cap. Caller-side model construction may already have validated
or normalized fields before the entrypoint; MCP framing and transport allocation before
Python invocation are outside this application boundary.

Documents use an independent 8 MiB per-file limit; an import may exceed 4 MiB total or
produce more than 100 records. Operator tags and free-text import options are bounded
separately. See [document import](cli.md#importing-decision-shaped-markdown---docs) and
[security policy](../../SECURITY.md#resource-limits-and-redaction-runtime) for file admission
and the practical scope of the four linear redaction recognizers.

## Commit hint

A store is committed with the repository, so a record follows the branch and the checkout it was
written on. When the store sits inside a git repository, the write tools add a `commit_hint` key
to a successful result:

- `add_decision`, `supersede_decision`, `add_fact`, `supersede_fact`, `add_anchors`,
  `add_domain` and `supersede_domain` return one dict, which gains the key.
- `propose_decisions` and `propose_domains` return a list, and only the elements that wrote a
  record gain it: `status` `"written"` for the first, `"proposed"` for the second. A `deduped`,
  `rejected` or `skipped` element has none, and neither does `add_anchors`' `{"error": ...}`.

The value is one sentence, naming the store's path relative to the repository root:
`Commit .sidegraph/ in the same change as the work that produced this record, so it reaches
other checkouts and teammates.` Outside a git repository the key is absent, and so it is for a
store that is a symlink to a directory outside the repository, because git commits the link and
not the records behind it. `ratify` and
`ratify_decisions` carry none, because their results are keyed by record id, and `sync_anchors`
writes a committed file only when a rung moves. The key is additive, like every field the tools'
results may still gain ([stability](stability.md)).

## Annotations

Every tool carries MCP annotations, so a host can tell what a call does before it makes one. A
host with an auto-reviewer (Codex's `approvals_reviewer = auto_review`) reviews a call to a tool
that has none, because the MCP defaults say "destructive" and "open-world". It skips the review
for a tool marked read-only, or marked non-destructive and closed-world. Two annotation sets
cover the 24 tools, defined once in `server.py`:

| Hint | Read-only tools | Every other tool |
|---|---|---|
| `readOnlyHint` | `true` | `false` |
| `destructiveHint` | `false` | `false` |
| `openWorldHint` | `false` | `false` |

- **`openWorldHint` is `false` everywhere.** No tool sends anything off the machine.
- **`destructiveHint` is `false` everywhere.** The store is append-only: a write adds a record or
  closes one with `valid_to`, and never deletes one.
- **`readOnlyHint` is `true` only for a tool that never changes a tracked file.** `idempotentHint`
  is not set.

| Set | Tools |
|---|---|
| Read-only | `list_facts`, `find_entity`, `get_entity_history`, `list_proposed`, `list_domains`, `list_domain_candidates`, `verify_store` |
| Not read-only: they run the lazy sync | `retrieve_decisions`, `get_task_context`, `query_structure`, `query_decisions`, `drill_down` |
| Not read-only: they write records | `add_decision`, `supersede_decision`, `add_fact`, `supersede_fact`, `propose_decisions`, `ratify`, `ratify_decisions`, `add_domain`, `supersede_domain`, `propose_domains`, `sync_anchors`, `add_anchors` |

What does not count as a write: `.sidegraph/index.db` is local, gitignored state (the derived
index, usage statistics, the capture ledger and the session meta), never committed and never
sent, so a tool that only writes there is still read-only. So is opening the store: the first
open in a process may create the store's `format` file, its `.gitignore` and its record
directories, or run a legacy migration, whichever tool comes first.

The tools that run the lazy sync are not read-only. Its moved rung can adopt a moved symbol into
a tracked `entities/<id>.json` file, which is the one tracked write a sync makes (see
[`sync_anchors`](#sync_anchors)). A test holds the read-only set to this: it runs every
read-only tool on a store where a lazy sync would adopt a moved symbol, and fails if any of them
changes a tracked file.

### Argument names

`get_task_context`, `query_structure` and `query_decisions` also accept two names that agents
try:

- `paths` is `files`.
- `seeds` is a list, or one bare string. Each item that has no whitespace and either contains a
  `/` or ends in a known file extension (`.py`, `.swift`, `.toml`, `.md` and the like; the list
  is `_FILE_EXTENSIONS` in `server.py`, matched without regard to case) is a file path and goes
  to `files`. Any other item goes to `entities` as `{"name": item}`: a symbol (`RetryPolicy`,
  `Store.close`, whose `.close` is not a file extension), an issue id, free text. Routed values
  are appended to an explicit `files` or `entities`, order kept and duplicates dropped.

`task` is not accepted: there is no free-text query. `intent` is a label for statistics and never
affects the answer, so a query passed as `task` would become a call with no seeds that looks
successful. Pass `files` or `entities`.

Read tools answer an unknown argument with a tool error that names it and lists the
parameters, so one retry corrects it:

```text
Unknown argument `include_archived` for `get_task_context`. Its parameters are: files, entities, structure_budget, memory_budget, intent.
```

Logical writes list valid parameters without repeating an unknown argument name.

For `task` on a seed tool, the error adds a line: `There is no free-text query: pass files or
entities. intent is only a label for statistics.` Nothing is dropped silently.

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
`rejected`/`consequences`, tag text before slugification, and `initiative`) is **redacted first** — the
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
in CI as defense in depth. A quoted secret value is redacted whole (`password: "one two"`,
`{"token": "…"}`, or a backtick-quoted value), not only up to its first space. An escaped quote inside a quoted
value does not end it, and a quoted JSON key does not consume the next field. An all-secret
`initiative` is dropped and binds nothing, as an all-secret tag does. An all-secret tag redacts to `[REDACTED]` and is skipped, never minted as a
`tag:redacted` entity — same rule as `propose_decisions`. Each `anchors` entry is resolved against the
current graph and multi-anchored (leaf + domain/community) via `resolve_and_bind` —
best-effort: with no graph present the decision still writes, but the requested code anchors
are skipped and both anchor feedback lists are empty. The initiative Tier-0 binding, when
`initiative` is given, is decision-level: it is made whether or not a graph or any anchor is
present, and never takes an anchor's `relation`. An anchor's optional `relation` (one of
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
created for them; empty when every anchor resolved cleanly or no graph reader is present. Inside a git repository the result also carries a `commit_hint` ([Commit hint](#commit-hint)).

**`anchors_orphaned` is the one to act on.** It carries an entity summary plus a `reason`
(`{"entity_id", "canonical_name", "tier": 2, "reason": str}`) for every anchor that resolved
to **nothing**.
The leaf is still written — orphaned, never dropped — but it is dead on arrival: retrieval,
`drill_down` and the PreToolUse records block all skip orphaned bindings, and no Tier-1 community
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
successor is written or the predecessor closed. Inheriting from a predecessor whose bindings file
the store could not read (it is left out of the index, see [`store-format.md`](store-format.md))
is refused before anything is written: there would be nothing to copy, and the successor
would silently start with no anchors. MCP omits the raw cause/file name; use `sidegraph-doctor` to identify the binding file to repair. Passing
`anchors` still works.

**Returns:** `{"id": str, "supersedes": str, "bindings": int, "entities": list[dict],
"anchors_skipped": list[dict], "anchors_orphaned": list[dict], "redactions": int}` — same `bindings`/`entities`/
`anchors_skipped`/`anchors_orphaned`/`redactions` shapes as `add_decision`'s, reflecting whichever path
(explicit or inherited) produced the replacement's bindings (`anchors_skipped` is always
`[]` on the inherited path — inheritance never resolves against the graph). Text fields are
redacted exactly like `add_decision`'s. Inside a git repository the result also carries a `commit_hint` ([Commit hint](#commit-hint)).

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
this fact informed; every id must already reference an existing `Decision` or the write is
refused before anything is committed. MCP returns a controlled error rather than a raw
`ValueError`; generic store failures retain the conservative inspection remedy above. An anchorless fact additionally needs at least one
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
`anchors_orphaned` is `[{"entity_id", "canonical_name", "tier": 2, "reason"}, ...]` when a graph
is present and the name resolved to nothing; with no graph every anchor lands there as
`{"entity_id", "canonical_name", "tier": 2}` with no `reason` key, and heals on the next sync.
Repair either with [`add_anchors`](#add_anchors). Inside a git repository the result also carries a `commit_hint` ([Commit hint](#commit-hint)).

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
it stays retrievable as "believed before, corrected because…". An `old_fact_id` that does
not resolve to an existing fact refuses the write with controlled MCP diagnostics. Every text field is redacted exactly like
`add_fact`'s. `supports` defaults to the **predecessor's own** `supports` when omitted — a
superseding fact is assumed to inform the same decisions unless told otherwise.

Anchoring mirrors `supersede_decision`'s two exclusive paths: pass `anchors` to resolve and
bind the successor to ONLY those refs (same no-graph orphaned-leaf fallback `add_fact`
gives); omit `anchors` (the default) to **inherit the predecessor's bindings verbatim** —
same `entity_id`/`tier`/`weight`/`relation`/`status`, *including* any `orphaned` ones,
carried as-is. Passing `anchors` replaces inheritance; it never adds to it. Explicit
`anchors` are validated exactly like `add_decision`'s, before the successor is written or
the predecessor closed. Inheriting from a predecessor whose bindings file the store could not
read is refused the same way as in `supersede_decision`.

**Returns:** `{"id": str, "statement": str, "status": str, "redactions": int, "entities":
list[dict], "anchors_skipped": list[dict], "anchors_orphaned": list[dict], "supersedes": str}` — same shape as `add_fact`'s
plus `supersedes` (the predecessor's id), with the same `anchors_orphaned` shapes;
`anchors_skipped` is always `[]` on the inherited path. Inside a git repository the result also carries a `commit_hint` ([Commit hint](#commit-hint)).

## `retrieve_decisions`

```python
retrieve_decisions(
    include_superseded: bool = False,
    status: str | list[str] | None = None,   # proposed | accepted | superseded | rejected | deprecated
    kind: str | list[str] | None = None,     # adr | lesson | constraint | gotcha
    files: list[str] | None = None,          # repo-relative paths, read as get_task_context reads them
    query: str | None = None,                # every word must occur in the record's text
    ids: list[str] | None = None,            # exact record ids
    limit: int = 25,                         # clamped to 1..100
    budget_chars: int = 24000,               # clamped to 4000..60000
) -> ToolResult                              # one text block holding a JSON object
```

Lists decisions, bounded. The tool used to return a full dump of every decision in the store,
about 143,000 tokens on a store of 260 records, and agents that wanted a handful of records
filtered that dump themselves. The answer is now one JSON object, sent once as text with no
structured copy, so `budget_chars` is the real cost on every host. **The length of the
serialized answer never exceeds the clamped `budget_chars`.**

**With no narrowing filter, an overview.** The narrowing filters are `status`, `kind`, `files`,
`query` and `ids`; an empty value (`""`, a blank `query`, `[]`) counts as absent.
`include_superseded` is not one of them: it widens.

```json
{
  "overview": true,
  "total": 263,
  "counts": {"status": {"accepted": 226, "superseded": 28, "rejected": 8, "proposed": 1},
             "kind": {"gotcha": 126, "lesson": 33, "adr": 86, "constraint": 18}},
  "newest": [{"id": "01K…", "kind": "gotcha", "status": "accepted",
              "title": "…", "valid_from": "2026-10-04T06:13:51Z"}],
  "hint": "Full records come back only for a narrowed call: status=..., kind=..., files=[...], query=..., ids=[...]."
}
```

`total` and `counts` cover every record the proposal policy lets through, superseded and
rejected ones included, whatever `include_superseded` says: they tell the caller that history
exists. `newest` holds up to `limit` compact rows (exactly the five keys above), newest
`valid_from` first, of the records `include_superseded` admits: by default everything except
`superseded` and `rejected`.

**With a narrowing filter, full records.**

```json
{
  "overview": false,
  "matched": 36,
  "returned": 9,
  "omitted": 27,
  "decisions": [{"…": "full Decision.model_dump(mode='json') dicts"}],
  "unresolved_files": [],
  "unresolved_omitted": 0,
  "hint": "27 more matched; the character budget cut the list. Narrow with files=[...] or query=..., or raise budget_chars (max 60000)."
}
```

The filters combine with AND:

- **`status`** and **`kind`** take one value or a list, in any letter case. An unknown value is
  an error that names the valid ones. `status` replaces the default exclusion of `superseded`
  and `rejected`; without it, those two are left out unless `include_superseded=True`.
  `deprecated` is listed by default.
- **`files`** are read the way [`get_task_context`](#get_task_context) reads file seeds, so a
  basename, a path suffix, a letter-case slip, a `./` prefix, an absolute path inside the
  repository and a directory all work: a path the code graph does not hold goes through the
  [seed ladder](#seeds-are-read-tolerantly), the seeds through the node mapping and the entity
  descriptors, and every entity whose stored `file_path` equals a path read is added, which
  reaches a file that has since left the graph. A record matches when any of its bindings, of
  any status (an orphaned one too), points at one of those entities: the question is where
  the record was anchored, not whether it is still delivered there. Without a code graph only
  the stored paths are compared.
- **`query`** is split on whitespace; every word must occur, in any letter case, in the
  record's `title`, `context`, `choice`, `rejected` or `consequences`.
- **`ids`** returns those records by exact id, superseded and rejected ones too; the default
  exclusion does not apply to `ids` unless `status` is also given.

**Order and bounds.** Records come back `gotcha`, then `lesson`, then everything else; within
a rank newest `valid_from` first, `id` descending as the tie-break. Records are added in that
order while the answer stays within `budget_chars` and the count within `limit`. A record is
never cut in the middle: the first one that does not fit ends the list, and if even the first
does not fit, `decisions` is empty and the hint says so. `limit` is clamped to 1..100 and
`budget_chars` to 4,000..60,000, so no argument turns the call back into a dump.

**`unresolved_files` and `hint`.** When `files` was given, `unresolved_files` lists the entries
nothing could read, at most ten, and `unresolved_omitted` counts the rest. Each entry says why:

```json
{"path": "src/sidegraph", "reason": "directory-too-large",
 "candidates": ["src/sidegraph/host 7", "src/sidegraph/engine 3", "src/sidegraph/sync.py"]}
```

- `path` is the entry in its normalized spelling, as given for a directory or a basename;
- `reason` is `ambiguous` (several files match; `candidates` holds up to three of them),
  `directory-too-large` (the directory holds more files than a path can stand for;
  `candidates` holds up to three subdirectories, each followed by its file count, or files, to
  pass instead), `not-in-graph`, or
  `no-graph` when there is no code graph at all;
- a file the graph holds with no record anchored to it is not listed: it was read, and
  `matched` says it has no memory.

`hint` is `null` only when something matched, nothing was left out and every file was read.
Otherwise it says which case applies: nothing matched (with the filters as given, and for
`query` a suggestion to try one or two distinctive words; it also says that superseded and
rejected records need `status` or `include_superseded=True`, unless unreadable files already
explain the empty answer); which bound cut the list, `limit` or the character budget, and how
to narrow or raise it (a bound already at its maximum is not offered); or that some files
could not be read, which it leaves to `unresolved_files` to name. Read it before concluding
that the store holds nothing.

Everything the caller's own text adds to the answer (the filters echoed in a hint, the paths
and candidates of `unresolved_files`) is clipped to 80 characters as JSON writes them. If the
answer still would not fit, the echo of the filters goes first, then entries of
`unresolved_files`, last first, and `unresolved_omitted` counts them.

**Proposal policy applies here too.** A `proposed` record
outside the surfacing window (`SIDEGRAPH_PROPOSAL_WINDOW_DAYS`, default 30) or any proposed
record in regulated mode (`SIDEGRAPH_UNRATIFIED=off`) is omitted from every shape and every
filter, `ids` included, and from `total` and `counts`. Accepted records are never affected.
This closes what a 2026-08-04 reviewer correctly identified as a documented bypass of an
advertised control — an agent could otherwise fetch through a raw tool precisely the content
regulated mode exists to withhold. Each record carries its `status`, so a `proposed` record
is distinguishable from an `accepted` one even though the tool doesn't tag it `[unratified]`
the way rendered text does.

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
None, "bindings": [{"record_id": str, "record_type": "decision" | "fact" | "unknown", "tier":
int, "status": str}, ...]}` — `record_id` is the id of whichever bound *record* the binding
belongs to (a `Decision` or a `Fact`; `AnchorBinding.record_id` was renamed from
`decision_id` when facts started sharing the same anchoring machinery), and `record_type`
tells you which one it resolved to, so a caller knows whether the id is a decision or a fact
without guessing (`retrieve_decisions(ids=[…])` returns a decision's full record; a fact is
listed by `list_facts`). The types of all the bindings come from one read of the index.
`"unknown"` means the record exists in neither table: a binding whose record file was
removed by hand or lost in a merge. Treat it as a dangling anchor, not as a decision to
fetch. On ambiguity:
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

The tool resolves every binding's record type in one read of the index, then loads each
record with the getter its type names. A binding whose record exists in neither table stays
skipped, same as before (`find_entity` reports such a binding as `"unknown"`). Previously this tool only ever called `get_decision`, so
a fact-only binding vanished from an entity's history with no trace — that gap is closed.

**Returns:** a list of full `Decision`/`Fact` dicts (`model_dump(mode="json")`, all fields,
including `status`), each with one additive key: `"record_type": "decision" | "fact"`, so a caller can
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
When a seed path (from `files`, or an entity's `file_path`) is not a file the code graph holds,
the string ends with a `## Not in the code graph` block that says what happened to each such
path instead of leaving a bare empty answer:

- a path that exists in the repository but is missing from a **stale** graph (built at a commit
  `HEAD` has moved past): the block gives the build commit and the gap, and says to rebuild with
  `graphify update .` from the repository root, then call again;
- the same from a graph that shows no committed change since the build: no commit explains the
  gap, so the file may be newer than the build and not committed yet (rebuild with
  `graphify update .`), sit under an excluded path, or be a file type Graphify skips;
- the same when the comparison could not be made: the reason, and the rebuild command if the
  files are new;
- a path that does not exist, one that names nothing the graph holds, and one that points outside
  the repository (`../x`, `.`, an absolute path elsewhere): check the path. No rebuild would
  help, and no graph comparison is made for it.

The paths named are the ones [the seed ladder](#seeds-are-read-tolerantly) could not read, in
their normalised spelling: `./pkg/n.py`, `pkg//n.py` and an absolute path under the repository
root all name `pkg/n.py`, so a file that exists but is not in the graph gets the graph-state
advice above whichever way it was written. The `N` of "N of M seed paths" stays the number of
distinct paths as you wrote them.

Nothing is added when the graph holds every seed, and then no git call is made. The block belongs
to `get_task_context` only; `query_structure` and `query_decisions` are unchanged.

In a **linked worktree** that has no graph of its own, every read tool opens the main checkout's
graph (see [the Graphify integration](../integrations/graphify.md#linked-worktrees-read-the-main-checkouts-graph))
and syncs it index-only (no tracked file is rewritten). The `## Not in the code graph` block is then worded for it:

- a seed that exists only in the worktree gets "This worktree reads the main checkout's graph,
  which does not hold files that exist only on this branch." with no freshness check, because the
  graph was never built from that file;
- a seed that also exists in the main checkout gets the stale or current verdict above, with the
  advice to rebuild in the main checkout (`rebuild it in <main> with graphify update .`).

When no graph could be opened and the call named at least one seed, the string ends with:

```
## No code graph
No code graph at <path>: memory anchored to code cannot be looked up. Build it from the
repository root with `graphify update .`.
```

`<path>` is the graph the server looked at: the store's own, or, in a linked worktree whose main
checkout has none either, the main checkout's, with "Build it in the main checkout `<main>` with
`graphify update .`." A graph that is there and cannot be read (permissions, a corrupt file) reads
"The code graph at `<path>` is not readable" instead of "No code graph at". A call that has a reader
is unchanged by this block.

### Seeds are read tolerantly

A seed the graph does not hold as written is read through a short ladder before it is given up
on. Each rewrite is a guess, and a `## How your seeds were read` block after the rendered text
(before the not-in-graph block) says so, one line per seed. Seeds that resolve as written are
never touched: a call whose seeds all resolve gets no block and costs nothing extra.

For a file path (`files`, or an entity's `file_path`):

1. **Normalised.** Surrounding whitespace, a trailing `:12` or `:12:3`, a leading `./`, doubled
   slashes, `a/../b` and an absolute path under the repository root all read as the repo-relative
   path: `` `./pkg/n.py` → read as `pkg/n.py` (normalised) ``. A file that is itself named like a
   location (`notes:12`) is tried as written before the location is stripped.
2. **A file on disk is never swapped for another.** A path that is a file in the repository, with
   exactly that letter case, but not in the graph is not guessed to be a different file with the
   same name. It goes to the not-in-graph block.
   Letter case is compared exactly, even on a case-insensitive filesystem; a path that differs
   from a graph file only by case reads as that file, marked `guessed: different letter case`
   (two such files are ambiguous).
3. **Directory.** A path with files under it in the graph reads as a directory: up to 8 files,
   shallowest first. A directory of more than 24 files is listed, not read, with its five largest
   subdirectories and their file counts (with some of its top-level files when the subdirectories
   leave room), so you can pass a file or a smaller directory. A trailing slash means "a directory" and skips step 4.
4. **The shortest tail that matches.** Each tail of the path, longest first, is matched against
   the graph's files, and the first tail that matches anything decides. One file: it is read,
   marked `guessed: the only file with that path ending` (or `with that name`, when only the file
   name matched). Several files: the seed is reported as ambiguous with up to five candidates and
   nothing is read. A path outside the repository (`../x`, `.`) is never matched.

For an entity `{"name", "file_path"}`: an entity that resolves as written is untouched. A
`file_path` the graph lacks goes through steps 1, 2 and 4, and the seed is rewritten only when the
entity is in the file that comes out. Failing that, the name alone is tried across the
repository, and a `Type.member` (also `Type::member`, `Type#member`) is found through the
graph's owner edge: one match is read, several are reported as ambiguous. A name with no file
that matches several symbols is **reported as ambiguous and not read**; it used to be expanded
to all of them, and the list counts distinct files, not nodes. A name that matches nothing gets
its own line: `` `Frobnicate` → matches no symbol in the code graph ``, and a name missing from
a file the graph holds says `` matches no symbol in `pkg/m.py` ``. An entity whose `file_path` is
in the graph, or is a file on disk that the graph lacks, is never moved to another file (the
latter gets the not-in-graph advice). A rewritten or ambiguous entity seed keeps the original as an
exact seed, so a record stored under the original file keeps the tier it always had.

A guessed seed ranks below the ones you got right. When at least one seed resolved as written
to a node of the graph, the records of the guessed ones appear in `## Related` and
`## Known facts`, after every record of the seeds you got right (their related records, their
superseded history, global decisions and facts) have had their budget, and the structural map is
the one those seeds alone would give; when none did, the guessed seeds act as the seeds. The
block lists at most ten seeds, then `… and N more seeds`. Telemetry records a rewritten path
under the path that was read, and a directory under its normalised key, not its files.

### An empty answer says why

Besides the not-in-graph block, a call that gave **no** `files` and no `entities`, and whose
answer is exactly `No context found.`, ends with:

```
## Why this is empty
No files or entities were given, so nothing could be matched. Pass files=[…] with the
repo-relative paths you are working on, or drill_down(<slug>) for a named area: Auth (auth),
Payments (payments).
```

It names at most 12 accepted domains by title and slug (then `, and N more`), and stops at
"working on." when the store has none. A seed with neither a name nor a path (`files=[""]`,
`entities=[{}]`, blanks) counts as no seed. With no code graph the block also says to build it first
(`graphify update .`). Every other empty answer already says why: a path the
graph lacks gets the not-in-graph block, a name that matches nothing gets its own line above, and
a missing graph gets the `## No code graph` block above. An empty answer for a file seed that has
anchored files near it adds [the nearest anchored records](#the-nearest-anchored-records) to this
block.

### The nearest anchored records

A file seed that no read could place, and one that resolved with no current record anchored to
it, is not a dead end: the files around it often carry the memory you wanted. For each such file
seed the reply says where the nearest memory is, in one sentence. A seed qualifies when it is
a file path read as given or as one other file, and it either names a file the graph lacks (a
path inside the repository) or names one with no current record of its own. A directory, an
ambiguous seed, an entity seed and a path outside the repository get none.

```
## Nearest anchored
No current record is anchored to `core/a.py`. Nearest anchored: `net/c.py` (linked in the code graph, 2 records), `core/b.py` (same directory, 3 records).
```

- **Anchored** means a live or degraded binding (the two through which a record is shown) from a
  current record (accepted, or proposed and inside the proposal window, and not past its
  `valid_to`) to an entity in a file the graph holds. An orphaned binding, a rejected or
  superseded record (a superseded one shows only as a "tried, reverted" line) and a record on a
  file the graph lacks do not count.
- **Candidates**, in this order: the anchored files that hold a node next to a node of the seed in
  the graph, the two with the most records; then the anchored files under the nearest ancestor
  directory that has any (at any depth), the two with the most records. Ties go by path. An ancestor
  that holds more than a quarter of all anchored files, and more than four of them, is the
  repository's hub and is skipped, and the repository root is never used. The two lists are merged without repeats and cut to three
  files. A directory label is `same directory` or ``under `dir` ``.
- **One sentence per seed**, at most three, then `… and N more seeds with no anchored record`. A
  seed with nothing anchored near it gets no sentence. When the answer is empty the sentences go
  under `## Why this is empty`; otherwise under `## Nearest anchored`, after the other blocks.

When the main answer has **no decision memory** (no mistake, decision, fact, related or
unratified line; a structural map does not count), the records of the files the sentences name
follow under `## Nearest anchored records (not anchored to your files)`, rendered like the main
answer with their headings one level lower and the supersede line once at the very end. The
block carries the memory guard line itself when the main answer has none (an empty answer). A
main answer that already has memory gets the sentence alone, so no record shows twice. Those
records count as shown in the local statistics (their render counters are added to the call's),
and the neighbour files are never recorded as seeds.

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
  `evidence:` lines), `## Known facts` (standalone facts), `## Related`, and
  `## Unratified proposals` (proposed records inside the surfacing window, last), with no structural
  map — internally it still walks the structural subgraph to resolve peripheral entities for
  bucket C, it just never renders the map itself. Degrades exactly like `get_task_context`:
  named-seed resolution needs a reader, but `scope: global` decisions still surface without
  one.

`query_decisions` takes the same optional `intent` as `get_task_context`: a label for what
asked, recorded for local statistics only, never affecting what is returned.

**Neither tool has the seed ladder.** They match the seeds exactly as written. A path spelled
`./pkg/mod.py` or `mod.py`, which [`get_task_context` reads](#seeds-are-read-tolerantly) as
`pkg/mod.py`, finds nothing here, and `query_decisions` answers `No context found.` with none of
the blocks that explain why: no `## How your seeds were read`, no `## Not in the code graph`, no
`## Nearest anchored`. When a path might be spelled loosely, call `get_task_context` first.

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
consequences **and** tag and initiative text) → dedup (conservative, same kind+canonicalized
redacted title on a shared anchor entity) → validate → write with `status="proposed"` → anchor best-effort
(per-anchor `relation` carried through) → bind an initiative if named or derivable from the
current branch of the repository the store lives in → bind tags → write each of the draft's
own `facts` (attached — see below). Per-draft failures don't abort the batch: a step that
fails after the write leaves the result `written` with a `reason` naming the step; an error
in an incomplete pipeline makes that draft `rejected` with an `internal error` reason;
a store-operation failure can still have left a canonical record on disk.

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
meaning: an auto-ratified draft still reports `"written"`. A `written` result may carry a
`reason`: the record is on disk, but a step after the write (anchors, initiative, tags,
attached facts, auto-ratify) failed and the later steps did not run. For a proposed record
the remedy is to drop it with `ratify(drop=[...])` and propose the complete draft again; for
an accepted one (`SIDEGRAPH_AUTO_ACCEPT=on`), anchors can be added with `add_anchors`, while
initiative and tags cannot be added afterwards. A `rejected` result whose `reason` starts with
`internal error` may have left the record on disk unindexed until the store is reopened: do not
re-propose it in the same session, and check `list_proposed` in the next one. Inside a git repository, each element that wrote a record also carries a `commit_hint` ([Commit hint](#commit-hint)).

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
`auto_ratify_error` opens with `activation:` followed by a controlled unresolved-membership or operation-failure
notice with a sync retry hint, or a `path rule too broad` notice, which means the
`path_prefixes` claim was rejected and only the `seed_anchors` that resolve, if any, are
still applied. Per-draft failures don't abort the batch: a step that fails after the write
leaves the result `proposed` with a `reason` telling you to review the domain's
`path_prefixes` and anchors before ratifying, and a failed TOC rebuild adds a `toc:` line to
the `warnings` of every auto-ratified result.

**Returns:** a list of `ProposeDomainResult` dicts, one per draft, in the same order:
`{"status": "proposed" | "skipped" | "rejected", "domain_id": str | None, "reason": str |
None, "redactions": int, "warnings": list[str], "ratified_by": str | None,
"auto_ratify_error": str | None}`. `status` keeps its write-action value even when
auto-ratified: an eligible domain still reports `"proposed"` here, never `"written"` —
`ratified_by` is what tells the two cases apart. Inside a git repository, each element that wrote a record also carries a `commit_hint` ([Commit hint](#commit-hint)).

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

`title` and `summary` are **redacted first**, with the same patterns as every other prose
field; the slug is an identifier and is not.

**Returns:** `{"domain_id": str, "status": str, "redactions": int}` (`status` is always
`"proposed"`; `redactions` counts the secret replacements made in the title and summary). Inside a git repository the result also carries a `commit_hint` ([Commit hint](#commit-hint)).

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
`ratify(accept=[...])` before the new name/scope is TOC-visible. The new title and summary
are redacted first, and the result carries `"redactions": int` next to `domain_id`, `status`
and `supersedes`.

Raises (before anything is written) if: `old_slug_or_id` doesn't resolve to any domain;
`parent_slug` is given but doesn't resolve to any domain; or `new_slug` collides with some
OTHER still-live (proposed/accepted) domain (the predecessor itself is excluded from that
check, so the successor may reuse the same slug). This is also the direct fix for [recovering
from a mass-drop](../guides/naming-your-domains.md#recovering-from-a-mass-drop): supersede a
dropped domain with a successor that gets no matching `path_prefixes`/`seed_anchors`, and its
`communities` stays empty — freeing the community for a future `sidegraph-domains bootstrap`
pass to reconsider.

**Returns:** `{"domain_id": str, "status": str, "supersedes": str, "redactions": int}` — `status` is always
`"proposed"`, `domain_id` is the successor's, `supersedes` is the predecessor's resolved
`domain_id`. Inside a git repository the result also carries a `commit_hint` ([Commit hint](#commit-hint)).

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
ignored)"`. An unknown id gets a fixed unknown-ID error; an unsuccessful transition
gets a controlled ratification failure with reopen/inspect guidance. Raw exception text
is omitted. The result's keys remain the caller's requested IDs. One bad id never aborts
the rest of the batch.

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

**Deprecated alias for [`ratify`](#ratify), and still kept.** It has been deprecated since 0.1.0,
and no removal date is set. Despite the name, it now
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

A move a previous pass left as `moved_uncommitted` is also re-verified once git `HEAD` has
moved, even when the graph was not rebuilt. That is a *narrow* pass: `synced` is `true`,
`from_version` equals `to_version`, `outcomes` covers only the re-verified entities,
`stale_decisions` and `slug_conflicts` are recomputed, and the domain fields
(`domains_refreshed`, `empty_domains`, `overbroad_domains`, `domain_failures`) stay empty
because domain membership was not recomputed. If the rename was reverted or stashed, the old
path is back on disk and the entity is left untouched and reported `unchanged`, and it stays
watched: it is re-checked after the next `HEAD` change, so a rename that comes back later is
still noticed. A new file that takes the old path before the move is committed and synced
hides it: the move surfaces as `orphaned` after the next graph rebuild.

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
`force`, no cold-reload flag pending, no remembered `moved_uncommitted` entity to
re-verify after `HEAD` moved, and no remembered Tier-1 community reconcile whose retry
repaired the record or failed again (a retry that abstains reports nothing); with a
`moved_uncommitted` entity, the narrow pass runs and `synced` is `True` with
`outcomes` covering just those entities) — when skipped, **every other field is an empty
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
  nothing to do. Also reported, with `detail` `tier-1 community reconcile retried` and a
  `canonical_name` of `community rows of record <id>`, when a remembered Tier-1 community
  reconcile that failed earlier has now been repaired. That is a record's bindings, not a
  file move, and needs no action.
- **`moved_uncommitted`** — informational, not actionable (yet): same disk-level evidence as
  `moved`, but git's `HEAD` doesn't yet back it up (an uncommitted delete/rename/stash).
  Nothing is touched. Commit the move (or set `SIDEGRAPH_TRUST_DIRTY_TREE=on`) and re-sync.
- **`ambiguous`** — the name now matches more than one graph node; the leaf is `degraded`,
  not lost. Needs a human judgment call — see the `heal-anchors` skill.
- **`orphaned`** — no match at all for this entity. Check `stale_decisions` for whether this
  orphan took a whole decision down with it (every one of its Tier-2 leaves orphaned).
- **`error`** — the entity raised during rebind; `detail` carries the exception text. One bad
  entity never aborts the rest of the pass. Also reported when a Tier-1 community reconcile
  (`community rows of record <id>`) failed; it stays remembered and is retried on the next
  sync.

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
(<resolved path>)"}`, where the path is the store-anchored one; when the same relative value
exists beside the server's cwd the message adds `; <cwd path> exists beside the server's cwd --
set SIDEGRAPH_GRAPH to an absolute path to use it`, instead of crashing — explanatory, not silent, since this tool *is* the
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
references an existing entity; every fact `supports` references an existing decision; every domain's `parent_id` names an existing domain and no
parent chain loops; ULIDs
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
nothing and returns `{"error": "unknown record"}` (never a guess). Anchors are
validated *before* anything is written, exactly like `add_decision`/`add_fact`: an invalid
`relation`, a `name` or `file_path` that is not a string, or a non-empty list in which no
anchor has a `name` raises, and the whole call fails atomically rather than leaving a
half-anchored write behind. A record whose bindings file the store could not read (it is left out
of the index until restored or fixed, see [`store-format.md`](store-format.md)) is refused the
same way, before any entity is minted, and writes nothing. MCP omits the raw file name and
cause; use `sidegraph-doctor` to identify the binding file to repair. Generic errors retain
the conservative inspection remedy above.

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
per-anchor feedback `add_decision`'s `anchors_skipped` gives. On success, inside a git repository, the result also carries a `commit_hint` ([Commit hint](#commit-hint)); the error dict has none.
