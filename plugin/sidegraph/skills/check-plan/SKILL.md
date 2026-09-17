---
name: check-plan
description: Use BEFORE designing or implementing anything non-trivial — entering plan mode, drafting a spec, "we're going to do X", "план такой", "давай сделаем X через Y", a refactor about to start — to ask memory what is already known against the plan. Also reachable directly as /sidegraph:check-plan. Pulls the touched area's rejected alternatives, constraints, and paid-for gotchas and returns a verdict: conflicts (the plan re-proposes something already tried and abandoned — with the record saying why), constraints the plan must respect, or a clean bill. The one-tool-call version of the review a long-tenured teammate used to give for free.
---

# Check a plan against memory

The most expensive failure this store exists to prevent: an agent (or a new teammate)
confidently proposing the design the team already tried, shipped, and reversed — because
nothing in the current code says "we were here." `SessionStart` and `PreToolUse` deliver
memory *while working*; this skill is the deliberate pull *before* the work is shaped,
when changing course still costs nothing.

Run it at the moment a plan exists in words but not yet in code: before writing a spec,
before `ExitPlanMode`, before the first commit of a refactor.

## 1. Extract the plan's touchpoints

From the plan (or the conversation), list what it would touch: files and directories,
named symbols/classes it changes or replaces, and the area it lives in. Include what the
plan would *delete or bypass* — that's where reversal records live.

## 2. Pull the memory, mistakes first

- **Primary:** `get_task_context(files=[...], entities=[{"name": ..., "file_path": ...}])`
  — mistakes first, decisions with evidence, then structure. This is the budget-bounded
  view an agent would get mid-work; you're pulling it early.
- **Area-level plans** ("rework how retrieval budgets work") → `list_domains()` +
  `drill_down(domain_slug)` — the domain's decisions, mistakes first.
- **The reversal hunt — the step ordinary retrieval doesn't do:** live records answer
  "what is true"; a plan needs "what was *abandoned*". Pull
  `retrieve_decisions(include_superseded=True)` and scan superseded/rejected records
  touching the same entities/area, plus every live record's `rejected` field. A match
  here is the highest-value finding this skill can produce: the plan's approach, already
  tried, with the abandonment reason attached.

## 3. Verdict — three buckets, cited by record id

- **Conflicts.** The plan re-proposes something a record says was tried and abandoned, or
  contradicts a live `constraint`. Quote the record's reason, not just its existence —
  "rejected because X" is what lets the human decide whether X still applies. **A conflict
  is a stop-and-discuss, not an auto-veto:** the recorded reason may have expired (the
  vendor limit lifted, the dependency replaced). The record's job is to force that
  conversation, not to win it.
- **Constraints & gotchas to carry.** Records that don't block the plan but must shape
  it — hard external rules (`constraint`), traps in the touched code (`gotcha`/`lesson`).
  One line each in the plan's own terms ("step 3 will hit: …").
- **Clean bill.** Nothing found — say so explicitly, and say what was checked (files,
  entities, domains), so silence is a statement about the store's coverage, not an
  unexamined gap. An empty answer on a heavily-worked area is itself information: the
  area's knowledge lives in heads, and this plan is about to make more of it.

Caveats ride along as usual: `[unratified]` findings are proposals (flag, don't treat as
settled), `[drifted]` findings need a glance at the current code before being cited as
blockers (`sidegraph:triage-drift` if they no longer hold).

## 4. Close the loop after the decision

Whatever the plan decides *against* a finding — "the constraint no longer applies
because…", "we'll take the rejected approach anyway because the context changed" — is
itself a decision with a ready-made `rejected` field. Capture it via
`sidegraph:record-decision` (propose path), superseding the record it overturns. A
check-plan run that changed the plan and wrote nothing back has done half its job.

## See also

- [`docs/guides/retrieval-in-sessions.md`](../../../../docs/guides/retrieval-in-sessions.md) —
  reading the rendered sections, tags, and budgets.
- `sidegraph:explain-why` — the same lookup shaped for a question about existing code
  rather than a plan.
- `sidegraph:record-decision` — writing back what the plan decided.
