# Claude Code integration

The deep dive on how Sidegraph wires into Claude Code. For the copy-paste happy path, see
[`getting-started/claude-code-setup.md`](../getting-started/claude-code-setup.md).

## Manual MCP registration

Add to the target repo's `.mcp.json` (create it if absent):

The commands below follow the current development branch. For durable environments, see
[how to pin mutable development references](../getting-started/installation.md#mutable-development-references).

```json
{
  "mcpServers": {
    "sidegraph": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/SantyagoSeaman/sidegraph.git@main", "sidegraph-mcp"],
      "env": { "SIDEGRAPH_DIR": ".sidegraph", "SIDEGRAPH_GRAPH": "graphify-out/graph.json" }
    }
  }
}
```

Or via the CLI — carry both env vars so this setup lands on the same store path as every
other setup path (manual, plugin, Codex):

```bash
claude mcp add sidegraph -s project --env SIDEGRAPH_DIR=.sidegraph --env SIDEGRAPH_GRAPH=graphify-out/graph.json -- uvx --from git+https://github.com/SantyagoSeaman/sidegraph.git@main sidegraph-mcp
```

**From a source checkout** (contributors — see
[installation: from source](../getting-started/installation.md#option-d--from-source-contributors)),
replace the `uvx --from git+...` invocation in either form above with `uv run --project
/ABSOLUTE/PATH/TO/sidegraph sidegraph-mcp`.

The released package is available from PyPI, so both shorten further — no git/checkout path
needed:

```bash
claude mcp add sidegraph -s project --env SIDEGRAPH_DIR=.sidegraph --env SIDEGRAPH_GRAPH=graphify-out/graph.json -- uvx --from sidegraph sidegraph-mcp
```

```json
{ "mcpServers": { "sidegraph": { "command": "uvx", "args": ["--from", "sidegraph", "sidegraph-mcp"], "env": { "SIDEGRAPH_DIR": ".sidegraph", "SIDEGRAPH_GRAPH": "graphify-out/graph.json" } } } }
```

## Hooks in detail

Three hooks, all console scripts, all read the same env vars as the server (see below).

### `SessionStart` — `sidegraph-session-start`

Runs a best-effort lazy sync (`maybe_sync`) against the current graph, then injects
`additionalContext` from one of two renderers, chosen by whether the store has any
**accepted domains** (see [mind model](../concepts/mind-model.md) and
[retrieval: SessionStart TOC](../concepts/retrieval.md#sessionstart-toc)):

- **Once at least one domain is ratified**, `render_toc()` renders the real, domain-named
  table of contents: one line per accepted domain (title, one-line summary, mistake count,
  subdomain count) — the normal steady-state view once a project's mind model has any names
  in it.
- **Before any domain is ratified**, `top_tier_map()` is the fallback — a **phase-1 top-tier
  map** with a nameless `## Communities` section (the largest communities in the graph by
  member count, labeled by their god node) in place of `## Domains`.

Either way, `session_start` prepends the same standing instruction ahead of whichever
renderer's text follows — added once at the hook-assembly level, not inside either renderer
(see [`reference/hooks.md`](../reference/hooks.md#sidegraph-session-start)):

> When you need to find or understand code in this project, call get_task_context(files=[…])
> with repo-relative paths before any grep or file search — decisions, gotchas and a domain map
> are indexed here. Before a non-trivial change, run the sidegraph check-plan skill if it is
> available. If the tool is listed only by name, load it first.

Unlike the `PreToolUse` nudge below (which only ever fires on `Read`/`Grep`, and fires each
of its two forms, generic and path-specific, at most once per agent), this instruction is
unconditional and covers every search surface — bash
`grep`/`rg`/`find`, MCP structure-query tools, everything — by telling the agent up front,
not by gating a specific tool call.

Both renderers also inject:

- **Initiatives** — named work threads decisions have been grouped under, if any.
- **Global mistakes & constraints** — the most recent `gotcha`/`lesson`/`constraint`
  decisions scoped `global` (not tied to one entity), capped at 10.

Finally, unless `SIDEGRAPH_RATIFY_NUDGE=off`, one more line — own `try/except`, so a count
failure never costs the map above it — whenever the pending-ratification queue is non-empty
(nothing appended at zero):

> Sidegraph: N record(s) awaiting ratification (X decisions, Y facts, Z domains; oldest D days) — review
> with the ratify MCP tool or sidegraph-ratify.

The count is proposed decisions, standalone proposed facts (one riding a proposed decision's
cascade is covered by it and never double-counted), and proposed domains — see
[`reference/hooks.md`](../reference/hooks.md#sidegraph-session-start) for the exact
`Store.pending_ratification_counts()` decomposition.

If the store is empty and no graph exists, this renders a short stub — that's expected on a
fresh repo, not an error.

### `Stop` — `sidegraph-stop`

A **guarded block-to-distill**, gated on the session actually looking substantial: at most
once per session (a per-session capture ledger in the store, plus Claude Code's own
`stop_hook_active` re-entrancy flag), and only once the transcript has produced **>= 2 real
user prompts** (counting actual typed prompts, not tool-result noise — a session's very first
`Stop` no longer triggers it; see [`reference/hooks.md`](../reference/hooks.md#sidegraph-stop)
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
or an opt-in `SIDEGRAPH_RATIFY_POLICY` accepts an eligible proposal at write time with an
`auto:<policy>` stamp (see [configuration](../reference/configuration.md)).
Set `SIDEGRAPH_CAPTURE_NUDGE=off` to disable this hook entirely.

Claude Code renders the block response under an error-styled banner (`Stop hook error: ...`)
— that's the host's standard styling for any `decision: block` response, not a failure; the
"error" text IS the nudge, and `suppressOutput: true` just keeps the raw JSON out of the
transcript.

### `PreToolUse` — `sidegraph-pre-tool-use`

Matcher `Read|Grep|Edit|Write` (see the snippet in
[`claude-code-setup.md`](../getting-started/claude-code-setup.md#2-add-the-hooks)). On a
`Read`/`Grep` call that looks like it targets a file (any string `file_path`/`path`/`pattern`
argument — deliberately permissive), when the store has >= 1 accepted domain or >= 1 valid
decision, it emits a **non-blocking**, `additionalContext`-only `hookSpecificOutput` nudge
toward `get_task_context`/`drill_down` — no `permissionDecision` field is set, so the tool call
is never denied, escalated, or auto-approved; it only gets annotated with the nudge text, and
then runs through Claude Code's normal permission flow exactly as it would have otherwise.
Fires at most once per agent for each of its two forms, generic and path-specific (each
guarded by its own marker in the store's `meta` table, separate from the `Stop` hook's own
capture ledger so the guards don't consume each other's one-shot), so one agent can see up
to two of these nudges. The session's own agent and every subagent it starts count
separately, because Claude Code gives a subagent's hook payload its parent's session id and
its own `agent_id`: without that, the main agent's nudge would have used up the one a
subagent needed. Set `SIDEGRAPH_GREP_NUDGE=off` to disable it
entirely. The matcher also admits `Edit` and `Write`: those reach the process only to be
recorded as touch events (no nudge). Like the other two hooks, it never crashes
or blocks the tool call: any failure (or the store simply having nothing to offer yet) prints
`{}`.

### Silent degradation

All three hooks are written to **never crash the session** and never block a tool call. Any
exception inside `session_start` prints `{}` (Claude Code proceeds with no injected context);
any exception inside `stop` prints `{}` (Claude Code proceeds to stop normally); any exception
inside `pre_tool_use` — or the nudge conditions simply not holding — also prints `{}` (the
tool call proceeds through the normal permission flow). A missing store, missing graph, or
misconfigured env var degrades to "no memory this session," never a broken session.

## Plugin install path

Sidegraph ships a Claude Code plugin + marketplace manifest **in the repo itself** — this is
the recommended install path, and it works today, no PyPI publish required:

```
/plugin marketplace add SantyagoSeaman/sidegraph
/plugin install sidegraph@sidegraph
```

> **Public and development manifests differ.** The marketplace receives
> `plugin/sidegraph/.mcp.public.json` and `plugin/sidegraph/hooks/hooks.public.json`, renamed
> to the canonical filenames during the public release. Those variants run `uvx --from
> git+https://github.com/SantyagoSeaman/sidegraph.git@main …`, so the plugin deliberately
> follows the public repository rather than the PyPI package. The unsuffixed files in this
> development repository run the checkout the plugin was loaded from, with
> `UV_PROJECT_ENVIRONMENT=.venv uv run --project "${CLAUDE_PLUGIN_ROOT}/../.." --package
> sidegraph --frozen --no-active`, in whatever project the session is in.

## Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `SIDEGRAPH_DIR` | `.sidegraph` | Path to the decision store directory (file-per-record, git-committed). Point it at a path inside the target repo, e.g. `.sidegraph`, so it can be committed as the repo's memory. `SIDEGRAPH_DB` is honored for back-compat (deprecated) — see [`reference/configuration.md`](../reference/configuration.md#store-path-resolution). |
| `SIDEGRAPH_GRAPH` | `graphify-out/graph.json` | Path to Graphify's output graph, read-only. |
| `SIDEGRAPH_CAPTURE_NUDGE` | unset | Set to `off` to disable the `Stop` hook's block-to-distill nudge entirely (no other value has any effect). |
| `SIDEGRAPH_GREP_NUDGE` | unset | Set to `off` to disable the `PreToolUse` Read/Grep redirect nudge entirely (no other value has any effect). |
| `SIDEGRAPH_RATIFY_NUDGE` | unset | Set to `off` to disable the `SessionStart` pending-ratification line entirely (no other value has any effect). |
| `SIDEGRAPH_AUTO_ACCEPT` | unset | Set to `on` to land agent-proposed decisions/facts as `accepted` immediately, bypassing the ratification queue (no other value has any effect; domains are always exempt). See [`guides/capturing-decisions.md#4-auto-accept-opt-in`](../guides/capturing-decisions.md#4-auto-accept-opt-in). |

A relative `SIDEGRAPH_DIR` is anchored to the **launch directory**: `CLAUDE_PROJECT_DIR` for the
hooks, the working directory of the process for the MCP server and the CLI. On the host surfaces
(the hooks and the MCP server), a relative store that does not exist there is then looked up in the
parent directories, up to the repository's `.git` boundary (see
[Launching in a subdirectory](#launching-in-a-subdirectory)); the CLI never does. A relative
`SIDEGRAPH_GRAPH` is resolved against the project of the store `SIDEGRAPH_DIR` names (the store's
parent directory) by the CLI, the MCP server and the hooks. The `SIDEGRAPH_DIR` half is the crux of
the cwd caveat below.

## The cwd caveat

A relative `SIDEGRAPH_DIR` is anchored to the launch directory: `CLAUDE_PROJECT_DIR` for the hooks
(the directory Claude Code was started in, not necessarily the repository root), `os.getcwd()` for
the MCP server and the CLI. With the default `.sidegraph` in the project, `SIDEGRAPH_GRAPH`
therefore also lands in the project. Where the relative store does not exist at that anchor, the
hooks and the MCP server look for it in the parent directories, up to the repository's `.git`
boundary (see [Launching in a subdirectory](#launching-in-a-subdirectory)); the CLI never does.
Anchoring to the launch directory is fine when Claude Code launches the MCP server/hooks with the
project directory as cwd — the common case for the manual `.mcp.json`/`.claude/settings.json` setup
above. It is **not guaranteed** for a plugin-distributed MCP server or hook, where cwd can differ
from the project root.

Two mitigations, both config-level (no source change needed to use them):

- **MCP servers**: `.mcp.json` supports a `"cwd"` field per server, but it is **not** part of
  Claude Code's documented MCP server schema (documented fields are `command`/`args`/`env`
  and a couple of transport-specific ones) — so the plugin doesn't actually rely on it to pin
  the working directory. Instead, the published `.mcp.json` (sourced from
  `plugin/sidegraph/.mcp.public.json`) wraps the real command in a shell that `cd`s into the
  project root first (`env` omitted below for brevity):
  ```json
  {
    "command": "sh",
    "args": ["-c", "cd \"${CLAUDE_PROJECT_DIR}\" && exec uvx --from git+https://github.com/SantyagoSeaman/sidegraph.git@main sidegraph-mcp"],
    "cwd": "${CLAUDE_PROJECT_DIR}"
  }
  ```
  The `sh -c 'cd ... && exec ...'` wrapper is what actually pins cwd — `exec` replaces the
  shell with the `uvx` process, so no extra process is left in between. The `"cwd"` field is
  kept alongside it as harmless belt-and-braces: if a future Claude Code version does start
  honoring it, that's a free bonus, not something the plugin depends on today.
- **Hooks**: hook command entries have no `cwd`/`env` fields, so the public command carries a
  `cd` prefix: `cd "${CLAUDE_PROJECT_DIR}" && SIDEGRAPH_DIR=... SIDEGRAPH_GRAPH=... uvx --from
  git+https://github.com/SantyagoSeaman/sidegraph.git@main sidegraph-session-start`. This is
  the content of `plugin/sidegraph/hooks/hooks.public.json`; the release renames it to
  `hooks.json`. The development variant uses the same prefix and runs the checkout the plugin
  was loaded from: `UV_PROJECT_ENVIRONMENT=.venv uv run --project "${CLAUDE_PLUGIN_ROOT}/../.."
  --package sidegraph --frozen --no-active`. Every hook command also ends with a guard (`|| printf '{}\n'`, and for
  `SessionStart` a `systemMessage` telling you to run the hook command in a terminal) that
  turns a command that cannot start into exit 0: Claude Code reads exit 2 with text on stderr
  as a block on `Stop` and `PreToolUse`, and `uv` exits 2 on some of its own errors.

Belt and braces: the hook entry points themselves also anchor a relative `SIDEGRAPH_DIR` to
`CLAUDE_PROJECT_DIR` when Claude Code exports it, so even a hook invoked without the `cd` prefix
resolves the store correctly; the graph then follows the store. The MCP server deliberately does
not do this env-anchoring itself (the portable core stays host-agnostic) — it leans on the
plugin's `sh -c` wrapper above to fix its cwd instead.

### Launching in a subdirectory

`CLAUDE_PROJECT_DIR` is the directory Claude Code was launched in, not the repository root: a
launch in `repo/pkg` gives `repo/pkg`. The hooks and the MCP server therefore anchor the relative
`.sidegraph` to `repo/pkg`. Where it does not exist there, they look for it in the parent
directories, up to the repository root (the nearest directory with a `.git` entry), and use the
first one found, so a launch in `repo/pkg` uses `repo/.sidegraph` and does not create an empty
second store. A store that exists in the launch directory still wins, which keeps a per-package
store working. Touches are recorded relative to the directory the store was found in (`pkg/a.py`),
the same as every anchor.

This applies wherever a subdirectory launch reads the plugin. A launch in `repo/pkg` does not read
`repo/.claude/settings.json` (measured with Claude Code 2.1.287), so a plugin enabled only there is
not active for it: enable it at user scope, or in the subdirectory's own `.claude/settings.json`.

A session that an older Sidegraph started in a subdirectory may have left a `.sidegraph` there. An
empty one is named by `SessionStart` together with the repository's store; remove it to use the
repository's. One that holds any record (a single proposed decision from an old capture is enough)
is not reported and keeps hiding the repository's store: review it with
`sidegraph-ratify --db <subdirectory>/.sidegraph`, record what matters again in the repository's
store, then remove the subdirectory's directory. A `.git` entry at your home directory is not a
repository root for the lookup, and the CLI does not search at all: from a subdirectory, pass
`--db`.
