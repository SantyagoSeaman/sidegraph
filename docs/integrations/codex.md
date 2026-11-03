# Codex CLI integration

> **Start here:** Install the host plugin, start a new session in your repository, then [verify it](../guides/verifying-your-setup.md). [Glossary](../concepts/glossary.md).

The deep dive on how Sidegraph wires into the OpenAI Codex CLI. For the copy-paste happy
path, see [`getting-started/codex-setup.md`](../getting-started/codex-setup.md).

Codex uses the same MCP server and hook entry points as Claude Code, with host-specific
handling kept in Sidegraph's host layer. Its hooks support `additionalContext` on
`SessionStart`. Plugin installation, update and hash-based trust metadata were checked on
Codex CLI 0.162.1 on macOS ARM on 2026-10-15.
The version-specific primary references are the
[plugin contract](https://developers.openai.com/plugins/build/plugins),
[CLI reference](https://learn.chatgpt.com/docs/cli/reference) and
[hooks documentation](https://learn.chatgpt.com/docs/hooks).

## `config.toml` reference

Codex CLI reads MCP server definitions under `[mcp_servers.*]` from project
`.codex/config.toml` and user `~/.codex/config.toml`. Prefer the project file for a server
that belongs to one repository. Pin the working directory rather than relying on ambient cwd:

```toml
[mcp_servers.sidegraph]
command = "bash"
args = [
  "-lc",
  "cd '/ABSOLUTE/PATH/TO/your-repo' && SIDEGRAPH_DIR=.sidegraph SIDEGRAPH_GRAPH=graphify-out/graph.json uvx --from git+https://github.com/SantyagoSeaman/sidegraph.git@main sidegraph-mcp"
]
```

Register the same thing with the CLI:

```bash
codex mcp add sidegraph -- bash -lc "cd '/ABSOLUTE/PATH/TO/your-repo' && SIDEGRAPH_DIR=.sidegraph SIDEGRAPH_GRAPH=graphify-out/graph.json uvx --from git+https://github.com/SantyagoSeaman/sidegraph.git@main sidegraph-mcp"
```

From a source checkout (contributors; see
[installation: from source](../getting-started/installation.md#option-d--from-source-contributors)),
replace `uvx --from git+https://github.com/SantyagoSeaman/sidegraph.git@main` here and in the
hooks below with `uv run --project '/ABSOLUTE/PATH/TO/sidegraph'`. `@main` is a mutable ref; to
pin it, see
[mutable development references](../getting-started/installation.md#mutable-development-references).

`SIDEGRAPH_DIR` (default `.sidegraph`) and `SIDEGRAPH_GRAPH` (default
`graphify-out/graph.json`) are the same two environment variables the Claude Code integration
reads — see [`claude-code.md`](claude-code.md#environment-variables) for their meaning.

## Hooks (GA)

For a manual setup on macOS, Linux or WSL, add this file. Native Windows users should use the plugin path below.

Project-scoped, at `.codex/hooks.json`:

```json
{
  "hooks": {
    "SessionStart": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "cd \"$(git rev-parse --show-toplevel 2>/dev/null || pwd)\" && SIDEGRAPH_HOST=codex SIDEGRAPH_DIR=.sidegraph SIDEGRAPH_GRAPH=graphify-out/graph.json uvx --from git+https://github.com/SantyagoSeaman/sidegraph.git@main sidegraph-session-start || printf '%s\\n' '{\"systemMessage\":\"Sidegraph: the SessionStart hook could not start (uv/uvx, network or project path); run the hook command in a terminal to see the error\"}'"
          }
        ]
      }
    ],
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "cd \"$(git rev-parse --show-toplevel 2>/dev/null || pwd)\" && SIDEGRAPH_DIR=.sidegraph SIDEGRAPH_GRAPH=graphify-out/graph.json uvx --from git+https://github.com/SantyagoSeaman/sidegraph.git@main sidegraph-stop || printf '{}\\n'"
          }
        ]
      }
    ],
    "SubagentStart": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "cd \"$(git rev-parse --show-toplevel 2>/dev/null || pwd)\" && SIDEGRAPH_DIR=.sidegraph SIDEGRAPH_GRAPH=graphify-out/graph.json uvx --from git+https://github.com/SantyagoSeaman/sidegraph.git@main sidegraph-subagent-start || printf '{}\\n'"
          }
        ]
      }
    ]
  }
}
```

Use the plugin path below for native hook wiring. The JSON above is the complete manual POSIX recipe. The reference explains its behavior. SessionStart supplies a map, SubagentStart introduces retrieval, and Stop can remind an interactive agent to capture learning. Sidegraph does not register a Codex file-read hook.

See the [full contract](../reference/codex-wiring.md#hooks-ga).

### Headless runs

A `codex exec` run does not trigger the Stop capture reminder and does not show the interactive hook-approval prompt. Arrange reviewed approval explicitly for automation.

See [details](../reference/codex-wiring.md#headless-runs).

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

Start an interactive `codex` session in the repo and review the Sidegraph definitions
(`SessionStart`, `Stop`, `SubagentStart`) through the startup warning or `/hooks`. Approve
only definitions you have reviewed. Installation does not grant trust. Declining leaves
those hooks inactive while the MCP registration remains available. Trust is per definition
hash and covers the command selected for the host OS: a Windows-only command change needs
review on Windows, without changing the POSIX approval.

**Updating an existing install.** After each Sidegraph release, run this in a terminal:

```bash
codex plugin marketplace upgrade sidegraph
```

Then start a new Codex session. On 0.162.1 the native upgrade updates the installed plugin
without uninstalling; unchanged definitions keep their trust even when the cache root
moves. A never-approved definition (observed at first install) is `untrusted`; a previously
approved definition whose command changed is `modified` and needs review again through
`/hooks`. Those status names are the
observed 0.162.1 metadata. Updating never grants approval to a changed command. The runtime
launcher's `@main` does not update cached skills, manifests or hook definitions, so this
marketplace update remains necessary. Update Codex itself if this command is unavailable.

**How the plugin launches.** The MCP server and SessionStart resolve the configured package source. Repeated hooks use the [shared launch-commit contract](../reference/hooks.md#the-launch-commit-file). See the [launcher reference](../reference/codex-wiring.md) when adapting the commands.

**Native Windows.** The MCP server needs no shell: `codex/mcp.json` starts `uvx` (`uv` in the
development manifest) directly with an argument list. Each hook carries a second key,
`commandWindows`, a PowerShell command that does the same job as the POSIX one: `Stop` and
`SubagentStart` read the launch-commit file; all of them move to the git top-level, set the
environment variables, run `uvx`, and print the same fallback message when the launch fails.
Codex picks the key on
Windows, where it runs a hook with `pwsh` when PowerShell 7 is installed and with Windows
PowerShell 5.1 otherwise; the commands are written so 5.1 parses them. So on native Windows the
plugin needs only Codex, uv and Git for Windows with its default options: no `sh` on PATH and
no PowerShell 7. Native Windows hooks need a Codex release that supports `commandWindows`
(verified with Codex CLI 0.162.1); if Sidegraph's hooks fail to start on Windows, update Codex.
The manual hook recipes on this page and in
[Codex setup](../getting-started/codex-setup.md) stay POSIX/WSL-only.

This is the same repo-committed manifest set the Claude Code plugin uses, in Codex's own
shape: `.agents/plugins/marketplace.json` at the repo root, and
`plugin/sidegraph/.codex-plugin/plugin.json` naming the MCP config
(`plugin/sidegraph/codex/mcp.json`), the hooks config
(`plugin/sidegraph/codex/hooks.json`), and the skills directory (all 14 skills, each with
its own `agents/openai.yaml` declaring the `sidegraph` MCP dependency and whether Codex may
invoke it implicitly). No `PreToolUse` entry ships because Codex has no stable file-read tool pair equivalent to Claude Code's Read/Grep pair. Call `get_task_context` explicitly for file records.

**The cwd risk this page used to defer on is resolved, not sidestepped.** Codex plugin MCP
servers and hooks do not get a project-root variable. That was confirmed live with `codex mcp
list --json` against a locally installed build of this plugin (codex-cli 0.154.0): the
registered `sidegraph` server carries `"cwd": null`. So the plugin pins the project itself.
`codex/hooks.json` runs `cd "$(git rev-parse --show-toplevel 2>/dev/null || pwd)"` (the
`commandWindows` form does `git rev-parse` and `Set-Location`) before anything else, the same
way the manual `.codex/hooks.json` recipe above does, instead of assuming an ambient project
directory. `codex/mcp.json` passes `--repo-root` to `sidegraph-mcp`, which anchors the server
to the project: `CLAUDE_PROJECT_DIR` when set, else the git top-level, else the cwd. That
anchoring, not a plugin-provided variable, is what keeps `SIDEGRAPH_DIR`/`SIDEGRAPH_GRAPH`
pointed at the right repo.

The [native verification record](../reference/codex-wiring.md#native-verification) describes the checked plugin install, upgrade and trust behavior.
