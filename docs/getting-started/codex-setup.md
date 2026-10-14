# Codex setup

Connect Sidegraph to the [OpenAI Codex CLI](https://developers.openai.com/codex/cli).
Codex CLI ships GA hooks with the same event vocabulary as Claude Code (including
`additionalContext` on `SessionStart`), so the same Sidegraph entry points serve both hosts.
Check the version-specific configuration and hook requirements in
[`integrations/codex.md`](../integrations/codex.md).

Run these in the repo you want memory over (not the Sidegraph checkout). Two ways to get
there: the **plugin** (one step, recommended) or **manual registration** (config.toml, hooks,
and AGENTS.md registered separately, for fine control or a source checkout).

**Windows:** read the [Windows setup notes](installation.md#windows) first. The plugin
(Option A) runs natively with uv and Git for Windows at its default options: no PATH change
and no PowerShell 7. The manual recipes below (Option B) use Bash and stay POSIX/WSL only:
run them inside WSL with the host and toolchain installed there. Native core/stdio CI does
not verify a complete Codex plugin session.

## Option A: plugin (recommended)

From a terminal, in that repo:

The plugin follows the public repository's `main`, which advances at releases. To pin a
version for reproducible environments, see
[how to pin mutable development references](installation.md#mutable-development-references).

```bash
codex plugin marketplace add SantyagoSeaman/sidegraph
codex plugin add sidegraph@sidegraph
```

These are shell commands, not slash commands typed inside a Codex session. The marketplace
source takes `owner/repo[@ref]`, a local path or a Git URL.

Installs the MCP server, the `SessionStart`/`Stop`/`SubagentStart` hooks, and all 12 skills in one step,
pointed at `SIDEGRAPH_DIR=.sidegraph`/`SIDEGRAPH_GRAPH=graphify-out/graph.json` by default.
It installs from the public Git repository: the MCP server and `SessionStart`
run `uvx --from git+https://github.com/SantyagoSeaman/sidegraph.git@main` under the hood, and
`Stop` and `SubagentStart` start from the commit `SessionStart` resolved. See
[the plugin install path](../integrations/codex.md#plugin-install-path) for exactly what
gets registered and the cwd-pinning details, and what has and hasn't been verified live.

The plugin needs no other install. The CLI on your `PATH`
([`uv tool install` from `main`](installation.md#option-a--the-cli-on-your-path)) is
optional, for typing `sidegraph-*` commands yourself. It never updates itself: after every
release run `uv tool upgrade sidegraph`. SessionStart says when it differs from the session's version
([`path-skew`](../guides/troubleshooting.md#path-skew)), and the plugin's skills ignore it
([running the CLIs beside the plugin](../reference/cli.md#running-the-clis-beside-the-plugin)).

1. Trust the hooks: start an interactive `codex` session in the repo. Codex detects the new
   Sidegraph hook definitions (`SessionStart`, `Stop`, `SubagentStart`) and prompts you in the
   terminal to trust them. Approve to complete it. Skip or decline and the MCP tools still work,
   but retrieval at session start, the capture reminder at session end and the memory brief a
   subagent gets at its start stay silent. A hook
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
            "command": "cd \"$(git rev-parse --show-toplevel 2>/dev/null || pwd)\" && SIDEGRAPH_HOST=codex SIDEGRAPH_DIR=.sidegraph SIDEGRAPH_GRAPH=graphify-out/graph.json uv run --project /ABSOLUTE/PATH/TO/sidegraph sidegraph-session-start || printf '%s\\n' '{\"systemMessage\":\"Sidegraph: the SessionStart hook could not start (uv/uvx, network or project path); run the hook command in a terminal to see the error\"}'"
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

The `cd "$(git rev-parse --show-toplevel 2>/dev/null || pwd)"` prefix pins the hook's working
directory to the repo root regardless of where Codex actually launches it from, so the relative
`SIDEGRAPH_DIR` / `SIDEGRAPH_GRAPH` paths resolve correctly. The `|| pwd` fallback keeps this
working on a non-git corpus too (Sidegraph doesn't require the corpus to be a git repo — see
[`integrations/graphify.md`](../integrations/graphify.md#non-git-and-doc-only-corpora)), where a
bare `git rev-parse --show-toplevel` would fail and leave `cd` with no argument.

The `SubagentStart` entry gives each subagent a short brief (the `get_task_context` call to make,
and what memory holds), because a subagent gets no `SessionStart` context; see
[`reference/hooks.md`](../reference/hooks.md#sidegraph-subagent-start).

The trailing `|| printf …` is a guard, and it should stay. On `Stop`, Codex feeds a hook's stderr
back to the model when the hook exits 2, and `uv` exits 2 on some of its own errors (a project it
cannot find, a cache it cannot write), so a command that never reaches Python could keep
continuing your session. With the guard the hook exits 0 and answers `{}`. On `SessionStart` it
answers a `systemMessage` telling you to run the hook command in a terminal to see the error
(see [`reference/hooks.md`](../reference/hooks.md#if-you-see-this-message)). The same guard ends
every hook command in the plugin; see [`integrations/codex.md`](../integrations/codex.md#hooks-ga).

No `PreToolUse` entry is included above. Codex can invoke that event for local function tools,
but it has no stable `Read`/`Grep` tool pair to which Sidegraph's Claude-specific records block
can attach. Therefore Claude Code's `PreToolUse` delivery of a file's records
(`sidegraph-pre-tool-use`) has no Codex counterpart to wire up yet — see
[`integrations/codex.md`](../integrations/codex.md) for details.

## Verify

Start Codex CLI in the repo. You should see project memory injected at session start (a stub
if the store is empty). Then follow the [quickstart](quickstart.md) to record and retrieve
your first decision.

If nothing is injected and no error appears, check trust first: Codex only loads
project-local `.codex/` hooks when the project layer is marked trusted, so a hook that
silently never fires is often a trust prompt/setting, not a config mistake.
