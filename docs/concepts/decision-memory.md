# Decision memory

Sidegraph stores decisions, lessons, constraints, and non-derivable facts alongside the code
and docs they concern. Committing that memory lets another developer or agent session
retrieve the reasoning behind earlier work.

## Why session memory evaporates

A coding agent may learn "we tried X, it broke Y, so we do Z instead" during a session.
Unless that reasoning is saved and retrieved later, another session may need to repeat the
investigation or encounter the same failure. Session transcripts and summaries can preserve
some context; Sidegraph records the durable finding against the code or docs it concerns.

## Why ADR markdown isn't retrieved

ADR and software architecture documents (SADs) already preserve reasoning in the repository.
The remaining problem is finding the relevant document before changing the code it
constrains. A file such as `docs/adr/0042-*.md` can be easy to miss unless its links,
index, or the project's workflow lead the reader to it. Sidegraph adds code/doc anchors
and task-scoped retrieval; supported decision-shaped documents can also be
[imported](../guides/capturing-decisions.md#already-have-adrs-import-them).

## Memory before the question

A document index starts working after the reader knows what to ask. Sidegraph carries forward
what the project already knows, organizing each record around the code it governs and the tasks
that need it. The next session does not need to know that a relevant ADR, rejected alternative,
or production lesson exists before it can benefit from it: the memory is already part of the
path into the work.

This is why storage, anchoring, and delivery form one product. Together they let separate agent
sessions act on the accumulated experience of the project instead of treating every task as a
new onboarding exercise.

## What Sidegraph stores instead

Sidegraph's store holds `Decision` records of four kinds — `adr`, `lesson`, `constraint`,
`gotcha` — and, critically, each records not just what was chosen but what was **rejected**
and why:

| Field | Purpose |
|---|---|
| `context` | why this decision was needed |
| `choice` | what was decided |
| `rejected` | what was tried or considered **and abandoned** — the mistake paid for twice if nobody writes it down |
| `consequences` | trade-offs accepted by making this choice |

Bind a `Decision` to the code/doc entities it concerns (see [anchoring](anchoring.md)) so
task-scoped retrieval can find it. Delivery depends on the anchors, the host integration,
and the retrieval budget — see [retrieval](retrieval.md). The `rejected` field preserves
what was considered or tried and why it was abandoned; gotchas, lessons, and constraints
rank ahead of ADRs on the task's own entities.

## Append-only as a feature

The store never hard-deletes a `Decision`. Reversing one sets `valid_to` on the old record and
creates a new one with `supersedes` pointing back at it. The old record stays retrievable —
not as noise, but as **"tried before, abandoned because…"**. That history is the product: an
agent about to re-propose a rejected approach sees it was already tried, and why it didn't
stick, instead of relearning that the hard way. See the full write-path rules in the
[store format reference](../reference/store-format.md).

## Own the memory, rent the graph

Sidegraph uses [Graphify](https://github.com/Graphify-Labs/graphify) for entity extraction and
graph construction. Its own append-only store uses durable identities that remain stable
when graph node ids or community labels change. Graphify's `graph.json` is read-only input
to Sidegraph: the engine can regenerate it, so durable records live separately in the
`.sidegraph/` directory, committed next to the code they document.

## See also

- [Data model](data-model.md) — record types and their fields.
- [Anchoring](anchoring.md) — how a decision attaches to code/docs, and survives renames.
- [Retrieval](retrieval.md) — how mistakes-first context reaches the agent.
- [Store format](../reference/store-format.md) — the on-disk contract.
