# Desktop apps

> **Start here:** Open a local coding project, use the installation path below for your surface, then ask the agent to run Sidegraph setup. [Glossary](../concepts/glossary.md).

Sidegraph keeps the project's decision graph for AI coding agents. The supported desktop
surfaces are a local session in Claude Desktop's Code tab and the Codex view of the ChatGPT
desktop app. Both were verified live on macOS with plugin 0.23.0: the Code tab on 2026-10-31 and
the Codex view on 2026-11-02. The [host feature matrix](support.md) lists
which delivery paths each surface uses.

Claude Desktop's regular chat and Cowork, and ChatGPT's Chat and Work views, are not
supported by Sidegraph. Use one of the coding surfaces below.

## Claude Desktop: local Code tab

Install [Git and uv](../getting-started/installation.md#prerequisites), then open your
repository in the Code tab using the local **This computer** environment. The live run
used a marketplace already added by Claude Code on that computer. Adding the marketplace
from the app on a fresh machine was not exercised.

1. If Claude Code already added the Sidegraph marketplace on this computer, use that entry.
   Otherwise open Settings › Plugins › Add › Add marketplace and enter `SantyagoSeaman/sidegraph`.
2. Open Discover and filter by Source › sidegraph.
3. Choose Install for project (shared). This enables the plugin in `.claude/settings.json`.
   Commit that file for the team. For nested-directory sessions, follow
   [Claude Code's scope guidance](../getting-started/claude-code-setup.md#option-a--plugin-recommended).
4. Start a new session in the repository, review any workspace trust prompt and ask:
   “Run the Sidegraph setup skill for this repository.”
5. After setup, start a fresh session and check the opening map. Ask the agent to call
   `get_task_context` for an existing repository-relative file.

The live macOS run verified project installation, setup, the opening map, MCP retrieval,
records before an anchored file read, a subagent brief and the interactive Stop capture
reminder. The app launched from the GUI and could find `uv` and `uvx` on that machine.

## Codex view of the ChatGPT desktop app

Use the verified CLI-to-app installation path. Install
[Git and uv](../getting-started/installation.md#prerequisites) and the Codex CLI, then run
these in a terminal:

```text
codex plugin marketplace add SantyagoSeaman/sidegraph
codex plugin add sidegraph@sidegraph
```

1. Open an interactive Codex CLI session in the repository. Use `/hooks` to review and trust
   the current Sidegraph hook definitions. Installing the plugin does not grant hook trust.
2. Open the app's Codex view. Choose project › New project › Add folder, select the repository
   and review its folder trust prompt.
3. In Plugins › Installed, confirm Sidegraph is listed with its MCP server, skills and hooks.
4. Start a session and ask: “Run the Sidegraph setup skill for this repository.” After setup,
   start another session and check the opening map.
5. Ask the agent to call `get_task_context` for an existing repository-relative file.

Use the same local Codex configuration for the CLI and app. The live macOS run used a plugin
already installed and hooks already trusted through the CLI. It verified visibility in the
app, setup, the opening map, MCP capture and retrieval, the subagent brief and the interactive
Stop reminder followed by a proposal. It did not exercise a first app-only install or trust
review, or ratification inside the app.

Codex has no Sidegraph read hook. Call `get_task_context` for the files you plan to inspect
or change. The opening instruction asks for context before grep or file search and for
check-plan before a non-trivial change. The live run verified explicit retrieval. An interactive Stop reminder asks the agent to propose durable learning;
it does not ratify by itself. Whether the separate proposal call lands accepted depends
on the project's [ratification policy](../reference/configuration.md#environment-variables).
The reminder also names updates to existing decisions. Follow
[status-sensitive recovery](../guides/surviving-refactors.md#reading-and-healing-stale-decisions-warnings):
a `supersede_decision` call the agent makes on its own writes
a `proposed` replacement that waits for ratification; the predecessor stays current until
then. Under the `auto-all` policy it is accepted at write time when it has a live anchor.

## Verify the current project

Use [Verify your setup](../guides/verifying-your-setup.md) for capture, ratification and
sync checks. A tool reply with no matching record is a valid first-run result. Once you have
an accepted record anchored to a file, retrieve it explicitly on either surface. Claude's
Code tab can also deliver it automatically on a supported read.

The desktop runs above cover macOS local projects. Native Windows desktop-app sessions
have not been verified by these runs. The [OS coverage matrix](../getting-started/installation.md#platform-coverage)
separates CLI and hook checks from GUI validation.

If the plugin cannot launch `uv` (Claude) or `uvx` (Codex), inspect the environment the app gives its subprocesses
and restart the app after correcting PATH. A successful terminal command alone does not
establish the GUI environment. See [launcher diagnostics](../reference/hooks.md#if-you-see-this-message)
for the launch command and failure details.

## Vendor references

The live runs establish the Sidegraph capabilities above. For current host configuration,
see [Claude Desktop Code sessions](https://code.claude.com/docs/en/desktop),
[Codex MCP configuration](https://learn.chatgpt.com/docs/extend/mcp) and
[Codex hook trust](https://learn.chatgpt.com/docs/hooks).
