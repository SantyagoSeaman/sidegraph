# Codex CLI integration

The deep dive on how Sidegraph wires into the OpenAI Codex CLI. For the copy-paste happy
path, see [`getting-started/codex-setup.md`](../getting-started/codex-setup.md).

Codex support is newer and less iterated than the Claude Code integration. It reuses the same
MCP server and hook entry points — no Codex-specific code exists in Sidegraph — because Codex
CLI (as documented mid-2026, when this page was last verified) ships **GA hooks with the same
event vocabulary as Claude Code** (including `additionalContext` on `SessionStart`) and its own
MCP server support. Codex CLI's config/hooks surface is younger and more likely to move than
Claude Code's — **verify the `config.toml`/hooks shapes below against the current Codex CLI
docs** before relying on them if some time has passed since mid-2026.

## `config.toml` reference

Codex CLI reads MCP server definitions from `~/.codex/config.toml` under `[mcp_servers.*]`
tables. Note this file is **global**, not per-project, so each entry should pin its own
working directory rather than relying on ambient cwd:

```toml
[mcp_servers.sidegraph]
command = "bash"
args = [
  "-lc",
  "cd /ABSOLUTE/PATH/TO/your-repo && SIDEGRAPH_DIR=.sidegraph SIDEGRAPH_GRAPH=graphify-out/graph.json uv run --project /ABSOLUTE/PATH/TO/sidegraph sidegraph-mcp"
]
```

Register the same thing with the CLI:

```bash
codex mcp add sidegraph -- bash -lc "cd /ABSOLUTE/PATH/TO/your-repo && SIDEGRAPH_DIR=.sidegraph SIDEGRAPH_GRAPH=graphify-out/graph.json uv run --project /ABSOLUTE/PATH/TO/sidegraph sidegraph-mcp"
```

