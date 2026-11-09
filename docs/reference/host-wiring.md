# Claude Code hook wiring

> **Start here:** Use the section matching the command or payload you are checking. For everyday work, begin with the [documentation paths](../README.md). [Glossary](../concepts/glossary.md).

## Hooks in detail

The four hooks are console scripts. See the [host environment settings](../integrations/claude-code.md#environment-variables) and the [complete variable reference](configuration.md#environment-variables) for their configuration.

### `SessionStart` — `sidegraph-session-start`

Runs a best-effort lazy sync (`maybe_sync`) against the current graph, then injects
`additionalContext` from one of two renderers, chosen by whether the store has any
**accepted domains** (see [mind model](../concepts/mind-model.md) and
[retrieval: SessionStart TOC](../concepts/retrieval.md#sessionstart-toc)):

- **Once at least one domain is ratified**, `render_toc()` renders the real, domain-named
  table of contents: one line per accepted domain (title, mistake count,
  subdomain count) — the normal steady-state view once a project's mind model has any names
  in it.
- **Before any domain is ratified**, `top_tier_map()` is the fallback — a **phase-1 top-tier
  map** with a nameless `## Communities` section (the largest communities in the graph by
  member count, labeled by their god node) in place of `## Domains`.

Either way, `session_start` prepends the same standing instruction ahead of whichever
renderer's text follows — added once at the hook-assembly level, not inside either renderer
(see [`reference/hooks.md`](hooks.md#sidegraph-session-start)):

> When you need to find or understand code in this project, call get_task_context(files=[…])
> with repo-relative paths before any grep or file search — decisions, gotchas and a domain map
> are indexed here. Before a non-trivial change, run the sidegraph check-plan skill if it is
> available. If the tool is listed only by name, load it first.

Unlike the `PreToolUse` records block below (which fires only when a `Read`, `Edit`, `Write`,
`Grep` or `sed`/`grep`/`rg`/`cat` line names a file with records, each file at most once per
agent), this instruction is unconditional and covers every search surface — bash `find`, MCP
structure-query tools, a search that names no file, everything — by telling the agent up front,
not by gating a specific tool call.

The next line names the package the hooks run, so a session that launched an old install says so
in its own context: `Sidegraph X.Y.Z`, where `X.Y.Z` is the running version. It appears in every
`SessionStart` that injects the map.

Both renderers also inject:

- **Global mistakes & constraints** — the most recent `gotcha`/`lesson`/`constraint`
  decisions scoped `global` (not tied to one entity), capped at 10.
- **Unratified proposals** — proposed records inside the surfacing window, after everything
  accepted. The domain map lists proposed global mistakes only. The nameless map lists proposed
  decisions and facts, up to 10 of each.

The nameless map adds two more sections that the domain map does not have: **Recorded
decisions** (up to 10 accepted decisions that no section above already shows) and **Known
facts** (up to 10 accepted facts). So the global mistakes are not the only decisions a session
sees at start. Everything task-scoped still waits for `get_task_context`.

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

Three groups in the snippet (see
[`claude-code-setup.md`](../getting-started/claude-code-setup.md#2-add-the-hooks)): matcher
`Read|Grep|Edit|Write`, matcher `Bash` with four entries whose `if` is `Bash(sed *)`,
`Bash(grep *)`, `Bash(rg *)` and `Bash(cat *)`, and matcher `Agent|Task` (below). When a `Read`,
`Grep`, `Edit`, `Write` or Bash call names a file that decisions are anchored to, the hook hands
the agent those records in the same call, as a **non-blocking**, `additionalContext`-only
`hookSpecificOutput` (the `Agent|Task` group is the exception: it returns `updatedInput`, below):
the guard line, then for each file (at most three per call) its top two records, or three when the
first two are mistakes, each as `- [kind] title — first sentence of the choice (id …)`, and a
`More: get_task_context(files=[…])` line when records were left out. No `permissionDecision` field is set, so the tool call is never
denied, escalated, or auto-approved; it only gets annotated with the block, and then runs
through Claude Code's normal permission flow exactly as it would have otherwise.

Each file is delivered **once per agent**, up to **ten files per agent** (one statement in the
store's `meta` table counts and inserts, separate from the `Stop` hook's own capture ledger so the
guards don't consume each other). The session's own agent and every subagent it starts count
separately, because Claude Code gives a subagent's hook payload its parent's session id and its
own `agent_id`: without that, the main agent's delivery would have used up the one a subagent
needed. A call that names no file with records prints `{}`, and no counting form is printed
anywhere. Set `SIDEGRAPH_GREP_NUDGE=off` to stop delivery at the point of reading, on every tool
but `Agent` (that has its own switch, below); touch recording is not affected. A Bash line counts only for the regular files that its `sed`, `grep`, `rg` and `cat`
commands read (`head`, `awk` and `git grep` name nothing), and a Bash read is not a touch event;
the `if` field exists from Claude Code 2.1.85 and matching on compound command lines is correct
from 2.1.89, so use 2.1.89 or later; a host older than 2.1.85 runs the hook on every
Bash call and gets an early exit with no output (details in the
[hooks reference](hooks.md#bash-lines)). It reads the store's index directly rather
than opening the store (see the [hooks reference](hooks.md#sidegraph-pre-tool-use)),
so a call costs a few tens of milliseconds, and in a project with no store it does nothing and
creates nothing. Like the other hooks, it never crashes or blocks the tool call: any failure (or
nothing to deliver) prints `{}`.

After a new store or canonical index reload, record delivery and the `SubagentStart` brief
wait until a successful sync computes binding statuses. `SessionStart` and memory retrieval
attempt lazy sync; without a graph reader, these hot-hook outputs remain withheld until a
reader and successful sync are available. Touch recording and file claims remain available.

**Subagent briefs.** The third group, matcher `Agent|Task`, runs the same command when the agent
spawns a subagent. The brief the parent writes is where the task is, so the hook reads the files
it names (and, one hop away, the files named in a plan or notes document it names), and appends a
block with the records anchored to them to the brief: per file the top two records, at most six
lines and 3,000 characters in all, under a header that names the document when a plan named the
file. It returns the call's own input with only `prompt` changed (`updatedInput`), sets no
`permissionDecision`, and claims none of the per-agent keys above. **The block reaches the
subagent, in its first message, but the parent's view of its own `Agent` call does not show it**:
the parent's transcript keeps the prompt it wrote. It is on by default; set
`SIDEGRAPH_AGENT_BRIEF=off` to turn it off. In Claude Code's auto mode (`permission_mode: auto`)
it leaves the call unchanged: that mode reviews a call before hooks run and refuses an input a hook
changed. The subagent still gets the `SubagentStart` brief and the records of the files it reads.
Grok Build runs hooks before its permission gate, so it keeps the brief. It was not tested on agent-team teammates or `fork`
spawns, and Codex does not carry it. See the
[hooks reference](hooks.md#subagent-briefs).

### `SubagentStart` — `sidegraph-subagent-start`

No matcher. A subagent (Explore, Plan, general-purpose, or one you define) starts without the
`SessionStart` context, and Explore and Plan agents load no `CLAUDE.md` either, so nothing
standing tells one that decision memory exists. When Claude Code creates a subagent, this hook
answers with an `additionalContext` that arrives in front of its first turn: the same standing
call `SessionStart` gives the main agent (`get_task_context(files=[…])` with repo-relative
paths, the check-plan skill before a non-trivial change, and a reminder to load the tool first if
it is listed only by name), then one sentence of what memory holds: how many records are anchored
to code, in how many files, and how many of them are recorded mistakes. It carries no records:
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
graph leaves new records without code anchors and gives a shorter map; after a new store or canonical index
reload, hot-hook record delivery and briefs remain withheld until sync can run with a reader.
A missing store is not an error either:
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

The [launch-commit contract](hooks.md#the-launch-commit-file) explains how installs older than
0.25.0, which launched `@main`, pinned the package between hook calls. A failed first start is
covered in [troubleshooting](../guides/troubleshooting.md#the-plugins-first-start). For pinning a
version in your own commands, see
[pinned releases](../getting-started/installation.md#mutable-development-references).
