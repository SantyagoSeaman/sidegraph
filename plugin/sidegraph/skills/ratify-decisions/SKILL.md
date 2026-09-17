---
name: ratify-decisions
description: Use when pending Sidegraph proposals need human review — "review pending decisions", "what's waiting for ratification", "ratify the queue", "что там накопилось", right after a propose_decisions/propose_domains call, when SessionStart shows a "Sidegraph: N record(s) awaiting ratification (X decisions, Y facts, Z domains)" line, or when retrieval output shows [unratified] tags. Also reachable directly as /sidegraph:ratify-decisions. Walks the human through the pending queue (decisions, facts, and domains) item by item — present with a recommendation, wait for explicit verdicts, then one ratify call. A decision's nested evidence (its attached, still-proposed facts) rides that decision's own verdict, not a separate one. Never auto-accepts.
---

# Ratify decisions

The human gate of the capture loop: everything `propose_decisions` (decisions **and** their
attached or standalone facts), `propose_domains`, `add_domain`, and `sidegraph-import
--propose` wrote as `status=proposed` stays tagged `[unratified]` in every retrieval until a
human accepts or drops it here. You are the presenter and advisor; the human is the gate.
Under an opt-in `SIDEGRAPH_RATIFY_POLICY`, eligible drafts are accepted at write time
(stamped `auto:<policy>`) and never reach this queue; whatever the policy left `proposed`
still waits for the human here.

**HARD GATE — never call `ratify` before the human has given explicit per-item (or
explicit whole-batch) verdicts.** Silence, "looks reasonable", or your own confidence that
a draft is obviously good are NOT a verdict. Present, recommend, and wait — same
discipline as `sidegraph:name-domains`' pick gate. The one exception is a human who
already named the outcome in their request ("accept everything from the import",
"drop those two") — that IS the verdict; don't re-ask.

**Trigger: the `SessionStart` pending line.** Every session start, if the queue is
non-empty, `SessionStart`'s injected context ends with one line:
`Sidegraph: N record(s) awaiting ratification (X decisions, Y facts, Z domains) — review
with the ratify MCP tool or sidegraph-ratify.` That line exists so the queue is never
silently forgotten — treat it as a standing invitation to run this skill, not just a status
readout (`SIDEGRAPH_RATIFY_NUDGE=off` disables the line for a session that would rather not
see it).

## Flow

1. **Call `list_proposed()`** — renders every `status="proposed"` decision, fact, *and*
   domain human-readably, sectioned "Decisions:" (each block carries its still-proposed
   supporting facts nested underneath as `  evidence: <statement> [<source>]  (<id>)`
   lines — a preview of the cascade below), then "Facts:" (standalone facts — ones not
   nested under any decision above), then "Domains:" — each section printed only when
   non-empty. If it returns `No proposed decisions, facts, or domains pending
   ratification.`, report that and stop.

