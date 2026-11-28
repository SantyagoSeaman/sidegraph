# Kimi Code

[Glossary](../concepts/glossary.md)

On Kimi Code, Sidegraph is set up in two parts:

1. **The plugin** gives Kimi the shared skills.
2. **The MCP server** gives Kimi the decision tools. You add it by hand to your user `mcp.json`.

The plugin does not bundle the MCP server. Kimi Code starts a plugin's MCP server in the
plugin's own folder and does not tell it which project the session is in, so a bundled server
cannot find your repository's store. A server from your user `mcp.json` starts in the session's
directory.

This is a basic integration. There are no hooks, so Kimi gets no session map, no records on
reads, no subagent brief and no capture reminder. Call the tools yourself
(`retrieve_decisions`, `get_task_context`) and capture decisions explicitly with
`propose_decisions`.

Install `uv` (0.6.10 or newer) and Git first.

## 1. Add the MCP server

Open `~/.kimi-code/mcp.json` (if you set `KIMI_CODE_HOME`, the file is
`$KIMI_CODE_HOME/mcp.json`). If the file does not exist, create it with this content. If it
already has an `mcpServers` object, add only the `sidegraph` entry to it.

```json
{
  "mcpServers": {
    "sidegraph": {
      "command": "uvx",
      "args": ["--from", "sidegraph==0.36.1", "--python", "3.13", "sidegraph-mcp", "--repo-root"]
    }
  }
}
```

Use the user file, not a `mcp.json` inside a repository: one entry serves every project,
because each session's server starts in that session's directory. Do not add `cwd`, and do not
set `SIDEGRAPH_DIR` to an absolute path. Either one ties every project to one store.

## 2. Install the plugin

In Kimi Code, open the plugin manager with `/plugins`, choose **Custom** and enter the
repository URL:

```text
https://github.com/SantyagoSeaman/sidegraph
```

Kimi installs the latest GitHub release of a bare repository URL. Start a new session with
`/new` after installing or updating either part.

## 3. Check it

Start Kimi in your project directory. `--repo-root` moves the server from there to the top
level of the enclosing Git repository, so a Kimi started in a package subdirectory of a monorepo
uses the repository's store.

Follow the setup skill to prepare the graph and store. Then ask Kimi to call `list_domains()`.
An answer with an empty `domains` array is normal for a fresh store. A connected server in
Kimi's status is not enough: only a tool call shows which store the server reads.

If a call reports `no store at …`, read the path in the message:

- A path under `~/.kimi-code/plugins/` means the server comes from the 0.36.0 plugin, which
  bundled it. Update the plugin from `/plugins`, or disable the plugin's MCP server there, and
  use the `mcp.json` entry above.
- A path in your project means the store is not set up yet. Run the setup skill.

## Updating

The two parts update separately:

- Update the plugin (the skills) from `/plugins`.
- Update the MCP server by changing the version number in the `--from` argument of your
  `mcp.json` entry. A plugin update does not change it. Keep it equal to the plugin's version, which `/plugins` shows.
