# Claude Code setup

Connect Sidegraph to [Claude Code](https://docs.claude.com/en/docs/claude-code).
Two ways to get there: the **plugin** (one step, recommended) or **manual registration** (MCP
server + hooks registered separately — for fine control, or a source checkout). For how these
pieces work under the hood — what each hook injects, the plugin install path, env vars, cwd
caveats — see the deep dive at
[`integrations/claude-code.md`](../integrations/claude-code.md).

Run these in the repo you want memory over (not the Sidegraph checkout).

**Windows:** read the [Windows setup notes](installation.md#windows) first. The plugin
starts its MCP server through `uvx` directly and Claude Code runs the hooks through Git Bash,
so natively you need only Claude Code, uv and Git for Windows with its default options: no
PATH change. Claude Code finds Git Bash itself; if it cannot, set `CLAUDE_CODE_GIT_BASH_PATH`.
Native core/stdio CI does not verify the complete Claude Code plugin; WSL with a single Linux
toolchain is the other supported route.

## Option A — plugin (recommended)

Inside a Claude Code session, in that repo:

The plugin follows the public repository's `main`, which advances at releases. To pin a
version for reproducible environments, see
[how to pin mutable development references](installation.md#mutable-development-references).

```
/plugin marketplace add SantyagoSeaman/sidegraph
/plugin install sidegraph@sidegraph
```

Installs the MCP server and all four hooks (`SessionStart`, `Stop`, `PreToolUse`,
`SubagentStart`) in one step, pointed at `SIDEGRAPH_DIR=.sidegraph`/`SIDEGRAPH_GRAPH=graphify-out/graph.json` by
default. It installs from the public Git repository: the MCP
server and `SessionStart` run `uvx --from git+https://github.com/SantyagoSeaman/
sidegraph.git@rc/0.14.0-1` under the hood, and `Stop`, `PreToolUse` and `SubagentStart` start from the
commit `SessionStart` resolved. See
[the plugin install path](../integrations/claude-code.md#plugin-install-path) for exactly
what gets registered and the cwd-pinning details. Skip to [Verify](#verify) once installed.

The plugin needs no other install. The CLI on your `PATH`
([`uv tool install` from `main`](installation.md#option-a--the-cli-on-your-path)) is
optional, for typing `sidegraph-*` commands yourself. It never updates itself: after every
release run `uv tool upgrade sidegraph`. SessionStart says when it differs from the session's version
([`path-skew`](../guides/troubleshooting.md#path-skew)), and the plugin's skills ignore it
([running the CLIs beside the plugin](../reference/cli.md#running-the-clis-beside-the-plugin)).

**Scope.** `/plugin install` offers three scopes: user, local and project. From a terminal the
flag is `claude plugin install sidegraph@sidegraph --scope user|project|local`, and the default
is user. Enable the plugin at user scope, or in the repository root's
`.claude/settings.local.json`, and it covers sessions started in any subdirectory. Project scope
(`.claude/settings.json`) covers only the directory it was installed from: Claude Code reads a
launch directory's own project settings and no parent's, so a plugin installed at the repository
root leaves sessions started below it without it. `sidegraph-doctor` reports the gap as
[`plugin-off-in-subdirectories`](../guides/troubleshooting.md#plugin-off-in-subdirectories).

### Update an existing plugin

From the repository where you use Sidegraph, run these in your terminal:

```bash
claude plugin marketplace update sidegraph
claude plugin update sidegraph@sidegraph
```

The first command refreshes the marketplace; the second updates the installed plugin.
If you installed the plugin at a specific scope, pass the same `--scope user`,
`--scope project` or `--scope local` to the second command when needed. Keep the existing
`sidegraph@sidegraph` install and decision store; this layout requires no migration or
uninstall/reinstall.

Start a new Claude Code session after updating. To apply pending changes in a running
session, use Claude Code's `/reload-plugins` and follow its prompts. Review the install or
reload summary and approval prompts before continuing. Check the Sidegraph skills in the
slash menu and the server in `/mcp`.

Third-party marketplaces have auto-update off by default in current Claude Code. You can
enable it for `sidegraph` in `/plugin` → Marketplaces, or use the commands above. The
runtime's `@main` source policy does not refresh skills or manifests already loaded by the
host. See [Claude Code's update reference](https://code.claude.com/docs/en/discover-plugins#keep-plugins-updated).

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
      "args": ["--from", "git+https://github.com/SantyagoSeaman/sidegraph.git@rc/0.14.0-1", "sidegraph-mcp"],
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
claude mcp add sidegraph -s project --env SIDEGRAPH_DIR=.sidegraph --env SIDEGRAPH_GRAPH=graphify-out/graph.json -- uvx --from git+https://github.com/SantyagoSeaman/sidegraph.git@rc/0.14.0-1 sidegraph-mcp
```

`-s project` writes a repo-committed `.mcp.json`, so the whole team gets the server
through git — the same merge-like-code convention as the decision store itself. Drop the
flag if you want a private, user-local registration instead.

**From a source checkout instead** (contributors — see
[installation: from source](installation.md#option-d--from-source-contributors)): replace the
`uvx --from git+...` invocation above with
`uv run --project /ABSOLUTE/PATH/TO/sidegraph sidegraph-mcp` (CLI form: `-- uv run --project
/ABSOLUTE/PATH/TO/sidegraph sidegraph-mcp`).

The released package is available from PyPI, so both shorten further, to `uvx --from
sidegraph sidegraph-mcp` — see
[`integrations/claude-code.md`](../integrations/claude-code.md#plugin-install-path)
for the pinning policy. Use `sidegraph==X.Y.Z` in automation.

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
            "command": "SIDEGRAPH_HOST=claude SIDEGRAPH_DIR=.sidegraph SIDEGRAPH_GRAPH=graphify-out/graph.json uvx --from git+https://github.com/SantyagoSeaman/sidegraph.git@rc/0.14.0-1 sidegraph-session-start || printf '%s\\n' '{\"systemMessage\":\"Sidegraph: the SessionStart hook could not start (uv/uvx, network or project path); run the hook command in a terminal to see the error\"}'"
          }
        ]
      }
    ],
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "SIDEGRAPH_DIR=.sidegraph SIDEGRAPH_GRAPH=graphify-out/graph.json uvx --from git+https://github.com/SantyagoSeaman/sidegraph.git@rc/0.14.0-1 sidegraph-stop || printf '{}\\n'"
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
            "command": "SIDEGRAPH_DIR=.sidegraph uvx --from git+https://github.com/SantyagoSeaman/sidegraph.git@rc/0.14.0-1 sidegraph-pre-tool-use || printf '{}\\n'"
          }
        ]
      },
      {
        "matcher": "Bash",
        "hooks": [
          {
            "type": "command",
            "if": "Bash(sed *)",
            "command": "SIDEGRAPH_DIR=.sidegraph uvx --from git+https://github.com/SantyagoSeaman/sidegraph.git@rc/0.14.0-1 sidegraph-pre-tool-use || printf '{}\\n'"
          },
          {
            "type": "command",
            "if": "Bash(grep *)",
            "command": "SIDEGRAPH_DIR=.sidegraph uvx --from git+https://github.com/SantyagoSeaman/sidegraph.git@rc/0.14.0-1 sidegraph-pre-tool-use || printf '{}\\n'"
          },
          {
            "type": "command",
            "if": "Bash(rg *)",
            "command": "SIDEGRAPH_DIR=.sidegraph uvx --from git+https://github.com/SantyagoSeaman/sidegraph.git@rc/0.14.0-1 sidegraph-pre-tool-use || printf '{}\\n'"
          },
          {
            "type": "command",
            "if": "Bash(cat *)",
            "command": "SIDEGRAPH_DIR=.sidegraph uvx --from git+https://github.com/SantyagoSeaman/sidegraph.git@rc/0.14.0-1 sidegraph-pre-tool-use || printf '{}\\n'"
          }
        ]
      },
      {
        "matcher": "Agent|Task",
        "hooks": [
          {
            "type": "command",
            "command": "SIDEGRAPH_DIR=.sidegraph uvx --from git+https://github.com/SantyagoSeaman/sidegraph.git@rc/0.14.0-1 sidegraph-pre-tool-use || printf '{}\\n'"
          }
        ]
      }
    ],
    "SubagentStart": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "SIDEGRAPH_DIR=.sidegraph SIDEGRAPH_GRAPH=graphify-out/graph.json uvx --from git+https://github.com/SantyagoSeaman/sidegraph.git@rc/0.14.0-1 sidegraph-subagent-start || printf '{}\\n'"
          }
        ]
      }
    ]
  }
}
```

These recipes keep plain `@main` on every command, which is simpler to copy. The plugin's own
`Stop`, `PreToolUse` and `SubagentStart` commands start from the commit `SessionStart` resolved,
which is faster on each call; see [the plugin install path](../integrations/claude-code.md#plugin-install-path).

From a source checkout, replace each `uvx --from git+... <entrypoint>` above with `uv run
--project /ABSOLUTE/PATH/TO/sidegraph <entrypoint>` (see the note in step 1). Keep the trailing
`|| printf …` on each command: Claude Code treats a hook that exits 2 with text on stderr as a
block on `Stop` (it continues the conversation) and on `PreToolUse` (it blocks the tool call), and
`uvx` exits 2 on some of its own errors, so without the guard a command that cannot start would
block you instead of just doing nothing. The guard makes it exit 0 and answer `{}`, or, on
`SessionStart`, a `systemMessage` telling you to run the hook command in a terminal to see the
error (see [`reference/hooks.md`](../reference/hooks.md#if-you-see-this-message)).

The env vars are inlined into the command because Claude Code hook entries have no separate
`env`/`cwd` fields.

- `SessionStart` injects a compact project-memory map at the start of every session, led by a
  standing instruction to call `get_task_context` before any search — bash grep/rg/find and
  MCP structure-query tools included, not just `Read`/`Grep` (see
  [`reference/hooks.md`](../reference/hooks.md#sidegraph-session-start)).
- `Stop` nudges the agent, once per session and only once the session has produced
  **>= 2 real user prompts** (a substance gate — a session's very first `Stop` no longer
  triggers it), to propose durable decisions via `propose_decisions` before it finishes, and
  again after 30 minutes and 10 new commits; set
  `SIDEGRAPH_CAPTURE_NUDGE=off` (alongside the other env vars in the command) to disable it
  entirely. See [`reference/hooks.md`](../reference/hooks.md#sidegraph-stop) for the exact
  nudge text and the substance gate's mechanics.
- `PreToolUse` hands the agent the records anchored to a file when it reads or edits it — a
  `Read`, `Grep`, `Edit` or `Write`, or a Bash line whose `sed`, `grep`, `rg` or `cat` names it —
  as a non-blocking block: the top two records (three when the first two are mistakes) with their
  ids, and a line naming `get_task_context` for the rest. Each file arrives once per agent, at
  most three files per call and ten per agent, and the session's own agent and every subagent it
  starts count separately; a file with no records prints nothing. Bash is wired as four entries
  (`if` is `Bash(sed *)`, `Bash(grep *)`, `Bash(rg *)`, `Bash(cat *)`) with the same command.
  The `if` field exists from Claude Code 2.1.85, and matching on compound command lines is
  correct from 2.1.89, so use 2.1.89 or later. Set `SIDEGRAPH_GREP_NUDGE=off` (alongside the
  other env vars in the command) to turn off all of it. See
  [`reference/hooks.md`](../reference/hooks.md#sidegraph-pre-tool-use).
- The `Agent|Task` group runs that same command when the agent spawns a subagent. It appends the
  records for the files the subagent's brief names (and for the files named in a plan or notes
  document that the brief names) to the brief, so the subagent starts with them. The subagent sees the block in
  its first message; the parent's view of its own call is unchanged. Set
  `SIDEGRAPH_AGENT_BRIEF=off` (alongside the other env vars in the command) to turn it off. See
  [`reference/hooks.md`](../reference/hooks.md#subagent-briefs).
- `SubagentStart` gives every subagent a short brief, because a subagent gets no `SessionStart`
  context and Explore and Plan agents load no `CLAUDE.md`: the same `get_task_context` call the
  `SessionStart` instruction names, and one sentence on how many records memory holds and on how
  many files. Without this entry your subagents start unaware that memory exists. Set
  `SIDEGRAPH_SUBAGENT_BRIEF=off` (alongside the other env vars in the command) to disable it. See
  [`reference/hooks.md`](../reference/hooks.md#sidegraph-subagent-start).

> **Don't also run `graphify claude install`.** It writes its own `PreToolUse` hooks into
> `.claude/settings.json` that nudge toward `graphify query` on every qualifying read. These
> overlap with Sidegraph's hook; the recommendation is to use Sidegraph's wiring alone. See
> [why, and how to keep both cleanly if you want to](../integrations/graphify.md#graphify-claude-install-is-redundant-with-the-sidegraph-plugin--skip-it).

## Verify

Start Claude Code in the repo (`claude`). You should see project memory injected at session
start (a stub if the store is empty — that's expected). Then follow the
[quickstart](quickstart.md) to record and retrieve your first decision.

## Reset / cleanup

The store is `.sidegraph/`; everything Graphify owns is `graphify-out/`. Delete both to
start over. Your source files are never touched. Sidegraph can also leave a few things
outside those two directories:

- If you installed the graph refresh hook (`sidegraph-init --hooks`, or a yes to its
  question), run `sidegraph-init --remove-hooks` first. Deleting the directories does not
  remove the helper and the three hook blocks in `.git/hooks/`.
- `sidegraph-init --no-hooks` records the git config key `sidegraph.graphRefresh`.
  `--remove-hooks` clears it.
- `sidegraph-init` may have written `SIDEGRAPH_RATIFY_POLICY` into `.claude/settings.json`.
- The SessionStart hook keeps `${XDG_CACHE_HOME:-~/.cache}/sidegraph/launch-commit`, one
  file shared by every project on the machine.

[SECURITY.md](../../SECURITY.md) lists every write.
