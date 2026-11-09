# Using the memory day to day

> **Start here:** Call `get_task_context` with your task files, then use the sections below to interpret the reply. [Glossary](../concepts/glossary.md).

Sidegraph shows up in two places during a normal session: an automatic map at session start,
and an on-demand, task-scoped pull whenever you ask the agent something that touches specific
code or docs. Ask for the relevant records before choosing an approach.

## What `SessionStart` injects

The session map gives a short orientation before work. A **domain** is a named area of the project. Accepted domains appear as a table of contents with their titles, mistake counts and subdomain counts. Before domains are named, the hook can show graph communities instead. A community is a cluster of related code or document nodes.

The standing instruction is:

> When you need to find or understand code in this project, call get_task_context(files=[…])
> with repo-relative paths before any grep or file search — decisions, gotchas and a domain map
> are indexed here. Before a non-trivial change, run the sidegraph check-plan skill if it is
> available. If the tool is listed only by name, load it first.

`Sidegraph X.Y.Z` identifies the running package. A pending-ratification line means someone must review proposed records. A drift line asks for a content check. Read other diagnostic lines using [troubleshooting](troubleshooting.md). None of these lines means the whole store is loaded into the agent's context.

Claude Code, Codex and Muse receive a session map through hooks. Grok Build receives its map through the MCP server's instructions. OpenCode uses explicit calls. If a map is absent, ask for task context directly and [verify the setup](verifying-your-setup.md).

