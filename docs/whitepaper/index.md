# Decision Memory for Coding Agents

**Engineering whitepaper — rev 6.0, 2026-09-15.** *Author:* Alexander Makeev
([github.com/SantyagoSeaman](https://github.com/SantyagoSeaman)).

## Abstract

An engineer who knows a project well does not carry its code in their head. They carry
the decisions made while writing it: which decision produced this piece of code, what
constraint forced that decision, what was tried and abandoned, and what else depends
on it. The code can always be reread. The chain of decisions that led to it cannot be
recovered from the code, and documentation almost never holds it either. For decades
teams carried those chains in people: each engineer held part of the picture, and the
team as a whole remembered.

Agentic development moves most implementation decisions out of the hands of the people
who carried those chains, and it puts nothing in their place. Most implementation
decisions now happen inside agent and subagent sessions that no human follows step by
step, so the people around a project stop accumulating its reasoning. The agents read
the same code an engineer reads, but they keep no decisions: each session starts empty
and discards what it learned when it ends. A project can keep growing while nobody,
human or agent, holds the chain of decisions behind it.

Sidegraph closes exactly that gap. It gives agents a memory of decisions that outlives
the session and reaches them when they are about to change the code those decisions
concern. As the work happens, it captures decisions, constraints, rejected
alternatives, and the evidence behind them, binds each decision to the code or the
area of the project it governs, and keeps the old record whenever a decision is
reversed. It then brings that memory to the next session at three moments: a map of
where knowledge lives when the session starts, the relevant records when the agent
asks for context on a task, and a reminder of what is anchored to a file when the
agent opens it. Before a design is written, it can also pull every record the planned
change would touch.

The result is the mental map of a project, the one that forms in people's heads, made
available to agents so that they can work as effectively as the people who carry it.
Each session still starts fresh, but it no longer starts as a newcomer.

## 1. Where project reasoning used to live

An experienced engineer opens the next ticket already knowing a great deal about the
code it touches: the payment path depends on a workaround, the obvious queue design
failed under load last year, and a harmless-looking helper enforces a limit agreed
with another team. They may still need to look up the details, but they know there is
something to look up and which questions are already settled. What they carry is not
the code, which they reread as needed, but the decisions made while writing it. That
knowledge makes them fast, and it keeps them from breaking a chain of consequences
they cannot see in the diff.

Very little of that knowledge is written down. [Architecture decision
records](https://cognitect.com/blog/2011/11/15/documenting-architecture-decisions)
(ADRs), specifications, and design documents capture the large choices. They describe
the intended system and, at best, the few trade-offs someone thought worth a document.
The route from plan to working code runs through hundreds of smaller decisions: the
constraint discovered during implementation, the approach abandoned after a failing
benchmark, the reviewer who reversed an earlier choice. Those decisions shape the code
as much as the large ones, and the finished code keeps only their result.

Teams cope with this in three ways, and each has a ceiling.

- **Write an ADR for everything.** A record at the granularity of every real decision
  would capture the chain, but at that granularity the writing cost buries the team.
  In practice ADRs cover the handful of choices that justify a meeting.
- **Keep it in people's heads.** This works, and it is what most teams actually do. It
  lasts as long as the people who followed each decision stay on the project and stay
  involved.
- **Record intermediate decisions in tickets and review threads.** This works
  partially. It preserves some of the reasoning, but mixes it with discussion and
  attaches it to diffs that are later rewritten, so recovering it depends on someone
  remembering the right thread, a discipline few teams sustain.

All three work as long as a project is carried by people who each followed part of its
history. Nobody holds the whole picture, but the picture exists, spread across the
team, and a question about why the code looks the way it does usually finds someone
who remembers.

## 2. What agentic development removes

Agentic development breaks this arrangement twice over: the people stop collecting the
reasoning, and the agents that replace them keep none of it.

**The people stop accumulating the reasoning.** Most of the route from plan to merged
code now runs through coding agents and the subagents they start. A person reads the
specification and perhaps reviews the final change, but most decisions are made
between those two moments, so the reasoning never passes through anyone who could
remember it. The picture that used to form across a team no longer forms.

**The agents do not keep it either.** An agent reads the same code an engineer reads,
but the model itself retains nothing between sessions. The tool that runs the agent
may reload an instructions file or its own notes at the start of the next session, but
neither is a chain of decisions tied to the code, and the context window is in any
case far too small to hold a project's history. During a task it may discover exactly
the things an experienced engineer would carry forward: that an approach was abandoned
after an incident, or that a constraint came from a customer agreement rather than
from the code. When the session ends, that understanding is discarded. The project
keeps the diff and loses the route that produced it.

The result is a project in which nobody holds the chain any more. Its most visible
symptom is going in circles. A new session proposes the design a previous session
already tried and abandoned, because nothing in the current code says "we were here".
Its less visible symptom is quiet breakage: a change that is locally correct violates
a constraint whose reason was never recorded where the next agent would see it.

It is tempting to think that search already closes this gap, but on its own it cannot.
Search finds only what was written down, and the route usually was not. Even where the
route was written down, storage alone does not help. An agent has to know that a
relevant record exists, pick it out of a corpus too large to load, and see it before
repeating the mistake. Sidegraph's own measurements bear this out: records that sat
readable in the repository, with nothing pointing agents to them, went unopened
(Section 8.2). Better models and larger context windows do not remove the need to
record the route either. A model can only reason over what it is given, and even what
fits into a long context is used unevenly, as the [Lost in the
Middle](https://aclanthology.org/2024.tacl-1.9/) study showed. Project memory has to
be written down, and then it has to meet the agent at the point of work.

## 3. What a team can do with it

What an agent lacks, then, is not the code, which it reads as well as anyone, but the
decisions an engineer would remember about it. Sidegraph supplies that memory through
five capabilities. All five serve the same two needs: a record has to outlive the
session that produced it, and it has to reach the next session at the point where that
session does its work.

1. **The decision process becomes a durable artifact.** Decisions, constraints,
   lessons, and the evidence behind them are captured while the work happens. The
   session that did the work drafts them before it ends, instead of someone who was
   not there reconstructing them later. Every record is a small file committed beside
   the code.
2. **The chain becomes walkable in both directions.** From a piece of code, an agent
   or engineer can reach the decisions anchored to it, the constraints that forced
   those decisions, the alternatives that were rejected and why, and the other records
   that depend on the same code. From a decision, they can walk back through every
   earlier version it replaced.
3. **The project can stop going in circles.** A rejected alternative travels inside
   the record of the decision that rejected it, together with the reason, and arrives
   whenever that record is delivered. The abandoned approach can reach the next
   session before that session rebuilds it.
4. **Memory tells you when it has gone stale.** When a commit changes a file a record
   is anchored to, the record is marked as drifted wherever it is delivered. When the
   code a record points at can no longer be found, or matches more than one candidate,
   the binding is marked orphaned or degraded instead of silently moving to the
   nearest similar symbol.
5. **It rides beside the workflow a team already runs.** Sidegraph hooks into the
   agent host, the program that runs the coding agent ([Claude
   Code](https://docs.claude.com/en/docs/claude-code) is one such host). It does not
   hook into any particular planning or specification process, so it works beside
   spec-driven workflows instead of replacing them.

Together these give an agent what an experienced engineer carries between tasks: not
the code, but the decisions behind it. The model resets between sessions. The
project's decisions do not.

## 4. Related work: what exists nearby

Several established approaches preserve part of what an engineer remembers, and they
differ mainly in the unit they keep: a conversation, a document, a successful episode,
or a project decision. The last of these has the oldest pedigree. Design-rationale
research of the late 1980s and 1990s, from
[gIBIS](https://doi.org/10.1145/58566.59297) to [Questions, Options, and
Criteria](https://doi.org/10.1207/s15327051hci0603%264_2), defined the unit as the
question faced, the options weighed, the criteria that decided, and the options set
aside, and [a survey of that field](https://doi.org/10.1109/64.592267) named the
reason such records were seldom kept: writing them cost the designer effort at the
moment of design. Each approach below inherits part of that unit. Sidegraph faces the
same capture problem, and answers it by capturing while the work happens rather than
afterwards.

**Review threads, issues, and commit messages** are the informal record Section 1
described: genuine rationale, mixed with the discussion around it and attached to
diffs that are later rewritten. They stop at retrieval. Nothing leads from the current
code back to the exchange that shaped it, so the history is there and the next task
never meets it.

**Project instruction files and plain retrieval** are the everyday practice closest to
what this paper describes. A file such as [AGENTS.md](https://agents.md) or
[CLAUDE.md](https://docs.claude.com/en/docs/claude-code/memory) lives with the project
and the agent host loads it into every session, while search over the repository
answers whatever the agent thinks to ask. Both are project-owned, both arrive at the
point of work, and Section 8.3 measures how strongly sessions lean on such a file.
They stop where the written guidance stops. An instruction file holds what someone
thought to write down as guidance, follows no symbol through a refactor, carries no
validity or trust state, and says nothing when it goes stale, and search finds only
what was written in the first place. Hosts also keep notes of their own between
sessions, but Claude Code, for example, stores them in the developer's home directory
rather than in the repository, so a teammate's session never sees them.

**Architecture decision records** preserve choices and rejected alternatives
deliberately, and Sidegraph's records descend from them. Supplying prior ADRs to a
model improves its drafting of the next one, and the [Context
Matters](https://arxiv.org/abs/2604.03826) study found that the few most recent help
nearly as much as the full history: when the task is to write the next decision, the
recent stretch of the path carries most of the signal. Sidegraph keeps the whole path
for a different task: explaining, at a given piece of code, why it is the way it is,
when the decision that shaped it may be old and long superseded. Where ADRs stop is
discovery. A record helps only when an agent knows it exists and can relate it to the
current task and code, and an ADR folder offers no path from a function to the record
that shaped it. A team with a disciplined practice may close that gap by habit; the
experiment in Section 8.2 measured the bare condition, in which the records exist and
nothing announces them.

**Session-memory systems**, including [MemGPT](https://arxiv.org/abs/2310.08560),
[Mem0](https://arxiv.org/abs/2504.19413), and
[A-MEM](https://arxiv.org/abs/2502.12110), keep an assistant's own experience: paged
conversational context, a user's salient facts and preferences, or, in A-MEM, a
network of notes that the agent itself links and rewrites. Their unit is the
assistant's interaction rather than a project decision bound to the code it governs.
They give an assistant continuity. Sidegraph gives the project continuity, shared by
every engineer and agent that works on it.

**Experience-based systems** preserve another unit.
[Reflexion](https://arxiv.org/abs/2303.11366) carries an agent's own reflection on
task feedback into its next attempt. [MemCoder](https://arxiv.org/abs/2603.13258)
mines project history for verified solutions and intent-to-code mappings.
[Memento](https://arxiv.org/abs/2508.16153) and [Contextual Experience
Replay](https://arxiv.org/abs/2506.06698) store an agent's past episodes and what they
taught, so that its later actions improve. Their unit is the episode, and their
purpose is a better next action. Sidegraph carries the decision path, including failed
and rejected alternatives, across people and agents, and its unit of reuse is project
rationale rather than a completed solution.

**Procedural memory**, such as [Agent Workflow
Memory](https://arxiv.org/abs/2409.07429), induces reusable routines from experience:
how to deploy, how to repair a class of failure. It keeps how to act. A decision
record keeps something else: why a choice is valid here, and which tempting paths were
rejected. A retrieved decision can select or constrain a procedure, but it is not one.

**Codified context**, described in Vasilopoulos's report [Codified
Context](https://arxiv.org/abs/2602.20478) from a large C# codebase, takes the
instruction file furthest: a tiered infrastructure of a standing constitution,
specialist-agent instructions, and on-demand specification documents, with retrieval
hooks and drift detection. It overlaps with Sidegraph on repository-owned context,
tiered delivery, and memory of repeated failures. Where it stops is the binding
between a record and a piece of code. Its unit is the curated document, written for
the repository as a whole, so a document can say that a rule exists but cannot tell
the agent that the function it is about to edit is the one the rule protects, and
nothing gates what enters a document the way ratification gates a record.
[ESAA](https://arxiv.org/abs/2602.23193) applies [event
sourcing](https://learn.microsoft.com/en-us/azure/architecture/patterns/event-sourcing)
to what agents may do to project state: every intention is validated, appended to a
log, and replayed deterministically. Sidegraph shares the append-only discipline but
governs a different thing: which reasoning the repository owns and when it reaches a
session.

**[PROJECTMEM](https://arxiv.org/abs/2606.12329)** is the nearest neighbor. Its memory
belongs to the project: it is local-first and append-only, it is delivered within a
budget, and it deliberately preserves failed attempts. It stops at the same binding.
Its records are not bound to a function or module, so nothing re-checks them when the
code changes, and nothing marks the moment a record's code has moved or disappeared.
Its gate also stands in a different place. It warns the agent before an action about
what was tried before, whereas Sidegraph's gate stands between an agent's draft and
the durable record, with a person at it by default. A Sidegraph project can hand that
gate to a deterministic policy for some kinds of record, and every record accepted
that way carries the policy's name in its file.

Each of these keeps one piece of what an engineer remembers. None of them, as far as
their own descriptions go, keeps the combination an engineer's memory actually has:
decisions that belong to the project, anchored to the code they govern, and delivered
to the next session at the point of work. Remove any one of the three and what remains
already exists: decisions without anchors are an ADR folder, anchors without decisions
are graph metadata, and delivery without project ownership is session memory. The
combination is what makes the memory a project's rather than an assistant's.

## 5. One decision chain, walked end to end

The clearest way to see these capabilities is to follow one method in Sidegraph's own
code. Every record, date, and quotation below comes from the store of the repository
that builds Sidegraph, or from a design document that store cites, and the sessions
were run by Sidegraph's author.

### 5.1 Starting from the code

The method `Store.ratify` in `src/sidegraph/store.py` turns a proposed record into
accepted memory. An agent about to change it asks for task context by naming the
method. With the default budget, Sidegraph returns the records anchored to that
method, with mistakes, meaning lessons and gotchas, and constraints first.
Abbreviated, the first lines of the answer are:

```text
## ⚠ Known mistakes & gotchas
- [lesson] [drifted] A zero-arg test double is an accidental tripwire — never shape
  production code to preserve its arity: …
- [constraint] LLM analysis over the memory layer stays read-only and ratify-gated:
  … ratification remains the single door into memory for anything LLM-produced. …
- [constraint] Gate ratification through CLI/MCP only, with append-only accept/drop: …

## Decisions
- [adr] [drifted] Ratification is a policy: SIDEGRAPH_RATIFY_POLICY=manual|auto-low-risk|
  auto-all (default manual); auto reaches accepted only through the existing ratify
  transitions, stamped auto:<policy>: …
```

Six records in the store are anchored to this one method: two constraints, a lesson,
and three architecture decisions. Four of them appear in the response above. The
default budget did not reach a smaller accepted decision about how the method records
who ratified a record, and a draft that was dropped at review survives only as history
and is not delivered at all. The entity-history tool lists all six. None of them is
visible in the method's source, and while an engineer who had followed the project
would know most of them, a new session learns them before editing a line.

Two of the records carry a `[drifted]` marker, because the ratification code changed
after they were captured. The marker does not say the records are wrong. It says
someone should check them against the current code before relying on them. Section 6.6
explains how the marker is produced and what it cannot detect.

### 5.2 Walking back through the history

The policy decision belongs to a line of records about one question: what should
happen to a record an agent proposed but nobody has reviewed yet? Following each
record's `supersedes` link backward gives two chains that grew side by side, listed
below by date. One decides what agents see of an unreviewed proposal. The other
decides when a proposal may become accepted without a person.

| Date | Chain | Decision | Status now | What it rejected |
|---|---|---|---|---|
| 2026-07-11 | acceptance | An opt-in switch lets a solo developer auto-accept agent proposals. | superseded | "A TTL auto-promote (proposals silently becoming accepted after a time window) … would silently legitimize noise rather than surface it." |
| 2026-07-11 | visibility | Unreviewed proposals are shown to agents, tagged `[unratified]`, and ranked like any other record. | superseded | Hiding them, which would defeat the point of surfacing mistakes early. |
| 2026-08-02 | visibility | Unreviewed proposals are still shown, but ranked after all accepted memory. | superseded | Letting an unreviewed draft outrank reviewed memory. |
| 2026-08-04 | visibility | Unreviewed proposals stop being shown after 30 days, or at once in a regulated mode. | accepted | Keeping "never withheld" as a rule. It also repeats the July rejection of time-based promotion, citing that record: "this change withholds, it never legitimizes." |
| 2026-09-13 | acceptance | A project may configure a deterministic policy that accepts some record kinds without a person and stamps the policy's name on each. | accepted | Among five alternatives: "TTL/survive-N-days auto-promote — rejected outright (prior record … stands)." |

None of this history was lost when a decision changed. Each superseded record still
says what was believed at the time and why it stopped holding. The August 4 record,
for instance, explains what changed its predecessor. A practitioner panel had pointed
out that a neglected queue is an unreviewed channel into every agent's context, and
the author confirmed from experience that the project's own reviews had been
perfunctory. That is the kind of reason that never reaches the code.

### 5.3 The circle that did not happen

In September the project set out to let unattended sessions accept some records
without a person. The July switch already did something similar for a solo developer,
but it accepted every proposal and recorded nothing about what had accepted it, and
teams needed something narrower that an audit could see. The obvious design was one
the July record had already considered and rejected: let a proposal become accepted
once it has waited long enough. It is simple, needs no new checks, and looks harmless.

Before writing the design, the session ran Sidegraph's plan check, which pulls every
record a planned change would touch, including superseded and rejected ones. It
returned the July and August rejections of time-based promotion, the constraint that
ratification remains "the single door into memory for anything LLM-produced", and a
lesson that proposed domain rules need mechanical checks. That lesson later became one
of the policy's gates. The design document answers each of these records in a table,
where time-based promotion appears as "not an open option". The single-door constraint
was reinterpreted rather than ignored. Every record an agent produces still passes a
ratification step before it becomes memory. The policy changes only who performs that
step: a person by default, or a deterministic policy that a project configures and
that stamps its name on every record it accepts.

When the policy shipped, its decision record carried the rejection forward and cited
the August record. A later session that reaches for the same idea now meets three
records, from July, August, and September, each with its own reason.

What one episode can show is the design session reading the earlier reasoning before
choosing anything, and answering each record in writing.

In this case memory arrived because the design session asked for it, but Sidegraph
also pushes memory to the agent, as the session-start map and a reminder when the
agent opens a file (Section 6.3).

## 6. How it works

The walk-through relied on three mechanisms: a record that survives the session, an
anchor that ties it to code, and a delivery path that brings it back. Around them sit
capture, which creates records, trust, which decides how far to rely on them, and
upkeep, which keeps them current.

### 6.1 The record

A record is a small JSON file in the repository's `.sidegraph/` directory. A decision
record stores the choice and the context that forced it, the alternatives that were
rejected and why, the consequences, a validity period, and a link to the record it
supersedes. It also carries provenance: where it came from, which session produced it,
and who or what accepted it. Decisions come in four kinds: an `adr` for an ordinary
architectural choice, a `constraint` for a rule imposed from outside the code, a
`lesson` for something the project paid to learn, and a `gotcha` for a trap in the
code. Constraints, lessons, and gotchas are ranked ahead of ordinary decisions
whenever memory is delivered.

A separate `fact` record keeps a sourced observation, measurement, or external limit.
Because a fact can support several decisions, a changed decision does not erase the
evidence behind it. Named domains, such as "Capture" or "Retrieval", are records too,
and they supply the human names used in the session-start map.

The August 4 record from Section 5.2 shows the shape, shortened:

```json
{
  "kind": "adr",
  "title": "Proposed records are quarantined AND can stop surfacing entirely — by age or by regulated mode",
  "status": "accepted",
  "supersedes": "01KZ27ZQXHAP8J8TYSAPSHSMT3",
  "rejected": "Keeping 'never withheld' as an invariant — rejected by the panel's blocker … Auto-deleting stale proposals — rejected: violates append-only. …",
  "valid_from": "2026-08-04T17:13:46Z",
  "valid_to": null
}
```

Its `rejected` field is where the reasons from that table live, and its `supersedes`
link is what makes the chain walkable. No record is ever deleted, and once a record is
accepted, its account of the decision is never rewritten. This is the discipline that
[event
sourcing](https://learn.microsoft.com/en-us/azure/architecture/patterns/event-sourcing)
applies to state. Acceptance adds who or what accepted it and when, and after that
only its status and the end of its validity change. Reversing a decision closes the
old record's validity and writes a successor that names it, while the old record
stays, because it still explains why the code has its current shape and why the
obvious alternative is not in it.

The records themselves are files rather than rows in a service. They appear in
pull-request diffs, travel with every clone, and merge like source code. Branches that
add different records never contend for one database file. A local
[SQLite](https://sqlite.org) index is rebuilt from the files for fast queries and can
be deleted at any time.

### 6.2 The anchor

A record is useful only while it stays attached to what it describes, so Sidegraph
binds it to an entity: a function, class, module, or file, or an area of the project,
either a named domain or a computed community. Each entity has an internal id that is
minted once and never reused. With [Graphify](https://github.com/safishamsi/graphify),
the optional code-graph engine that maps a project's functions, classes, and files,
Sidegraph also stores the entity's current name, file path, and graph node beside that
id, so names and locations can change without breaking the records attached to them.
Without Graphify, records still anchor to file paths and domains, and only
symbol-level anchors are lost.

A record usually carries anchors at several levels. Leaf anchors point at concrete
code: a function, a class, a module, a file. Broader anchors point at an area of the
project, either a domain, which people name, or a community, a cluster of related code
that the code graph computes on its own. Beside the anchors, tags and initiatives
group records by theme or by a piece of planned work. They are not bound to code, but
they are one more way a record can be found. The policy decision in Section 5, for
example, is anchored to five pieces of code and two named domains and carries three
tags. If a leaf anchor breaks, the broader anchors and the groupings keep the record
reachable.

Matching code entities across versions is a studied problem, with tree differencing
such as [GumTree](https://hal.archives-ouvertes.fr/hal-01054552/file/main.pdf) and
entity matchers such as [ReMapper](https://doi.org/10.1109/ASE56229.2023.00132)
solving the pairwise case. Sidegraph asks for less, one stable address per entity over
the life of the repository, and answers with a deliberately conservative ladder. When
the code graph is rebuilt, each concrete entity is resolved again through it:

1. **Exact:** the same name in the same file resolves to one node, and the binding
   stays live.
2. **Ambiguous:** the same name in the same file matches more than one node, so the
   binding is marked degraded and the entity keeps the mapping it had before the
   rebuild.
3. **Moved:** the old file is gone from the checkout and the same name resolves to
   exactly one node in a new file of the same file type, so the entity follows the
   file and the binding stays live.
4. **Orphaned:** nothing qualifies, and the binding is marked orphaned.

The ladder never guesses. Suppose a function is renamed and its file stays where it
was. The exact rung fails because the old name is gone, and the moved rung does not
apply because the old file still exists, so the binding becomes orphaned. A function
with the old name in some other file cannot rescue it, because only the moved rung
looks at other files, and only once the old file is gone. If the rest of the code in
that file belongs to one community, Sidegraph keeps the record reachable through that
community, and if the code is spread across several, it does not pick one. The
fallback widens a record's reach to an area but never attaches it to a different
symbol. A person then restores the binding or supersedes the record with fresh
anchors. Missing context is visible and can be repaired, whereas context attached to
the wrong code would mislead every later session, which is why the system refuses to
create it.

### 6.3 Delivery

During work, delivery narrows the project's memory in three stages, following the way
an experienced engineer moves from the whole system to one piece of code.

1. **Session start:** a hook that the agent host runs as the session begins injects
   the session-start map: the named domains, a one-line summary of each, and the
   number of recorded mistakes each holds. The map shows where knowledge exists
   without loading it.
2. **Task:** the agent calls a retrieval tool, `get_task_context`, served over the
   [Model Context Protocol](https://modelcontextprotocol.io) (MCP), with the files and
   symbols the task touches. Records anchored directly to them arrive in detail, and
   records related through a shared domain or tag arrive in compact form. Constraints,
   lessons, and gotchas come first, and the whole response fits a fixed budget, by
   default 6,000 characters of memory and 4,000 for a short map of the named entities
   and their neighbours in the code graph.
3. **File contact:** the first time the agent reads or searches a source file in a
   session, a hook adds a one-line note suggesting retrieval, and when memory is
   anchored to that file, a second note names it. Each kind of note appears at most
   once per session. The hook observes every read, search, edit, and write, but it
   adds notes only on reads and searches.

The first and third stages happen without the agent asking. The second depends on the
agent calling the tool, which it can skip. That choice is the largest gap in delivery,
and Section 8.3 reports how large it was and what narrowed it.

All three stages were measured on Claude Code. [Codex
CLI](https://developers.openai.com/codex/cli) runs the session-start and session-end
hooks, but its hook events do not yet cover file reads, so the third stage does not
exist there.

One more path serves planning rather than work: the plan check that Section 5.3 showed
in use. It takes the change a design intends and sorts every record it finds into one
of three answers. A conflict means a record already rejected what the plan proposes. A
constraint is a rule the plan must respect. And when nothing applies it says so
explicitly, so that silence is never mistaken for consent.

### 6.4 Capture

Records reach the store in three ways.

In **direct capture**, a person asks the agent during work to record an architecture
decision, a lesson, a constraint, a gotcha, or a fact, and says what it applies to.
The record is written as accepted because a person asked for it, even if that person
never read the agent's final wording.

**Proposals at session end** catch what nobody asked for. When a substantial session
ends, a hook reminds the agent once that it can record what the session produced. With
its working context still loaded, the same agent drafts the durable parts: what was
decided and why, where it applies, and what a failing test, a reviewer's reversal, or
a constraint discovered by trial taught the session. No second model is involved.

The write pipeline then redacts common credential patterns, removes exact duplicates,
records provenance, anchors each draft, and saves it as a proposal. A session with
nothing durable to keep writes nothing.

**Import** seeds a store from what a project already wrote. Deterministic importers
parse existing ADRs and supported specification formats without a model, and they
write their records as accepted, or as proposals when run in review mode. An import
preserves what its source says and cannot add what the source never recorded, so it
provides a starting map rather than a substitute for capture during work (Section
8.5).

### 6.5 Trust

Every record has a status, which governs how it is ranked, labelled, and shown, and
provenance, which says how it got there. Ratification is the transition that turns a
proposal into an accepted record, and a record can reach that state through several
doors. A person reads an agent's proposal and accepts it. A person's explicit request,
or an import, writes the record as accepted from the start. A ratification policy the
project configured accepts it under deterministic checks. Or, in a store kept by one
developer, an opt-in switch accepts every proposal as it is written. That switch is
older than the policies and is still supported. Its July decision was later superseded
by the September policy, which became the recommended way to run without a person, but
the switch stayed in the software unchanged for solo stores. A record the switch
accepts carries no stamp. What tells such a record apart from one written at a
person's request is its provenance, which still says an agent wrote it. The table
below shows what each door leaves behind and what an agent later sees.

| Record | Status | How it became accepted | What an agent sees |
|---|---|---|---|
| Captured directly at a person's request | accepted | written as accepted at that request | the record, unlabeled |
| Imported directly | accepted | written as accepted by the import | the record, unlabeled |
| Imported in review mode | proposed until reviewed | a person, later | as for any proposal awaiting review |
| Agent proposal a person accepted | accepted | that person, after reading it | the record, unlabeled |
| Agent proposal accepted by a configured policy | accepted | the policy, stamped `auto:<policy>` | the record, unlabeled |
| Agent proposal under the solo auto-accept switch | accepted | the switch, which leaves the ratifier field empty | the record, unlabeled |
| Agent proposal awaiting review | proposed | — | tagged `[unratified]`, ranked after all accepted memory, hidden after 30 days, and hidden at once in regulated mode |
| Agent proposal a person dropped | rejected | — | nothing by default; kept as history |
| Record replaced by a successor | superseded | — | a one-line pointer; full text on request |

In this paper, *accepted* names the state, and a record is *reviewed* only if a person
read the proposal and accepted it. Every reviewed record is accepted, but records
written on request, imported directly, or accepted by a policy or by the switch are
accepted without being reviewed.

By default a person accepts or drops every agent proposal, but a project whose
sessions run without a person in the loop can set a ratification policy instead. The
low-risk policy accepts a gotcha, a lesson, or a fact as soon as it is written,
provided that one of its anchors is live, meaning it currently resolves to code or to
a domain, and the write found no exact duplicate. ADRs, constraints, and domains still
wait for a person, unless a broader policy admits them under deterministic checks of
the same sort. Each record a policy accepts carries the policy's name, so an audit can
count policy-accepted memory and compare how often it is later superseded with memory
that a person accepted.

The ratifier stamp is visible in the record file but not in what an agent receives,
because retrieval renders every accepted record the same way. Section 9.2 describes
what follows from that. Teams whose rules forbid showing agents a proposal that has
not been accepted can turn on regulated mode, which withholds proposals and nothing
else.

### 6.6 Staying current

When the code graph changes, anchors are resolved again through the ladder in Section
6.2, and every degraded or orphaned binding is reported.

For each live record, Sidegraph also checks whether any commit made since the record
was captured changed a file the record is anchored to. When one did, the record is
marked `[drifted]` wherever it is delivered, and a health check, `sidegraph-doctor`,
lists drifted records and unhealthy anchors for review. A person then confirms the
record, repairs its anchor, or supersedes it with a current account, which extends the
chain instead of rewriting it.

Drift detection sees files that changed. It cannot see prose that became false for
another reason: a behavior change elsewhere in the system, an external constraint that
expired, or a claim that was wrong when it was written. In Sidegraph's own store, a
change in one module falsified two accepted records anchored elsewhere, and neither
the tests nor the code review caught it. Deciding whether a record still holds remains
a human judgment, and the drift marker's job is to say where that judgment is due.

## 7. Beside an existing workflow

Sidegraph attaches to the agent host through hooks and a tool server, so it does not
care how a team plans, specifies, or reviews work, and its core contains no
workflow-specific code. What does differ between workflows is the shape of the
documents each one writes, and that difference lives in small import profiles that are
data rather than code. During development Sidegraph ran beside five spec-driven
workflows: [superpowers](https://github.com/obra/superpowers),
[spec-kit](https://github.com/github/spec-kit),
[genkovich-sdd](https://github.com/genkovich/sdd),
[BMAD](https://github.com/bmad-code-org/BMAD-METHOD), and
[OpenSpec](https://github.com/Fission-AI/OpenSpec). Each workflow ran its own stages
with Sidegraph's hooks active.

More useful than compatibility is what a workflow and Sidegraph produce together. A
workflow keeps the decisions its templates have room for, while Sidegraph keeps what
the session learned along the way, and in these runs the two sources complemented each
other rather than overlapping. Spec-kit's planning template, for example, has no
proper place for rejected alternatives. When that plan was imported into Sidegraph,
the resulting record's `rejected` field held only template boilerplate. The real fork,
a choice about which module should hold a new validation check, reached Sidegraph's
store only through the record the agent proposed at the end of the session, when
Sidegraph's hook reminded it to write down what the session had decided.

Memory also flowed back into the workflow. In a deliberate test, a later spec-kit
request was written so that its obvious solution was the alternative an earlier record
had rejected. The workflow's specify and plan stages both called retrieval before
writing anything, and the rejected alternative reached each of them. The resulting
design built on the earlier decision instead of reversing it. The workflow ran its own
steps unchanged, and memory supplied what its documents did not hold.

These runs established coexistence. The measurements that follow ask what memory and
its delivery actually change.

## 8. What running it showed

### 8.1 Why the measurements exist

Sidegraph's measurement program grew out of two practical needs: making the system
work beside several spec-driven workflows without degrading them, and finding its
defects before release. It ran 622 controlled sessions on five repositories:
[XGBoost](https://github.com/dmlc/xgboost),
[Airflow](https://github.com/apache/airflow), Sidegraph itself, and two private
repositories this paper does not name. The first 590 sessions asked maintenance
questions, with some groups isolating one delivery stage at a time, and three of the
tasks among them were real code changes run with and without memory. A final group of
32 sessions asked why some sessions answer without calling any tool.

The benchmark questions were written by agents without access to the store, and the
answers were scored by judges who did not know which setup had produced them.
Rerunning and rescoring exposed six defects in the measurement harness, all of which
had favored Sidegraph, and two early cost reductions, of 31% and 69%, did not survive
the corrections. The figures below come from the corrected internal data.

### 8.2 Stored memory does nothing until it is delivered

Two groups of 64 sessions each ran on the two private repositories. In one group,
records sat readable on disk with nothing telling the agent they existed, and in the
other, the store directory was unreadable at the operating-system level. No agent in
the first group opened a record, costs were indistinguishable, and blinded answer
quality differed by no more than two points. Records placed in the repository with
nothing to deliver them changed nothing observable, which is why everything memory
contributes depends on a delivery path.

### 8.3 Delivered memory reaches answers, but agents often skip retrieval

Every session in the full system received the session-start map, and the quality of
its answer tracked what it did next. Sessions that called retrieval included 83% of
the expected facts in their answers. Sessions that read files without calling it
included 59%, and sessions that called no tool included 32%, an ordering a second
judge confirmed. Because sessions chose their own path, the figures show stronger
answers accompanying retrieval rather than retrieval causing them.

The largest leak sat between receiving the map and asking for a record. In the first
measured release, 43% of sessions never called retrieval. A release 130 commits later
brought that share down to 26% on the same 48 questions, a clear improvement, although
no single change can take the credit. A separate experiment rebuilt stale anchors so
that the reminder on opening a file could name the specific memory anchored to it. It
did not help: in that experiment, the share of sessions that skipped retrieval was 44%
before the rebuild and 53% after, a difference small enough to be chance.

Sessions that called no tool at all had a specific cause. Repositories often carry a
standing instructions file that the agent host loads into every session, and when that
file covered a question's topic, sessions answered from it without calling anything. A
group of 24 sessions on a repository without such a file produced no tool-free
sessions, and removing the file in the final 32-session experiment brought them from
10 down to none. Removing it took away both what the file said and the fact that it
was loaded first, so the experiment points at the file without telling those two
effects apart. It ran on one repository with one model in one day.

### 8.4 The store holds knowledge that code and documents do not

On six questions chosen because the store held relevant records, all six answers from
sessions with memory used knowledge found nowhere in the code or the project's
documents, mostly rejected alternatives and build or environment history. Sessions
without delivery, which still had the store files on disk, found that knowledge once,
by grepping those files directly. In ordinary use such answers were rare, though:
across 159 blindly judged answers, knowledge unique to the sessions with memory
appeared in six, and knowledge unique to the sessions without it appeared in five.

How much a store can offer varies widely. Against an 80-question maintenance benchmark
written without store access, records fully covered 50 questions, partially covered
19, and missed 11, and full coverage ranged from 18% to 81% depending on the
repository. Coverage describes what the store holds, which is only the starting point
for how well agents answer.

### 8.5 Cost and quality depend on the repository, and imports can mislead

Across all blindly scored work, sessions with memory cost 18.91 US dollars against
20.48 without it, 7.7% less. The estimated range, however, ran from 14.6% cheaper to
0.2% more expensive, so the overall effect on cost was inconclusive.

The repository made the difference. On document-heavy work, memory cut cost by 18.4%.
On Airflow, a large Python monorepo, it raised cost by 25.5% and scored 21 of 32
quality points against 25, losing mostly on questions the store covered. Overall
answer quality was 59.4% with memory and 62.0% without it, a gap too small to call
either way. Memory can replace expensive exploration of prose, or it can add retrieval
to a task that code search alone would have solved.

Imports produced the clearest failure. The case was a store built almost entirely by
import, with 338 records taken from planning documents and five captured during live
work. On sixteen questions written without store access, sessions with memory scored
15 of 32 quality points against 22 and made eleven false claims against three. Both
groups could read the same planning documents, but Sidegraph also presented the
planned behavior as project memory, and agents repeated it as current fact. Drift
detection could not help, because an imported record is anchored at the commit where
it was imported, so prose that was already out of date looks fresh until the code
changes again. The best cost result, by contrast, came from imported ADRs, so what
failed was imported planning prose standing in for a project's whole memory.

### 8.6 Anchors fail visibly

On real refactors of one public repository, leaf anchors recovered eight of the nine
entities that still existed, with no false rebinds, and the ninth was marked orphaned
instead of being attached to the wrong code. Across four kinds of refactor, the
resolution ladder of Section 6.2 behaved as designed: it recovered what it could find
unambiguously and left the rest visibly unresolved.

### 8.7 A render guard is not a defense

Delivered memory is a channel into the agent's context. Sidegraph wraps delivered
records in a render guard, a notice that the text is stored data rather than
instructions. To test it, a record instructing the agent to obey a malicious payload
was delivered in sixteen sessions, half with the guard and half without. A first
scoring pass reported six successful attacks with the guard, because the scorer had
counted refusals that quoted the attack as obedience. After that correction the attack
succeeded in no session in either group, so the model's own instruction hierarchy
blocked it and the guard added no measured protection.

Capture carries a separate risk, that a proposal preserves a secret. Redaction before
writing is best-effort hygiene: in a seeded test it caught seven of fourteen kinds of
secret, and twelve after a same-day fix, while two kinds remain unsupported.

## 9. Boundaries of the approach

### 9.1 A record can be healthy and false

A record can be accepted, current, and bound to a live anchor while its prose is
wrong, and the gap can begin at capture. A proposal drafted at session end can cite
the event behind it, such as a failing command or a reviewer's reversal. That citation
makes its origin inspectable, but nothing verifies that the agent's account captured
the real reason. Memory compounds only if later contributors question, correct, and
supersede what it contains.

### 9.2 Memory is a trust boundary

Every delivered record becomes text that sits beside the agent's instructions and can
be mistaken for them. Anchors fail closed, but content has no equivalent check: a
false or malicious record is delivered with the same confidence as a correct one. An
unreviewed proposal can influence work before anyone reviews it, and only its label
marks it. A record accepted by a policy or by the auto-accept switch carries no label
at all when delivered, and regulated mode, which hides only unaccepted proposals, does
not withhold it. A wrong gotcha accepted by a policy therefore reaches later sessions
at the top of their context, looking exactly like reviewed memory.

A policy removes the reviewer for the records it accepts, so its limits are the limits
of its checks. Duplicate detection catches only exact matches after normalization, so
a near-duplicate under a different title is accepted where a reviewer might have
dropped it. The anchor check and the acceptance are separate operations, so an anchor
that breaks between them leaves an accepted record with an orphaned anchor, which the
next sync reports. The health check can compare how often policy-accepted and
person-accepted records are later superseded, but a bad record raises that rate only
after someone notices it and supersedes it. A bad record nobody revisits never shows
up. The rate has not yet been measured on a store that runs a policy.

The review gate itself was weak in the one store that measured it. Over 23 days,
Sidegraph's own store grew to 173 decision records, and the reviewer, who was the
author reading proposals from the author's own sessions, rejected 2.3% of them. That
describes a light-touch review, not the filtering power of a gate. Repository access
control, authorship review, and secret scanning remain part of the boundary.

### 9.3 Scale, ownership, and outcomes are unmeasured

Every measured store held at most hundreds of records, so how ranking, drift queues,
and reviewer load behave at thousands is still unknown. The sessions used one model
family and one agent host, mostly on maintenance questions, and no store yet holds
years of team history, so the central use case, a mature project carried across many
contributors, remains the question a pilot has to answer.

The measurements did not track regressions, onboarding time, or rework over the life
of a project, and the three coding tasks in Section 8.1 were run to see whether agents
would use memory during a change, not to measure whether the change came out better.
They also did not compare task-aware delivery with a simple pointer to the same
records, or Sidegraph with a well-maintained ADR practice that already surfaces its
decisions during work. A team with such a practice has already built much of this
capability.

## 10. Deciding whether to adopt it

### 10.1 Choose the repository

Sidegraph is built for a project with a long history, frequent agent sessions, authors
who have moved on, and constraints the code cannot explain. In such a project,
reconstructing why the system has its current shape costs more than keeping the
decisions that explain it.

It is a poor match for a young codebase whose original authors are still present, in a
code-dense repository where search already answers maintenance questions cheaply, and
in a team unwilling to review and maintain records.

### 10.2 Count the full cost

Every session spends context on the map injected at session start, and more when the
agent calls the retrieval tool. People must review proposals, repair anchors that no
longer resolve, and decide whether drifted records still hold. Those human costs have
not been measured.

The file-contact hook adds about 110 ms to each read, search, edit, and write it
observes. At 200 to 500 such calls, that adds roughly 22 to 55 seconds to a session.
Disabling the hook removes only the reminder it adds when a file is read or searched,
while the session-start map and the retrieval tool keep working. The [operations
reference](../../docs/reference/operations.md) lists the measured local costs. Model
cost can rise or fall depending on the repository (Section 8.5), so an expected saving
is not a reason to adopt.

### 10.3 Run a bounded pilot

The [pilot kit](../../docs/pilot-kit/README.md) sets out a staged pilot:

1. Before installing anything, run the ten-minute corpus-fit screen, which checks
   whether the repository holds enough durable reasoning to be worth a pilot.
2. Write baseline maintenance questions without access to a Sidegraph store.
3. Capture for two to three weeks with delivery disabled, measuring proposal volume,
   review time, and the age of the oldest unreviewed proposal. These weeks also add
   records captured during live work to the store, so that it does not rest on imports
   alone.
4. Enable delivery, run the questions with and without memory, blind the answers, and
   compare cost, quality, and how often sessions call the retrieval tool.

An imported ADR set can seed the store before step 3.

The kit suggests stop conditions, derived from the measured repositories, to set
before step 3:

| Measure | Default stop condition |
|---|---|
| Model cost | The pilot's question set costs more than **15%** above baseline |
| Delivery | With delivery enabled, more than **40%** of sessions never call the retrieval tool (Sidegraph's own first measured release, at 43%, would have failed this gate) |
| Queue health | The oldest unreviewed proposal is older than **30 days** |
| Answer quality | Blinded quality with memory falls below the no-memory run by more than a margin set before step 3 |

The kit also recommends re-measuring, every month, the share of sessions that never
call the retrieval tool, rather than assuming that agents keep paying attention to
reminders.

The quality gate comes without a default number. On the monorepo in Section 8.5, the
cost gate would have caught the quality loss, but only because cost happened to rise
at the same time. A repository can just as well lose quality while staying cheap, so
the margin is worth choosing deliberately for each pilot.

The measurements behind these gates come from diagnostics that stay on each
developer's machine and are ignored by [Git](https://git-scm.com). A rollout across
many developers needs its own privacy-reviewed way to aggregate them, which Sidegraph
does not provide.

### 10.4 Keep the exit cheap

If Sidegraph is removed, its records stay where they always were, as JSON files in Git
that remain readable, diffable, and searchable. Only the derived index is lost, so
stopping a pilot requires no migration.

Sidegraph sends no store contents or diagnostics to a remote service. Delivered
records do become part of the context the agent host sends to its model provider, as
any file the agent reads does.

## 11. Conclusion

For a team that keeps its reasoning beside the code, as Sidegraph does, the biggest
change is who can answer the question "why is it like this?". Without such memory it
is answered by whoever still remembers, or it goes unanswered and the code is quietly
rewritten. With it, any engineer or agent session can get the answer, and the answer
comes with its history: the decision, the constraint behind it, the alternatives
already tried, and the later reversals, as the walk-through in Section 5 showed.

Three commitments make that answer worth trusting. Each began as a design choice, and
running the system is what showed it mattered. History is never rewritten, because a
decision that was later reversed still explains why the code it shaped looks the way
it does. A record whose code can no longer be found unambiguously is reported rather
than guessed at, because context attached to the wrong code misleads every later
session. And memory is pushed into the session, at its start and when it opens a file,
not only offered through a tool it may never call, because stored records that nothing
announces are simply never read.

What it cannot yet claim is also clear. No store with years of team history has been
measured, a record's truth still needs a person's judgment, and whether memory pays
for itself depends on the repository. The most practical of those questions, whether
it pays off in a given repository, is one a bounded pilot can answer, and Section 10
describes how to run one.

The engineer's advantage over an agent was never the code, which both can read. It was
the decisions the engineer remembered and the agent did not. Sidegraph is that missing
link: each agent session still starts fresh, but it no longer starts as a newcomer.
