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

Codex CLI reads MCP server definitions under `[mcp_servers.*]` from project
`.codex/config.toml` and user `~/.codex/config.toml`. Prefer the project file for a server
that belongs to one repository. Pin the working directory rather than relying on ambient cwd:

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

Project-scoped, at `.codex/hooks.json`:

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

No `PreToolUse` entry is offered above. Codex can invoke the event for local function tools
(including shell, patch, and MCP calls), but it has no stable `Read`/`Grep` pair equivalent to
Claude Code's. The `sidegraph-pre-tool-use` Read/Grep redirect nudge (see
[`claude-code.md`](claude-code.md#pretooluse--sidegraph-pre-tool-use)) has nothing to attach
to on Codex today. Re-check the current [Codex hooks documentation](https://learn.chatgpt.com/codex/hooks)
before adding one; hosted tools are outside the hook surface.

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

1. Trust the hooks: start an interactive `codex` session in the repo. Codex detects the two
   new Sidegraph hook definitions (`SessionStart`, `Stop`) and prompts you in the terminal to
   trust them. Approve to complete it. Skip or decline and the MCP tools still work, but
   retrieval at session start and the capture reminder at session end stay silent (see why
   below). A hook definition change in a later release needs a fresh approval. Review or
   re-approve anytime with `/hooks`.

This is the same repo-committed manifest set the Claude Code plugin uses, in Codex's own
shape: `.agents/plugins/marketplace.json` at the repo root, and
`plugin/sidegraph/.codex-plugin/plugin.json` naming the MCP config
(`plugin/sidegraph/codex/mcp.json`), the hooks config
(`plugin/sidegraph/codex/hooks.json`), and the skills directory (all 12 skills, each with
its own `agents/openai.yaml` declaring the `sidegraph` MCP dependency and whether Codex may
invoke it implicitly). No `PreToolUse` entry ships, for the reason given above.

**The cwd risk this page used to defer on is resolved, not sidestepped.** Codex plugin MCP
servers and hooks do not get a project-root variable. That was confirmed live with `codex mcp
list --json` against a locally installed build of this plugin (codex-cli 0.154.0): the
registered `sidegraph` server carries `"cwd": null`. So both `codex/mcp.json` and
`codex/hooks.json` wrap their commands the same way the manual `.codex/hooks.json`
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
records back. `SessionStart` and `Stop` did not fire in that session, and that is expected,
not a bug. Codex gates hooks with two independent checks. Project trust (`trust_level` in
`config.toml`) is one. Hook trust is separate and hash-based: Codex records trust against a
hook definition's current hash and refuses to run a hook it has not seen approved at that
hash. Installing or enabling a plugin does not grant hook trust. Per the [Codex hooks
docs](https://learn.chatgpt.com/codex/hooks), Codex skips plugin-bundled hooks until the user
reviews and trusts the current hook definition. Observed on a real machine running codex-cli
0.154.0, not documented as a guarantee: after `codex plugin add sidegraph@sidegraph`, the
first interactive `codex` session detects the two new hook definitions and prompts in the
terminal to trust them, and answering yes completes the approval. No `/hooks` visit is needed
for that normal path. A non-interactive `codex exec` session never shows the prompt at all,
which is exactly why automation needs the bypass below. `/hooks` is the surface for reviewing
what is trusted and for re-approving after a hook definition changes, since trust is recorded
per-hash. There is no config-file way to pre-approve it. The only bypass is `codex exec
--dangerously-bypass-hook-trust`, which the docs themselves flag as dangerous and intended for
automation that already vets its hook sources, not as the normal path. So after `/plugin
install sidegraph@sidegraph`, the MCP server works immediately but `SessionStart` and `Stop`
stay silent until that one-time approval. What was verified without
a live, trusted Codex session: the hook entry points themselves honor the Codex JSON contract.
Fed Codex-shaped payloads on stdin, `sidegraph-session-start` wrote the session telemetry row
and returned `hookSpecificOutput.additionalContext`; `sidegraph-stop` wrote a
`capture_sessions` row and returned its reminder when the transcript passed the substance
gate, and correctly wrote nothing when it did not. Also unverified against the
published docs: the top-level `mcpServers`/`hooks` fields in `.codex-plugin/plugin.json` are
not documented on `developers.openai.com/plugins/build/plugins` as of this writing, which
instead sketches an `extensions.com.openai` nesting. The live install above is the evidence
they work, not the published schema. Re-verify both if the Codex CLI you're on behaves
differently.
