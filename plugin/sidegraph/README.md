# Sidegraph

Sidegraph keeps the project's decision graph for AI coding agents: decisions, rejected
alternatives and gotchas, anchored to your code. Sessions, subagents and different tools share
it, and each learns at that code what was decided and why.

## What you use it for

- **Keep the reason for a decision.** Tell the agent what was decided and why; Sidegraph
  stores it with the alternatives you rejected and anchors it to the code it concerns.
- **Avoid retrying what already failed.** Before a non-trivial change, the session is told to
  check the plan against the alternatives that were tried and abandoned, and why.
- **Get the constraints before a change.** When the agent opens a file, it gets the most
  relevant decisions, limits and gotchas recorded for that code, and can ask for the rest.

## Requirements

- Git and [uv](https://docs.astral.sh/uv/). uv provides `uvx` and fetches the Python that
  Sidegraph needs.
- [Graphify](https://github.com/SantyagoSeaman/sidegraph/blob/main/docs/getting-started/installation.md#install-graphify),
  the code-graph engine that anchors records to files and symbols. The setup step installs it.

## Install and first run

In Claude Code, in your repository:

```text
/plugin marketplace add SantyagoSeaman/sidegraph
/plugin install sidegraph@sidegraph
```

Then start a new session and run `/sidegraph:setup`. It builds the graph, creates the
`.sidegraph/` store, adds `graphify-out/` to `.gitignore`, and asks you before it adds a
git hook or accepts any records automatically. The [Quickstart](https://github.com/SantyagoSeaman/sidegraph#quickstart) walks
through a first record and a check that it comes back.

## Where it works

Supported: Claude Code, and a local session in Claude Desktop's Code tab (verified on macOS). Claude Desktop's
regular chat and Cowork are not supported by Sidegraph. Codex, Grok Build, Muse and OpenCode V2
have their own install routes: see the
[host support table](https://github.com/SantyagoSeaman/sidegraph#host-support) and the
[desktop guide](https://github.com/SantyagoSeaman/sidegraph/blob/main/docs/integrations/desktop-apps.md).

## What it runs

The MCP server and the hooks run locally. uv starts them from the plugin's configuration:

- **An MCP server** (local stdio) with the tools the agent calls to read and write records.
- **SessionStart hook:** adds a short map of the project's domains and records to the session.
  When the code graph has changed, it also re-anchors records to the moved code, which can
  update files in `.sidegraph/`.
- **PreToolUse hook** on `Read`, `Grep`, `Edit`, `Write` and on `cat`, `grep`, `rg` and `sed`
  shell calls: adds the records anchored to the files involved as context. On an `Agent` or
  `Task` call it appends those records to the subagent's prompt, so the subagent sees them too.
  It never approves or blocks a tool call.
- **SubagentStart hook:** gives a starting subagent a short brief about the project's records.
- **Stop hook:** reads the session's local transcript to count prompts and tool calls. When
  the session did substantial work, it keeps the session going for one more turn with a
  reminder to propose what it learned as records for your review. It repeats only after much
  more work in the same session. Nothing from the transcript is stored or sent.
- **A git hook, only if you agree during setup:** git runs it after commits, merges and branch
  checkouts, and it rebuilds the graph with Graphify in the background.

## Your data

- **Where records live.** Records are plain JSON in `.sidegraph/` inside your repository (one
  file per record, packed shard files, or archive segments), meant to be committed with your
  code. Sidegraph never commits for you. Each record carries its provenance where capture can read it:
  the agent session id, the commit, and an initiative named after the branch. A record you
  ratify also carries your git user name. Review `.sidegraph/`
  before you push it to a shared or public repository. A derived index (`.sidegraph/index.db`)
  and the graph (`graphify-out/`) stay local and can be rebuilt.
- **What leaves your machine.** The Sidegraph runtime opens no network connections and sends no
  telemetry. The plugin starts the MCP server and the SessionStart hook with uv from Sidegraph's
  public GitHub repository, so uv contacts GitHub at each session start to check for a newer
  version, and downloads it and its Python dependencies (from PyPI) when needed. The other hooks
  use the version that session start recorded, from uv's cache; without that record they check
  GitHub too. The CLI commands the skills run always check GitHub's `main`. Setup installs
  Graphify from PyPI, and `sidegraph-domains snapshot --build-with` downloads the Graphify release
  you name when you run it. Your agent sends what Sidegraph returns to its model provider, like any other context.
- **Optional semantic extraction.** Graphify can extract design rationale from documents with
  an LLM (`graphify extract`). It sends those documents to the provider you configure for
  Graphify, and runs only when you or your agent start it explicitly.
- **What gets recorded.** Agent proposals wait for your review unless you turn on automatic
  acceptance. A record you ask the agent to write is accepted at once, and so are imported
  documents unless their source marks them as drafts or rejected, or you import with
  `--propose`. Review
  `.sidegraph/` changes like code. Known secret patterns are redacted before storage,
  as a best effort.
- **Local usage data.** The local index keeps a log of which records were shown and which files
  were touched, with session ids and timestamps, for `sidegraph-stats`. It is never uploaded.
  `SIDEGRAPH_TELEMETRY=off`, set in the environment Claude Code starts with, stops the log;
  working state (session ids, timestamps, which files' records were already shown, when the
  last capture reminder fired) is still kept so records and reminders are not repeated.

Details: [Privacy](https://github.com/SantyagoSeaman/sidegraph/blob/main/docs/privacy.md) and
[SECURITY.md](https://github.com/SantyagoSeaman/sidegraph/blob/main/SECURITY.md).

## Removing it

If you ran setup, first remove the graph-refresh git hook and its setting (it also clears the
setting when you declined the hook):

```text
uvx --from git+https://github.com/SantyagoSeaman/sidegraph.git@main sidegraph-init --remove-hooks
```

Then run `/plugin uninstall sidegraph@sidegraph`. The records in `.sidegraph/` belong to your
project: delete them only if you mean to lose them. `graphify-out/` can be deleted and rebuilt.
Also left behind: Graphify, installed as a uv tool (`uv tool uninstall graphifyy`); the
`graphify-out/` line setup added to `.gitignore`; `SIDEGRAPH_RATIFY_POLICY` in
`.claude/settings.json` if setup wrote it; the hook's `sidegraph-graph-refresh.log` and
`sidegraph-graph-refresh.repair` files in the `.git` directory; and one small file,
`sidegraph/launch-commit`, in your user cache directory. See
[Removing Sidegraph](https://github.com/SantyagoSeaman/sidegraph/blob/main/docs/getting-started/installation.md#removing-sidegraph).

## Links

[Documentation](https://github.com/SantyagoSeaman/sidegraph/blob/main/docs/README.md) ·
[Support](https://github.com/SantyagoSeaman/sidegraph/issues) ·
[Security](https://github.com/SantyagoSeaman/sidegraph/blob/main/SECURITY.md) ·
[Privacy](https://github.com/SantyagoSeaman/sidegraph/blob/main/docs/privacy.md) ·
[License: Apache-2.0](https://github.com/SantyagoSeaman/sidegraph/blob/main/LICENSE)
