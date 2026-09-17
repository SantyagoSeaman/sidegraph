---
name: record-fact
description: Use when a session surfaces a hard-won empirical finding that isn't a choice — "запомни факт", "зафиксируй цифру/лимит/результат бенчмарка", "record a fact", "we learned that X", "оказалось, что", "note this benchmark result", "the API caps at N requests", "turns out httpx doesn't retry", or any trial-learned/observed/external-limit knowledge with no `choice` and no `rejected` attached. Also reachable directly as /sidegraph:record-fact. The authoring craft — pass the scope razor first, tell a fact apart from a decision, attach vs. stand alone, fill `source`, and pick the right write path: add_fact (human-asked, immediate) vs. propose_decisions's facts (agent-initiated, gated).
---

# Record a fact

Turn a non-derivable, hard-won piece of knowledge into its own durable memory record —
distinct from a `Decision`, but built the same way: right write path, tight `statement`,
mandatory `source`, on-target anchors.

## The razor — check this before anything else

Before touching any tool, ask whether the code graph could answer this by being read. If
yes, don't store it at all — this is the entry gate, ahead of the write path:

> Only facts the code graph cannot derive belong here: empirics (benchmarks, observed
> behavior), external constraints (API limits, library capabilities), trial-learned
> knowledge — never 'the code does X'.

"The code does X" is Graphify's job, and it goes stale with every commit. If retrieval
could read it straight off the graph, it isn't a fact — it's noise that drifts from the
north star this razor guards.

## Decision vs. fact — tell them apart first

- **"We chose X because…"** — a choice was made among alternatives → a **decision**. Go
  to `sidegraph:record-decision` instead.
