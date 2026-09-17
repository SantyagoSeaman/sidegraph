# Claude Code setup

Happy-path wiring for Sidegraph in [Claude Code](https://docs.claude.com/en/docs/claude-code).
Two ways to get there: the **plugin** (one step, recommended) or **manual registration** (MCP
server + hooks registered separately — for fine control, or a source checkout). For how these
pieces work under the hood — what each hook injects, the plugin install path, env vars, cwd
caveats — see the deep dive at
[`integrations/claude-code.md`](../integrations/claude-code.md).

Run these in the repo you want memory over (not the Sidegraph checkout).

## Option A — plugin (recommended)

Inside a Claude Code session, in that repo:

> **`@main` is a mutable ref.** Every `git+…@main` command on this page tracks the
> branch: what you install today is not what you installed yesterday, and a `uvx` cache
> refresh can change it under you. Fine for trying Sidegraph out; for anything you depend
> on — CI, a shared team setup, a pilot you intend to measure — replace `@main` with a
> commit SHA (`git+https://github.com/SantyagoSeaman/sidegraph@<sha>`) so the version is a
> decision you made rather than whatever HEAD happened to be. See [`reference/stability.md`](../reference/stability.md) for what each surface promises.

```
/plugin marketplace add SantyagoSeaman/sidegraph
/plugin install sidegraph@sidegraph
```

Installs the MCP server and all three hooks (`SessionStart`, `Stop`, `PreToolUse`) in one
step, pointed at `SIDEGRAPH_DIR=.sidegraph`/`SIDEGRAPH_GRAPH=graphify-out/graph.json` by
default. It runs everything via `uvx --from git+https://github.com/SantyagoSeaman/
sidegraph.git@main` under the hood, so it builds straight from this repository — no PyPI
publish needed, works today. See
[the plugin install path](../integrations/claude-code.md#plugin-install-path) for exactly
what gets registered and the cwd-pinning details. Skip to [Verify](#verify) once installed.

## Option B — manual registration

Prefer explicit `.mcp.json`/`.claude/settings.json` files (e.g. for review in a PR), or want
to point at a source checkout? Register the two pieces yourself.

### 1. Register the MCP server

Create (or merge into) `.mcp.json`:

```json
{
  "mcpServers": {
    "sidegraph": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/SantyagoSeaman/sidegraph.git@main", "sidegraph-mcp"],
      "env": {
        "SIDEGRAPH_DIR": ".sidegraph",
        "SIDEGRAPH_GRAPH": "graphify-out/graph.json"
      }
    }
  }
}
```

Or via the CLI — carry both env vars so this lands on the same store path as the JSON above:

```bash
claude mcp add sidegraph -s project --env SIDEGRAPH_DIR=.sidegraph --env SIDEGRAPH_GRAPH=graphify-out/graph.json -- uvx --from git+https://github.com/SantyagoSeaman/sidegraph.git@main sidegraph-mcp
```

`-s project` writes a repo-committed `.mcp.json`, so the whole team gets the server
through git — the same merge-like-code convention as the decision store itself. Drop the
flag if you want a private, user-local registration instead.

**From a source checkout instead** (contributors — see
[installation: from source](installation.md#option-d--from-source-contributors)): replace the
`uvx --from git+...` invocation above with
`uv run --project /ABSOLUTE/PATH/TO/sidegraph sidegraph-mcp` (CLI form: `-- uv run --project
/ABSOLUTE/PATH/TO/sidegraph sidegraph-mcp`).

> **Once Sidegraph is published to PyPI**, both shorten further, to `uvx --from sidegraph
> sidegraph-mcp` — see
> [`integrations/claude-code.md`](../integrations/claude-code.md#plugin-install-path)
> for the pin-at-1.0 policy.

The store (`SIDEGRAPH_DIR`) is meant to live **inside the repo it documents** — commit
`.sidegraph/` alongside your code (its committed record directories, not the gitignored
`index.db`), not the Sidegraph source tree.

### 2. Add the hooks

Add to `.claude/settings.json` (merge into an existing file):

```json
{
  "hooks": {
    "SessionStart": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "SIDEGRAPH_DIR=.sidegraph SIDEGRAPH_GRAPH=graphify-out/graph.json uvx --from git+https://github.com/SantyagoSeaman/sidegraph.git@main sidegraph-session-start"
          }
        ]
      }
    ],
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "SIDEGRAPH_DIR=.sidegraph SIDEGRAPH_GRAPH=graphify-out/graph.json uvx --from git+https://github.com/SantyagoSeaman/sidegraph.git@main sidegraph-stop"
          }
        ]
      }
    ],
    "PreToolUse": [
      {
        "matcher": "Read|Grep|Edit|Write",
        "hooks": [
          {
            "type": "command",
            "command": "SIDEGRAPH_DIR=.sidegraph SIDEGRAPH_GRAPH=graphify-out/graph.json uvx --from git+https://github.com/SantyagoSeaman/sidegraph.git@main sidegraph-pre-tool-use"
          }
        ]
      }
    ]
  }
}
```

From a source checkout, replace each `uvx --from git+... <entrypoint>` above with `uv run
--project /ABSOLUTE/PATH/TO/sidegraph <entrypoint>` (see the note in step 1).

The env vars are inlined into the command because Claude Code hook entries have no separate
`env`/`cwd` fields.

- `SessionStart` injects a compact project-memory map at the start of every session, led by a
  standing instruction to call `get_task_context` before any search — bash grep/rg/find and
  MCP structure-query tools included, not just `Read`/`Grep` (see
  [`reference/hooks.md`](../reference/hooks.md#sidegraph-session-start)).
- `Stop` nudges the agent, at most once per session and only once the session has produced
  **>= 2 real user prompts** (a substance gate — a session's very first `Stop` no longer
  triggers it), to propose durable decisions via `propose_decisions` before it finishes; set
  `SIDEGRAPH_CAPTURE_NUDGE=off` (alongside the other env vars in the command) to disable it
  entirely. See [`reference/hooks.md`](../reference/hooks.md#sidegraph-stop) for the exact
  nudge text and the substance gate's mechanics.
- `PreToolUse` redirects a blind `Read`/`Grep` on a source file toward
  `get_task_context`/`drill_down` with a one-line, non-blocking nudge: a generic form and a
  path-specific form, each firing at most once per session on its own one-shot key, so a
  session can see up to two, only when the store actually has decision memory to offer. Set
  `SIDEGRAPH_GREP_NUDGE=off` (alongside the other env vars in the command) to disable it.

> **Don't also run `graphify claude install`.** It writes its own `PreToolUse` hooks into
> `.claude/settings.json` that nudge toward `graphify query` on every read — redundant with
> the Sidegraph hook above (and noisier). They coexist without breaking anything, but you
> don't need both. See
> [why, and how to keep both cleanly if you want to](../integrations/graphify.md#graphify-claude-install-is-redundant-with-the-sidegraph-plugin--skip-it).

## Verify

Start Claude Code in the repo (`claude`). You should see project memory injected at session
start (a stub if the store is empty — that's expected). Then follow the
[quickstart](quickstart.md) to record and retrieve your first decision.

## Reset / cleanup

Everything Sidegraph owns in the target repo is `.sidegraph/`; everything Graphify owns is
`graphify-out/`. Delete both to start over — your source files are never touched.
