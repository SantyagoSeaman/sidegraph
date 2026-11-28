# Kimi Code

[Glossary](../concepts/glossary.md)

Sidegraph gives Kimi Code its MCP tools and its shared skills. This is a basic integration:
there are no hooks, so Kimi gets no session map, no records on reads, no subagent brief and no
capture reminder. Call the tools yourself (`retrieve_decisions`, `get_task_context`) and
capture decisions explicitly with `propose_decisions`.

Install `uv` and Git. In Kimi Code, open the plugin manager with `/plugins`, choose **Custom**
and enter the repository URL:

```text
https://github.com/SantyagoSeaman/sidegraph
```

Kimi installs the latest GitHub release of a bare repository URL, so the plugin matches the
newest published Sidegraph version. Start a new session with `/new` after installing or
updating; Kimi applies plugin changes to new sessions only.

Start Kimi from the intended project directory. The MCP server starts there, then moves to the
top level of the enclosing Git repository (the plugin passes `--repo-root`), so a Kimi started
in a package subdirectory of a monorepo uses the repository's store.

Follow the setup skill to prepare the graph and store. Call `list_domains()` to verify MCP
access; an empty `domains` array is normal for a fresh store.

If the tools are missing, run `/plugins info sidegraph` and read the diagnostics.
