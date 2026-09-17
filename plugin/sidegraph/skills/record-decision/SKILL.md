---
name: record-decision
description: Use when something durable should be written into Sidegraph memory — "record a gotcha", "remember this decision", "note this lesson", "запиши это", "we should not forget this", or when the Stop-hook capture nudge fires at session end. Also reachable directly as /sidegraph:record-decision. The authoring craft — pick the right kind, fill rejected (the highest-value field), anchor to what the decision is actually about, and choose the right write path: add_decision (human-asked, immediate) vs propose_decisions (agent-initiated, gated). A pure fact with no choice made and no rejected — a benchmark, a limit, something trial-learned — belongs to sidegraph:record-fact instead; a decision backed by evidence stays here, with the evidence attached.
---

# Record a decision

Turn what just happened into one durable, well-anchored memory record. The store is
append-only and every retrieval is budget-bounded and mistakes-first — so the craft is:
right write path, right `kind`, filled `rejected`, tight prose, on-target anchors.

## The one rule: who initiated decides the write path

- **The human explicitly asked to record this** ("record a gotcha: ...", "remember that
  we chose X") → **`add_decision`**. It lands `status=accepted` immediately, and that's
  correct: the asking human was the gate.
- **You (the agent) decided it's worth keeping** — including when the Stop-hook nudge
  ("Sidegraph: if this session produced a durable decision...") prompts you — →
  **`propose_decisions`**. It lands `status=proposed` (or `accepted` immediately under
  `SIDEGRAPH_AUTO_ACCEPT=on` — see [auto-accept's
  trade-off](../../../../docs/guides/capturing-decisions.md#4-auto-accept-opt-in) — or
  accepted at write time when an opt-in `SIDEGRAPH_RATIFY_POLICY` admits it, the result's
  `ratified_by` then reading `auto:<policy>`, so don't tell the human it awaits review),
  tagged `[unratified]` in retrieval, for a human to accept later via
  `sidegraph:ratify-decisions` / `sidegraph-ratify`.

**Never call `add_decision` on your own initiative.** Self-initiated writes always go
through the proposal gate — the same gate every other Sidegraph write path follows (a human
one by default).

## Field craft

Draft shape (`propose_decisions` drafts; `add_decision` takes the same fields as
parameters, minus `supersedes` — reversal on the accepted path is `supersede_decision`):
`{"title", "kind", "context", "choice", "rejected"?, "consequences"?,
"anchors": [{"name", "file_path", "relation"?}], "initiative"?, "supersedes"?, "tags"?,
"layer"?}`.

- **`kind` — pick by what it costs the next person:**
  - `gotcha` — a trap that costs time when you don't know it (ranked first in retrieval).
  - `lesson` — a root-caused mistake: what went wrong and why.
  - `constraint` — a hard external rule the code must respect.
  - `adr` — a deliberate architectural/design choice.

  `gotcha`/`lesson`/`constraint` all ride the **mistakes-first block** in retrieval —
  plain `adr` is the only kind that doesn't. Reserve `gotcha`/`lesson` for things that
  actually cost time or caused a mistake, and remember: downgrading noise *out* of the
  mistakes block means `adr`, not `constraint`.

- **`rejected` is the highest-value field** — what was tried, considered, or hit before
  landing on `choice`, and why it was abandoned. A decision with an empty `rejected` is a
  plain fact; one with a filled `rejected` is the durable memory this tool exists for.
  Before writing, ask yourself what the session actually ruled out — it's almost never
  nothing.

- **Keep `context`/`choice`/`consequences` short and concrete.** Retrieval budgets are
  measured in characters; a long record crowds out its neighbors in the same task context.
  `title`: one concrete line.

- **Optional axes:** `tags` (free text — slugified into durable `tag:<slug>` entities; a
  comma-separated string works too), `layer` (`"business"` | `"technical"`, a filter axis
  for mixed corpora), `initiative` (binds a Tier-0 initiative entity; derivable from the
  git branch in the propose path when omitted).

## Anchor discipline

Anchor to what the decision is **about**, not everything touched incidentally — weak
anchors resurface as noise in someone else's task context later.

- Each anchor is `{"name", "file_path", "relation"?}`, resolved against the current graph
  and bound at up to three tiers (leaf + community + initiative). `relation` is one of
  `creates`/`modifies`/`affects`/`deprecates`/`considered` (default `affects`); an invalid
  value fails before anything is written — the whole call on `add_decision`, that one
  draft (`rejected`, rest of the batch still writes) on `propose_decisions`.
- **Always pair the precise anchor with its containing file** — for code symbols and doc
  headings alike, not just ones you expect to churn: a rename/move orphans the precise
  anchor, the file anchor keeps the decision retrievable:
  `[{"name": "Vocabulary and Entity Relations", "file_path": "adr/ADR-001.md"},
  {"name": "ADR-001.md", "file_path": "adr/ADR-001.md"}]`. In the first store maintenance
  pass (2026-08-09), this one rule would have kept 7 of 10 fully-invisible records
  reachable.
- **Two name shapes look valid and never resolve.** A bare module-level constant
  (`MEMORY_GUARD_LINE`) — the engine indexes functions/classes/files, not constants;
  anchor the containing file or function and name the constant in prose. A path- or
  class-qualified name (`src/sidegraph/store.py`, `Store._touch_digest`) — the graph
  labels files by basename and methods bare; the `file_path` field carries the path,
  the `name` stays bare (`store.py`, `_touch_digest`).
- **Two targets resolve today and die later.** A file in another repository — this
  store's graph will never contain it; anchor the nearest in-repo consumer and name the
  foreign path in prose. An ephemeral process artifact (task brief, run report) — if the
  lesson outlives the wave, anchor what outlives the wave: the code or doc it is about.
- **Check `anchors_skipped` in the result.** An entry means that name matched more than
  one graph node (`candidates` lists up to 5) and no precise leaf was created. Fix an
  already-written record with `supersede_decision` carrying precise `{"name", "file_path"}`
  anchors — a repeat `add_decision` call would mint a duplicate record; for a
  still-proposed draft, drop it and re-propose corrected. Don't leave a decision
  effectively anchorless because of a silent skip.
- **Check `anchors_orphaned` too — it is the more common failure.** An entry means the name
  is **not in the graph at all**. The leaf is still written, but orphaned, and an orphaned
  binding is skipped by retrieval, by `drill_down` and by the PreToolUse nudge — the record
  has no delivery path through that anchor. It will never surface. **Read the `reason`
  before fixing anything — the causes need opposite actions:** `file-not-in-graph` usually
  means your graph is stale (run `graphify update .`, then re-anchor — the name was probably
  fine); `name-not-in-file` means the name is wrong (`find_entity`/`query_structure` will say
  what is really there); `no-file-path` means pass one. Repair with `add_anchors`
  (bindings-only, no duplicate record, no content-free supersession). Anchor to real symbols
  and real file paths — a feature name, a directory, a domain title or a heading you invented
  reads fine and resolves to nothing.
- No graph present? A `propose_decisions` draft still writes, with an orphaned leaf that
  heals once a graph appears (see `sidegraph:heal-anchors`) — but `add_decision` silently
  **drops** anchors when there is no graph (nothing minted, nothing to heal later). On the
  direct path, make sure the graph exists before recording, or use the propose path.
  **`add_fact` does not repeat this asymmetry** — see [Facts](#facts) below.

## Facts

Not every durable observation is a decision — some are non-derivable knowledge that
*informed* one. The scope razor (verbatim, same rule the write path itself enforces):

> Only facts the code graph cannot derive belong here: empirics (benchmarks, observed
> behavior), external constraints (API limits, library capabilities), trial-learned
> knowledge — never 'the code does X'.

"The code does X" is the engine's job, and it goes stale with every commit — a fact record
that just restates what Graphify already shows is exactly the north-star drift this razor
guards against. If retrieval could read it straight off the graph, it doesn't belong in the
store.

- **Attached vs standalone.** A fact that informed one decision in this same call rides that
  decision draft's own `facts` list — `DraftFact = {"statement", "source", "anchors"?,
  "supports"?}`. It always supports that decision and, absent its own `anchors`, inherits
  the decision's anchors — but bindings are minted on the fact's own id, so it's still its
  own record: ratifying the decision rides through to the fact (see
  `sidegraph:ratify-decisions`'s cascade), a fact that also supports another still-live
  decision survives a drop of this one, and a fact whose ONLY supporter is this decision is
  dropped along with it. A fact that doesn't attach to any decision drafted in this call goes
  in `propose_decisions`'s top-level `facts` parameter instead — standalone, and it needs at
  least one anchor or one `supports` id, or it would be unreachable and is rejected with a
  reason.

- **Same one-rule write-path split as decisions.** The human explicitly asked you to record
  a fact ("note that httpx has no built-in retry") → `add_fact` — lands `status=accepted`
  immediately, the asking human was the gate. You noticed it's worth keeping — including an
  attached fact discovered while drafting a decision → a draft's `facts` list or the
  top-level `facts` param on `propose_decisions` — `status=proposed` (or `accepted`
  immediately under `SIDEGRAPH_AUTO_ACCEPT=on`, or accepted at write time when an opt-in
  `SIDEGRAPH_RATIFY_POLICY` admits it — the result's `ratified_by` then reads
  `auto:<policy>`, so don't tell the human it awaits review), tagged `[unratified]` until a
  human ratifies it. **Never call `add_fact` on your own initiative.**

- **`statement` is 1-2 sentences, hard-compact; `source` is mandatory.** `source` is the
  epistemics — how you know it ("benchmark run 2026-07-09", "httpx docs", "trial-learned in
  session"). There's no default to fall back on: a fact with no source is unfalsifiable
  noise.

- **No-graph contrast with `add_decision`.** `add_decision` with no reader silently *drops*
  anchors — nothing minted, nothing to heal later. `add_fact` does the opposite: with no
  reader it still binds an ORPHANED Tier-2 leaf per anchor (the same propose-path behavior a
  no-graph `propose_decisions` draft gets), so a fact never writes unreachable — the binding
  heals once a graph exists (see `sidegraph:heal-anchors`). This holds on both the direct
  (`add_fact`) and propose paths.

- **Falsification is `supersede_fact`** — never a fresh, unrelated record. Same append-only
  discipline as `supersede_decision`: the predecessor closes (`valid_to` set, `status`
  flipped to `superseded`) in the same transaction the successor writes, with `supersedes`
  pointing back. Omit `anchors` to inherit the predecessor's bindings verbatim — including
  any `orphaned` ones, carried as-is; `supports` likewise defaults to the predecessor's own
  `supports` when omitted.

For the full fact-authoring craft — the scope razor as an entry gate, telling a fact apart
from a decision, and the discernment rules for attached vs. standalone — see
`sidegraph:record-fact`.

## Reversing an earlier decision

**Use `supersede_decision` (or `supersedes: <old_id>` in a draft) — never a fresh,
unrelated record.** The chain of "tried before, abandoned because..." is the product; the
store closes the predecessor (`status=superseded`, `valid_to` set) in the same
transaction, never deletes it. Anchoring on supersede: omit `anchors` and the replacement
inherits the predecessor's bindings verbatim (right when the reversal concerns the same
entities); pass `anchors` to re-anchor from scratch (the two are exclusive — passing
anchors replaces inheritance).

## Notes

- Redaction (AWS keys, tokens, `key=value` secrets, private-key blocks) runs automatically
  on every write path — the direct `add_decision`/`supersede_decision` calls included;
  their result carries a `redactions` count. It's a net, not permission: don't paste
  secrets into a draft.
- The propose pipeline dedups conservatively (same `kind` + canonicalized `title` on a
  shared anchor entity → `deduped`); when unsure it writes and lets the human drop it at
  ratification. Check the returned per-draft `status` (`written`/`deduped`/`rejected`) and
  `ratified_by` (non-null means a policy already accepted it).
- Exact signatures and return shapes:
  [`docs/reference/mcp-tools.md`](../../../../docs/reference/mcp-tools.md).

## See also

- `sidegraph:ratify-decisions` — where a proposed draft goes next.
- [`docs/guides/capturing-decisions.md`](../../../../docs/guides/capturing-decisions.md) —
  the full capture walkthrough, including the What/Why/Where/Learned mapping.
- [`docs/concepts/anchoring.md`](../../../../docs/concepts/anchoring.md) — tiers,
  multi-anchor, and the never-guess resolve ladder.
