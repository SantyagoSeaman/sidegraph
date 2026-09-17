# Claude Code integration

The deep dive on how Sidegraph wires into Claude Code. For the copy-paste happy path, see
[`getting-started/claude-code-setup.md`](../getting-started/claude-code-setup.md).

## Manual MCP registration

Add to the target repo's `.mcp.json` (create it if absent):

> **`@main` is a mutable ref.** Every `git+…@main` command on this page tracks the
> branch: what you install today is not what you installed yesterday, and a `uvx` cache
> refresh can change it under you. Fine for trying Sidegraph out; for anything you depend
> on — CI, a shared team setup, a pilot you intend to measure — replace `@main` with a
> commit SHA (`git+https://github.com/SantyagoSeaman/sidegraph@<sha>`) so the version is a
> decision you made rather than whatever HEAD happened to be. See [`reference/stability.md`](../reference/stability.md) for what each surface promises.

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

**Once Sidegraph is published to PyPI**, both shorten further — no git/checkout path needed:

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

> When you need to find or understand code in this project, call get_task_context(seeds)
> before any grep or file search — decisions, gotchas and a domain map are indexed here.

Unlike the `PreToolUse` nudge below (which only ever fires on `Read`/`Grep`, and fires each
of its two forms, generic and path-specific, at most once per session), this instruction is
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

Matcher `Read|Grep` (see the snippet in
[`claude-code-setup.md`](../getting-started/claude-code-setup.md#2-add-the-hooks)). On a
`Read`/`Grep` call that looks like it targets a file (any string `file_path`/`path`/`pattern`
argument — deliberately permissive), when the store has >= 1 accepted domain or >= 1 valid
decision, it emits a **non-blocking**, `additionalContext`-only `hookSpecificOutput` nudge
toward `get_task_context`/`drill_down` — no `permissionDecision` field is set, so the tool call
is never denied, escalated, or auto-approved; it only gets annotated with the nudge text, and
then runs through Claude Code's normal permission flow exactly as it would have otherwise.
Fires at most once per session for each of its two forms, generic and path-specific (each
guarded by its own session-scoped marker in the store's `meta` table, separate from the
`Stop` hook's own capture ledger so the guards don't consume each other's one-shot), so a
session can see up to two of these nudges. Set `SIDEGRAPH_GREP_NUDGE=off` to disable it
entirely. Like the other two hooks, it never crashes
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

> **Works without PyPI.** The plugin's bundled `.mcp.json`/`hooks.json`
> (`plugin/sidegraph/.mcp.json`, `plugin/sidegraph/hooks/hooks.json`) run
> `uvx --from git+https://github.com/SantyagoSeaman/sidegraph.git@main sidegraph-mcp` —
> installed straight from the repository, so the plugin is functional as soon as the repo
> exists, PyPI or not. `uv` caches the build after the first run; a private repository works
> too as long as your local git credentials can clone it (if you authenticate over SSH,
> substitute `git+ssh://git@github.com/...` in the two files after installing). Once
> Sidegraph publishes to PyPI, the commands switch to the package form (`uvx --from
> sidegraph`), and the policy is to pin an exact `sidegraph==X.Y.Z` starting with the first
> stable (1.0) release — pinning earlier would add a version axis that drifts during fast
> iteration.

## Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `SIDEGRAPH_DIR` | `.sidegraph` | Path to the decision store directory (file-per-record, git-committed). Point it at a path inside the target repo, e.g. `.sidegraph`, so it can be committed as the repo's memory. `SIDEGRAPH_DB` is honored for back-compat (deprecated) — see [`reference/configuration.md`](../reference/configuration.md#store-path-resolution). |
| `SIDEGRAPH_GRAPH` | `graphify-out/graph.json` | Path to Graphify's output graph, read-only. |
| `SIDEGRAPH_CAPTURE_NUDGE` | unset | Set to `off` to disable the `Stop` hook's block-to-distill nudge entirely (no other value has any effect). |
| `SIDEGRAPH_GREP_NUDGE` | unset | Set to `off` to disable the `PreToolUse` Read/Grep redirect nudge entirely (no other value has any effect). |
| `SIDEGRAPH_RATIFY_NUDGE` | unset | Set to `off` to disable the `SessionStart` pending-ratification line entirely (no other value has any effect). |
| `SIDEGRAPH_AUTO_ACCEPT` | unset | Set to `on` to land agent-proposed decisions/facts as `accepted` immediately, bypassing the ratification queue (no other value has any effect; domains are always exempt). See [`guides/capturing-decisions.md#4-auto-accept-opt-in`](../guides/capturing-decisions.md#4-auto-accept-opt-in). |

`SIDEGRAPH_DIR`/`SIDEGRAPH_GRAPH` are resolved relative to the **working directory of the
process that reads them** — which is the crux of the cwd caveat below.

## The cwd caveat

`SIDEGRAPH_DIR`/`SIDEGRAPH_GRAPH` are plain relative-path lookups against `os.getcwd()`; there
is no path resolution against the Claude Code project root in the current source. That's fine
when Claude Code launches the MCP server/hooks with the project directory as cwd — the common
case for the manual `.mcp.json`/`.claude/settings.json` setup above. It is **not guaranteed**
for a plugin-distributed MCP server or hook, where cwd can differ from the project root.

Two mitigations, both config-level (no source change needed to use them):

- **MCP servers**: `.mcp.json` supports a `"cwd"` field per server, but it is **not** part of
  Claude Code's documented MCP server schema (documented fields are `command`/`args`/`env`
  and a couple of transport-specific ones) — so the plugin doesn't actually rely on it to pin
  the working directory. Instead, `plugin/sidegraph/.mcp.json` wraps the real command in a
  shell that `cd`s into the project root first (`env` omitted below for brevity — the real
  file also carries `SIDEGRAPH_DIR`/`SIDEGRAPH_GRAPH` there, same as the hook command below):
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
- **Hooks**: hook command entries have no `cwd`/`env` fields at all, so the command itself
  carries a `cd` prefix: `cd "${CLAUDE_PROJECT_DIR}" && SIDEGRAPH_DIR=... SIDEGRAPH_GRAPH=... uvx --from git+https://github.com/SantyagoSeaman/sidegraph.git@main sidegraph-session-start` (the actual, current content of `plugin/sidegraph/hooks/hooks.json`; a source-checkout wiring would use `uv run --project /ABSOLUTE/PATH/TO/sidegraph sidegraph-session-start` in its place instead).

Belt and braces: the hook entry points themselves also anchor relative
`SIDEGRAPH_DIR`/`SIDEGRAPH_GRAPH` values to `CLAUDE_PROJECT_DIR` when Claude Code exports it,
so even a hook invoked without the `cd` prefix resolves paths correctly. The MCP server
deliberately does not do this env-anchoring itself (the portable core stays host-agnostic) —
it leans on the plugin's `sh -c` wrapper above to fix its cwd instead.
