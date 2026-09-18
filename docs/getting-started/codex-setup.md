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

> **`@main` is a mutable ref.** Every `git+…@main` command on this page tracks the
> branch: what you install today is not what you installed yesterday, and a `uvx` cache
> refresh can change it under you. Fine for trying Sidegraph out. For anything you depend
> on, such as CI, a shared team setup, or a pilot you intend to measure, replace `@main`
> with a commit SHA (`git+https://github.com/SantyagoSeaman/sidegraph@<sha>`) so the version
> is a decision you made rather than whatever HEAD happened to be. See [`reference/stability.md`](../reference/stability.md) for what each surface promises.

```
/plugin marketplace add SantyagoSeaman/sidegraph
/plugin install sidegraph@sidegraph
```

Installs the MCP server, the `SessionStart`/`Stop` hooks, and all 11 skills in one step,
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

Prefer explicit `config.toml`/`.codex/hooks/hooks.json` files (e.g. for review in a PR), or
want to point at a source checkout? Register the pieces yourself.

### 1. Register the MCP server

Via the CLI:

```bash
codex mcp add sidegraph -- bash -lc "cd /ABSOLUTE/PATH/TO/your-repo && SIDEGRAPH_DIR=.sidegraph SIDEGRAPH_GRAPH=graphify-out/graph.json uv run --project /ABSOLUTE/PATH/TO/sidegraph sidegraph-mcp"
```

Or add directly to `~/.codex/config.toml`:

```toml
[mcp_servers.sidegraph]
command = "bash"
args = [
  "-lc",
  "cd /ABSOLUTE/PATH/TO/your-repo && SIDEGRAPH_DIR=.sidegraph SIDEGRAPH_GRAPH=graphify-out/graph.json uv run --project /ABSOLUTE/PATH/TO/sidegraph sidegraph-mcp"
]
```

`config.toml` is a single global file, not per-project, so the `cd` wrapper pins each
registration to the repo whose `.sidegraph/` it should read — use a distinct
`[mcp_servers.*]` name per repo if you wire up more than one.

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

Create `.codex/hooks/hooks.json` in the repo:

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

No `PreToolUse` entry is included above: Codex CLI's `PreToolUse` event (as of this writing)
only intercepts Bash/patch/MCP tool calls, not file-read tools like `Read`/`Grep`, so Claude
Code's Read/Grep redirect nudge
(`sidegraph-pre-tool-use`) has no Codex counterpart to wire up yet — see
[`integrations/codex.md`](../integrations/codex.md) for details.

## Verify

Start Codex CLI in the repo. You should see project memory injected at session start (a stub
if the store is empty). Then follow the [quickstart](quickstart.md) to record and retrieve
your first decision.

If nothing is injected and no error appears, check trust first: Codex only loads
project-local `.codex/` hooks when the project layer is marked trusted, so a hook that
silently never fires is often a trust prompt/setting, not a config mistake.
