# Install Sidegraph in OpenCode V2

Requires OpenCode V2, `uv` and Git on PATH. The current native integration baseline is
OpenCode 2.0.29. Sidegraph provides MCP tools and shared skills; lifecycle hooks are not
included. Ask for context and capture decisions explicitly through the tools.

Install the Git package with OpenCode's native command:

```bash
opencode plugin add 'git+https://github.com/SantyagoSeaman/sidegraph.git#main'
```

Start a new OpenCode session in your project. Follow `@sidegraph-setup` to prepare its
graph and store, then call `list_domains()` to check MCP access. Use
`get_task_context(files=[...])` before working on files and `propose_decisions` to capture
durable findings. Other shared skills use `@sidegraph-<skill>` (for example,
`@sidegraph-check-plan`). There is no automatic SessionStart map or Stop nudge.

The equivalent `opencode.json` entry is:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "plugins": ["git+https://github.com/SantyagoSeaman/sidegraph.git#main"]
}
```

Merge that entry with any existing plugin list. The plugin supplies one `sidegraph` MCP
server only when that name has no explicit configuration. An existing entry, including
`disabled: true`, stays unchanged. OpenCode V2 stores overrides under `mcp.servers`:

```json
{
  "mcp": {
    "servers": {
      "sidegraph": {
        "type": "local",
        "command": [
          "uvx",
          "--from",
          "git+https://github.com/SantyagoSeaman/sidegraph.git@main",
          "sidegraph-mcp",
          "--repo-root"
        ],
        "environment": {
          "SIDEGRAPH_DIR": ".sidegraph",
          "SIDEGRAPH_GRAPH": "graphify-out/graph.json"
        }
      }
    }
  }
}
```

Use this override only when you want to manage the server yourself. Keep OpenCode's normal
MCP and skill permissions; no additional Sidegraph plugin-approval command is required.

Check and apply package updates, then restart OpenCode:

```bash
opencode plugin check 'git+https://github.com/SantyagoSeaman/sidegraph.git#main'
opencode plugin update 'git+https://github.com/SantyagoSeaman/sidegraph.git#main'
```

The Git package target `#main` follows public releases. The MCP runtime uses its configured
Git ref (normally `@main`); this host has no SessionStart version pin. Updating cached
plugin files and resolving the MCP runtime are separate operations.