2. **Present the queue compactly, one line-block per item, with your recommendation.**
   For a decision: short id, `kind`, title, one-line what/why, whether `rejected` is
   filled, where it's anchored, and its nested evidence (if any) — flag that accepting/
   dropping the decision carries those facts with it. For a standalone fact: id,
   statement, source, and what it supports (if anything). For a domain: id, slug, title,
   and its membership rule — `list_proposed` prints `paths:`/`communities:` precisely so
   an over-broad rule is visible at this gate; flag one that looks like it would claim
   half the repo. Recommend one of **accept** / **drop** / **fix first**, each with a
   one-line reason. The quality bar (from [what makes a GOOD decision
   record](../../../../docs/guides/capturing-decisions.md#what-makes-a-good-decision-record)):

   - `rejected` filled — the highest-value field; empty `rejected` on a `lesson`/`gotcha`
     is a "fix first" flag, not an automatic drop.
   - Right `kind` — `gotcha`/`lesson`/`constraint` all ride the mistakes-first block in
     retrieval; recommend downgrading to `adr` anything that's a routine choice rather
     than a trap, hard rule, or paid-for mistake.
   - Anchors on-target — anchored to what the decision is *about*, not everything touched.
   - Short, concrete `context`/`choice`/`consequences` — retrieval is budget-bounded;
     a bloated draft crowds out neighbors.
   - A fact's `source` is filled and specific — an epistemics-free fact ("we found this
     out") isn't falsifiable later; recommend "fix first" over accepting one with no
     real source.

   Then **STOP and wait** for verdicts.

3. **On explicit verdicts, make ONE `ratify(accept=[...], drop=[...])` call** with all the
   ids you were given verdicts for — a decision id alone is enough to also cover its
   nested evidence (see [cascade](#facts-ride-their-decision-cascade) below); you don't
   need to separately list a fact the human already accepted/dropped via its decision.
   Routing is by lookup (decision first, then fact, then domain — never guessed); one bad
   id never aborts the rest (`"error: ..."` per id). Accepting a domain also mints its
   `domain:<slug>` entity and immediately rebuilds the `SessionStart` TOC cache; a
   decisions-only call leaves the cache untouched. Report the per-id results — including
   any cascaded fact entries the call reports back (see below).

## Facts ride their decision (cascade)

A fact attached to a decision (a draft's own `facts` list, or a `DraftFact` whose
`supports` names it) is its own record, but its ratification rides the decision's
verdict — one human gate, not two:

- **Accept a decision → every still-proposed fact that supports it accepts too.** These are the
  `  evidence: ...` lines `list_proposed`/the bare `sidegraph-ratify` listing already show
  nested under the decision — the queue previews exactly what one verdict will cover.
- **Drop a decision → a fact that supported ONLY that decision drops too** (→ `rejected`,
  append-only, same as any direct fact drop). A fact that also supports a still-live
  decision is left alone — not orphaned, just no longer riding this one's cascade.
- **The `ratify` result reports every record actually touched**, not just the ids you
  passed: a cascaded fact gets its own entry, `"accepted (evidence of <decision_id>)"` /
  `"dropped (evidence of <decision_id>)"`. Relay these to the human alongside the
  decision's own result line.
- **Standalone facts are their own queue rows** — presented and ratified by their own id,
  decision-style; there's no decision to cascade from.
- **`--all` (CLI) accepts standalone facts too** — but never double-counts a nested one: a
  fact already riding a decision's cascade is excluded from `--all`'s own id list (it
  would otherwise be processed twice). The in-session equivalent is simply not listing a
  nested fact's id yourself in step 3 above — its decision's id already covers it.

## Fixing a draft ("fix first")

There is no in-place edit for a proposal. Two clean paths:

- **Drop + re-propose** — `ratify(drop=[id])`, then `propose_decisions(drafts=[...])` with
  the corrected draft. Stays gated: the fixed version comes back through this same queue.
- **Drop + `add_decision`** — only when the human has explicitly dictated or approved the
  corrected text in this conversation. `add_decision` lands `status=accepted` immediately,
  and that's legitimate here: the human was the gate. Never use this path for a fix the
  human hasn't seen.

Don't "accept then supersede" as an edit — it writes a pointless supersession chain for a
record that was never valid in the first place.

## Drop semantics (they differ by kind)

- **Decision drop** requires `proposed` — flips to `rejected`, append-only (`valid_to`
  set, record kept, still retrievable as "considered and rejected"). An *accepted*
  decision is memory — you retire it with `supersede_decision`, never a drop.
- **Fact drop** — same rule as a decision, requires `proposed` — flips to `rejected`,
  append-only. An *accepted* fact is memory — falsify it with `supersede_fact`, never a
  drop (see `sidegraph:record-decision`'s Facts section). Dropping a fact directly (its
  own id) and a fact dropping via a decision's cascade land in the same state; only the
  reported reason string differs (`"dropped"` vs `"dropped (evidence of <decision_id>)"`).
- **Domain drop** works on `proposed` **or accepted** — flips to `dropped` (append-only).
  Retiring an accepted domain is legitimate (e.g. resolving a cross-branch slug conflict);
  see `sidegraph:manage-domains` for the domain-side judgment calls.

## Notes

- **Large batches: selective beats `--all`.** After a `sidegraph-domains bootstrap` or a
  bulk `--propose` import, accepting everything wholesale defeats the gate — see
  [why selective, not --all](../../../../docs/guides/naming-your-domains.md#why-selective-not---all).
  Present the batch grouped, recommend a subset, let the human widen it.
- **CLI parity:** `sidegraph-ratify` (no flags = the same pending listing;
  `--accept`/`--drop`/`--all`) is the terminal/PR-review equivalent — in a repo without a
  Sidegraph checkout, run it as `uvx --from
  git+https://github.com/SantyagoSeaman/sidegraph.git@main sidegraph-ratify`. Point the
  human there when they'd rather review outside the session.
- Proposed records are never invisible while they wait — they surface in retrieval tagged
  `[unratified]`. Ratification removes the tag; it doesn't make the record appear.

## See also

- `sidegraph:record-decision` — authoring the decision that ends up in this queue.
- `sidegraph:manage-domains` — domain-side judgment (rename, re-scope, slug conflicts).
- [`docs/guides/capturing-decisions.md`](../../../../docs/guides/capturing-decisions.md) —
  the full capture → ratify walkthrough this skill is the gate of.
