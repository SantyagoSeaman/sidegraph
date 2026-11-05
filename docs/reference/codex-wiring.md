# Codex hook wiring

> **Start here:** Use this page to inspect the implementation contract. For installation, follow the [host setup paths](../README.md). [Glossary](../concepts/glossary.md).

For pinning a version, see [pinned releases](../getting-started/installation.md#mutable-development-references).

## Hooks (GA)

The copyable project-scoped POSIX recipe is in [Codex integration](../integrations/codex.md#hooks-ga).


The public plugin wraps its POSIX commands in `sh -c '...'`. Keep that explicit POSIX shell
boundary when adapting the manual recipe for a host that launches another shell. The recipe
uses POSIX assignments, substitutions and fallback guards. Windows uses the separate
`commandWindows` launcher.

The `cd "$(git rev-parse --show-toplevel 2>/dev/null || pwd)"` prefix stands in for the
`cd "${CLAUDE_PROJECT_DIR}"` prefix of the Claude Code hooks — Codex hook entries, like Claude Code's,
have no dedicated `cwd`/`env` fields, so the working directory has to be pinned inside the
command itself. The `|| pwd` fallback matters because Sidegraph explicitly supports non-git
corpora (a plain folder of docs Graphify was pointed at directly — see
[`graphify.md`](../integrations/graphify.md#non-git-and-doc-only-corpora)): a bare
`git rev-parse --show-toplevel` fails outside a git repo and would leave `cd` with no argument,
breaking the hook entirely, whereas falling back to `pwd` at least keeps the relative
`SIDEGRAPH_DIR`/`SIDEGRAPH_GRAPH` paths anchored to wherever Codex launched the hook from.
`sidegraph-session-start` emits the same `additionalContext` payload described in
[`claude-code.md`](../integrations/claude-code.md#sessionstart--sidegraph-session-start) (communities,
global mistakes); `sidegraph-stop` emits the same guarded block-to-distill
nudge, once per session and again after 30 minutes and 10 new commits (see
[`reference/hooks.md`](hooks.md#re-arm-after-more-work)). `sidegraph-subagent-start`
gives each subagent Codex starts a short brief, because a subagent gets no `SessionStart`
context: the `get_task_context(files=[…])` call to make, and one sentence on how many records
memory holds and in how many files (see the
[hooks reference](hooks.md#sidegraph-subagent-start); `SIDEGRAPH_SUBAGENT_BRIEF=off`
disables it). `sidegraph-stop` and `sidegraph-subagent-start` degrade silently on any failure.
`sidegraph-session-start` does too, with one exception: a store that cannot be opened. It then
sends a notice (a `systemMessage` that names the cause and the fix) and tells the model that
memory tools will fail, because a broken store should not go unseen. A missing store is never created by a hook:
`SessionStart` sends a notice that asks for an explicit `sidegraph-init`, and a missing graph only shortens the map; neither blocks the session.

Keep the fallback guards when adapting the recipe. Their behavior and the terminal reproduction steps are in [the command guard](hooks.md#the-command-guard).

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
[`reference/hooks.md`](hooks.md#sidegraph-stop).

Codex only loads project-local `.codex/` hooks (like the [manual recipe](../integrations/codex.md#hooks-ga)) when the project layer
is marked trusted — if these hooks silently don't fire, check the trust prompt/settings before
assuming the recipe JSON is wrong.

The manual recipe has no `PreToolUse` entry. Codex can invoke the event for local function tools
(including shell, patch, and MCP calls), but it has no stable `Read`/`Grep` pair equivalent to
Claude Code's, and the records block the `sidegraph-pre-tool-use` hook hands over for a file (see
[`claude-code.md`](../integrations/claude-code.md#pretooluse--sidegraph-pre-tool-use)) needs a tool call that
names the file. Delivery there is deferred. Re-check the current [Codex hooks documentation](https://learn.chatgpt.com/docs/hooks)
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


## Native verification

**Native verification for the 0.14.0 release (Codex CLI 0.162.1, macOS ARM, 2026-10-15).** A fresh owned
`CODEX_HOME` installed the public-form Sidegraph from a Git marketplace. All 30 installed
plugin files matched committed source after the four public substitutions. The native
registries contained the 12 skills shipped then (14 today), the expected enabled `sidegraph` MCP command/arguments/
environment, and exactly three initially untrusted hooks. The existing native gate's
`config/batchWrite` approval path wrote only their current hashes; all three then reported
trusted. No project-trust write was needed for this plugin registry check.

A local fixture upgrade changed the version and one skill body while keeping MCP/hook
bytes unchanged. It updated the installed cache root without uninstalling and preserved
all three hook hashes and trust without new writes. With the origin unavailable, upgrade
exited 1 and retained the old version, installed bytes, enabled registration and trust.
After the origin was restored, a fixture changing only Stop's selected command made that
hook `modified`, with a new hash; the other two remained trusted. This is evidence of
source-unavailable retention, not a guarantee of atomic recovery from every interrupted
filesystem write. Fixture versions were synthetic; the committed package was not bumped.

That check used an owned reserved HTTPS-shaped Git URL rewritten to a local bare origin.
It did not repeat a public GitHub fetch, use a model, execute hooks or call MCP tools.
The earlier 0.154.0 check verified the GitHub `owner/repo` shorthand and real MCP calls;
those are historical observations, not fresh authenticated evidence for 0.162.1. Full
native acceptance of the exact release candidate remains a separate Windows/macOS gate.

Project trust and hook trust are separate. Project trust controls project-local settings;
installing or enabling a plugin does not approve its hook hashes. A non-interactive
`codex exec` run does not show the interactive approval prompt, so automation must arrange
reviewed approval explicitly, as the native gate does in its own profile. User installation
should use the startup warning or `/hooks`, without a trust bypass. The unchanged nested
compatibility manifest works with 0.162.1; the current plugin contract also documents that
compatibility form. Recheck the primary host documentation when using another Codex version.
