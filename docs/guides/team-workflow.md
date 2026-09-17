# Running Sidegraph in a team

Sidegraph's core bet is that the decision store belongs in version control, next to the code
and docs it's about — not in a separate service, and not in any one person's session history.
The store is a **git-native log**: every `Decision`, `Domain`, `Entity`, and anchor set is a
small, independent text file, so it merges the same way your code does. There is no
binary-blob merge story to work around anymore — see [`reference/store-format.md`](../reference/store-format.md)
for the full on-disk layout.

## Commit the store

The store lives at `.sidegraph/` (the `SIDEGRAPH_DIR` default — see
[`reference/configuration.md`](../reference/configuration.md)), as a directory of small JSON
files: one per decision, one per domain, one per tracked entity, one per decision's anchor
set. Commit it like any other repo-owned artifact:

```bash
git add .sidegraph/
git commit -m "record: gotcha on Trader order-locking"
```

`.sidegraph/index.db` (the local, derived query index) is **not part of that commit** — the
store writes `.sidegraph/.gitignore` itself the first time it opens, so `index.db` and any
crash-debris temp files never show up in `git status` in the first place. It holds nothing a
teammate needs: everything in it is either recomputed from the committed files (a fresh
`git clone` rebuilds it on first open) or re-derived from the engine's graph on the next sync.
Losing it is a non-event.

The committed files under `decisions/`, `domains/`, `entities/`, `bindings/`, and
`initiatives/` **are** the team's memory. Every fact lives in one of them, append-only. Never
`.gitignore` those directories, and never treat them as disposable local cache the way you
might Graphify's `graph.json` — that one really is regenerated on every build; the decision
store is the opposite, deliberately durable, and is the one piece of Sidegraph's state that
must survive a laptop being wiped.

## Ratification as part of PR review

Because each decision is its own small file, a raw `git diff` on a branch that ratified
something is directly readable — a PR that adds `decisions/01J8Z....json` reads as exactly
what it is: a new gotcha, lesson, ADR, or constraint entering the team's memory, with its
`rejected` field right there in the diff for a reviewer to weigh in on before it's permanent.
That's the founding promise of moving off a single opaque database file: ratification is
**batched into the PR diff itself**, not hidden behind a tool you have to remember to run.

Two things still help beyond just reading the diff:

- **The file path is the signal.** Any PR touching `.sidegraph/decisions/*.json` or
  `.sidegraph/domains/*.json` is a PR that added, superseded, or (via a domain file) named an
  area of the system — worth a second look before reading the content, the same way a
  migration file in a diff draws attention.
- **Run the tools for anything still pending.** `sidegraph-ratify` (no flags) against a
  checkout of the branch lists everything still `proposed`, or call `list_proposed` /
  `retrieve_decisions` from an agent session. With no `--db` flag, `sidegraph-ratify` resolves
  the store via `$SIDEGRAPH_DIR` if set, else the deprecated `$SIDEGRAPH_DB` dispatch, else
  the default `.sidegraph/` — existing, or created fresh with a stderr warning (see
  [`reference/cli.md`](../reference/cli.md)) — so this "just works" from a checkout of the
  branch as long as the store lives at the recommended path. Review the `rejected` field
  especially — that's the part worth arguing about before it becomes permanent memory. Ratify
  (`--accept`) or drop (`--drop`) as part of the review, the same gesture as approving a code
  change, before merging.

Facts ride the same queue but in two shapes. An **attached** fact (named in a decision draft's
own `facts` list) never gets a ratify id of its own to remember — it's nested under its
decision in the listing as an indented `  evidence: ...` line, and rides that decision's
verdict automatically: one `--accept` on the decision accepts it and its nested evidence in
the same call; dropping the decision drops a fact only once every decision it supports is now
rejected (a fact still backing another live decision survives). A **standalone** fact
(proposed via `propose_decisions`'s top-level `facts` parameter — not `add_fact`, which lands
`accepted` immediately with no ratify hop, same rule as `add_decision`) is its own row under
`Facts:`, with its own id to `--accept`/`--drop` explicitly. `sidegraph-ratify --all` sweeps
every pending decision, domain, AND standalone fact in one pass; a nested fact is never listed
there too — its decision's cascade already covers it.

Treat an un-ratified proposal sitting in a PR the same as an open review comment: it's
visible (tagged `[unratified]`, see
[`guides/retrieval-in-sessions.md`](retrieval-in-sessions.md)) but not yet part of the team's
accepted memory until someone signs off.

## Two branches ratifying different decisions: merges silently

This is the case the file-per-record layout exists for. Branch A records a gotcha; branch B,
started from the same base and never seeing A's work, records an unrelated lesson. Both are
new files (`decisions/<ulid-a>.json`, `decisions/<ulid-b>.json`) — ULIDs make a filename
collision between two independently-authored records essentially impossible. Merging A and B
is an ordinary git merge with **no conflict**: both files land side by side. The next time
anyone opens the store (or the next `sidegraph-sync`/`get_task_context` call triggers the lazy
freshness check), the on-disk digest no longer matches what's indexed, the index reloads from
the committed files, and both decisions become retrievable — no extra command, no manual
reconciliation.

The same holds for two branches that each name a different domain, or each supersede a
different decision: different files, silent merge, absorbed on next open.

## When it isn't silent: a same-record dispute

