# Installation

Install **Sidegraph** for the decision store, MCP server, CLI, and host hooks. Add
**Graphify** for anchors to files and symbols and for structural context. Without a graph
the store, the session map and listings still work, but records cannot be anchored to code
(see [Without the graph](#without-the-graph)); the complete workflow below installs both.

## Prerequisites

- **Python 3.13+** for Sidegraph (`.python-version` pins it).
- **Python 3.10+** for Graphify.
- [`uv`](https://docs.astral.sh/uv/) — used to install and run both.
- For automatic session integration: [Claude Code](claude-code-setup.md),
  [Codex](codex-setup.md) or [Grok Build](../integrations/grok.md).
  [OpenCode V2](../../.opencode/INSTALL.md) provides MCP tools and shared skills without
  lifecycle hooks; [Muse](../integrations/muse.md) adds the session map and a
  subagent brief through two hooks.
  Other stdio MCP clients can call the tools directly; the CLI also works without a host.

### Windows

Current native CI covers selected core/MCP checks, wheel installation, CLI execution, and
MCP stdio paths; WSL is not a separate CI target. Full Windows plugin sessions were exercised
in the historical 0.14.0 release-candidate campaign (see
[Platform coverage](#platform-coverage)).

**Version requirement:** releases before 0.12.0 can stall a native Windows write: a Git
child inherits the MCP stdin pipe and holds the write until the request times out (see
the [changelog](../../CHANGELOG.md)). Use 0.12.0 or later; marketplace plugin users get
the fix from the public repository's `main`, which their launchers fetch. WSL is the
workaround for older versions.

For the complete supplied plugin workflow, install and launch the agent host, uv/Python,
Sidegraph and optional Graphify inside the same WSL distribution. Follow the Linux setup
commands there and use Linux paths. Do not point a WSL host at a Windows Python environment
or mix `C:\...` and `/mnt/c/...` paths in one launch recipe. The host-specific WSL setup is
described by [Claude Code](https://code.claude.com/docs/en/setup#set-up-on-windows) and
[Codex](https://learn.chatgpt.com/docs/windows/wsl).

On native Windows, the plugins need Git for Windows at its default options, uv and the
host, and nothing else: no change to PATH and no PowerShell 7.

- Both MCP launchers start `uvx` (`uv` in the development manifest) directly with an
  argument list, so no shell is involved. Their `--repo-root` argument anchors the server
  to the project: `CLAUDE_PROJECT_DIR` when set, else the git top-level, else the working
  directory.
- Claude Code runs its hooks through Git Bash, which it finds itself.
- Codex hooks carry a PowerShell `commandWindows` next to the POSIX command. Codex runs it
  with `pwsh` when PowerShell 7 is installed and with Windows PowerShell 5.1 otherwise, and
  the commands parse in both. Native Windows hooks need a Codex release that supports
  `commandWindows` (verified with Codex CLI 0.162.1); if Sidegraph's Codex hooks fail to
  start on Windows, update Codex.
- Claude Code finds Git Bash itself; if it cannot, set `CLAUDE_CODE_GIT_BASH_PATH` to
  `bash.exe`. Without Git Bash, Claude Code runs hooks in PowerShell, which cannot parse
  Sidegraph's POSIX hook commands.

The manual Codex hook recipes stay POSIX/WSL-only. If something still fails to start, see
[Windows launch troubleshooting](../guides/troubleshooting.md#windows-plugin-launch).

For MCP-only use with a client that can launch `uvx` directly, Claude Code's
[manual MCP JSON recipe](claude-code-setup.md#1-register-the-mcp-server) starts `uvx`
directly, as the plugin does. Set `SIDEGRAPH_DIR` and `SIDEGRAPH_GRAPH` to absolute paths for
the intended repository, or set a working directory supported by your client. In JSON, use forward
slashes (`C:/work/your repo/.sidegraph`) or escape each backslash
(`C:\\work\\your repo\\.sidegraph`); keep paths with spaces as single argument values.
If `uvx` is not on the host's PATH, use the full executable path in `command`.
This registration is separate from automatic hooks: it supplies MCP tools but no
SessionStart memory brief, Stop capture reminder, or SubagentStart brief. Claude's
PreToolUse delivery also requires its hooks. The supplied
[Codex manual recipe](codex-setup.md#1-register-the-mcp-server) uses Bash and belongs to
the WSL route. Native manual MCP registration using these recipes is outside the
current CI coverage.

## Mutable development references

The `git+…@main` examples below track a mutable branch: a later `uvx` cache refresh can
install different code. That is useful while trying Sidegraph. For CI, shared setup, or a
measured pilot, replace `@main` with a release tag or commit SHA, for example
`git+https://github.com/SantyagoSeaman/sidegraph.git@<sha>`. The plugin marketplace is the
exception: its public manifests intentionally track `main`, and the release process moves
that `main` only when it cuts a release. See
[`reference/stability.md`](../reference/stability.md) for the promises made by each surface.

For an existing store, follow the [0.14 migration guide](../guides/migrating-to-0.14.md)
before upgrading Graphify, and update every Sidegraph client first.

## Install Sidegraph

### Option A — the CLI on your PATH

This puts the `sidegraph-*` commands on your `PATH` under their bare names, for your own
terminal, scripts and CI. Without a plugin it is the usual setup, though the `uvx` forms below
and in [Option C](#option-c--uvx-directly-from-git) run Sidegraph with no install at all. Beside
the Claude Code or Codex plugin
([Option B](#option-b--host-plugin-recommended-for-claude-code-and-codex)) it is optional: the
plugin runs its own Sidegraph through `uvx` and its skills never use this copy, so install it only
if you want to type the commands yourself.

It installs from the same place as the plugin, the public repository's `main` branch, which
changes only at releases:

```bash
uv tool install git+https://github.com/SantyagoSeaman/sidegraph.git@main
```

That puts `sidegraph-mcp` and every `sidegraph-*` CLI on your PATH, where the documentation's
bare command names work.

**Keep it updated. This step is part of the install.** A `uv tool` install never updates
itself: it stays at the version you installed until you upgrade it. After every Sidegraph
release (listed in the [CHANGELOG](../../CHANGELOG.md) and on
[GitHub Releases](https://github.com/SantyagoSeaman/sidegraph/releases)), run:

```bash
uv tool upgrade sidegraph
```

For an install from `main`, `uv tool upgrade` asks GitHub for the current `main` commit, so it
moves to the latest release. An older install from PyPI, pinned or not, moves to the same
source with the install command above. The releases are also published on PyPI as `sidegraph`;
use that only to pin an exact version (see below).

Beside a plugin, an old copy keeps answering your own shell while the code the plugin runs moves
to each release, and it can lack commands or flags the documentation shows. On macOS and Linux,
SessionStart adds a [`path-skew`](../guides/troubleshooting.md#path-skew) line naming both
versions when the copy on `PATH` differs from the session's, and shows it to you at most once a
day.

To stop maintaining the copy, run `uv tool uninstall sidegraph`. The plugin keeps working, and
any command still runs as `uvx --from git+https://github.com/SantyagoSeaman/sidegraph.git@main <command>`.

For CI or a reproducible pilot, pin a version instead (`pip install 'sidegraph==X.Y.Z'`) and move
the pin when you upgrade. For an ephemeral run without installing,
`uvx --from sidegraph sidegraph-mcp` works — the entry-point name differs from the package
name, so `--from sidegraph` is required (a bare `uvx sidegraph-mcp` will not resolve).

### Option B — host plugin (recommended for Claude Code and Codex)

In the repo you want memory over. In Claude Code, inside an interactive session:

```
/plugin marketplace add SantyagoSeaman/sidegraph
/plugin install sidegraph@sidegraph
```

In Codex, from a terminal:

```bash
codex plugin marketplace add SantyagoSeaman/sidegraph
codex plugin add sidegraph@sidegraph
```

The Claude Code host installs the MCP server and all four hooks (`SessionStart`, `Stop`,
`PreToolUse`, `SubagentStart`). Codex installs the MCP server, `SessionStart`/`Stop`/
`SubagentStart`, and the bundled skills;
its tool surface has no equivalent target for the Claude-specific `PreToolUse` records block. The
plugin's bundled config installs from the public repository, not PyPI: the MCP server and
`SessionStart` run `uvx --from git+https://github.com/SantyagoSeaman/sidegraph.git@main`, and the
`Stop`, `PreToolUse` and `SubagentStart` hooks start from the commit `SessionStart` resolved. Unlike the package
install, the plugin deliberately follows the public repository's `main` branch. See
the integration details for [Claude Code](../integrations/claude-code.md#plugin-install-path)
or [Codex](../integrations/codex.md#plugin-install-path).

**Terminal commands beside the plugin.** The plugin needs no other install. If you also want
the bare `sidegraph-*` commands in your own terminal, install the CLI as in
[Option A](#option-a--the-cli-on-your-path), together with its update step.

**Scope (Claude Code).** Install at user scope, or enable the plugin in the repository root's
`.claude/settings.local.json`, so sessions started in subdirectories get it too; see
[plugin scope](claude-code-setup.md#option-a--plugin-recommended).

For installation and updates for every host, see the
[host plugin recipes](#host-plugins-install-and-update). Existing Claude Code and Codex installs
keep `sidegraph@sidegraph`; update cached assets with the host's native update commands,
then start a new session and review any changed approvals. The runtime's `@main` does not
refresh cached skills or manifests. OpenCode V2 uses the
[canonical Git package guide](../../.opencode/INSTALL.md).
[Muse](../integrations/muse.md) supplies MCP, skills, the session map and a subagent brief,
and must be restarted from the intended project when switching workspaces; expanded native
acceptance is deferred. Grok Build installs from its own plugin marketplace; see the
[Grok Build guide](../integrations/grok.md).

### Option C — uvx directly from git

Prefer manual wiring, or need an entry point outside Claude Code (e.g. `sidegraph-init` from
a plain terminal)? Run it straight from the repository with
[`uvx`](https://docs.astral.sh/uv/guides/tools/), with no persistent install and no local
checkout. `@main` moves only when a release is cut. For reproducibility, pin a tag or commit SHA instead
(see "Mutable development references" above):

```bash
uvx --from git+https://github.com/SantyagoSeaman/sidegraph.git@main sidegraph-init
uvx --from git+https://github.com/SantyagoSeaman/sidegraph.git@main sidegraph-bootstrap --help
```

Every Sidegraph entry-point name differs from the source location, so any `uvx` invocation
must use `--from git+<url>@<ref> <entrypoint>` — a bare `uvx sidegraph-init` will not work.
`uv` caches the build after the first run, so repeat invocations are fast. Substitute
`git+ssh://git@github.com/...` for the same URL if you authenticate over SSH.

### Option D — from source (contributors)

```bash
git clone https://github.com/SantyagoSeaman/sidegraph.git   # or your local checkout
cd sidegraph
uv sync --locked        # installs the checked-in dependency versions into .venv
uv run pytest -q        # sanity check — all tests should pass
```

Invoke any entry point from another directory with `uv run --project`:

```bash
uv run --project /ABSOLUTE/PATH/TO/sidegraph sidegraph-mcp
```

Use `--project`, not `--directory` — `--directory` changes the working directory of the
invoked process, which breaks the relative `SIDEGRAPH_DIR` / `SIDEGRAPH_GRAPH` paths described
below.

**Troubleshooting**: if another project's virtualenv is already active in your shell (e.g.
you `source`d its `.venv/bin/activate`) when you run `uv run --project ...`, `uv` prints
`warning: VIRTUAL_ENV=... does not match the project environment path ... and will be
ignored` to stderr. That's `uv` itself, not Sidegraph, and it's harmless — the command still
runs against Sidegraph's own `.venv`. Silence it with `uv run --no-active --project ...` or by
deactivating the other venv first.


## Host plugins: install and update

Each recipe below installs the plugin and gives the command that updates it. Host plugin
updates and the MCP runtime are separate: the default runtime follows the public repository's
`main`, which advances at releases, and that does not refresh cached skills, manifests or hook
definitions. Use your host's update recipe after a release, then start a new session.

| Host | Automatic session memory | Checked |
|---|---|---|
| Claude Code | yes: hooks, capture reminders, context on reads | macOS and Windows |
| Codex | yes: hooks, after you approve them | macOS and Windows |
| Grok Build | yes: hooks, capture reminders, context on reads; the session map through the MCP server | macOS |
| Muse | partly: session map and subagent brief, after you approve them | macOS |
| OpenCode V2 | no: MCP and skills | macOS |

### Claude Code

In a Claude Code session in the repository:

```text
/plugin marketplace add SantyagoSeaman/sidegraph
/plugin install sidegraph@sidegraph
```

Review the installation summary and any approval prompts, then start a new Claude Code
session. The plugin provides MCP, skills, session memory, capture reminders and context on reads.
To check the connection, run `/mcp` in the new session: `sidegraph` should be listed as connected.
For an existing install, run these in your terminal:

```sh
claude plugin marketplace update sidegraph
claude plugin update sidegraph@sidegraph
```

Keep the same install scope; see the [Claude Code guide](claude-code-setup.md#update-an-existing-plugin).
Start a new session after updating, or use `/reload-plugins` and review its prompts.

### Codex

Run these in a terminal in the repository:

```sh
codex plugin marketplace add SantyagoSeaman/sidegraph
codex plugin add sidegraph@sidegraph
```

Start a new interactive Codex session. Review Sidegraph's hooks through the startup warning
or `/hooks`, then approve them. Without approval, MCP remains available but automatic session
memory, capture reminders and subagent notices stay silent.
For an existing install, run this in your terminal:

```sh
codex plugin marketplace upgrade sidegraph
```

Start a new session and review any changed hook definitions through the startup warning or
`/hooks`; updating does not grant approval. This update recipe requires Codex CLI 0.162.1
or later. See the [Codex guide](codex-setup.md#keep-the-plugin-updated).

### Grok Build

The plugin brings the MCP tools, the skills and the hooks: records arrive when the agent reads,
searches, edits or writes a file or starts a subagent, and the agent is reminded to propose
decisions once a session has done some work. Grok does not pass the `SessionStart` hook's output
to the model, so the session map comes from the MCP server instead. Run in a terminal:

```sh
grok plugin marketplace add SantyagoSeaman/sidegraph
grok plugin install sidegraph@sidegraph
```

Accept the trust prompt (`--trust` skips it), then start a new Grok session in the repository.
Ask the agent which Sidegraph tools it has: `get_task_context` should be among them. Checked
on macOS with Grok Build 1.0.70 (hooks, session map, capture reminder) and 1.0.62 (install,
MCP tools, skills, a gotcha recorded in one session and retrieved in the next). Windows is not
checked. See the [Grok Build guide](../integrations/grok.md).
For an existing install, run these in your terminal, then start a new session:

```sh
grok plugin marketplace update
grok plugin update sidegraph
```

### Muse

MCP, shared skills, a session map at start and a memory brief for subagents; no context on
reads and no capture reminder. The hooks need `python3` (3.9 or later; the macOS one works).
Add the marketplace and install the plugin:

```sh
muse plugins marketplace add sidegraph https://github.com/SantyagoSeaman/sidegraph
muse plugins install sidegraph@sidegraph
muse plugins inspect sidegraph
```

Review the two hook capabilities and the MCP capability, then approve them:

```sh
muse plugins approve sidegraph
```

Exit Muse and start a new Muse process from the intended project directory. Restart it there
whenever you switch projects; selecting a workspace in a running process is insufficient.
For an existing install, refresh the marketplace and the plugin, then inspect the changed
capability:

```sh
muse plugins marketplace update sidegraph
muse plugins update sidegraph
muse plugins inspect sidegraph
```

Review changes, then approve and restart Muse from the target project:

```sh
muse plugins approve sidegraph
```

Installation, update, MCP approval, the session map and the subagent brief were checked on
Muse 1.4.14 on macOS; Windows is not checked. See the [Muse guide](../integrations/muse.md).

### OpenCode V2

Requires OpenCode V2 (native baseline 2.0.29), `uv` and Git on PATH. Native packaging and MCP
were checked on macOS; Windows is not checked. In your terminal:

```sh
opencode plugin add 'git+https://github.com/SantyagoSeaman/sidegraph.git#main'
```

Start a new OpenCode session in your project and use `@sidegraph-setup`. Keep OpenCode's
normal MCP and skill permissions; no additional plugin-approval command is needed. This
route supplies MCP and skills only: ask for context and capture decisions explicitly.
For an existing install, check and update the Git package, then restart OpenCode:

```sh
opencode plugin check 'git+https://github.com/SantyagoSeaman/sidegraph.git#main'
opencode plugin update 'git+https://github.com/SantyagoSeaman/sidegraph.git#main'
```

The [OpenCode installation guide](../../.opencode/INSTALL.md) is the canonical recipe, including
configuration overrides. The package target `#main` and the MCP runtime's Git ref are
separate settings.

### Any MCP client

Without a plugin, register the stdio server with your client and run it from the repository
root. For a client that reads an `mcpServers` file:

```json
{ "mcpServers": { "sidegraph": { "command": "uvx", "args": ["--from", "git+https://github.com/SantyagoSeaman/sidegraph.git@main", "sidegraph-mcp", "--repo-root"], "env": { "SIDEGRAPH_DIR": ".sidegraph", "SIDEGRAPH_GRAPH": "graphify-out/graph.json" } } } }
```

There is no setup skill without a plugin: build the graph and create the store from a terminal
as in the [Quickstart](quickstart.md), and ask for context with the MCP tools
([reference](../reference/mcp-tools.md)). No hooks run, so memory reaches the agent only when
it calls a tool.

## Upgrading an existing install

Read the [changelog](../../CHANGELOG.md) entries since your version first. Before upgrading
Graphify, follow the [0.14 migration guide](../guides/migrating-to-0.14.md) so accepted
domains keep their membership. [0.17](../guides/migrating-to-0.17.md) introduced the packed
store format. From 0.22, the first `sidegraph-compact` that packs or drops a drift check makes 0.17 to
0.21 refuse the store, so upgrade every teammate and CI pin before running it. Then use your
host's [update recipe](#host-plugins-install-and-update); Claude Code and Codex keep the same
`sidegraph@sidegraph` identity, so no reinstall is needed.

## Without the graph

Skip the Graphify install and `graphify update`, run `sidegraph-init` as usual, and answer no
to its *Keep the code graph fresh?* question. The store, the SessionStart map, the proposal
queue, store listings and global memory still work. Anchoring does not: `add_decision` keeps
no anchors without a graph, a proposed draft keeps only an orphaned anchor that waits for a
graph, and retrieval for a file or a symbol finds nothing (a seed that names a descriptor
already stored with a record still finds it). There is no structural map, no automatic domain
candidates and no following of moved code. The loop of capturing a record for a file and
recalling it when that file is read needs the graph. See
[the graph dependency](../reference/operations.md#the-graph-dependency-stated-plainly).

## If something does not work

- Run `sidegraph-doctor` (as `uvx --from git+https://github.com/SantyagoSeaman/sidegraph.git@main sidegraph-doctor`,
  or by its plain name if you installed the CLI, see [Option A](#option-a--the-cli-on-your-path)):
  it lists what is wrong with the store, the graph and the wiring.
  [Troubleshooting](../guides/troubleshooting.md) explains each message, and the
  [verification checklist](../guides/verifying-your-setup.md) walks the whole workflow.
- `get_task_context` finds nothing for a function: there is no graph yet, or the record was
  saved before the graph existed. Build the graph first (`graphify update .`). A proposed
  draft's orphaned anchors then heal with `sidegraph-sync`; a record written directly with
  `add_decision` kept no anchors, so add them with `add_anchors`.
- The agent has no Sidegraph tools: start a new session after installing, approve the
  plugin's prompts (Codex: its hooks too), and in Claude Code check `/mcp`.

## Removing Sidegraph

Remove the plugin with your host's plugin command (`/plugin uninstall sidegraph@sidegraph` in
Claude Code, `grok plugin uninstall sidegraph` in Grok Build).
`sidegraph-init --remove-hooks` removes the git hook. The records in `.sidegraph/` are your
project's memory and are committed: delete them only if you mean to lose them.
`graphify-out/` can always be deleted and rebuilt.

## Entry points

This table is the terminal reference. In day-to-day use you rarely type any of it: everyday
operations (recording, ratifying, syncing, domain work) run conversationally inside a
session through the MCP tools and plugin skills — see
[`reference/mcp-tools.md`](../reference/mcp-tools.md). The CLI commands below matter for
setup, scripted/CI use, and the three deliberately human-run jobs (`sidegraph-import`,
`sidegraph-compact`, `sidegraph-migrate`).

| Command | What it runs |
|---|---|
| `sidegraph-mcp` | The decision MCP server (stdio transport). |
| `sidegraph-session-start` | `SessionStart` hook — injects project memory. |
| `sidegraph-stop` | `Stop` hook — nudges the agent to distill decisions. |
| `sidegraph-pre-tool-use` | `PreToolUse` hook — hands the agent the records anchored to a file when it reads or edits it (`Read`, `Grep`, `Edit`, `Write`, and `sed`/`grep`/`rg`/`cat` in Bash). |
| `sidegraph-subagent-start` | `SubagentStart` hook — tells a starting subagent that decision memory exists and how to ask it. |
| `sidegraph-bootstrap` | Guided CLI — scan one of seven supported ADR/spec profiles, preview and review candidates, write only after confirmation, verify anchors/host integration, and prove production retrieval. |
| `sidegraph-init` | CLI — bootstrap `.sidegraph/` in a repo: create the store, check for the graph, ask (in a terminal) whether to install the git hook that keeps the graph fresh (`--hooks`, `--no-hooks`, `--remove-hooks`), print the plugin install path (and the no-plugin `claude mcp add` alternative). |
| `sidegraph-ratify` | CLI — review/accept/drop proposed decisions, facts, and domains. |
| `sidegraph-sync` | CLI — re-anchor the store after a Graphify rebuild. |
| `sidegraph-import` | CLI — bootstrap decisions from Graphify rationale nodes (code docstrings, or ADR/SAD prose after a semantic pass), or from existing ADR/spec markdown directly (`--docs`). |
| `sidegraph-domains` | CLI — author Domain proposals: `bootstrap` from graph communities, or `add` manually. |
| `sidegraph-compact` | CLI — pack terminal-status decisions (superseded/rejected/deprecated) and domains (superseded/dropped) into an immutable archive segment. On a packed (format 2) store it is maintenance instead: it folds settled per-record files and archive segments into the shard files verbatim. Never commits. |
| `sidegraph-migrate` | CLI — convert a store to the packed format 2 (shard files): switches the `format` marker first, then folds every per-record file and archive segment into the shards. Run on the default branch; never commits. |
| `sidegraph-require` | CLI — make the store refuse Sidegraph versions older than `--sidegraph X.Y.Z`: rewrites `.sidegraph/format` and prints the commit command; never lowers a requirement. |
| `sidegraph-verify` | CLI — lint the store's canonical files against its write-path invariants (schema, validity windows, referential integrity, ULID uniqueness); `--against <git-ref>` additionally checks that every store file changed vs that ref was mutated legally (CI mode). |
| `sidegraph-doctor` | CLI — one-stop store health: composes `sidegraph-verify`'s strict lint with an advisory curation pass (stale proposals, dangling/degraded/orphaned records, never-surfaced decisions); `--check` also fails on advisory findings. |
| `sidegraph-viz` | CLI — render an interactive HTML/JSON view of decisions, facts, entities, anchor status, supersession, and evidence links. |
| `sidegraph-export-adr` | CLI — preview ADR projections and diffs from canonical memory; explicitly write managed documents with preserved human notes. See [ADR export](../guides/adr-export.md). |
| `sidegraph-export-okf` | CLI — project the store into a deterministic [Open Knowledge Format v0.1](https://github.com/GoogleCloudPlatform/knowledge-catalog/tree/main/okf) bundle any OKF-aware tool can read; strictly one-way, the store stays the source of truth. |
| `sidegraph-stats` | CLI — one screen of local usage statistics from the gitignored index (how often memory was asked for, how much of the code worked on has memory anchored to it, what the store holds, anchor health); read-only, never creates the index, `--json` for the same report as data. Reachable in a session as `/sidegraph:stats`. |
| `sidegraph-prepare-commit-msg` | `prepare-commit-msg` git hook — comments candidate `Sidegraph-Decision:` trailers into the commit message template; fail-open with bounded git/SQLite stages. See [`reference/git-bindings.md`](../reference/git-bindings.md). |
| `sidegraph-blame` | CLI — `git blame` a file, joined to the decisions/facts each hunk's commit carries (commit trailers + `provenance.commit`). See [`reference/git-bindings.md`](../reference/git-bindings.md). |

Commands that need the graph use `SIDEGRAPH_GRAPH` (default
`graphify-out/graph.json`). Store-aware entry points resolve `SIDEGRAPH_DIR` through the same
shared precedence: an explicit `--db` flag where available, then `$SIDEGRAPH_DIR`, then the
deprecated `$SIDEGRAPH_DB`, then an existing `.sidegraph/`, then the default `.sidegraph` — see
[`reference/configuration.md`](../reference/configuration.md#store-path-resolution) for the
exact rules.

## Install Graphify

```bash
uv tool install "graphifyy==0.9.90"   # note the double-y package name; the CLI is `graphify`
export GRAPHIFY_NO_AUTO_REFRESH=1        # macOS, Linux: keeps Graphify off your agent skills
$env:GRAPHIFY_NO_AUTO_REFRESH = "1"      # Windows PowerShell: use this line instead
graphify --version                    # sanity check
```

**Pin the version.** Graphify releases well ahead of Sidegraph's verified range, and the
engine seam is the one place an engine release can break Sidegraph. Known-good:
**0.9.6 and 0.9.90**. 0.9.90 was verified for the 0.14.0 release on this repository and on
the openspec corpus (build, sync and domain snapshot). Installing unpinned may select a
newer, unverified version. See
[engine version pinning](../integrations/graphify.md#engine-version-pinning) for how to
re-verify after an upgrade.

Fallback if you don't use `uv`: `pip install 'graphifyy==0.9.90'`.

`[mcp]` is an **optional** extra (`uv tool install "graphifyy[mcp]==0.9.90"`) that adds Graphify's
*own* MCP server — a deeper structure-query layer over the same `graph.json`. It coexists
fine with Sidegraph; Sidegraph only ever reads `graph.json` directly and doesn't need it. Add
`[pdf]` if your corpus includes PDFs (extras compose: `"graphifyy[mcp,pdf]==0.9.90"`).

See [`integrations/graphify.md`](../integrations/graphify.md) for how Sidegraph reads
Graphify's output, corpus types, and troubleshooting.

## Platform coverage

On every pull request that is ready for review (not a draft), on `main` and before every
publish, the automated matrix runs the full suite on Linux with Python 3.13 and 3.14. Native
Windows x64 (`windows-2022`) runs a selected portable core/MCP suite on both versions:
Store mutation/rollback and cross-process conflicts, UTF-8 and descriptor cleanup,
MCP result contracts, invocation admission, and wheel-probe guards. It also builds and
installs a non-editable wheel outside the checkout, checks its package bytes against the
artifact, runs a CLI, and exchanges real MCP stdio messages in a Unicode/spaced directory.
The exact selection is in [the CI workflow](../../.github/workflows/ci.yml).

For the 0.14.0 release-candidate campaign, real Claude Code and Codex sessions ran with the
installed plugin, in fresh host profiles, on Windows Server 2022 x64 and macOS 15 arm64,
each with Python 3.13 and 3.14. Each run was the full plugin session for both hosts: the
plugin's MCP server, `SessionStart` context, records before a Read, a bundled skill, a
subagent brief, the `Stop` capture prompt, capture and ratification, and recall in a fresh
session. The project folder and host profile paths contained a non-ASCII character.

**0.14.0 publication gate:** Claude Code 2.1.296 and Codex CLI 0.162.1 on all four cells
above, including a Windows 3.14 cell with PowerShell 7 unavailable for preflight and both
real host sessions. Windows 3.13 retains PowerShell 7; its launcher checks also cover 5.1.
Stable publication of 0.14.0 required the full gate to pass without a known-issue exception.
The README's badges show the latest workflow run and release, not support for every
published version. Muse, OpenCode and Grok Build have the narrower
coverage in their installation guides and are outside this native matrix.

On native Windows, the Claude Code hooks run through Git Bash, which comes with Git for
Windows; the MCP launchers and the Codex hooks need no POSIX shell. The native checks run
with Git for Windows installed; they do not establish support for Windows hosts without it. Graphify integration
remains outside these checks.

Package dependencies support `fastmcp>=3.4.10,<4`, `mcp>=1.30,<2` and `markdown-it-py>=4,<5`
(markdown section scope for drift checks). Runtime security
floors also require `certifi>=2026.10.19`,
`opentelemetry-api>=1.45.1`, `authlib>=1.9.1` and `dnspython>=2.9.0`, including standalone wheel installations. Major SDK upgrades
require a separate compatibility review; an unlocked package install may resolve newer
versions within those bounds than the development lockfile.

## Next

Continue to the [quickstart](quickstart.md) for the shortest path to a first captured and
retrieved decision. If the repository already contains ADRs or supported flow specs, use the
[Bootstrap existing rationale guide](bootstrap.md) for the preview-first path instead. Once
you've done that, run through
[verifying your setup](../guides/verifying-your-setup.md) — a four-case checklist for host
wiring, durable capture/retrieval, ratification with evidence, and sync/repository hygiene.
