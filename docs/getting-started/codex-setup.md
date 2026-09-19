# Codex setup

Happy-path wiring for Sidegraph in the [OpenAI Codex CLI](https://developers.openai.com/codex/cli).
Codex CLI ships GA hooks with the same event vocabulary as Claude Code (including
`additionalContext` on `SessionStart`), so the same Sidegraph entry points serve both hosts.
Codex CLI's config/hooks surface is younger and more likely to move than Claude Code's, so
verify the `config.toml`/hooks shapes below against the current Codex CLI docs before relying
on them if some time has passed since mid-2026. For the deeper mechanics, including exactly
what was verified live against a real Codex CLI install, see
[`integrations/codex.md`](../integrations/codex.md).

Run these in the repo you want memory over (not the Sidegraph checkout). Two ways to get
there: the **plugin** (one step, recommended) or **manual registration** (config.toml, hooks,
and AGENTS.md registered separately, for fine control or a source checkout).

## Option A: plugin (recommended)

Inside a Codex CLI session, in that repo:

The commands below follow the current development branch. For durable environments, see
[how to pin mutable development references](installation.md#mutable-development-references).

```
/plugin marketplace add SantyagoSeaman/sidegraph
/plugin install sidegraph@sidegraph
```

Installs the MCP server, the `SessionStart`/`Stop` hooks, and all 12 skills in one step,
pointed at `SIDEGRAPH_DIR=.sidegraph`/`SIDEGRAPH_GRAPH=graphify-out/graph.json` by default.
It runs everything via `uvx --from git+https://github.com/SantyagoSeaman/sidegraph.git@main`
under the hood, so it builds straight from this repository, no PyPI publish needed. See
[the plugin install path](../integrations/codex.md#plugin-install-path) for exactly what
gets registered and the cwd-pinning details, and what has and hasn't been verified live.

1. Trust the hooks: start an interactive `codex` session in the repo. Codex detects the two
   new Sidegraph hook definitions (`SessionStart`, `Stop`) and prompts you in the terminal to
   trust them. Approve to complete it. Skip or decline and the MCP tools still work, but
   retrieval at session start and the capture reminder at session end stay silent. A hook
   definition change in a later release needs a fresh approval. Review or re-approve anytime
   with `/hooks`.

Skip to [Verify](#verify) once installed.

## Option B: manual registration

Prefer explicit `.codex/config.toml`/`.codex/hooks.json` files (e.g. for review in a PR), or
want to point at a source checkout? Register the pieces yourself.

### 1. Register the MCP server

Via the CLI:

```bash
codex mcp add sidegraph -- bash -lc "cd /ABSOLUTE/PATH/TO/your-repo && SIDEGRAPH_DIR=.sidegraph SIDEGRAPH_GRAPH=graphify-out/graph.json uv run --project /ABSOLUTE/PATH/TO/sidegraph sidegraph-mcp"
```

Or add it to the repo's `.codex/config.toml` (recommended for project-specific setup):

```toml
[mcp_servers.sidegraph]
command = "bash"
args = [
  "-lc",
  "cd /ABSOLUTE/PATH/TO/your-repo && SIDEGRAPH_DIR=.sidegraph SIDEGRAPH_GRAPH=graphify-out/graph.json uv run --project /ABSOLUTE/PATH/TO/sidegraph sidegraph-mcp"
]
```

Codex also reads `~/.codex/config.toml`. Use the project file when the registration belongs
to this repository; use the global file only when you intentionally want the server in every
project. The `cd` wrapper remains useful in either scope because Sidegraph's relative paths
must resolve against the repository whose memory it serves.

### 2. Tell the agent about Sidegraph

Add to the repo's `AGENTS.md`:

```markdown
## Sidegraph — decision memory

This repo's `.sidegraph/` holds durable decisions, lessons, gotchas, and
constraints, anchored to code/doc entities via Graphify's graph. Before editing an area you
haven't touched this session, call `get_task_context` (files=[...]) — mistakes are ranked
first. When you make a real decision or hit a hard-won gotcha, call `propose_decisions`
(What/Why/Where/Learned) before the session ends.
```

### 3. Add the hooks

Create `.codex/hooks.json` in the repo:

```json
{
  "hooks": {
    "SessionStart": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "cd \"$(git rev-parse --show-toplevel 2>/dev/null || pwd)\" && SIDEGRAPH_DIR=.sidegraph SIDEGRAPH_GRAPH=graphify-out/graph.json uv run --project /ABSOLUTE/PATH/TO/sidegraph sidegraph-session-start"
          }
        ]
      }
    ],
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "cd \"$(git rev-parse --show-toplevel 2>/dev/null || pwd)\" && SIDEGRAPH_DIR=.sidegraph SIDEGRAPH_GRAPH=graphify-out/graph.json uv run --project /ABSOLUTE/PATH/TO/sidegraph sidegraph-stop"
          }
        ]
      }
    ]
  }
}
```

The `cd "$(git rev-parse --show-toplevel 2>/dev/null || pwd)"` prefix pins the hook's working
directory to the repo root regardless of where Codex actually launches it from, so the relative
`SIDEGRAPH_DIR` / `SIDEGRAPH_GRAPH` paths resolve correctly. The `|| pwd` fallback keeps this
working on a non-git corpus too (Sidegraph doesn't require the corpus to be a git repo — see
[`integrations/graphify.md`](../integrations/graphify.md#non-git-and-doc-only-corpora)), where a
bare `git rev-parse --show-toplevel` would fail and leave `cd` with no argument.

No `PreToolUse` entry is included above. Codex can invoke that event for local function tools,
but it has no stable `Read`/`Grep` tool pair to which Sidegraph's Claude-specific redirect can
attach. Therefore Claude Code's Read/Grep redirect nudge
(`sidegraph-pre-tool-use`) has no Codex counterpart to wire up yet — see
[`integrations/codex.md`](../integrations/codex.md) for details.

## Verify

Start Codex CLI in the repo. You should see project memory injected at session start (a stub
if the store is empty). Then follow the [quickstart](quickstart.md) to record and retrieve
your first decision.

If nothing is injected and no error appears, check trust first: Codex only loads
project-local `.codex/` hooks when the project layer is marked trusted, so a hook that
silently never fires is often a trust prompt/setting, not a config mistake.
