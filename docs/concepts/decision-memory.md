# Decision memory

Sidegraph is a **decision / lessons layer**: durable, repo-committed memory of *why* the code
and docs are the way they are, so that memory survives developers (and agent sessions)
moving on.

## Why session memory evaporates

A coding agent's session memory dies with the session. Whatever it figured out — "we tried X,
it broke Y, so we do Z instead" — is gone the moment the context window rolls over or the
session ends. The next session (yours, a teammate's, or another agent's) re-derives the same
conclusion from scratch, or worse, re-makes the mistake the previous session already paid for.

## Why ADR markdown isn't retrieved

Flat ADR/SAD markdown files don't have this problem in principle — they're durable, they're
in the repo. In practice they fail for a different reason: they are **unanchored** and never
surface **at the moment of need**. An ADR sitting in `docs/adr/0042-*.md` is not linked to the
function or module it constrains, so nothing prompts an agent to go read it before editing
that code. It gets read once, at authoring time, and then forgotten until someone thinks to
grep for it.

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

Every `Decision` is bound to the code/doc entities it concerns (see
[anchoring](anchoring.md)), so it is retrieved automatically when an agent's task touches
that entity — see [retrieval](retrieval.md). The `rejected` field is why this beats a wiki
page: it's not just "what we do," it's "what we tried and it didn't work," ranked to the top
of what an agent sees.

## Append-only as a feature

The store never hard-deletes a `Decision`. Reversing one sets `valid_to` on the old record and
creates a new one with `supersedes` pointing back at it. The old record stays retrievable —
not as noise, but as **"tried before, abandoned because…"**. That history is the product: an
agent about to re-propose a rejected approach sees it was already tried, and why it didn't
stick, instead of relearning that the hard way. See the full write-path rules in the
[store format reference](../reference/store-format.md).

## Own the memory, rent the graph

Sidegraph does not build a second code-graph engine. It rents a mature one
([Graphify](https://github.com/safishamsi/graphify)) for entity extraction and graph
construction, and owns only the thin, durable layer that graph doesn't provide: an
append-only decision store, keyed by an identity that survives the engine's own churn (node
ids that shift on every rebuild). The engine's `graph.json` stays strictly read-only input —
Sidegraph never writes into it, because the engine regenerates it from cache on every commit
and anything written there would be erased. Everything Sidegraph owns lives in its own
sidecar file, committed next to the code it documents.

## See also

- [Data model](data-model.md) — the three record types and their fields.
- [Anchoring](anchoring.md) — how a decision attaches to code/docs, and survives renames.
- [Retrieval](retrieval.md) — how mistakes-first context reaches the agent.
- [Store format](../reference/store-format.md) — the on-disk contract.
