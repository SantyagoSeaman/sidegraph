# OpenCode V2

Follow the [OpenCode installation guide](../../.opencode/INSTALL.md) for the native Git
package install, configuration and update commands. Prerequisites are OpenCode V2, `uv`
and Git; the current native baseline is 2.0.29.

Sidegraph registers the shared skills and one MCP server. Explicit server configuration
and existing skill IDs take precedence. Native MCP/skill permissions remain in effect.

Use `@sidegraph-setup` for onboarding and call `get_task_context(files=[...])` for context.
Lifecycle hooks, automatic session maps, read injection and Stop capture nudges are not
part of this integration. No V1 compatibility or session-attribution guarantee is claimed.