`SIDEGRAPH_DIR` (default `.sidegraph`) and `SIDEGRAPH_GRAPH` (default
`graphify-out/graph.json`) are the same two environment variables the Claude Code integration
reads — see [`claude-code.md`](claude-code.md#environment-variables) for their meaning.

## Hooks (GA)

Project-scoped, at `.codex/hooks/hooks.json`:

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

The `cd "$(git rev-parse --show-toplevel 2>/dev/null || pwd)"` prefix stands in for Claude
Code's `"cwd": "${CLAUDE_PROJECT_DIR}"` plugin field — Codex hook entries, like Claude Code's,
have no dedicated `cwd`/`env` fields, so the working directory has to be pinned inside the
command itself. The `|| pwd` fallback matters because Sidegraph explicitly supports non-git
corpora (a plain folder of docs Graphify was pointed at directly — see
[`graphify.md`](graphify.md#non-git-and-doc-only-corpora)): a bare
`git rev-parse --show-toplevel` fails outside a git repo and would leave `cd` with no argument,
breaking the hook entirely, whereas falling back to `pwd` at least keeps the relative
`SIDEGRAPH_DIR`/`SIDEGRAPH_GRAPH` paths anchored to wherever Codex launched the hook from.
`sidegraph-session-start` emits the same `additionalContext` payload described in
[`claude-code.md`](claude-code.md#sessionstart--sidegraph-session-start) (communities,
initiatives, global mistakes); `sidegraph-stop` emits the same guarded once-per-session
block-to-distill nudge. Both degrade silently on any failure — a missing store or graph never
blocks the session.

Codex only loads project-local `.codex/` hooks (like the ones above) when the project layer
is marked trusted — if these hooks silently don't fire, check the trust prompt/settings before
assuming the JSON above is wrong.

No `PreToolUse` entry is offered above: Codex CLI does have a `PreToolUse` event, but as of
this writing it only intercepts Bash/patch/MCP tool calls (file-read tools like Read/Grep don't
fire hook events there yet), so the `sidegraph-pre-tool-use` Read/Grep redirect nudge (see
[`claude-code.md`](claude-code.md#pretooluse--sidegraph-pre-tool-use)) has nothing to attach
to on Codex today — verify against current Codex CLI docs before assuming otherwise, and wire
it (same entry point) once tool coverage catches up.

## AGENTS.md pattern

Codex reads `AGENTS.md` as project instructions, the same role `CLAUDE.md` plays for Claude
Code. Add a section pointing the agent at Sidegraph's tools:

```markdown
## Sidegraph — decision memory

This repo's `.sidegraph/` holds durable decisions, lessons, gotchas, and
constraints, anchored to code/doc entities via Graphify's graph. Before editing an area you
haven't touched this session, call `get_task_context` (files=[...]) — mistakes are ranked
first. When you make a real decision or hit a hard-won gotcha, call `propose_decisions`
(What/Why/Where/Learned) before the session ends.
```

This is a manual substitute for the standing instruction Claude Code's `SessionStart` hook
injects automatically — worth keeping even after wiring the hook, since `AGENTS.md` is always
in context while injected `additionalContext` is session-scoped.

## Plugin install path

Sidegraph also ships a Codex plugin, the same one-line install as the Claude Code plugin:

```
/plugin marketplace add SantyagoSeaman/sidegraph
/plugin install sidegraph@sidegraph
```

This is the same repo-committed manifest set the Claude Code plugin uses, in Codex's own
shape: `.agents/plugins/marketplace.json` at the repo root, and
`plugin/sidegraph/.codex-plugin/plugin.json` naming the MCP config
(`plugin/sidegraph/codex/mcp.json`), the hooks config
(`plugin/sidegraph/codex/hooks.json`), and the skills directory (all 11 skills, each with
its own `agents/openai.yaml` declaring the `sidegraph` MCP dependency and whether Codex may
invoke it implicitly). No `PreToolUse` entry ships, for the reason given above.

**The cwd risk this page used to defer on is resolved, not sidestepped.** Codex plugin MCP
servers and hooks do not get a project-root variable. That was confirmed live with `codex mcp
list --json` against a locally installed build of this plugin (codex-cli 0.154.0): the
registered `sidegraph` server carries `"cwd": null`. So both `codex/mcp.json` and
`codex/hooks.json` wrap their commands the same way the manual `.codex/hooks/hooks.json`
recipe above does. They run `cd "$(git rev-parse --show-toplevel 2>/dev/null || pwd)"` before
anything else, instead of assuming an ambient project directory. That wrapper, not a
plugin-provided variable, is what keeps `SIDEGRAPH_DIR`/`SIDEGRAPH_GRAPH` pointed at the right
repo.

What was verified live (codex-cli 0.154.0, a throwaway `CODEX_HOME`): `codex plugin
marketplace add SantyagoSeaman/sidegraph` fetches the published repository by the
`owner/repo` shorthand, parses `.agents/plugins/marketplace.json` and resolves the plugin at
`./plugin/sidegraph`. `codex plugin add sidegraph@sidegraph` installs it, copying
`.codex-plugin/plugin.json`, `codex/mcp.json`, `codex/hooks.json`, and every skill's
`agents/openai.yaml` into the plugin cache unchanged. `codex mcp list --json` shows the
`sidegraph` server registered with the exact command from `codex/mcp.json`, and a
non-interactive `codex exec` session with that server registered called the Sidegraph tools
(`retrieve_decisions`, `list_proposed`, `query_decisions`, `find_entity`) and got real
records back. What was **not** independently verified: an actual `SessionStart`/`Stop` hook
firing end to end inside a real, authenticated Codex session. The CLI exposes no "list installed hooks" introspection command
this checkout could use to confirm it short of a live session. Also unverified against the
published docs: the top-level `mcpServers`/`hooks` fields in `.codex-plugin/plugin.json` are
not documented on `developers.openai.com/plugins/build/plugins` as of this writing, which
instead sketches an `extensions.com.openai` nesting. The live install above is the evidence
they work, not the published schema. Re-verify both if the Codex CLI you're on behaves
differently.
