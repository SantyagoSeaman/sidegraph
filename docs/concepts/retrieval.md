# Retrieval

> **Start here:** Read the opening explanation, then follow only the detail needed for your current task. [Glossary](glossary.md).

The read path is multi-altitude, budget-bounded, and task-aware: a cheap named map at session
start, a one-level-deeper walk on demand, and a precise, seeded pull when the agent is about to
do work. Implemented in [`src/sidegraph/retrieval.py`](../../src/sidegraph/retrieval.py); wired
into Claude Code via [`src/sidegraph/host/hooks.py`](../../src/sidegraph/host/hooks.py) and
exposed as MCP tools in [`src/sidegraph/server.py`](../../src/sidegraph/server.py). See
[mind model](mind-model.md) for why the altitudes exist at all.

## SessionStart TOC

The session map orients the agent before a task. With accepted domains, it lists their names and purposes. Without named domains, it uses graph communities and recorded decisions. Global mistakes and visible proposals can also appear.

The host adds the standing instruction:

> When you need to find or understand code in this project, call get_task_context(files=[…])
> with repo-relative paths before any grep or file search — decisions, gotchas and a domain map
> are indexed here. Before a non-trivial change, run the sidegraph check-plan skill if it is
> available. If the tool is listed only by name, load it first.

The version line identifies the running package. Diagnostic lines indicate follow-up work. See [captured output](../reference/examples.md#sessionstart-map).

See [implementation details](../reference/retrieval-internals.md#sessionstart-toc).

### When the TOC goes live

See [details](../reference/retrieval-internals.md#when-the-toc-goes-live).

## `drill_down(domain_slug)` — the Axis-1 operation

Use `drill_down` when you know the named area but need its summary, members and decisions. For example, `drill_down(domain_slug="cache")` asks for the domain whose slug is `cache`. Unknown slugs return candidates rather than a guessed match. Use `list_domains` to find the names in your project.

See [implementation details](../reference/retrieval-internals.md#drill_downdomain_slug--the-axis-1-operation).

## `get_task_context`: seeded, budgeted, mistakes-first

Supply the files or entities for the task. The result prioritizes known mistakes, then decisions, structure and related records. If output is clipped, narrow the task or follow its hints.

For the scratch repository's `cache.py`, that call returned this real excerpt:

```text
## ⚠ Known mistakes & gotchas
- [gotcha] Include the project in cache keys: Build cache keys from both project and resource name. (context: Two projects can use the same resource name.) (rejected: Using the resource name alone allows cross-project collisions.) (id: 01M6EPVG29W7ZP4FG6H9G24X40)

## Structural map
- cache_key() (code) [cache.py:L1]
- cache.py (code) [cache.py:L1]
```

Read the rejected alternative before editing. `[unratified]` means a proposal awaits a trust decision. `[drifted]` means its content needs checking against the current code. A record's visibility does not remove either qualification.


See the [detailed contract](../reference/retrieval-internals.md#get_task_context-seeded-budgeted-mistakes-first).


### Seeds

Pass a repository-relative file in `files`, or a name and file in `entities`. Example: `get_task_context(files=["cache.py"])`. A seed identifies the area you plan to inspect or edit.

See [the detailed contract](../reference/retrieval-internals.md#seeds).

### Char-based budgets

The response is bounded by character budgets. If a result says records were omitted, narrow the file list or request the omitted records explicitly.

See [the detailed contract](../reference/retrieval-internals.md#char-based-budgets).

### Ranking buckets, and why mistakes come first

Gotchas, lessons and constraints about the task come first. They explain approaches to avoid before the agent commits to a change. Directly anchored decisions follow before more distant related records.

See [the detailed contract](../reference/retrieval-internals.md#ranking-buckets-and-why-mistakes-come-first).

### Bucket C: domains and communities

Related records can come from the target's named domain or its graph community. They are area context and may not apply to every file in that area.

See [the detailed contract](../reference/retrieval-internals.md#bucket-c-domains-and-communities).

### Facts: inline evidence, and the Known-facts bucket

Facts preserve knowledge that the code cannot reconstruct, such as a measured external limit. They can support a decision or have their own anchors. A fact keeps its source so the agent can check the evidence.

See [the detailed contract](../reference/retrieval-internals.md#facts-inline-evidence-and-the-known-facts-bucket).

### Tiered rendering: direct entries get depth, related entries stay a pointer

A directly relevant record includes more of its reasoning. A distant related record may show only a short pointer. Retrieve its ID when the detail matters.

See [the detailed contract](../reference/retrieval-internals.md#tiered-rendering-direct-entries-get-depth-related-entries-stay-a-pointer).

### The `[drifted]` marker: code changed after this record was captured

Treat drift as a request to verify the claim. It does not mean the claim is false. Read the current code before relying on the record or replacing it.

See [the detailed contract](../reference/retrieval-internals.md#the-drifted-marker-code-changed-after-this-record-was-captured).

### Budget fallback: summaries instead of leaves

When detailed structure does not fit, retrieval can show a higher-level summary. Follow a named domain or ask for fewer files to narrow the result.

See [the detailed contract](../reference/retrieval-internals.md#budget-fallback-summaries-instead-of-leaves).

### Thin tools: the two halves, separately

Use `query_structure` when you need the graph and `query_decisions` when you need records. `get_task_context` combines both for a task.

See [the detailed contract](../reference/retrieval-internals.md#thin-tools-the-two-halves-separately).

### Community fallback

A record can remain reachable through a broader area anchor after its precise target changes. That broader reach does not prove its original claim still holds.

See [the detailed contract](../reference/retrieval-internals.md#community-fallback).

### Lazy sync on read

The read path checks for a changed graph and refreshes derived mappings when needed. To see an explicit report after a rebuild, run `sidegraph-sync`.

See [the detailed contract](../reference/retrieval-internals.md#lazy-sync-on-read).

### File applicability outside the graph

A stored file anchor can still make a record applicable when no current graph node exists for that file. Follow the response's path and graph hints when a seed cannot be read.

See [the detailed contract](../reference/retrieval-internals.md#file-applicability-outside-the-graph).

### Rendering

The result separates known mistakes, decisions, structure and related records. Proposed records carry an unratified marker. Use the ID to inspect the complete record and its history.

See [the detailed contract](../reference/retrieval-internals.md#rendering).

## Records that arrive without a query

An agent does not always call `get_task_context`, and a subagent starts without the session's
context. So the read path has three more channels, all wired as hooks and all reading the
store's index directly (see [hooks](../reference/hooks.md)):

- **At the point of reading.** When a `Read`, `Edit`, `Write` or `Grep` call, or a Bash `sed`,
  `grep`, `rg` or `cat` line, names a file that decisions are anchored to, the `PreToolUse` hook
  hands the agent the top decisions for that file in the same tool call, mistakes first. Each file
  is delivered once per agent. `SIDEGRAPH_GREP_NUDGE=off` stops it.
- **In a subagent's brief.** On an `Agent` call, the hook reads the files the brief names, and
  the files named in a plan or notes document it names, and appends the decisions anchored to them
  to the brief. The subagent sees the block in its first message; the parent's transcript keeps
  the prompt it wrote. `SIDEGRAPH_AGENT_BRIEF=off` stops it. In Claude Code's auto mode
  (`permission_mode: auto`) it leaves the call unchanged: that mode reviews a call before hooks run
  and refuses an input a hook changed. The subagent still gets the `SubagentStart` brief and the
  records of the files it reads.
- **At the start of a subagent.** The `SubagentStart` hook tells the new subagent that decision
  memory exists, how to ask it, and how many decisions, files and mistakes it holds. It carries no
  records. `SIDEGRAPH_SUBAGENT_BRIEF=off` stops it.

The first two deliver decisions and eligible facts. Use `list_facts` for full fact records; see [`sidegraph-pre-tool-use`](../reference/hooks.md#sidegraph-pre-tool-use).
The first two run on Claude Code and Grok Build. `SubagentStart` runs on Claude Code, Codex and
Muse; Grok Build runs it but does not pass its output to the model. The day to
day view is in [retrieval in sessions](../guides/retrieval-in-sessions.md#memory-without-asking).

## See also

- [Mind model](mind-model.md) — why the TOC/drill_down altitudes exist and what a domain is.
- [Decision memory](decision-memory.md) — why mistakes-first retrieval is the point.
- [Anchoring](anchoring.md) — how entities, tiers, and binding status get there in the first place.
- [Store format](../reference/store-format.md) — the underlying read methods' contract.
