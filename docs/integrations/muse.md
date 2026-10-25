# Muse (experimental)

Sidegraph exposes its shared skills and MCP tools through Muse's native plugin format.
Lifecycle hooks are not included: ask for memory and capture decisions explicitly through
the tools. Automatic session maps, read context and Stop nudges are unavailable.

Install `uv` and Git. Run installation and updates from the same stable directory outside
any project (for example, your home directory), keeping a checkout of the public repository:

```bash
git clone -c core.autocrlf=false https://github.com/SantyagoSeaman/sidegraph.git sidegraph-plugin
muse plugins install ./sidegraph-plugin/plugin/sidegraph
muse plugins inspect sidegraph
```

Review the MCP capability, then approve it:

```bash
muse plugins approve sidegraph
```

Start a new Muse process **from the intended
project directory**. The MCP server starts in that process's working directory, then moves to the
top level of the enclosing Git repository (the plugin passes `--repo-root`; a valid
`CLAUDE_PROJECT_DIR` wins when it is set), so a Muse started in a package subdirectory of a
monorepo uses the repository's store, not the package's. Outside a Git repository it stays where
it started. If you change
workspace, exit and start Muse again from the new directory; selecting a different workspace
with `--workspace` or inside an existing host does not reliably move the MCP server.

Follow the setup skill to prepare the graph and store. Call `list_domains()` to verify MCP
access; an empty list is normal for a fresh store. Call `get_task_context(files=[...])` before
working on files and use `propose_decisions` to capture durable findings explicitly.

To update, pull the maintained checkout, refresh the installed plugin and restart Muse:

```bash
git -C sidegraph-plugin pull --ff-only
muse plugins update sidegraph
muse plugins inspect sidegraph
```

Review any changed capability before approving it, then restart Muse from the project:

```bash
muse plugins approve sidegraph
```

The MCP runtime uses the release's configured Git ref (normally `main`). This integration has
no SessionStart version pin. Expanded hooks, workspace switching and cross-platform native
acceptance are deferred; this experimental support does not claim parity with Claude Code
or Codex.
