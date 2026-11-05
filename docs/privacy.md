# Privacy

Last updated: 2026-11-04. This page covers the Sidegraph plugin, its MCP server, its hooks and
its command-line tools. The [security policy](../SECURITY.md) has the technical detail behind it.

## What Sidegraph collects

Nothing is collected by the project or its maintainers. Sidegraph has no server, no account and
no analytics, and it sends no telemetry anywhere.

## What stays on your machine

- **Records.** Decisions, facts and domains are plain JSON in `.sidegraph/` inside your
  repository: one file per record, packed shard files, or archive segments. Sidegraph never
  commits: you decide whether to commit and push them, like any other file in the repository.
  Each record carries its provenance where capture can read it: the agent session id, the
  commit, and an initiative named after the branch. A record you ratify also carries your git
  user name. Review them before you push to a shared or public repository.
- **Derived index.** `.sidegraph/index.db` is rebuilt from the records and is ignored by git. It
  also keeps a local usage log for `sidegraph-stats`: which records were shown and which files
  were touched, with session ids and timestamps. It is never uploaded, and its event entries older than 30
  days are deleted at session start. `SIDEGRAPH_TELEMETRY=off`, set in the environment Claude
  Code starts with (or under `env` in `.claude/settings.json`), stops the log; working state
  (session ids, timestamps, which files' records were already shown, when the last capture
  reminder fired) is still kept, without that 30-day cleanup, so records and reminders are not
  repeated.
- **Code graph.** Graphify writes `graphify-out/` in your repository; it can be deleted and
  rebuilt.
- **Session state.** One small file, `sidegraph/launch-commit` under your user cache directory
  (`$XDG_CACHE_HOME`, or `~/.cache`): the commit the last session started from. It is written
  only when Sidegraph runs from its public repository's `main` branch, as plugin installs older
  than 0.25.0 do. Current plugins launch a published release and do not write it.
- **Graph refresh log.** If you install the graph-refresh git hook, it logs Graphify's output to
  `sidegraph-graph-refresh.log` in the repository's `.git` directory.
- **Transcripts.** At the end of a turn, the Stop hook reads the session's local transcript to
  count prompts and tool calls. It stores and sends none of it.

Before a record is stored, known secret patterns in its text are redacted. This is a best
effort, not a guarantee: do not ask the agent to record secrets.

## Network activity

The Sidegraph runtime opens no network connections of its own. Network access happens only
around it:

- **Starting Sidegraph in any plugin.** Every plugin starts Sidegraph with
  `uvx --from sidegraph==X.Y.Z --python 3.13`, naming its own version. The published package pins
  every dependency to an exact version. The first session after an install or an update has uv
  download that package and those dependencies from PyPI into uv's cache, and, on a machine
  without Python 3.13, a uv-managed CPython 3.13 from python-build-standalone on GitHub, which the
  pins do not cover. Nothing is fetched on later starts, and the skills' CLI commands run the same
  way. A CLI command run as `uvx --from sidegraph --python 3.13 <command>` checks PyPI for the
  latest release on each run.
- **Graphify.** The setup step installs Graphify from PyPI. `sidegraph-domains snapshot
  --build-with` downloads the Graphify release you name, through uv, when you run it.
- **Your agent.** What Sidegraph returns to your agent becomes part of the conversation, which
  your agent sends to its model provider under that provider's terms.
- **Optional semantic extraction.** If you run Graphify's semantic extraction on documents, the
  Graphify command sends them to the LLM provider you configure for it. It runs only when you
  or your agent start it explicitly, as the
  [semantic documentation guide](guides/semantic-docs.md) describes.

## Removing your data

Uninstalling the plugin leaves your records in place. If you ran setup, first remove the
graph-refresh git hook and its git setting, because the hook rebuilds `graphify-out/` after
commits:

```text
uvx --from sidegraph --python 3.13 sidegraph-init --remove-hooks
```

Then delete `.sidegraph/` and `graphify-out/` from the repository; records already committed stay
in your git history. Also left behind: Graphify, installed as a uv tool (`uv tool uninstall
graphifyy`); the `graphify-out/` line setup added to `.gitignore`; `SIDEGRAPH_RATIFY_POLICY` in
`.claude/settings.json` if setup wrote it; the refresh log and the `sidegraph-graph-refresh.repair`
file in `.git`; and the session-state file above. See also [Removing Sidegraph](getting-started/installation.md#removing-sidegraph).

## Contact

Questions go to [GitHub Issues](https://github.com/SantyagoSeaman/sidegraph/issues). Report
security problems privately as described in the [security policy](../SECURITY.md).

## Changes

Changes to this page are listed in the [changelog](../CHANGELOG.md) of the release that makes
them.
