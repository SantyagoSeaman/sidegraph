# Claude Code hook wiring

> **Start here:** Use the section matching the command or payload you are checking. For everyday work, begin with the [documentation paths](../README.md). [Glossary](../concepts/glossary.md).

## Hooks in detail

The four hooks are console scripts. See the [host environment settings](../integrations/claude-code.md#environment-variables) and the [complete variable reference](configuration.md#environment-variables) for their configuration.

### `SessionStart` — `sidegraph-session-start`

Runs a best-effort lazy sync (`maybe_sync`) against the current graph, then injects the opening
map as `additionalContext`. A usable cache of accepted domains selects the global sections.
Without that cache, the on-demand fallback also shows `## Communities` when a graph is available.
It lists the largest communities by member count and labels each by its god node.
See the [SessionStart map](../concepts/retrieval.md#sessionstart-toc).

Either way, `session_start` prepends the same standing instruction ahead of whichever
renderer's text follows — added once at the hook-assembly level, not inside either renderer
(see [`reference/hooks.md`](hooks.md#sidegraph-session-start)):

> Before you search or read files to answer a question about this project or to plan a change,
> call retrieve_decisions(query="…") with a few words that name the topic — decisions, gotchas and
> abandoned approaches are indexed here. For files or symbols you are about to change, call
> get_task_context(files=[…]) with repo-relative paths, or
> get_task_context(entities=[{"name": "…"}]) when you know a symbol's name and not its file; there
> is no need to search for the path first. Before a non-trivial change, run the sidegraph
> check-plan skill if it is available. If a tool is listed only by name, load it first.

Unlike the `PreToolUse` records block below (which fires only when a `Read`, `Edit`, `Write`,
`Grep` or supported Bash read names a file with records), this instruction is unconditional
and asks for memory before the search that answers a
question or plans a change, whatever the search tool — bash `find`, MCP structure-query tools, a
search that names no file — by telling the agent up front, not by gating a specific tool call.
The call it names for a question or a plan is `retrieve_decisions(query=…)`;
`get_task_context` is for the files and symbols about to change.

The next line names the package the hooks run, so a session that launched an old install says so
in its own context: `Sidegraph X.Y.Z`, where `X.Y.Z` is the running version. It appears in every
`SessionStart` that injects the map.

Both renderers also inject:

- **Global mistakes & constraints** — the most recent `gotcha`/`lesson`/`constraint`
  decisions scoped `global` (not tied to one entity), capped at 10.
- **Unratified proposals** — proposed records inside the surfacing window, after accepted
  records. The cached path shows proposed global mistakes. The fallback shows proposed
  decisions and facts, up to 10 of each.

The fallback also shows **Recorded decisions** (up to 10 accepted decisions absent from the
other sections) and **Known facts** (up to 10 accepted facts). Request task-scoped records with
`get_task_context`.

After the map come status lines from the integrity checks. A line appears while its problem
exists. Problems needing a person can also produce a top-level `systemMessage`, at most once
a day per check. `SIDEGRAPH_RATIFY_NUDGE=off` and `SIDEGRAPH_DRIFT_NUDGE=off` disable their
respective lines. The [hooks reference](hooks.md#sidegraph-session-start) owns the complete
check order and notice rules. Use [troubleshooting](../guides/troubleshooting.md) to choose
a follow-up action.

If the store was initialized but is empty and no graph exists, this renders a short stub: the instruction, the
version line, the header and a line saying there is no code graph. That's expected on a freshly initialized
repo, not an error.

### `Stop` — `sidegraph-stop`

A **guarded block-to-distill**, gated on the session actually looking substantial: the first
nudge comes once per session (a per-session capture ledger in the store, plus Claude Code's own
`stop_hook_active` re-entrancy flag), and only once the transcript has produced **>= 2 real
user prompts** (counting actual typed prompts, not tool-result noise; a session's very first
`Stop` no longer triggers it, and a headless one-prompt `claude -p` run is never nudged; see
[`reference/hooks.md`](hooks.md#sidegraph-stop)
for the exact counting rule). Once both gates clear, it returns:

```json
{"decision": "block", "reason": "<nudge>", "suppressOutput": true}
```

— which tells Claude Code not to end the session yet and to show the agent the nudge text
instead. The nudge itself is a compact one-liner pointing the agent at `propose_decisions`
(and `propose_domains` for a recurring unnamed area) if the session produced a durable
decision, lesson, or gotcha — silence is explicitly fine otherwise; the full What/Why/Where/
Learned field guidance lives in `propose_decisions`' own tool description, not in the nudge
text. Proposals land as `status=proposed`, redacted, and already show up in retrieval tagged
`[unratified]` (see [`retrieval-in-sessions.md`](../guides/retrieval-in-sessions.md)) until a
human ratifies them (`sidegraph-ratify`) — unless `SIDEGRAPH_AUTO_ACCEPT=on`, in which case
decisions and facts land `accepted` immediately instead (domains are always exempt from that
flag; see
[`guides/capturing-decisions.md#4-auto-accept-opt-in`](../guides/capturing-decisions.md#4-auto-accept-opt-in)),
or the project's `SIDEGRAPH_RATIFY_POLICY` accepts an eligible proposal at write time with an
`auto:<policy>` stamp (see [configuration](configuration.md)).
A session that was nudged early is nudged again after more work: at least 30 minutes and 10 new
commits (on any local branch) after the last nudge, with the count added to the nudge's text
(see [`reference/hooks.md`](hooks.md#re-arm-after-more-work)).
Set `SIDEGRAPH_CAPTURE_NUDGE=off` to disable this hook entirely.

Claude Code renders the block response under an error-styled banner (`Stop hook error: ...`)
— that's the host's standard styling for any `decision: block` response, not a failure; the
"error" text IS the nudge, and `suppressOutput: true` just keeps the raw JSON out of the
transcript.

### `PreToolUse` — `sidegraph-pre-tool-use`

Four groups in the snippet (see
[`claude-code-setup.md`](../getting-started/claude-code-setup.md#2-add-the-hooks)): matcher
`Read|Grep|Edit|Write`, matcher `Bash` with one entry that has no `if` and runs a shell filter
for the reader words (below), matcher `Agent|Task` (below), and the Sidegraph MCP
read group. When a `Read`, `Grep`, `Edit`, `Write` or Bash call names a file that decisions are
anchored to, the hook hands the agent those records in the same call as a **non-blocking**,
`additionalContext`-only `hookSpecificOutput`:
the guard line, then for each file (at most three per call) its top two records, or three when the
first two are mistakes, each as `- [kind] title — first sentence of the choice (id …)`. A
`Withheld:` line counts facts too long to show, and a `More: get_task_context(files=[…])` line
points to remaining renderable records. No `permissionDecision` field is set, so the tool call
is never denied, escalated, or auto-approved. It only gets annotated with the block, and then
runs through Claude Code's normal permission flow exactly as it would have otherwise.

The MCP read group records the caller for `get_task_context`, `query_decisions` and
`retrieve_decisions` and prints `{}`. [Sidegraph MCP read hook](hooks.md#sidegraph-mcp-reads)

Each agent gets **ten file claims per scope** for ordinary files, exact-path files, and files
named only by newly supported Bash readers or literal `Grep` globs. Exact-path and expanded
readers skip files already delivered to the agent. Ordinary reads keep their own first delivery.
Compaction and clearing reset all allowances for that agent. A single statement in the store's
`meta` table counts and inserts the claim.
The session's own agent and every subagent it starts count
separately, because Claude Code gives a subagent's hook payload its parent's session id and its
own `agent_id`: without that, the main agent's delivery would have used up the one a subagent
needed. A call that names no file with records prints `{}`, and no counting form is printed
anywhere. Set `SIDEGRAPH_GREP_NUDGE=off` to stop delivery at the point of reading, on every tool
but `Agent` (that has its own switch, below). Touch recording uses `SIDEGRAPH_TELEMETRY`.
Bash delivery follows the supported reader commands, and Bash reads produce no touch events.
`Grep` can name one file through a literal `glob`. See the
[hooks reference](hooks.md#bash-lines). The hook reads the store's index directly rather
than opening the store (see the [hooks reference](hooks.md#sidegraph-pre-tool-use)),
so a call costs a few tens of milliseconds, and in a project with no store it does nothing and
creates nothing. Like the other hooks, it never crashes or blocks the tool call: any failure (or
nothing to deliver) prints `{}`.

After an index rebuild (a pull or a branch switch that changes record files, a touched
record file, a compaction) record delivery and the `SubagentStart` brief continue for
every anchor whose entity is unchanged, by the status the last sync computed for it. That
status describes the code as the last sync saw it: when the code changed since, a record
can still arrive through an anchor the next sync will find gone, as it can between any
two syncs. A new or changed anchor for a graph-held file waits for the next successful sync.
PreToolUse delivers eligible records by exact path for existing files outside the last synced
graph and for existing files in a store no sync has checked. Those record lines carry
`[file-bound]`. `SubagentStart` and the Agent brief continue to use checked anchors.

**Subagent briefs.** The third group, matcher `Agent|Task`, runs the same command when the agent
spawns a subagent. The brief the parent writes is where the task is, so the hook reads the files
it names (and, one hop away, the files named in a plan or notes document it names), and appends a
block with the records anchored to them to the brief: per file the top two records, at most six
lines and 3,000 characters excluding the `Withheld:` line and its newline. The file header names
the document when a plan named the file. The hook returns the call's own input with only `prompt`
changed (`updatedInput`), sets no `permissionDecision`, and claims none of the per-agent keys
above. **The block reaches the subagent, in its first message, but the parent's view of its own
`Agent` call does not show it**: the parent's transcript keeps the prompt it wrote. It is on by
default. Set `SIDEGRAPH_AGENT_BRIEF=off` to turn it off. In Claude Code's auto mode
(`permission_mode: auto`) it leaves the call unchanged. That mode reviews a call before hooks
run and refuses an input a hook changed. The subagent still gets the `SubagentStart` brief and
the records of the files it reads.
Grok Build runs hooks before its permission gate, so it keeps the brief. It was not tested on agent-team teammates or `fork`
spawns, and Codex does not carry it. See the
[hooks reference](hooks.md#subagent-briefs).

### `SubagentStart` — `sidegraph-subagent-start`

No matcher. A subagent (Explore, Plan, general-purpose, or one you define) starts without the
`SessionStart` context, and Explore and Plan agents load no `CLAUDE.md` either, so nothing
standing tells one that decision memory exists. When Claude Code creates a subagent, this hook
answers with an `additionalContext` that arrives in front of its first turn: the same standing
call `SessionStart` gives the main agent (`retrieve_decisions(query=…)` before searching for
a question or a plan, `get_task_context(files=[…])` with repo-relative paths for the files about
to change, or with a symbol's name alone when its file is not known, the check-plan skill
before a non-trivial change, and a reminder to load the tool first if it is listed only by
name), then
one sentence of what memory holds: how many records are anchored to code, in how many files,
and how many of them are recorded mistakes. It carries no records:
which ones matter depends on the files the subagent works on, and `get_task_context` answers
that. In a store with nothing anchored to code, or where the index cannot be used, it prints
`{}`. Set `SIDEGRAPH_SUBAGENT_BRIEF=off` to disable it entirely. It reads the store's index
directly like the `PreToolUse` hook, so it costs a few tens of milliseconds per subagent. The
counts and the exact output are in the [hooks reference](hooks.md#sidegraph-subagent-start).

### Silent degradation

All four hooks are written to **never crash the session** and never block a tool call. Any
exception inside `session_start` prints `{}` (Claude Code proceeds with no injected context),
except that a store which cannot be opened prints a `systemMessage` naming the cause and the fix
(and a model line saying memory tools will fail) instead, and a lock another process holds still
prints `{}`;
any exception inside `stop` prints `{}` (Claude Code proceeds to stop normally); any exception
inside `pre_tool_use` — or the nudge conditions simply not holding — also prints `{}` (the
tool call proceeds through the normal permission flow); any exception inside `subagent_start`, or
nothing anchored to code to describe, prints `{}` (the subagent starts with no brief). A missing
graph leaves new records without code anchors and gives a shorter map. Before the first sync,
PreToolUse can deliver tagged records by exact path for existing files. Subagent briefs wait
for checked bindings. A missing store is not an error either:
`SessionStart` creates nothing: it sends a `systemMessage` that names the expected path and asks for
an explicit `sidegraph-init` there, and no hook creates the store, so memory stays off until you run it. An
env var that points at the wrong path gives an empty store there, so that session has no
memory. None of these breaks the session.

## Plugin launchers

**Public and development manifests differ.** The marketplace receives
`plugin/sidegraph/.mcp.public.json` and `plugin/sidegraph/hooks/hooks.public.json`, renamed
to the canonical filenames during the public release. Every host's variant runs the published
package at the plugin's own version: `uvx --from sidegraph==X.Y.Z --python 3.13 <entry point>
--repo-root`. That package pins every one of its dependencies, so a newer Sidegraph arrives when
the plugin updates and nothing floats between sessions. The command carries no `UV_*` variable,
no `SIDEGRAPH_DIR` or `SIDEGRAPH_GRAPH` (their defaults apply unless your shell exports them) and
no path into the plugin folder. The unsuffixed files in this
development repository run the checkout the plugin was loaded from, with
`UV_PROJECT_ENVIRONMENT=.venv uv run --project "${CLAUDE_PLUGIN_ROOT}/../.." --package
sidegraph --frozen --no-active` and `--repo-root`, in whatever project the session is in. The MCP entry of
every variant starts `uvx` or `uv` directly, with no `sh`; the Claude Code hooks run through
Git Bash on Windows. Git for Windows provides it and Claude Code finds it; if it cannot,
`CLAUDE_CODE_GIT_BASH_PATH` points it there. Without Git Bash, Claude Code runs hooks in
PowerShell, which cannot parse Sidegraph's POSIX hook commands.

**Hooks try `--offline` first.** Every hook launch on every host (Claude Code, Grok Build, Codex
and Muse) runs `uvx --from sidegraph==X.Y.Z --python 3.13 --offline <entry point>` first, with
its output held back, and runs the same launch without `--offline` only when that fails. Without
the flag, uv revalidates its package index once the cached pages are ten minutes old (about 68
pages, 220 to 460 ms with a network), and with no network it retries three times before it gives
up: 5.8 and 10 s per hook were measured with an aged index. The version is an exact pin and the
package pins its whole dependency set, so a stale index cannot change what runs, and nothing needs
the index once the package is in uv's cache. On a first run the offline attempt fails in about
45 ms and the online launch installs the package as before. The POSIX commands read the hook
payload once into a shell variable (`p=$(cat)`) and hand it to each attempt, so the second one
gets it whole, and they hold back the online attempt's output as well: the host gets the answer
of the attempt that succeeded, or the fallback (`{}`, or the `SessionStart` message) when both
failed, never a half-written answer followed by the fallback. The Codex PowerShell form runs the
offline launch with its output captured and writes it when its exit code is 0. Otherwise it runs
the online launch, captured the same way and written only when that exit code is 0, and then, if
that failed too, the fallback alone. It resets `$LASTEXITCODE` to 1 before each launch, so the 0
that `git rev-parse` left cannot pass for a `uvx` that never ran, and it decides on the exit code
alone: Windows PowerShell 5.1 sets `$?` to false after a native program writes to a redirected
stderr, even at exit 0, and uv logs there. PowerShell does not buffer stdin for a second
attempt (5.1 mangles non-ASCII text piped to a native command), so the online attempt reads
whatever the offline one left. With the exit code alone that is the whole payload in practice:
the online attempt runs only when uv failed, and uv fails before it starts the entry, so stdin is
unread. An entry that starts and then exits non-zero has consumed it, and the online attempt
then gets an empty stdin and answers `{}`. Muse's adapter runs the two argument lists in turn
under its one time budget. The MCP server starts once per session, so its launch keeps the plain
form.

**Bash is one entry.** Claude Code starts a hook for every `if` entry whose rule matches a Bash
command, and for all of them when it cannot split the command into subcommands (a quoted
variable it cannot resolve, a loop body that uses its variable, `${…}`, a brace group, `case`, a
heredoc followed by a redirect): four launches for one call. The plugin therefore wires Bash as
one entry with no `if`. Its command reads the payload and starts `uvx` only when the payload holds
`sed`, `grep`, `rg`, `cat`, `head`, `nl`, `awk` or literal `git show` followed by a space,
a JSON tab or carriage-return escape, a single quote or an escaped double quote, after a
character that cannot be part of a word or after a JSON newline, tab or carriage-return escape.
Other commands print `{}` after one `sh`. The parser also recognizes split words such as
`c'at' file`, a quoted Git subcommand such as `git 'show'`, and extra whitespace between `git`
and `show`. These spellings alone skip the launch because the prefilter matches the serialized
hook payload.
Grok's Bash entry is the same command with
`SIDEGRAPH_HOST=grok`, and Grok skips handlers that set `if`, so the one form serves both.

The [launch-commit contract](hooks.md#the-launch-commit-file) explains how installs older than
0.25.0, which launched `@main`, pinned the package between hook calls. A failed first start is
covered in [troubleshooting](../guides/troubleshooting.md#the-plugins-first-start). For pinning a
version in your own commands, see
[pinned releases](../getting-started/installation.md#mutable-development-references).