- **"It is true that X"** — no choice, no `rejected` — → a **fact**. This skill.
- **A fact discovered on the way to a decision in the same session** → don't write it
  standalone. It's an **attached** `DraftFact` inside that decision's own draft (its
  `facts` list) — see [Attached vs. standalone](#attached-vs-standalone) below.

## Field craft

- **`statement`** — the fact itself, 1-2 sentences, hard-compact. Retrieval budgets are
  measured in characters; a padded statement crowds out its neighbors in someone else's
  task context later.
- **`source` is mandatory and non-negotiable** — the epistemics, how you know it: a
  benchmark run with a date ("benchmark run 2026-07-09"), a docs page ("httpx docs,
  'Timeouts and retries' section"), or the session itself ("trial-learned in session").
  There is no default to fall back on — a fact with no source is unfalsifiable noise, and
  the store rejects an empty one outright.
- **`supports`** — decision ids this fact informed. Every id must already reference an
  existing `Decision` (any status) or the write is rejected before anything is committed —
  never guessed, never left dangling.
- **Always pair the precise anchor with its containing file** — code symbol or doc heading
  alike: a rename/move orphans one anchor, the file anchor keeps the fact retrievable.
  Never anchor to a bare module-level constant (the engine indexes functions/classes/
  files, not constants — anchor the file and name the constant in the statement), and
  keep `name` bare (`store.py`, `_touch_digest`) — `file_path`, not the name, carries
  the path. Cross-repo facts anchor to their nearest in-repo consumer (a foreign file
  never resolves here); ephemeral artifacts (task briefs, run reports) are not anchor
  targets — anchor what outlives the wave. Doc-corpus example:
  `[{"name": "Option C — Existing-contour execution with neutral result push (chosen)",
  "file_path": "ADR-001-dq-execution-and-triggering.md"},
  {"name": "ADR-001-dq-execution-and-triggering.md",
  "file_path": "ADR-001-dq-execution-and-triggering.md"}]`. Easy to miss: confirming the
  heading exists via `Grep` is not the same as passing it through as a second anchor — a
  live run defaulted to file-only for exactly this reason.

## Attached vs. standalone

- **Attached** — a `DraftFact` (`{"statement", "source", "anchors"?, "supports"?}`) nested
  inside a decision draft's own `facts` list, in the *same* `propose_decisions` call that
  writes the decision. It always `supports` that decision (plus anything else you name) and,
  absent its own `anchors`, **inherits the decision's anchors** — but the binding is minted
  on the fact's own id, so it's an independent record: a fact that also supports another
  still-live decision survives that decision being dropped; a fact whose *only* supporter
  is this decision is dropped along with it. Ratifying the decision rides through to the
  fact automatically — see `sidegraph:ratify-decisions`'s cascade.
- **Standalone** — a fact that doesn't attach to any decision drafted in this same call
  goes in `propose_decisions`'s top-level `facts` parameter instead. It needs **at least
  one anchor or one `supports` id** — omit both and it would be unreachable by any
  retrieval path, and the pipeline rejects it with the reason `"standalone fact needs at
  least one anchor or a supports id"` rather than writing a dead record.

## The one write-path rule — same one as decisions

This skill does not restate the rule — it's the same gate split (a human one by default)
`sidegraph:record-decision` documents, applied to facts instead of decisions: **the human
explicitly asked** ("note that httpx has no built-in retry") → `add_fact`, accepted
immediately, the asking human was the gate; **you (the agent) noticed it's worth
keeping** — including an attached fact discovered while drafting a decision → the draft's
own `facts` list, or the top-level `facts` param on `propose_decisions` — `status=proposed`
(or `accepted` immediately under `SIDEGRAPH_AUTO_ACCEPT=on` — see [auto-accept's
trade-off](../../../../docs/guides/capturing-decisions.md#4-auto-accept-opt-in) — or accepted
at write time when an opt-in `SIDEGRAPH_RATIFY_POLICY` admits it, the result's `ratified_by`
then reading `auto:<policy>`, so don't tell the human it awaits review) until a
human ratifies it. **Never call `add_fact` on your own initiative** — read the full
rationale in `sidegraph:record-decision` if the split is unclear; it doesn't change here.

## No-graph behavior

With no Graphify reader present, `add_fact` still binds each anchor as an **orphaned**
Tier-2 leaf rather than dropping it — the opposite of `add_decision`'s no-graph behavior,
which silently drops anchors when there's no reader. A fact never writes unreachable; the
orphaned binding heals once a graph exists (see `sidegraph:heal-anchors`).

## Falsification

The fact turned out wrong, or the world changed — never edit it, never write a fresh
contradicting fact next to the old one. Call `supersede_fact(old_fact_id, statement,
source, ...)`: the predecessor closes in the same transaction (`valid_to` set, `status`
flipped to `superseded`), never deleted, and stays retrievable as "believed before,
corrected because…". Omit `anchors` to inherit the predecessor's bindings verbatim —
*including* any `orphaned` ones, carried as-is; `supports` likewise defaults to the
predecessor's own `supports` when omitted. Passing `anchors` replaces inheritance outright,
it never adds to it.

## Notes

- **Check `anchors_skipped` in the result** — same discipline as `record-decision`. An
  entry means that anchor's name matched more than one graph node (`candidates` capped at
  5) and no precise leaf was created. Fix an already-**accepted** fact with `supersede_fact`
  carrying precise anchors — a repeat `add_fact` call would mint a duplicate record. For a
  **still-proposed** draft, don't use `supersede_fact`: it writes `status=accepted`/
  `provenance.source=human`, which would silently turn an ungated agent proposal into
  accepted memory — the same gate bypass this skill's write-path rule forbids. Instead
  drop it (`ratify(drop=[...])`) and re-propose corrected. Don't leave a fact effectively
  anchorless because of a silent skip.
- **Check `anchors_orphaned` too — same discipline, more common failure.** An entry means
  the name is **not in the graph at all**, so the leaf was bound `orphaned` and retrieval,
  `drill_down` and the PreToolUse nudge all skip it: the fact has no delivery path through
  that anchor and will never surface. Repair with `add_anchors` (bindings-only, no duplicate
  record, and it works on a proposed record without touching its status). **Read the
  `reason` first:** `file-not-in-graph` usually means a stale graph (run `graphify update .`
  and re-anchor — the name was probably fine), `name-not-in-file` means the name is wrong
  (`find_entity`/`query_structure` will say what is really there), `no-file-path` means pass
  one, and `no-graph` is the expected graph-less case described above — not an authoring
  mistake, and it heals on the next sync.
- **Check `redactions`** — every text field (`statement`/`source`) is redacted first, the
  same secret patterns `add_decision` runs; a non-zero count means something got scrubbed
  before it ever reached the store.
- Exact signatures and return shapes:
  [`docs/reference/mcp-tools.md`](../../../../docs/reference/mcp-tools.md#add_fact).

## See also

- `sidegraph:record-decision` — the write-path rule this skill shares, and where a fact
  discovered mid-decision actually belongs (attached, not standalone).
- `sidegraph:ratify-decisions` — facts ride their decision's cascade through this gate;
  standalone facts are ratified the same way, by their own id.
- [`docs/guides/capturing-decisions.md`](../../../../docs/guides/capturing-decisions.md#facts-the-evidence-layer) —
  the full attached/standalone/falsification walkthrough with worked examples.
- [`docs/reference/mcp-tools.md`](../../../../docs/reference/mcp-tools.md) — exact
  `add_fact`/`supersede_fact`/`propose_decisions` signatures and return shapes.
