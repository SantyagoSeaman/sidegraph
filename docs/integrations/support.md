# Host feature support

[Glossary](../concepts/glossary.md)

Use the setup page for your host, then [verify the installation](../guides/verifying-your-setup.md). MCP is the tool connection. Skills are reusable workflows. Read-time records arrive automatically when a supported file operation runs.

| Surface | MCP | Skills | Session map | Read-time records | Subagent brief | Stop capture reminder | Windows wiring |
|---|---|---|---|---|---|---|---|
| [Claude Code](../getting-started/claude-code-setup.md) | Yes | Yes | Hook | Yes | Hook and task brief | Yes | POSIX commands (Git Bash) |
| [Claude Desktop: local Code tab](desktop-apps.md#claude-desktop-local-code-tab) | Yes | Yes | Hook | Yes | Hook | Yes | POSIX commands (Git Bash); no Windows desktop run |
| [Codex CLI](../getting-started/codex-setup.md) | Yes | Yes | Hook | No | Hook | Interactive sessions | `commandWindows` |
| [Codex view of the ChatGPT desktop app](desktop-apps.md#codex-view-of-the-chatgpt-desktop-app) | Yes | Yes | Hook | No | Hook | Interactive sessions | `commandWindows`; no Windows desktop run |
| [Grok Build](grok.md) | Yes | Yes | MCP instructions | Yes | Task brief | Interactive sessions | POSIX commands |
| [Muse](muse.md) | Yes | Yes | Hook | No | Hook | No | Not supported |
| [OpenCode V2](opencode.md) | Yes | Yes | No | No | No | No | See installation guide |

Desktop cells come from macOS live runs with plugin 0.23.0: Code tab on 2026-10-31 and Codex view on 2026-11-02. The Codex run used a CLI-installed plugin and CLI-trusted hooks. See the [desktop guide](desktop-apps.md) for install and verification boundaries. Regular Claude chat, Cowork and ChatGPT Chat/Work are unsupported by Sidegraph.

Windows wiring describes the shipped launcher, not a live test of every host on Windows. The [OS coverage matrix](../getting-started/installation.md#platform-coverage) distinguishes CI checks from host sessions. The installation guide gives the prerequisites. The host pages describe the boundaries of each delivery path. A Stop reminder asks the agent to propose learning. It does not automatically accept records.
