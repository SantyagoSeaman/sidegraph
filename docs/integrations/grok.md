# Grok Build

Sidegraph's plugin installs into Grok Build through its plugin marketplace.
You get the MCP tools and the shared skills, and records are delivered when the agent reads,
searches, edits or writes a file or starts a subagent (see [Hooks on Grok](#hooks-on-grok)).
The project's session map reaches a Grok session through a workaround, the MCP server's
instructions (see [Session map on Grok](#session-map-on-grok-a-workaround)). The capture
nudge at the end of a turn works as on Claude Code in interactive sessions (see
[Capture on Grok](#capture-on-grok)).

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

What was checked on macOS:

- With Grok Build 1.0.62: installing the plugin from the marketplace, the MCP tools and skills
  appearing in a session, and a gotcha recorded in one session and retrieved in a new one after
  `graphify update .` and `sidegraph-sync`.
- With Grok Build 1.0.70: the hooks below and the session map, in headless (`grok -p`) and
  interactive sessions; the capture request in an interactive session, and its absence in a
  headless one.

Windows was not checked, and Grok Build is not part of the release gate.

The first session after installing, and the first after each Sidegraph release, takes a few
seconds longer: the `SessionStart` hook downloads and builds Sidegraph (5–7 seconds measured on
its own and about 10 inside Grok, where Grok's own limit for a hook is 5 seconds; the plugin
gives it 30). Later hooks take well under a second.

If the Hooks tab (`/hooks`) shows no Sidegraph hooks, open the Plugins tab (`/plugins`). A
plugin marked `[disabled]` loads nothing; turn it on with `grok plugin enable sidegraph`, or
`Space` on it in that tab, and start a new session.

Under `--sandbox read-only` the hooks still run, but they cannot write the repository's
`.sidegraph/` or their cache in `~/.cache`, so the agent gets no records; the `SessionStart`
hook may show a message in Grok's interface saying it could not start or open the store. Use a
sandbox that lets them write those two places, or run without one.

## Hooks on Grok

Grok Build does not support the `if` field of a hook handler, so it skips a handler that sets one.
The plugin gives Grok its own hooks file without `if`, so a shell read (`sed`, `grep`, `rg` or
`cat`) reaches the hook.

Grok names its tools differently from Claude Code, and the hook maps
Grok's `read_file`, `grep`, `search_replace`, `write` and `spawn_subagent` to the tools it knows, and
resolves a relative path against the directory the session started in. So a read, a search, an
edit or a write of a file that has records, and a subagent started with a brief that names such
a file, get those records. Grok's `search_replace` is recorded as an edit and its `write` as a write.
Other Grok tools, such as `hashline_edit`, are not covered.

The MCP tools and the skills do not depend on hooks and are unaffected.

## Capture on Grok

As on Claude Code, once a session has had two prompts from you, the Stop hook asks the agent
once to record what the session learned. It asks again only after at least 30 minutes and 10
new commits. Grok runs the Stop hook more often than Claude Code, and the hook ignores the
extra runs: the one when a `grok` process exits, and the turns Grok starts itself, such as the
one after a subagent or a background task finishes.

A headless `grok -p` run is never asked, including a `--resume` of an earlier session: Grok
prints the agent's reply to the request after the run's answer, so a script would read it as
part of the output. Record decisions in such a
run explicitly, or ask for them in the prompt. Checked with Grok Build 1.0.70.

## Session map on Grok (a workaround)

On Claude Code the `SessionStart` hook shows the agent the project's session map before it
starts: the domains, the recorded mistakes, the version. Grok Build (checked on 1.0.70) runs the
same hook but does not pass its output to the model. A hook's `additionalContext` is dropped and
its `systemMessage` appears only in Grok's own interface. The `SubagentStart` brief is dropped
the same way.

As a workaround, the Sidegraph MCP server sends the map itself. Grok starts the server once per
session and passes the server's `instructions` to the model, so when the MCP client is Grok's
(`grok-shell-…`), the server answers its first request with the session map as `instructions`.
No other host gets these instructions: Claude Code, Codex, Muse and OpenCode see exactly what
they saw before.

This is a stopgap for a gap in Grok, not how Sidegraph is meant to deliver the map, and it is
narrower than the hook:

- It carries the standing instruction, the version line and the domain table of contents from
  the store's cache. It has none of the status lines SessionStart adds: no integrity notices,
  no pending-ratification line, no drift line, no stale-graph warning.
- On a store with no named domains yet, it carries only the standing instruction and the version
  line, not the community listing SessionStart shows there: building that listing needs the code
  graph, which is too slow to load before Grok's session starts.
- No store yet means no map: the server does not create `.sidegraph/` to send one. The
  `SessionStart` hook still runs and tells Grok's interface to run `sidegraph-init`.
- Grok keeps only the first 2,033 characters of a server's instructions (checked on 1.0.70). A
  longer map is shortened to fit: the domain summaries are clipped, then dropped, and as a last
  resort the end of the table is cut with a note that `list_domains` and `list_proposed` have
  the rest.
- Grok waits about two seconds for MCP servers before its first request to the model. A server
  that starts later still delivers the map, but after the agent's first tool call instead of
  before its first answer. This happens in the first session after installing, while Sidegraph
  is downloaded and built, and now and then later: the server answered in 0.6–1.2 seconds when
  started on its own, and inside Grok it missed the two seconds in two of three measured runs. The server also gives up on the map
  after two seconds rather than hold the session up.
- A subagent sees the same map as its parent: Grok's subagents share the parent's MCP server
  and its instructions (checked on 1.0.70). It is the map of the moment the session started, not
  a fresh one.
- It relies on an internal detail of the MCP library Sidegraph uses. A library upgrade that
  changes it is caught by Sidegraph's tests, not by users, but it is the most fragile part of the
  Grok integration.

The workaround goes away once Grok passes `SessionStart` output to the model.

To update:

```bash
grok plugin marketplace update
grok plugin update sidegraph
```

Start a new session afterwards.
