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
            "command": "cd \"$(git rev-parse --show-toplevel 2>/dev/null || pwd)\" && SIDEGRAPH_DIR=.sidegraph SIDEGRAPH_GRAPH=graphify-out/graph.json uv run --project /ABSOLUTE/PATH/TO/sidegraph sidegraph-session-start || printf '%s\\n' '{\"systemMessage\":\"Sidegraph: the SessionStart hook could not start (uv/uvx, network or project path); run the hook command in a terminal to see the error\"}'"
          }
        ]
      }
    ],
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "cd \"$(git rev-parse --show-toplevel 2>/dev/null || pwd)\" && SIDEGRAPH_DIR=.sidegraph SIDEGRAPH_GRAPH=graphify-out/graph.json uv run --project /ABSOLUTE/PATH/TO/sidegraph sidegraph-stop || printf '{}\\n'"
          }
        ]
      }
    ],
    "SubagentStart": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "cd \"$(git rev-parse --show-toplevel 2>/dev/null || pwd)\" && SIDEGRAPH_DIR=.sidegraph SIDEGRAPH_GRAPH=graphify-out/graph.json uv run --project /ABSOLUTE/PATH/TO/sidegraph sidegraph-subagent-start || printf '{}\\n'"
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
initiatives, global mistakes); `sidegraph-stop` emits the same guarded block-to-distill
nudge, once per session and again after 30 minutes and 10 new commits (see
[`reference/hooks.md`](../reference/hooks.md#re-arm-after-more-work)). `sidegraph-subagent-start`
gives each subagent Codex starts a short brief, because a subagent gets no `SessionStart`
context: the `get_task_context(files=[…])` call to make, and one sentence on how many records
memory holds and in how many files (see the
[hooks reference](../reference/hooks.md#sidegraph-subagent-start); `SIDEGRAPH_SUBAGENT_BRIEF=off`
disables it). `sidegraph-stop` and `sidegraph-subagent-start` degrade silently on any failure.
`sidegraph-session-start` does too, with one exception: a store that cannot be opened. It then
sends a notice (a `systemMessage` that names the cause and the fix) and tells the model that
memory tools will fail, because a broken store should not go unseen. A missing store is created
by `SessionStart`, and a missing graph only shortens the map; neither blocks the session.

Each command ends with a guard, `|| printf '{}\n'` on `Stop` and `SubagentStart` and, on `SessionStart`,
`|| printf '%s\n' '{"systemMessage":"…"}'`. It covers the part the Python entry points cannot:
a command that never reaches Python. On `Stop`, Codex treats a hook that exits 2 with text on
stderr as a block and feeds that text back to the model, and `uv` exits 2 on some of its own
errors (a project it cannot find, a cache it cannot write), as does `dash` when the `cd` fails.
Without the guard such a failure keeps continuing the session. With it the hook exits 0 and
answers `{}`, which Codex reads as "nothing to say". On `SessionStart` a failed command is only
a failed hook, so the guard there is for the user: it answers a `systemMessage`, "Sidegraph: the
SessionStart hook could not start (uv/uvx, network or project path); run the hook command in a
terminal to see the error". The price of the guard is that a failure no longer shows up as a
failed hook, which is why `SessionStart` says so itself. Keep the guard if you adapt the recipe.
How to run the command in a terminal is in
[`reference/hooks.md`](../reference/hooks.md#if-you-see-this-message).

`sidegraph-stop` reads Codex's own transcript, the rollout file, to decide whether the session
is substantial enough to nudge. Only a person's interactive thread can arm it, and only once
it holds at least two real prompts of its own: the project instructions and environment
blocks Codex writes into a rollout, a Stop hook's own block reason fed back, and an answer to
the agent's question do not count. A subagent, Codex's automatic reviewer and a headless
`codex exec` run never arm it, and `codex exec` is the one that matters in automation: a Stop
block makes a headless run continue and replaces the output you asked for with `-o`. A run
started through the Codex TypeScript SDK is headless too: the gate recognises it by the
rollout's `source` of `exec`, because the SDK sets an originator of its own. Observed
on codex-cli 0.159.3, Codex fires `SubagentStop`, not `Stop`, for a subagent, and the plugin
registers no `SubagentStop` hook, so a subagent's thread is not nudged either way. The exact
rule is in
[`reference/hooks.md`](../reference/hooks.md#sidegraph-stop).

Codex only loads project-local `.codex/` hooks (like the ones above) when the project layer
is marked trusted — if these hooks silently don't fire, check the trust prompt/settings before
assuming the JSON above is wrong.

No `PreToolUse` entry is offered above. Codex can invoke the event for local function tools
(including shell, patch, and MCP calls), but it has no stable `Read`/`Grep` pair equivalent to
Claude Code's, and the records block the `sidegraph-pre-tool-use` hook hands over for a file (see
[`claude-code.md`](claude-code.md#pretooluse--sidegraph-pre-tool-use)) needs a tool call that
names the file. Delivery there is deferred. Re-check the current [Codex hooks documentation](https://learn.chatgpt.com/codex/hooks)
before adding one; hosted tools are outside the hook surface.

### Headless runs

A `codex exec` run never arms the capture nudge, as above, so a Stop block cannot replace the
`-o` output. If the plugin's hooks are trusted in the `CODEX_HOME` the run uses, they still run
there, and a broken install (`uv` cannot start, the project directory is gone) answers `{}` and
the run goes on. To run a review panel or any other automation without Sidegraph's hooks at all,
give it a `CODEX_HOME` that holds only `auth.json`: with no plugin installed there is no
Sidegraph hook, and a project's own `.codex/hooks.json` does not run there either, because that
home has not trusted the project. Do not add `--dangerously-bypass-hook-trust` to that run.
`SIDEGRAPH_CAPTURE_NUDGE=off` is narrower: it silences only the `Stop` nudge, and `SessionStart`
still injects the memory map. Name the model with `-m`, since the clean home carries no
`config.toml`.

## The auto-reviewer and memory reads

Under `approval_policy = "on-request"` with `approvals_reviewer = "auto_review"`, Codex's
auto-reviewer reviews an MCP tool call before it runs, unless the tool is annotated read-only,
or non-destructive and closed-world. Sidegraph's tools carry those annotations (see
[Annotations](../reference/mcp-tools.md#annotations)), so a memory read passes without a review.
Before 0.10.0 the tools carried none, and a session whose prompt forbade network calls could see
its memory reads denied as unacceptable risk, with the agent carrying on without memory for that
step. If a session on an older install shows that denial, upgrade the package.

A stricter per-server setting changes this. With `default_tools_approval_mode = "writes"` on
the Sidegraph server, only the tools marked read-only skip the review. The retrieval tools
(`get_task_context`, `query_decisions`, `query_structure`, `drill_down`, `retrieve_decisions`)
are not marked read-only, because their lazy sync can update a tracked entity file when a symbol
moved, so they still go to the reviewer. In our test run it judged a `get_task_context` call a
local read and approved it. We did not run the other four, and under that setting the verdict is
a judgment call, not a skip.

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

Sidegraph also ships a Codex plugin, from the same repository as the Claude Code plugin. Install
it from a terminal with Codex's own commands (they are not slash commands typed in a session, and
the Claude Code `/plugin` syntax does not apply):

```bash
codex plugin marketplace add SantyagoSeaman/sidegraph
codex plugin add sidegraph@sidegraph
```

1. Trust the hooks: start an interactive `codex` session in the repo. Codex detects the new
   Sidegraph hook definitions (`SessionStart`, `Stop`, `SubagentStart`) and prompts you in the
   terminal to trust them. Approve to complete it. Skip or decline and the MCP tools still work, but
   retrieval at session start, the capture reminder at session end and the memory brief a
   subagent gets at its start stay silent (see why below). A hook definition change in a later
   release needs a fresh approval. Review or re-approve anytime with `/hooks`. The release that added the start guard described under
   [Hooks](#hooks-ga) is such a change: Codex's trust hash covers the command, so after updating
   the plugin the two hooks stay inactive until you approve them again, once, in `/hooks` or at
   the next interactive session's trust prompt. If you wired the hooks by hand instead, append the
   guard to the commands in your own `.codex/hooks.json`, as the recipe above shows. The release
   that moved `Stop` onto the commit `SessionStart` recorded (below) changed the command once
   more, so approve it again once; the command no longer changes between releases. The release
   that added `SubagentStart` adds a hook definition: approve it once the same way, or subagents
   start without the memory brief while `SessionStart` and `Stop` keep working.

**How the plugin launches.** The MCP server and `SessionStart` run `uvx --from
git+https://github.com/SantyagoSeaman/sidegraph.git@main`, a mutable branch (see
[Mutable development references](../getting-started/installation.md#mutable-development-references)).
`Stop` and `SubagentStart` run many times a session and `uv` re-resolves a branch reference on every call, so they launch from the commit `SessionStart`
recorded in `${XDG_CACHE_HOME:-$HOME/.cache}/sidegraph/launch-commit`, falling back to `@main`
when there is no valid record. A plugin pinned to a tag or commit ignores the record and
launches its own ref. The file is the one the Claude Code plugin uses, so a release
reaches a running session at the next `SessionStart` on the machine, not at its next `Stop`.
Codex runs a hook through `$SHELL -lc`, and a fish login shell cannot parse the POSIX prefix
that reads the file, so the `Stop` and `SubagentStart` commands are each wrapped in `sh -c '…'`. See
[Troubleshooting](../guides/troubleshooting.md#the-launch-commit-file).

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

What was verified live on codex-cli 0.154.0, a throwaway `CODEX_HOME`, when the plugin's hooks
were `SessionStart` and `Stop`: `codex plugin
marketplace add SantyagoSeaman/sidegraph` fetches the published repository by the
`owner/repo` shorthand, parses `.agents/plugins/marketplace.json` and resolves the plugin at
`./plugin/sidegraph`. `codex plugin add sidegraph@sidegraph` installs it, copying
`.codex-plugin/plugin.json`, `codex/mcp.json`, `codex/hooks.json`, and every skill's
`agents/openai.yaml` into the plugin cache unchanged. `codex mcp list --json` shows the
`sidegraph` server registered with the exact command from `codex/mcp.json`, and a
non-interactive `codex exec` session with that server registered called the Sidegraph tools
(`retrieve_decisions`, `list_proposed`, `query_decisions`, `find_entity`) and got real
records back. The hooks did not fire in that session, and that is expected, not a bug. Codex
gates hooks with two independent checks. Project trust (`trust_level` in
`config.toml`) is one. Hook trust is separate and hash-based: Codex records trust against a
hook definition's current hash and refuses to run a hook it has not seen approved at that
hash. Installing or enabling a plugin does not grant hook trust. Per the [Codex hooks
docs](https://learn.chatgpt.com/codex/hooks), Codex skips plugin-bundled hooks until the user
reviews and trusts the current hook definition. Observed on a real machine running codex-cli
0.154.0, not documented as a guarantee: after `codex plugin add sidegraph@sidegraph`, the
first interactive `codex` session detected the new hook definitions and prompted in the
terminal to trust them, and answering yes completed the approval. No `/hooks` visit was needed
for that normal path. The plugin now registers three hooks, `SessionStart`, `Stop` and
`SubagentStart`, and trust is per definition, so a hook added by a later release is a new
definition to approve; that was not re-observed on a later Codex. A non-interactive `codex exec` session never shows the prompt at all,
which is exactly why automation needs the bypass below. `/hooks` is the surface for reviewing
what is trusted and for re-approving after a hook definition changes, since trust is recorded
per-hash. There is no config-file way to pre-approve it. The only bypass is `codex exec
--dangerously-bypass-hook-trust`, which the docs themselves flag as dangerous and intended for
automation that already vets its hook sources, not as the normal path. So after `codex plugin
add sidegraph@sidegraph`, the MCP server works immediately but `SessionStart`, `Stop` and
`SubagentStart` stay silent until that approval. What was verified without
a live, trusted Codex session: the hook entry points themselves honor the Codex JSON contract.
Fed Codex-shaped payloads on stdin, `sidegraph-session-start` wrote the session telemetry row
and returned `hookSpecificOutput.additionalContext`; `sidegraph-stop`, given a real rollout
of an earlier interactive session as the transcript, wrote a `capture_sessions` row and
returned its reminder, and wrote nothing for a rollout with fewer than two real prompts, for
a subagent's or the automatic reviewer's thread, and for a headless `codex exec` run. Also
unverified against the
published docs: the top-level `mcpServers`/`hooks` fields in `.codex-plugin/plugin.json` are
not documented on `developers.openai.com/plugins/build/plugins` as of this writing, which
instead sketches an `extensions.com.openai` nesting. The live install above is the evidence
they work, not the published schema. Re-verify both if the Codex CLI you're on behaves
differently.
