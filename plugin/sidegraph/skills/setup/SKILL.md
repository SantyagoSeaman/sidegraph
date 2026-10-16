---
name: setup
description: >-
  Use when Sidegraph needs to be set up (or its setup finished/verified) in a repo — "set up sidegraph", "install sidegraph here", "get sidegraph working on this project", "настрой sidegraph", a fresh clone where memory should be turned on, or "why is my SessionStart map empty". Also reachable directly as /sidegraph:setup. The end-to-end first run: engine → graph → store → wiring check — then hands off to name-domains / import-adrs / record-decision for the first real memory.
---

# Set up Sidegraph

Bring a repo from zero to a working memory loop. One fact up front: **if this skill is
running, the Sidegraph plugin is already installed**. Claude Code wires MCP and all four
hooks (`SessionStart`, `Stop`, `PreToolUse`, `SubagentStart`); Codex wires MCP and
`SessionStart`, `Stop`, `SubagentStart`. Both launch Sidegraph through `uvx` from the repository. OpenCode and experimental Muse support MCP and skills only:
use explicit MCP calls; there is no automatic session map, capture nudge or read context.
What may still be missing: the engine, the graph, the store, and the first
memory. Work through the steps in order; every one is idempotent and safe to re-run.

## 1. Engine

```bash
export GRAPHIFY_NO_AUTO_REFRESH=1        # macOS, Linux: keeps Graphify off your agent skills
$env:GRAPHIFY_NO_AUTO_REFRESH = "1"      # Windows PowerShell: use this line instead
graphify --version
```

