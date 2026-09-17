---
name: explain-why
description: Use when someone asks why code or a design is the way it is — "why is this like this", "почему тут так", "who decided this", "what did we try before", "history of this symbol/file", "this looks wrong/legacy, can we clean it up" — or when you are about to conclude that existing code is a mistake. Also reachable directly as /sidegraph:explain-why. Routes the question to the decision store before grep or git archaeology: resolve the entity, walk its history newest-first including superseded records (the "tried and abandoned" chain IS the answer), and report current truth separately from how it got that way.
---

# Explain why

The store's third line of the north star — *decisions are how it got that way* — as a
read path. Someone is looking at code and asking a why-question; the answer, if captured,
is a chain of records, not a diff. Grep and `git log` reconstruct *what* changed;
this skill retrieves *why*, including the alternatives that were tried and thrown away —
the part no amount of reading the current code can derive.

**Run this before concluding existing code is wrong.** The single most expensive failure
mode this store exists to prevent is confidently re-proposing the design the team already
rejected. If the answer to "why is this so strange?" is a recorded constraint or a paid-for
lesson, finding it costs one tool call; not finding it costs re-living it.

## Resolve the question to a seed

Three shapes of why-question, three entry points:

- **A file or a few files** ("why is this module structured like this") →
  `query_decisions(files=["src/..."])` — the decisions/mistakes/facts half of
  `get_task_context`, no structural map. Use `get_task_context(files=[...])` instead when
  you also need to orient in the area.
- **A named symbol, class, or doc heading** ("why does `X` do Y") →
  `find_entity(name, file_path=...)` first. A single match returns `entity_id`; then
  `get_entity_history(entity_id)` — every decision **and** fact ever bound to that
  entity, newest first, *regardless of status*. Ambiguity comes back as `candidates`,
  never a guess — re-call with `file_path`.
- **An area or theme** ("why do we do payments this way") → `list_domains()` for the
  slug, then `drill_down(domain_slug)` — the domain's summary, member sample, and its
  decisions, mistakes first.

A record tagged `[drifted]` is anchored to code that changed after capture — verify it
against the current code before presenting it as current truth (and if it no longer
holds, that's `sidegraph:triage-drift`'s job). `[unratified]` means a human hasn't
accepted it yet — present it as a proposal, not settled memory.

## Walk the chain, don't stop at the newest record

`get_entity_history` returns superseded records too — that's the point. The answer to a
why-question has two layers, and the reader deserves both, clearly separated:

1. **Current truth** — the live (`accepted`) records: the standing decision, its
   constraints, its evidence facts.
2. **How it got that way** — the `supersedes` chain walked backwards: each superseded
   record is one "tried before, abandoned because…" step. A record's `rejected` field is
   the alternatives that were considered *within* one decision; the supersession chain is
   the alternatives that were *shipped and then reversed*. Both belong in the answer.

When history for an entity comes back empty but the question smells area-level, widen
once: `retrieve_decisions(include_superseded=True)` lists the whole store (mistakes
ranked first, superseded/rejected included) — scan titles for the topic before declaring
the store silent.

## Answer shape

Lead with the current answer, cite record ids, keep the layers separate:

- *Why it is this way now:* the live decision(s) — choice + the constraint/lesson that
  forced it. Quote `rejected` when it directly answers "why not the obvious way?".
- *What was tried before:* the superseded chain, oldest to newest, one line each — what
  was abandoned and why. This is the part that stops the re-proposal.
- *Caveats:* `[drifted]` / `[unratified]` flags, and any gap between the record and the
  code you just read.

## When the store is silent

Say so plainly — never pad a thin answer into a confident one. Then:

- Fall back to real archaeology: `git log -L`/`git blame` on the file, PR descriptions,
  linked issues. Report it as reconstruction, not memory — it tells you *what* and
  *when*, and only sometimes *why*.
- **A why-question the store couldn't answer is capture material.** Once the human
  reconstructs the actual reason ("oh, that's because the vendor API double-fires"),
  that's a decision or fact worth writing — hand off to `sidegraph:record-decision` /
  `sidegraph:record-fact` so the next asker gets it for one tool call.

## See also

- [`docs/guides/retrieval-in-sessions.md`](../../../../docs/guides/retrieval-in-sessions.md) —
  how to read the rendered sections and tags.
- [`docs/reference/mcp-tools.md`](../../../../docs/reference/mcp-tools.md#get_entity_history) —
  exact shapes of `find_entity` / `get_entity_history` / `drill_down`.
- `sidegraph:check-plan` — the same lookup run *before* work starts, plan-shaped.
- `sidegraph:triage-drift` — when the answer you found no longer matches the code.
