# Anchoring

> **Start here:** Read the opening explanation, then follow only the detail needed for your current task. [Glossary](glossary.md).

Anchoring is how a `Decision` attaches to the code or docs it's actually about, so retrieval
can surface it when an agent's task touches that entity. Implemented in
[`src/sidegraph/anchoring.py`](../../src/sidegraph/anchoring.py) (capture-time resolution) and
[`src/sidegraph/sync.py`](../../src/sidegraph/sync.py) (rebind after the graph changes).

To anchor a new record, give the agent the target and its repository-relative path. For example: "Record this gotcha against the `cache_key` function in `cache.py` and against `cache.py`: resource names can collide between projects, so include the project in the key."

That pattern supplies two anchors:

```json
[
  {"name": "cache_key", "file_path": "cache.py"},
  {"name": "cache.py", "file_path": "cache.py"}
]
```

Check the write result. In the captured scratch run these anchors produced `bindings: 3`, `anchors_skipped: []` and `anchors_orphaned: []`. A nonempty skipped list means an ambiguous reference needs attention. A nonempty orphaned list means a target did not resolve. Correct its spelling or build the missing graph before trusting graph-based delivery. After a rename, rebuild and run `sidegraph-sync`, then review its report.

## Descriptors: name + file

Name the symbol, heading or file and give its repository-relative file path. For example, use `{"name": "cache_key", "file_path": "cache.py"}` for a function. Pair it with a file anchor so a later symbol rename does not make the record unreachable.

See [implementation details](../reference/anchoring-internals.md#descriptors-name--file).

## Lazy `Entity` creation

Sidegraph creates durable target identities only when records reference them. Rebuilding the graph does not create one store entity for every graph node.

See [implementation details](../reference/anchoring-internals.md#lazy-entity-creation).

## Multi-anchor at capture

A record can point to a precise target, its broader area and an abstract work label. If one target changes, another anchor may still make the record retrievable.

See [implementation details](../reference/anchoring-internals.md#multi-anchor-at-capture).

## Domain-aware Tier-1

When an accepted domain covers the target, the group anchor uses that durable domain name. Otherwise it uses the current graph community. Domain names survive community renumbering.

See [implementation details](../reference/anchoring-internals.md#domain-aware-tier-1).

## Tier semantics

| Tier | Meaning | Created by |
|---|---|---|
| 0 | Semantic/abstract — an owning initiative, or a cross-cutting tag | Naming `initiative=` at capture, or a non-empty slug in `tags=[...]` |
| 1 | Group — the entity's Leiden community, or (once ratified) the accepted domain covering it | Whenever the resolved (or ambiguous-but-shared) node has a community |
| 2 | Leaf — the concrete code/doc entity itself | Whenever the name+file resolves uniquely, or fails outright |

## Graceful degradation ladder: resolved -> ambiguous -> orphaned

Sidegraph never guesses. Resolution returns one of three statuses, and each
drives different bindings:

| Resolve status | Tier-2 leaf | Tier-1 community |
|---|---|---|
| **resolved** (unique match) | created, `live` | created if the node has a community, `live` |
| **ambiguous** (multiple matches) | **none created** | created only if *all* candidates share one community, `degraded` |
| **unresolved** (no match) | created, `orphaned` | none (no community to fall back to) |

The Tier-0 initiative binding does not depend on the resolve status: it is decision-level, made
once by the write path, `live`, whenever an initiative is named.

An ambiguous match with candidates spanning different communities gets **no community binding**.
The one exception is an accepted domain that holds every candidate: the group binding then goes to
that shared domain, `live`. Beyond that and a possible initiative it gets nothing — Sidegraph
would rather anchor nothing than anchor to a guess.

Identical path-less external stubs count as one match. Newer Graphify emits one stub node per
importing file for an imported name such as `BaseModel` or `Path`; when every match has no file,
the same label (case kept) and the same node type, the anchor resolves as **resolved** and gets a
group binding only when all the stubs share one community. A type and a module of one name
(`FastMCP` and `fastmcp`), and any repository code with a file, still count as separate matches.
Anchoring and sync collapse the stubs; what agents read keeps the ambiguous answer.

## Why multi-anchor matters for doc corpora

Anchoring decides where a record surfaces. Which sections of a long document a later
`record_drift_check` must watch is declared when the record is checked, not anchored.

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
symbol names — and the rebind ladder below handles it identically: exact match, then a unique
same-name match in a file that arrived with the old one's removal, then orphaned. No special-casing for concepts versus code symbols.

## What happens on rename or move

Rebuild the graph after a refactor and run `sidegraph-sync`. Exact matches stay live. A verified committed file move can be followed. A missing target is orphaned, and an ambiguous target is degraded. The record remains in the store. Review the report using the [refactor guide](../guides/surviving-refactors.md).

See [implementation details](../reference/anchoring-internals.md#what-happens-on-rename-or-move).

## See also

- [Data model](data-model.md) — the `Entity` / `Decision` / `AnchorBinding` / `Domain` fields.
- [Mind model](mind-model.md) — why domains exist and how they relate to communities.
- [Retrieval](retrieval.md) — how tiers and binding status feed the read path.
- [Store format](../reference/store-format.md) — write-path invariants for bindings.
