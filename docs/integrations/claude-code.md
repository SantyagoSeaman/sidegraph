# Claude Code integration

> **Start here:** Install the host plugin, start a new session in your repository, then [verify it](../guides/verifying-your-setup.md). [Glossary](../concepts/glossary.md).

The deep dive on how Sidegraph wires into Claude Code. For the copy-paste happy path, see
[`getting-started/claude-code-setup.md`](../getting-started/claude-code-setup.md).

## Manual MCP registration

Add to the target repo's `.mcp.json` (create it if absent):

The commands below follow the current development branch. For durable environments, see
[how to pin mutable development references](../getting-started/installation.md#mutable-development-references).

```json
{
  "mcpServers": {
    "sidegraph": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/SantyagoSeaman/sidegraph.git@main", "sidegraph-mcp"],
      "env": { "SIDEGRAPH_DIR": ".sidegraph", "SIDEGRAPH_GRAPH": "graphify-out/graph.json" }
    }
  }
}
```

Or via the CLI — carry both env vars so this setup lands on the same store path as every
other setup path (manual, plugin, Codex):

```bash
claude mcp add sidegraph -s project --env SIDEGRAPH_DIR=.sidegraph --env SIDEGRAPH_GRAPH=graphify-out/graph.json -- uvx --from git+https://github.com/SantyagoSeaman/sidegraph.git@main sidegraph-mcp
```

**From a source checkout** (contributors — see
[installation: from source](../getting-started/installation.md#option-d--from-source-contributors)),
replace the `uvx --from git+...` invocation in either form above with `uv run --project
/ABSOLUTE/PATH/TO/sidegraph sidegraph-mcp`.

The released package is also on PyPI. Use it to pin an exact version (see
[installation: the CLI on your PATH](../getting-started/installation.md#option-a--the-cli-on-your-path)),
replacing `X.Y.Z` with a release:

```bash
claude mcp add sidegraph -s project --env SIDEGRAPH_DIR=.sidegraph --env SIDEGRAPH_GRAPH=graphify-out/graph.json -- uvx --from 'sidegraph==X.Y.Z' sidegraph-mcp
```

```json
{ "mcpServers": { "sidegraph": { "command": "uvx", "args": ["--from", "sidegraph==X.Y.Z", "sidegraph-mcp"], "env": { "SIDEGRAPH_DIR": ".sidegraph", "SIDEGRAPH_GRAPH": "graphify-out/graph.json" } } } }
```

## Hooks in detail

The session starts with this instruction:

> When you need to find or understand code in this project, call get_task_context(files=[…])
> with repo-relative paths before any grep or file search — decisions, gotchas and a domain map
> are indexed here. Before a non-trivial change, run the sidegraph check-plan skill if it is
> available. If the tool is listed only by name, load it first.

SessionStart gives the initial map. PreToolUse supplies records when a supported file read or edit runs. SubagentStart introduces the lookup tools. Stop reminds an agent to capture durable learning after substantial work. See [real output](../reference/examples.md).

See the [detailed contract](../reference/host-wiring.md#hooks-in-detail).

## Plugin install path

Sidegraph ships a Claude Code plugin + marketplace manifest **in the repo itself** — this is
the recommended install path:

```
/plugin marketplace add SantyagoSeaman/sidegraph
/plugin install sidegraph@sidegraph
```

The plugin supplies the MCP tools, skills and four hooks together. On native Windows, install Git for Windows so Claude Code can run the hook commands through Git Bash. See [launcher details](../reference/host-wiring.md#plugin-launchers).

### Updating the installed plugin

The marketplace and plugin retain the `sidegraph@sidegraph` identity, so existing installs
need no layout migration. Follow [Update an existing plugin](../getting-started/claude-code-setup.md#update-an-existing-plugin)
to refresh the marketplace, update the plugin at its installed scope, and start a new
session or use `/reload-plugins`. Review the host's update summary and approval prompts before continuing.

Updating the runtime from public `@main` and updating Claude Code's cached plugin files
are separate operations. A running session keeps the skills and manifests it loaded;
resolving a newer runtime commit does not replace them. Third-party marketplace auto-update
is off by default, so enable it deliberately or use the explicit update commands.

## Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `SIDEGRAPH_DIR` | `.sidegraph` | Path to the decision store directory (per-record files and, once packed, shard files; git-committed). Point it at a path inside the target repo, e.g. `.sidegraph`, so it can be committed as the repo's memory. `SIDEGRAPH_DB` is honored for back-compat (deprecated) — see [`reference/configuration.md`](../reference/configuration.md#store-path-resolution). |
| `SIDEGRAPH_GRAPH` | `graphify-out/graph.json` | Path to Graphify's output graph, read-only. |
| `SIDEGRAPH_CAPTURE_NUDGE` | unset | Set to `off` to disable the `Stop` hook's block-to-distill nudge entirely (no other value has any effect). |
| `SIDEGRAPH_GREP_NUDGE` | unset | Set to `off` to turn off `PreToolUse` delivery of records at the point of reading, on every tool including Bash (no other value has any effect; touch recording is separate). It does not touch the records added to a subagent's brief on an `Agent` call: that is `SIDEGRAPH_AGENT_BRIEF`. |
| `SIDEGRAPH_RATIFY_NUDGE` | unset | Set to `off` to disable the `SessionStart` pending-ratification line entirely (no other value has any effect). |
| `SIDEGRAPH_SUBAGENT_BRIEF` | unset | Set to `off` to disable the `SubagentStart` memory brief entirely (no other value has any effect). |
| `SIDEGRAPH_AGENT_BRIEF` | unset | Set to `off` to stop the `PreToolUse` hook adding records to a subagent's brief on an `Agent` call (no other value has any effect; on by default). |
| `SIDEGRAPH_AUTO_ACCEPT` | unset | **Deprecated** (use `SIDEGRAPH_RATIFY_POLICY=auto-all`; SessionStart and `sidegraph-doctor` warn when it is set in their environment; `propose_decisions` results carry a `deprecation` note while it is `on`). Set to `on` to land agent-proposed decisions/facts as `accepted` immediately, bypassing the ratification queue (no other value has any effect; domains are always exempt). See [`guides/capturing-decisions.md#4-auto-accept-opt-in`](../guides/capturing-decisions.md#4-auto-accept-opt-in). |

A relative `SIDEGRAPH_DIR` is anchored to the **launch directory**: `CLAUDE_PROJECT_DIR` for the
hooks, the working directory of the process for the MCP server and the CLI. On the host surfaces
(the hooks and the MCP server), a relative store that does not exist there is then looked up in the
parent directories, up to the repository's `.git` boundary (see
[Launching in a subdirectory](#launching-in-a-subdirectory)); the CLI never does. A relative
`SIDEGRAPH_GRAPH` is resolved against the project of the store `SIDEGRAPH_DIR` names (the store's
parent directory) by the CLI, the MCP server and the hooks. The `SIDEGRAPH_DIR` half is the crux of
the cwd caveat below.

## The cwd caveat

A relative `SIDEGRAPH_DIR` is anchored to the launch directory: `CLAUDE_PROJECT_DIR` for the hooks
(the directory Claude Code was started in, not necessarily the repository root), `os.getcwd()` for
the MCP server and the CLI. With the default `.sidegraph` in the project, `SIDEGRAPH_GRAPH`
therefore also lands in the project. Where the relative store does not exist at that anchor, the
hooks and the MCP server look for it in the parent directories, up to the repository's `.git`
boundary (see [Launching in a subdirectory](#launching-in-a-subdirectory)); the CLI never does.
Anchoring to the launch directory is fine when Claude Code launches the MCP server/hooks with the
project directory as cwd — the common case for the manual `.mcp.json`/`.claude/settings.json` setup
above. It is **not guaranteed** for a plugin-distributed MCP server or hook, where cwd can differ
from the project root.

Two mitigations, both config-level (no source change needed to use them):

- **MCP servers**: `.mcp.json` supports a `"cwd"` field per server, but it is **not** part of
  Claude Code's documented MCP server schema (documented fields are `command`/`args`/`env`
  and a couple of transport-specific ones), so the plugin does not rely on it. Instead the
  published `.mcp.json` (sourced from `plugin/sidegraph/.mcp.public.json`) starts `uvx`
  directly, with no shell, and ends its arguments with `--repo-root` (`env` omitted below for
  brevity):
  ```json
  {
    "command": "uvx",
    "args": ["--from", "git+https://github.com/SantyagoSeaman/sidegraph.git@main", "sidegraph-mcp", "--repo-root"]
  }
  ```
  `--repo-root` anchors the server to the project: `sidegraph-mcp` moves to
  `CLAUDE_PROJECT_DIR` when it is set, else to the git top-level of its cwd, else stays in its
  cwd. Because it needs no `sh`, the same entry starts on native Windows.
- **Hooks**: hook command entries have no `cwd`/`env` fields, so the public command carries a
  `cd` prefix: `cd "${CLAUDE_PROJECT_DIR}" && SIDEGRAPH_HOST=claude SIDEGRAPH_DIR=... SIDEGRAPH_GRAPH=... uvx --from
  git+https://github.com/SantyagoSeaman/sidegraph.git@main sidegraph-session-start`. This is
  the content of `plugin/sidegraph/hooks/hooks.public.json`; the release renames it to
  `hooks.json`. Repeated hook calls use the [shared launch-commit file](../reference/hooks.md#the-launch-commit-file). The development variant uses the same prefix and runs the checkout the plugin
  was loaded from: `UV_PROJECT_ENVIRONMENT=.venv uv run --project "${CLAUDE_PLUGIN_ROOT}/../.."
  --package sidegraph --frozen --no-active`. Keep the [command guard](../reference/hooks.md#the-command-guard) when adapting a launcher.


Belt and braces: the hook entry points themselves also anchor a relative `SIDEGRAPH_DIR` to
`CLAUDE_PROJECT_DIR` when Claude Code exports it, so even a hook invoked without the `cd` prefix
resolves the store correctly; the graph then follows the store. The MCP server's portable core
does not do this env-anchoring itself (it stays host-agnostic): the host-seam entry point
`sidegraph-mcp` does it when the plugin passes `--repo-root`, as above.

### Launching in a subdirectory

`CLAUDE_PROJECT_DIR` is the directory Claude Code was launched in, not the repository root: a
launch in `repo/pkg` gives `repo/pkg`. The hooks and the MCP server therefore anchor the relative
`.sidegraph` to `repo/pkg`. Where it does not exist there, they look for it in the parent
directories, up to the repository root (the nearest directory with a `.git` entry), and use the
first one found, so a launch in `repo/pkg` uses `repo/.sidegraph` and does not create an empty
second store. A store that exists in the launch directory still wins, which keeps a per-package
store working. Touches are recorded relative to the directory the store was found in (`pkg/a.py`),
the same as every anchor.

This applies wherever a subdirectory launch reads the plugin. A launch in `repo/pkg` does not read
`repo/.claude/settings.json` (measured with Claude Code 2.1.287), so a plugin enabled only there is
not active for it: enable it at user scope, or in the subdirectory's own `.claude/settings.json`.

A session that an older Sidegraph started in a subdirectory may have left a `.sidegraph` there. An
empty one is named by `SessionStart` together with the repository's store; remove it to use the
repository's. One that holds any record (a single proposed decision from an old capture is enough)
is not reported and keeps hiding the repository's store: review it with
`sidegraph-ratify --db <subdirectory>/.sidegraph`, record what matters again in the repository's
store, then remove the subdirectory's directory. A `.git` entry at your home directory is not a
repository root for the lookup, and the CLI does not search at all: from a subdirectory, pass
`--db`.

### `SessionStart` — `sidegraph-session-start`

See [the detailed contract](../reference/host-wiring.md#sessionstart--sidegraph-session-start).

### `Stop` — `sidegraph-stop`

See [the detailed contract](../reference/host-wiring.md#stop--sidegraph-stop).

### `PreToolUse` — `sidegraph-pre-tool-use`

See [the detailed contract](../reference/host-wiring.md#pretooluse--sidegraph-pre-tool-use).

### `SubagentStart` — `sidegraph-subagent-start`

See [the detailed contract](../reference/host-wiring.md#subagentstart--sidegraph-subagent-start).

### Silent degradation

Hook failures keep the host session running. SessionStart reports a store that cannot be opened. Follow that notice or reproduce a failed launcher in a terminal to inspect the error.

See [the detailed contract](../reference/host-wiring.md#silent-degradation).