The one case a merge doesn't resolve automatically is two branches disagreeing about the
**same** record — say, one branch accepts a proposed decision while another drops it (in the
file that reads `"status": "accepted"` vs `"status": "rejected"` — a dropped decision is
recorded as `rejected`; the word `dropped` is domain vocabulary). Both
changes land in the same `decisions/<ulid>.json` file, so git reports a real, ordinary text
conflict in that one small JSON file. This is exactly what should happen: it's a genuine
disagreement about team memory, and it deserves a human resolving it in the merge, reading
both sides and picking (or reconciling) the outcome — the same way you'd resolve a conflict in
any other source file. There is no silent "last write wins" here, and there never was one to
lose: the whole point of moving to file-per-record was making this kind of disagreement visible
instead of an unmergeable binary diff.

A rarer version of the same shape: two branches independently name a domain with the **same
slug**. Because they're different `domain_id`s, the files themselves don't conflict — both
merge in cleanly. `find_domain_by_slug` resolves the ambiguity deterministically in the
meantime (accepted beats proposed, and within a status the newest wins), so retrieval keeps
working either way. You don't have to notice this yourself, though: every `sidegraph-sync`
run recomputes this live and flags it for you — a `slug conflict: 'payments' held by 2 live
domains (...) — drop one` line in the sync report (note `sidegraph-sync` needs a readable
`graphify-out/graph.json` to run at all — it exits with `graph not readable` otherwise), and
the same warning on stderr the moment any tool opens the store fresh, graph or no graph. Nothing resolves it automatically (a live duplicate
stays flagged until you act on it), so treat it as something to resolve by hand — pick a
survivor and retire the other:

- `sidegraph-ratify --drop <loser-id>` flips the loser to `dropped`, freeing the slug back up.
  This works whether the loser is still `proposed` or already `accepted` — dropping an
  accepted domain is a legitimate, append-only-safe retirement (the file stays, only its
  status flips), unlike dropping an accepted *decision*, which the store deliberately never
  allows. This is the right tool for the common case: both duplicates independently reached
  `accepted`, and you just want one of them gone.
- `supersede_domain` (via the `supersede_domain` MCP tool) is the right tool only when the
  retiring domain's content should live on under a **different** slug — its successor can't
  reuse the SAME slug while the other duplicate is still live (both would then collide on
  it), so pick a distinct slug for the successor if you go this route.

## Compaction: team hygiene, not a merge fix

Once a decision or domain reaches a terminal status (`superseded`/`rejected`/`deprecated` for
decisions, `superseded`/`dropped` for domains), it can never change again — append-only rules
guarantee it. `sidegraph-compact` packs those closed records out of the individual-file
directories into an immutable `archive/<date>-<seq>-<hash12>.jsonl` segment, so a repo that's
been accumulating history for a year doesn't accumulate an ever-growing pile of small files
that no PR will ever touch again. See [`reference/store-format.md`](../reference/store-format.md#archive-segments-sidegraph-compact)
for the segment format and [`reference/cli.md`](../reference/cli.md#sidegraph-compact) for the
flags.

This is explicit, human-run maintenance — run it on the default branch periodically, not on
every commit, and never from sync/retrieval/ratify (nothing calls it for you). It's also
designed so it never causes the cross-branch pain the rest of this page is about: the segment
filename bakes in a content hash, so two branches compacting on the same day, archiving
different records, land two different filenames with no conflict; two branches that happen to
compact the exact same record produce byte-identical output, so that "collides" on the same
filename too, but the content is trivially identical, and the loader dedups any record ULID
seen in more than one segment. Compaction never removes anything — records *move* into an
archive segment, and stay retrievable exactly the way they were before.

## The onboarding effect

Because the store rides along with `git clone`, a new developer's very first Claude Code
session in the repo gets the accumulated gotchas for free — `sidegraph-session-start` injects
the global mistakes and communities map before they've touched a single file, and their first
`get_task_context` call on whatever they're assigned surfaces every mistake/lesson/constraint
anchored near it. There is no separate "read the wiki" onboarding step for this class of
knowledge; it's already in the loop the agent runs every session.

## Handling disagreement: supersede, don't delete

The store is append-only by design (see the invariants in the project's `CLAUDE.md`) — there
is no operation that hard-deletes a `Decision`. When the team disagrees with a past decision,
or a new decision reverses an old one:

- Use `supersede_decision` (or `supersedes` in a `propose_decisions` draft), never a fresh,
  unrelated record. The store closes the predecessor (`valid_to` set, `status=superseded`) in
  the same call that writes the replacement — both committed files land together.
- The old record stays retrievable via `get_entity_history` and the `~ tried, reverted 2026-01:
  ...` one-liners in `## Related` — "we tried this, here's why we stopped" is exactly the memory
  that keeps the next person from re-litigating a settled argument or re-discovering the same
  dead end.
- Dropping a still-`proposed` draft you disagree with (`sidegraph-ratify --drop`) is different
  from superseding: it rejects a *pending* proposal that never got accepted, versus reversing
  one that already had.

## Non-git corpora: works, but not committable this way

Sidegraph doesn't require the corpus to be a git repo — `GraphifyReader.graph_version()` falls
back to a `content:<hash>` of `graph.json` when there's no `built_at_commit` to read, so
change detection and sync still work on a plain folder of docs. What breaks is the
team-memory story above: with no repo, there's no natural "commit the store next to the code"
mechanism, no PR to ratify against, and no `git clone` onboarding path. The store still works
locally (add, retrieve, sync all function the same), but you're responsible for distributing
`.sidegraph/` some other way if more than one person needs the same memory.