The [delivery contract](../reference/retrieval-internals.md#what-sessionstart-injects) has the exact limits and switches.

## Memory without asking

On Claude Code and Grok Build, a supported file read or edit can deliver the file's most relevant records automatically. A subagent task brief can receive records for the files it names. Each file is delivered once per agent, so use `get_task_context` when you need the full context again.

Claude Code, Codex and Muse also introduce retrieval to a starting subagent through a short hook brief. That introduction contains no records. Grok's subagents share the parent's MCP instructions. OpenCode uses explicit tool calls.

These channels supplement the task lookup. If the automatic block is absent, ask for `get_task_context(files=["<your file>"])`. See the [host matrix](../integrations/support.md) to choose the setup page for your host.

The [delivery contract](../reference/retrieval-internals.md#memory-without-asking) has the exact limits and switches.

## Asking task-scoped questions so `get_task_context` fires with the right seeds

The map above is deliberately thin. The real payoff is asking the agent something scoped to
what you're about to touch, so it calls `get_task_context(files=[...], entities=[...])` with
seeds that resolve to the right part of the graph:

> I'm about to edit `trader/exec.py`'s order-placement path. What should I know first?
>
> I'm refactoring the `Trader` class. Anything I should be careful about?

- A **file seed** (`files=["trader/exec.py"]`) pulls in every anchorable node in that file
  (code symbols or doc headings).
- An **entity seed** (`entities=[{"name": "Trader", "file_path": "trader/exec.py"}]`) resolves
  through the same name+file matching `add_decision`'s anchors use.

A seed does not have to be spelled the way the graph spells it. `./trader/exec.py`,
`trader/exec.py:42`, `exec.py` and an absolute path inside the repository or any linked worktree
all read as the same file, a directory (`trader/`) reads as up to eight of its files, and `Trader.place` finds the
method with no file at all. Each such rewrite is a guess, and the reply says so in a
`## How your seeds were read` block. When a name could mean several files or symbols, the tool
lists them and reads none: pass the repo-relative path.

Vague, non-task questions ("what does this project do") don't give the tool anything to seed
on — be concrete about the file or symbol so the retrieval has something to resolve. A call with
no seeds at all says so, and names the domains you can drill into.

When an available graph does not contain an explicitly requested file, the memory read
also checks exact durable descriptor paths. Eligible decisions and facts can then arrive as
`[file-bound]`: declared applicability to that file, with symbol anchors the graph cannot
verify. This reads no neighboring path as an exact match and changes no anchor or binding.
Ordinary verified seed routes take precedence over the marker. Named-entity requests,
ambiguous/directory guesses and graph-present orphan handling keep their existing rules;
with no graph reader at all, this fallback is unavailable. The file still appears in the
not-in-graph diagnostic, and proposal policy, history, mistakes-first and budgets still apply.

## Reading the output blocks

`get_task_context` renders up to five Markdown sections, in this order, each one skipped
entirely if empty:

| Block | What it means |
|---|---|
| `## ⚠ Known mistakes & gotchas` | `gotcha`/`lesson`/`constraint` decisions bound directly to your seed entities, newest first. Read this first — these are the "someone already stepped on this" warnings. |
| `## Decisions` | Accepted `adr` decisions bound directly to your seed entities — accepted supporting facts may nest under them. |
| `## Known facts` | Accepted facts bound to seed/peripheral entities that were not already shown inline. |
| `## Related` | Accepted decisions reached indirectly, plus one-line superseded history for seed entities. |
| `## Unratified proposals` | Proposed decisions and facts, always after accepted memory and omitted by the proposal policy when required. |

For the structural map, call `query_structure` with the same seeds. `structure_budget` still
sizes the graph walk that finds related records.

If the final answer has no memory, it starts with `No context found.`, followed
by a block that says why. A seed that was read some other way than written gets a
`## How your seeds were read` line (guessed, normalised, a directory, ambiguous, or a name that
matches no symbol). If a file you passed is not in the code graph, a trailing
`## Not in the code graph` block says why: the graph is stale and needs a rebuild (`graphify update .` with the environment variable `GRAPHIFY_NO_AUTO_REFRESH=1` set), the
file is newer than the build or one the engine skips, the file exists only in another
checkout (a sibling worktree), or the path names no file in any checkout (see [`get_task_context`](../reference/mcp-tools.md#get_task_context)). A file seed
that has no current record of its own, but has anchored files near it, gets a `## Nearest anchored`
sentence naming them, and when the answer holds no decision memory at all the records of those
files follow under `## Nearest anchored records (not anchored to your files)` (see
[the nearest anchored records](../reference/mcp-tools.md#the-nearest-anchored-records)). When those
records follow an empty primary lookup, the answer starts: "No record is anchored to the files
you named; the records of the nearest anchored files follow." If the hard cap removes all of
them, it keeps "No context found." and the nearest-files sentence under `## Why this is empty`.
A call with no seeds gets a `## Why this is empty` block. With no
code graph at all, a trailing `## No code graph` block names the path that was looked at and says
to build it: the graph is missing, so memory anchored to code could not be looked up. In a
linked worktree the graph is the main checkout's, and a file that exists only on the branch is
reported as not in it, with advice to build a graph in this worktree; rebuilding the main
checkout's graph cannot add that file. For a file only in another checkout, start the session
in that checkout to read its graph and records.

Each decision line looks like:

```
- [gotcha] Use locks in Trader: lock around order placement (rejected: no lock, relied on GIL)
```

Proposed records use the final quarantine block instead of the accepted sections above.

## Facts: inline evidence, and the `## Known facts` block

A `Fact` you attached to a decision (its own `facts` list — see
[`guides/capturing-decisions.md#facts-the-evidence-layer`](capturing-decisions.md#facts-the-evidence-layer))
rides that decision's line after both records are accepted: the moment an accepted decision is
rendered in `## ⚠ Known mistakes & gotchas`, `## Decisions`, or as a `scope: global` line in
`## Related`, its accepted supporting facts render right under it, one indented line each. The
other `## Related` lines are short capped one-liners and carry no evidence. A proposed fact is collected
for `## Unratified proposals` instead. The one exception:
the `~ tried, reverted 2026-01: ...` superseded one-liners also shown under `## Related` never
get evidence — a reverted decision's evidence isn't useful at that tight, unbudgeted-for-detail
tier.

```
- [adr] Wrap HTTPX with an explicit retry policy for the exchange client: ...
  evidence: HTTPX HTTPTransport retries connection establishment errors, not HTTP 5xx responses. [HTTPX Transports documentation]
```

The mistakes-first guarantee holds here too — accepted evidence is spent from the same
character budget as
decisions, strictly after them, so a fact can never eat the budget a **mistake** line needed;
that's the one hard guarantee. It's narrower than "never displaces any decision," though:
within `## Decisions`/`## Related`, an inline evidence line CAN still crowd out a
lower-ranked decision in the same bucket under budget pressure — ordinary same-bucket ranking
noise, not a violation of anything, and the mistakes block is never exposed to it.

A fact that ISN'T attached to (or doesn't yet support) any decision that made it into the
output above still surfaces, standalone, in its own `## Known facts` block — rendered right
after `## Decisions`, ahead of `## Related`:

```
## Known facts
- fact: HTTPX HTTPTransport retries connection establishment errors, not HTTP 5xx responses. [HTTPX Transports documentation]
```

Only accepted facts appear here. A proposed standalone fact appears in the final unratified
block while the proposal policy allows it to surface.

Standalone and inline evidence lines show up to 240 characters of the statement and 300
characters of the source. A clipped part ends with `…`; if either part is clipped, exactly
one ` (clipped; full: list_facts(ids=["<id>"]))` note gives the call to read the whole fact.
The SessionStart map uses the same format when the store has no accepted domain.

## When `related`-via-community appears

A decision shows up under `## Related` instead of the direct blocks in two situations:

1. **Community fallback.** The decision itself is Tier-1-anchored to a Graphify community
   (`community:<id>`), and one of your seed nodes belongs to that same community — even
   though the decision isn't anchored to your exact seed entity. This is what keeps a
   decision alive and reachable when its leaf anchor degrades (ambiguous) or orphans, and
   it's also how a decision anchored to *ADR-A* can still surface while you're editing
   *ADR-B*, if both live in the same reference cluster.
2. **Peripheral entities.** Anything that shows up as a non-seed node in the budgeted
   structural subgraph (reachable from your seeds within the `structure_budget`) also
   contributes its own decisions to `## Related` — a breadth-first neighbourhood of your
   seeds, bounded by `structure_budget` (it can reach several hops on a sparse graph), not a
   full graph walk.

Global-scope decisions land in `## Related` too (after community/peripheral), so a
repo-wide constraint you already saw at session start can resurface here if it's relevant.

## Why an unratified draft still shows up (and what the tag means)

A `propose_decisions` draft writes with `status="proposed"` immediately (unless
`SIDEGRAPH_AUTO_ACCEPT=on`, in which case it lands `accepted` directly and carries no tag at
all — see
[`guides/capturing-decisions.md#4-auto-accept-opt-in`](capturing-decisions.md#4-auto-accept-opt-in))
— and while it is **inside the surfacing window** it remains visible, but quarantined.
`get_task_context` places it after every accepted-memory section:

```
## Unratified proposals
- [gotcha] Use locks in Trader: lock around order placement [unratified]
```

The section and tag both signal lower trust: a proposed decision can be useful immediately,
but it cannot outrank accepted memory. Treat it
as provisional until someone runs `sidegraph-ratify --accept` (see
[`guides/capturing-decisions.md`](capturing-decisions.md)). Once ratified, the tag is gone
and the line is indistinguishable from a manually recorded decision. A **dropped** proposal
(`--drop`) is rejected and excluded from every default listing, same as a superseded one.

**Two limits on that visibility** (both read-time policies — the store itself is untouched,
and neither ever removes a record):

- **The surfacing window.** A proposal older than `SIDEGRAPH_PROPOSAL_WINDOW_DAYS`
  (default 30; `0` disables) stops rendering as content on every surface. The point is the
  default state of a queue nobody reviews: an unreviewed draft should stop influencing
  sessions, not accumulate influence. It stays in `sidegraph-ratify`, stays in the
  SessionStart counter, and ratifying it later is the ordinary accept path.
- **Regulated mode.** `SIDEGRAPH_UNRATIFIED=off` withholds all proposed content
  regardless of age — for deployments where unreviewed text must never reach an agent.
  Pin it in the repository's committed `.claude/settings.json` `env` block so CI can
  assert it.

The SessionStart pending-ratification counter is exempt from both: it reports the queue's
size **and the oldest item's age** even when nothing in it renders, because a queue you
cannot see is exactly the one that rots. `sidegraph-doctor` adds the latency counterpart
(`time-to-ratify`) once records carry the ratifier stamp.

## When a drift warning remains after inspection

`[drifted]` requests a content review; a moved file alone does not invalidate a decision.
Use `sidegraph:triage-drift` to read the claim and current code. If the claim still holds,
`record_drift_check` can append a justified inspection of the current clean committed
files without changing the decision. If it no longer holds, follow the
[status-sensitive recovery](surviving-refactors.md#reading-and-healing-stale-decisions-warnings).
The [tool reference](../reference/mcp-tools.md#record_drift_check) describes refusal cases
and coverage. Changed inspected files or scope re-arm the warning, and for a markdown file
checked with declared sections only a change to a declared or also-watched section or to the
file's link definitions does; unrelated commits do not. The check is an attributed assertion and can be wrong. `sidegraph-doctor` groups the
records by drifted file and may note `anchored region unchanged; sibling sections of the file
may still matter`: that shortens the reading, it does not replace it.