Missing → install it, pinned to a known-good version: `uv tool install "graphifyy==0.9.90"`
(double-y package name; the CLI is `graphify` — see
[engine version pinning](../../../../docs/integrations/graphify.md#engine-version-pinning)
for the current known-good range). If the command is still not found afterwards, the
`uv tool` bin directory isn't on `PATH` — `uv tool update-shell`, new shell, retry.

## 2. Graph

At the repo root, run `graphify update .` with the environment variable `GRAPHIFY_NO_AUTO_REFRESH=1` set. Without the variable, Graphify 0.9.87 and later refresh the agent skills they installed (such as `~/.claude/skills/graphify/`) on the first run after a Graphify upgrade; [set it persistently](../../../../docs/integrations/graphify.md#building-the-graph) to stop repeating it.

The LLM-free build — no API key. Produces `graphify-out/graph.json`, which Sidegraph reads
**read-only** (never writes). Works on code *and* markdown/ADR corpora, and on a non-git
folder too (sync falls back to a content-hash graph version — see
[non-git corpora](../../../../docs/integrations/graphify.md#non-git-and-doc-only-corpora)).

## 3. Store

```bash
uvx --from git+https://github.com/SantyagoSeaman/sidegraph.git@main sidegraph-init
```

Every Sidegraph CLI in this skill (`sidegraph-init --hooks`, `--no-hooks`, `--remove-hooks`,
`sidegraph-doctor --json`) is run this way: put the `uvx --from ...@main` prefix above before the
command name, so it uses the public runtime source rather than the copy on `PATH`
([why](../../../../docs/reference/cli.md#running-the-clis-beside-the-plugin)). Hosts without
SessionStart do not establish a session version pin.

Creates `.sidegraph/` — the repo-committed, file-per-record sidecar, plus its own
`.gitignore` keeping the derived `index.db` out of git — and reports the graph check.
Expected: `created store: .sidegraph` (or `already initialized: .sidegraph exists.` —
fine) and `found graph:` followed by the absolute path of `graphify-out/graph.json`.
`missing graph:` means step 2 didn't run here. Run from a session it is non-interactive, so it
does not ask whether to install the git hook that keeps the graph fresh; it prints how to
(`sidegraph-init --hooks`, or `--no-hooks` to stop the reminder). Ask the human, and do not
install it unasked (see [the Graphify
integration](../../../../docs/integrations/graphify.md#keeping-the-graph-fresh-git-hooks)).
Commit `.sidegraph/` with the repo — the store travels with the code.

## 4. Verify the wiring

On experimental Muse, first confirm that Muse was started in this repository: the MCP
server serves the process's starting directory. Otherwise exit and restart Muse here.

Call `list_domains()` — the cheapest read-only MCP tool. A well-formed answer proves the
server is alive; an empty list is normal on a fresh store. To verify the intended project
store, retrieve a known project record, or use the linked capture/recall proof for a new store. On hosts with lifecycle hooks, the `SessionStart` memory map appears at the start of the **next** session — a stub
on an empty store is expected, not broken. For the full four-case proof (host wiring, durable
capture and retrieval, ratification, sync), point the human at
[verifying your setup](../../../../docs/guides/verifying-your-setup.md).

For Codex, OpenCode and experimental Muse, the MCP call is the wiring check; skip the
following Claude-specific settings check. Shared `sidegraph:<skill>` names denote the corresponding
installed skill; in OpenCode use `@sidegraph-<skill>`.

On Claude Code, run `uvx --from git+https://github.com/SantyagoSeaman/sidegraph.git@main sidegraph-doctor --json` (a pure read) and read the finding `plugin-off-in-subdirectories`. It says that Claude
sessions started below the repository root run without Sidegraph: the plugin is enabled only in
the root's `.claude/settings.json`, which Claude Code reads for root launches alone, or a nested
directory's own settings leave it off. The fix changes the human's settings, so put the options
to them and do not edit the files unasked: enable the plugin in `.claude/settings.local.json` at
the root (it applies to every directory of the repository) or in their user settings. Version
skew is not part of this step: a session's Bash tool has no plugin root to compare against, so
the `Sidegraph <version>` line `SessionStart` prints and its notice cover it. See
[troubleshooting](../../../../docs/guides/troubleshooting.md#plugin-off-in-subdirectories).

## 5. Keep the graph fresh (ask the human first)

A graph nobody rebuilds goes stale, and memory then cannot see or anchor to new code. Ask the
human whether to install the refresh hook: a marked block in `post-commit`, `post-merge` and
`post-checkout` that rebuilds the **code** graph in the background, in the main checkout only
(linked worktrees read its graph). It never replaces an existing hook, and
`sidegraph-init --remove-hooks` takes it out. On a yes run
`uvx --from git+https://github.com/SantyagoSeaman/sidegraph.git@main sidegraph-init --hooks`; on a no run
`uvx --from git+https://github.com/SantyagoSeaman/sidegraph.git@main sidegraph-init --no-hooks`, which records the answer so that Sidegraph stops asking. Do not run
either unasked, and do not run `graphify hook install` instead: Graphify's own hook also rebuilds
in every linked worktree. (Doc/prose extraction still needs a manual rebuild after doc changes: run `graphify update .` with the environment variable `GRAPHIFY_NO_AUTO_REFRESH=1` set.) Details:
[keeping the graph fresh](../../../../docs/integrations/graphify.md#keeping-the-graph-fresh-git-hooks).

**Do NOT run `graphify claude install`.** The plugin already fronts the graph for Claude
Code (SessionStart map, records on read and edit, MCP); Graphify's own Claude wiring is redundant and
noisier — see
[why](../../../../docs/integrations/graphify.md#graphify-claude-install-is-redundant-with-the-sidegraph-plugin--skip-it).

## 6. First memory — hand off

In order of payoff:

1. **`sidegraph:name-domains`** — turn the graph's raw communities into a named table of
   contents; on hosts with lifecycle hooks, accepting a domain brings the `SessionStart`
   TOC to life.
2. **`sidegraph:import-adrs`** — if the repo already has ADRs/specs/runbooks, seed the
   store from them instead of starting empty.
3. **`sidegraph:record-decision`** — capture the first gotcha by hand; then call
   `get_task_context(files=[...])` for the relevant files to check recall on any host.

## Reset / cleanup

The store is `.sidegraph/`; everything Graphify owns is `graphify-out/`. Delete both to
start over. Source files are never touched. Sidegraph can also leave a few things outside
those two directories:

- the graph refresh hook, if installed (`sidegraph-init --hooks`, or a yes to its question):
  a helper and three hook blocks under `.git/hooks/`. Run
  `uvx --from git+https://github.com/SantyagoSeaman/sidegraph.git@main sidegraph-init --remove-hooks` first, because deleting the directories leaves them behind.
- the git config key `sidegraph.graphRefresh`, recorded by `sidegraph-init --no-hooks`.
  `--remove-hooks` clears it.
- `SIDEGRAPH_RATIFY_POLICY` in `.claude/settings.json`, if `sidegraph-init` wrote it.
- `${XDG_CACHE_HOME:-~/.cache}/sidegraph/launch-commit`, written by the SessionStart hook
  and shared by every project on the machine.

## See also

- [`docs/getting-started/quickstart.md`](../../../../docs/getting-started/quickstart.md) —
  the same path in doc form, including the no-plugin manual wiring alternative.
- [`docs/reference/configuration.md`](../../../../docs/reference/configuration.md) —
  store/graph path resolution (`SIDEGRAPH_DIR`, `SIDEGRAPH_GRAPH`), the most common cause
  of "nothing shows up".
