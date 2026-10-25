# Grok Build (experimental)

Sidegraph's plugin installs into Grok Build through its plugin marketplace.
You get the MCP tools and the shared skills. Lifecycle hooks did not run in the sessions we
checked, so there is no automatic session map, no context injected when a file is read and no
capture reminder at the end of a session: ask for memory and capture decisions explicitly.

Install `uv` and Git, then, in a terminal:

```bash
grok plugin marketplace add SantyagoSeaman/sidegraph
grok plugin install sidegraph@sidegraph
```

Grok asks you to trust the plugin; `--trust` skips the prompt. Start a new Grok session in the
repository. The MCP server works on the repository you started Grok in: records are written to that
repository's `.sidegraph/`.

Check the connection by asking the agent which Sidegraph tools it has: `get_task_context`,
`propose_decisions` and `ratify` should be among them. Then follow the setup skill to prepare
the graph and store (or the [quickstart](../getting-started/quickstart.md)), and use
`get_task_context(files=[...])` before working on a file.

What was checked, with Grok Build 1.0.62 on macOS: installing the plugin from the marketplace,
the MCP tools and skills appearing in a session, recording a gotcha in one session and
retrieving it in a new one after `graphify update .` and `sidegraph-sync`. Windows was not
checked, and Grok Build is not part of the release gate.

To update:

```bash
grok plugin marketplace update
grok plugin update sidegraph
```

Start a new session afterwards.
