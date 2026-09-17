"""Host seam — Claude Code integration, isolated from the portable core.

v1 ships Claude-Code-only. The core (store + retrieval + MCP) never imports from here; this
package holds the host-specific hooks (SessionStart / Stop / PreToolUse) so a second host
(Cursor, Cline, plain MCP) is *possible* later without touching the core. Multi-host support
is explicitly deferred past v1.
"""
