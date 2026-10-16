# Installation

Install **Sidegraph** for the decision store, MCP server, CLI, and host hooks. Add
**Graphify** for symbol-level anchors and structural context. Sidegraph can retrieve
file- and domain-linked records without a graph; the complete workflow below installs both.

## Prerequisites

- **Python 3.13+** for Sidegraph (`.python-version` pins it).
- **Python 3.10+** for Graphify.
- [`uv`](https://docs.astral.sh/uv/) — used to install and run both.
- For automatic session integration: [Claude Code](claude-code-setup.md) or
  [Codex](codex-setup.md). [OpenCode V2](../../.opencode/INSTALL.md) and experimental
  [Muse](../integrations/muse.md) provide MCP tools and shared skills without lifecycle hooks.
  Other stdio MCP clients can call the tools directly; the CLI also works without a host.

### Platform coverage

The automated matrix runs the full suite on Linux with Python 3.13 and 3.14. Native
Windows x64 (`windows-2022`) runs a selected portable core/MCP suite on both versions:
Store mutation/rollback and cross-process conflicts, UTF-8 and descriptor cleanup,
MCP result contracts, invocation admission, and wheel-probe guards. It also builds and
installs a non-editable wheel outside the checkout, checks its package bytes against the
artifact, runs a CLI, and exchanges real MCP stdio messages in a Unicode/spaced directory.
The exact selection is in [the CI workflow](../../.github/workflows/ci.yml).

Separately, each release candidate runs real Claude Code and Codex sessions with the
installed plugin, in fresh host profiles, on Windows Server 2022 x64 and macOS 15 arm64,
each with Python 3.13 and 3.14. Each run is the full plugin session for both hosts: the
plugin's MCP server, `SessionStart` context, records before a Read, a bundled skill, a
subagent brief, the `Stop` capture prompt, capture and ratification, and recall in a fresh
session. The project folder and host profile paths contain a non-ASCII character.

**0.14.0 publication gate:** Claude Code 2.1.296 and Codex CLI 0.162.1 on all four cells
above, including a Windows 3.14 cell with PowerShell 7 unavailable for preflight and both
real host sessions. Windows 3.13 retains PowerShell 7; its launcher checks also cover 5.1.
Stable publication requires the full gate to pass without a known-issue exception. The
README's blue badges summarize release acceptance, rather than live workflow status or
support for every published version. Muse and OpenCode have the narrower coverage in
their installation guides and are outside this native matrix.

On native Windows, the Claude Code hooks run through Git Bash, which comes with Git for
Windows; the MCP launchers and the Codex hooks need no POSIX shell. The native checks run
with Git for Windows installed; they do not establish support for Windows hosts without it. Graphify integration
remains outside these checks.
See [Windows](#windows) before installing a host plugin.

Package dependencies support `fastmcp>=3.4.2,<4` and `mcp>=1.24,<2`. Major SDK upgrades
require a separate compatibility review; an unlocked package install may resolve newer
versions within those bounds than the development lockfile.

### Windows

The native checks above cover the tested release, not every published version. On
Windows they cover the full plugin session; WSL is not a separate CI target.

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

**Scope.** `/plugin install` offers three scopes: user, local and project. From a terminal the
flag is `claude plugin install sidegraph@sidegraph --scope user|project|local`, and the default
is user. Enable the plugin at user scope, or in the repository root's
`.claude/settings.local.json`, and it covers sessions started in any subdirectory. Project scope
(`.claude/settings.json`) covers only the directory it was installed from: Claude Code reads a
launch directory's own project settings and no parent's, so a plugin installed at the repository
root leaves sessions started below it without it. `sidegraph-doctor` reports the gap as
[`plugin-off-in-subdirectories`](../guides/troubleshooting.md#plugin-off-in-subdirectories).

For installation and updates across all four hosts, see the
[README host recipes](../../README.md#claude-code). Existing Claude Code and Codex installs
keep `sidegraph@sidegraph`; update cached assets with the host's native update commands,
then start a new session and review any changed approvals. The runtime's `@main` does not
refresh cached skills or manifests. OpenCode V2 uses the
[canonical Git package guide](../../.opencode/INSTALL.md). Experimental
[Muse](../integrations/muse.md) supplies MCP/skills only and must be restarted from the
intended project when switching workspaces; expanded native acceptance is deferred.

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


## Entry points

This table is the terminal reference. In day-to-day use you rarely type any of it: everyday
operations (recording, ratifying, syncing, domain work) run conversationally inside a
session through the MCP tools and plugin skills — see
[`reference/mcp-tools.md`](../reference/mcp-tools.md). The CLI commands below matter for
setup, scripted/CI use, and the two deliberately human-run jobs (`sidegraph-import`,
`sidegraph-compact`).

| Command | What it runs |
|---|---|
| `sidegraph-mcp` | The decision MCP server (stdio transport). |
| `sidegraph-session-start` | `SessionStart` hook — injects project memory. |
| `sidegraph-stop` | `Stop` hook — nudges the agent to distill decisions. |
| `sidegraph-pre-tool-use` | `PreToolUse` hook — hands the agent the records anchored to a file when it reads or edits it (`Read`, `Grep`, `Edit`, `Write`, and `sed`/`grep`/`rg`/`cat` in Bash). |
| `sidegraph-subagent-start` | `SubagentStart` hook — tells a starting subagent that decision memory exists and how to ask it. |
| `sidegraph-bootstrap` | Guided CLI — scan one of six supported ADR/spec profiles, preview and review candidates, write only after confirmation, verify anchors/host integration, and prove production retrieval. |
| `sidegraph-init` | CLI — bootstrap `.sidegraph/` in a repo: create the store, check for the graph, ask (in a terminal) whether to install the git hook that keeps the graph fresh (`--hooks`, `--no-hooks`, `--remove-hooks`), print the plugin install path (and the no-plugin `claude mcp add` alternative). |
| `sidegraph-ratify` | CLI — review/accept/drop proposed decisions, facts, and domains. |
| `sidegraph-sync` | CLI — re-anchor the store after a Graphify rebuild. |
| `sidegraph-import` | CLI — bootstrap decisions from Graphify rationale nodes (code docstrings, or ADR/SAD prose after a semantic pass), or from existing ADR/spec markdown directly (`--docs`). |
| `sidegraph-domains` | CLI — author Domain proposals: `bootstrap` from graph communities, or `add` manually. |
| `sidegraph-compact` | CLI — pack terminal-status (superseded/rejected/dropped) decisions and domains into an immutable archive segment. |
| `sidegraph-require` | CLI — make the store refuse Sidegraph versions older than `--sidegraph X.Y.Z`: rewrites `.sidegraph/format` and prints the commit command; never lowers a requirement. |
| `sidegraph-verify` | CLI — lint the store's canonical files against its write-path invariants (schema, validity windows, referential integrity, ULID uniqueness); `--against <git-ref>` additionally checks that every store file changed vs that ref was mutated legally (CI mode). |
| `sidegraph-doctor` | CLI — one-stop store health: composes `sidegraph-verify`'s strict lint with an advisory curation pass (stale proposals, dangling/degraded/orphaned records, never-surfaced decisions); `--check` also fails on advisory findings. |
| `sidegraph-viz` | CLI — render an interactive HTML/JSON view of decisions, facts, entities, anchor status, supersession, and evidence links. |
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

## Next

Continue to the [quickstart](quickstart.md) for the shortest path to a first captured and
retrieved decision. If the repository already contains ADRs or supported flow specs, use the
[Bootstrap existing rationale guide](bootstrap.md) for the preview-first path instead. Once
you've done that, run through
[verifying your setup](../guides/verifying-your-setup.md) — a four-case checklist for host
wiring, durable capture/retrieval, ratification with evidence, and sync/repository hygiene.
