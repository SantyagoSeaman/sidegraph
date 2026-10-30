# Muse (experimental)

Sidegraph exposes its shared skills, MCP tools and two lifecycle hooks through Muse's native
plugin format:

- **SessionStart** injects the session map (domains, notices) when a Muse session starts.
- **SubagentStart** gives a starting subagent a one-line brief that the project keeps a decision
  memory, once the store has records anchored to files.

There is no context on reads and no capture reminder at the end of a session: call the tools
for that, and capture decisions explicitly. The hooks run `python3` (3.9 or later; the stock
macOS one works), which starts the runtime through `uvx`. If a hook cannot run, it prints
nothing and the session continues; MCP and skills are unaffected.

Install `uv` and Git. Add the public repository as a Muse marketplace and install the plugin
from it:

```bash
muse plugins marketplace add sidegraph https://github.com/SantyagoSeaman/sidegraph
muse plugins install sidegraph@sidegraph
muse plugins inspect sidegraph
```

Review the two hook capabilities (`session-start`, `subagent-start`) and the MCP capability,
then approve them:

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

To update, refresh the marketplace snapshot, then the installed plugin, and restart Muse:

```bash
muse plugins marketplace update sidegraph
muse plugins update sidegraph
muse plugins inspect sidegraph
```

Review any changed capability before approving it, then restart Muse from the project:

```bash
muse plugins approve sidegraph
```

The MCP runtime and the SessionStart hook use the release's configured Git ref (normally
`main`). SessionStart records the commit it ran, and the subagent hook starts from that commit,
so both hooks of a session run the same version. Context on reads, the capture reminder,
workspace switching and Windows are not supported yet; this experimental support does not
claim parity with Claude Code or Codex.
