# Security Policy

## Reporting a vulnerability

Report vulnerabilities privately through GitHub's form on this repository:
https://github.com/SantyagoSeaman/sidegraph/security/advisories/new (Security tab →
**Report a vulnerability**; you must be signed in). Do not open a public issue for a security
problem. Include the Sidegraph version, the agent host, the steps to reproduce and the impact
you see.

A report whose title starts with `[conduct]` is a conduct report, handled under the
[Code of Conduct](CODE_OF_CONDUCT.md): it gets no severity, advisory, CVE or embargo, only a
private reply, aimed like the acknowledgement below at a few days.

What happens next for a vulnerability:

- **Acknowledgement:** a first response within a few days.
- **Assessment:** we confirm the problem, agree its severity with you, and keep you informed.
- **Fix and disclosure:** the fix ships in a release, and a GitHub Security Advisory (with a
  CVE when one is warranted) is published with it. Please keep the details private until
  then, or for 90 days from your report, whichever comes first.
- **Credit:** reporters are credited in the advisory unless they ask not to be.

This is a single-maintainer project; these are targets, not a contractual service level.

## Scope

In scope: the `sidegraph` package (CLIs, MCP server, hook entry points), the plugin
manifests and skills in this repository, the store format and its integrity checks, capture
redaction, and the published PyPI package and Git snapshot.

Out of scope: the Graphify engine, the agent hosts (Claude Code, Codex, Muse, Grok Build,
OpenCode) and their model providers, and vulnerabilities in third-party dependencies. Report
those upstream, and tell us if the way Sidegraph uses a dependency makes it exploitable.
Attacks that need an attacker who already controls your machine or user account are out of
scope too.

## Threat model

- **Records are untrusted input to your agent.** The store is committed to your repository,
  and its records reach the agent at session start, when it reads or edits a file, and in
  subagent briefs. Anyone who can merge into `.sidegraph/` can change what your agents read.
  Review `.sidegraph/` diffs like code. Sidegraph frames delivered records as "data, not
  instructions"; that is a label, not sanitisation. Ratification is a workflow, not an access
  control: any agent with the MCP tools can write an accepted record directly
  (`add_decision`, or `supersede_decision` with `source="human"`) or call `ratify` itself
  with a `verdict` it wrote, whatever the ratification policy. `verdict` and
  `source="human"` are the calling agent's statements: they make an unasked acceptance
  explicit (a relayed verdict is stamped `agent-relayed:`, and `source="human"` is recorded
  in `provenance.source`); they authenticate nobody. An agent that makes a
  false statement has not bypassed a documented gate. The control is
  reviewing `.sidegraph/` diffs before they merge. A way to make an agent obey instructions
  planted in a record, or to bypass a documented gate (the store's append-only write rules, the
  write limits, `sidegraph-verify`'s integrity checks), is in scope.
- **Secrets in captured text.** Capture redacts common credential patterns before anything
  is stored, but redaction is best-effort. Review drafts before ratifying them and include
  the committed store in your secret scanning.
- **Local execution.** The runtime runs with your user's permissions, makes no outbound
  network calls of its own, and is started by your agent host and, if you install them, by
  git hooks.
- **Supply chain.** Every plugin launches the published package at its own version,
  `uvx --from sidegraph==X.Y.Z --python 3.13`. The release publishes to PyPI from a tagged public
  commit through Trusted Publishing, and the package pins every dependency to an exact version,
  the set the release's tests ran against; the release refuses a closure in which a compiled
  dependency lacks a prebuilt wheel for a supported platform. The pins freeze the dependency set: a fix in a dependency reaches
  you with the next Sidegraph release, so the release refreshes the lock and audits it for known
  advisories before it is cut. The plugin directory also carries a `uv.lock` of the same set, for
  tools that read a lock beside the manifest; Claude Code does not apply it. Teams that
  need a fixed version in their own commands name a release; see
  [pinned releases](docs/getting-started/installation.md#mutable-development-references).
- **Content a clone brings.** A repository you clone can carry files placed to steer the
  tools you run in it. Sidegraph treats three kinds as hostile input.
  *Git configuration inside the checkout.* Every git command Sidegraph runs goes through one
  function. It does not start git in a directory that git would take as a repository without
  a `.git` entry (the files of a git directory committed under `.sidegraph/`, `graphify-out/`
  or any other folder), including when the checkout itself sits below a folder named `.git`.
  It also runs git with `safe.bareRepository=explicit` and an empty `core.fsmonitor`, so a
  planted configuration is not read and no fsmonitor command is started. In a refused
  directory Sidegraph behaves as if git were not installed. The refusal works with any Git;
  the settings need Git 2.31, and `safe.bareRepository` Git 2.38. The checks were run with
  Git 2.54 on macOS only; older Git versions and Windows are reasoned from Git's release
  notes and source, not measured.
  *A `.env` file.* The MCP server takes `FASTMCP_*` settings from its environment only. It
  does not load a `.env` from the working directory unless you set `FASTMCP_ENV_FILE`
  yourself.
  *A store link.* Sidegraph does not open a store whose path leaves its repository through a
  symlink or a Windows junction, at `.sidegraph` or at a folder above it. SessionStart says so,
  and the commands and MCP tools name the link in their error. To share one store between
  projects, set `SIDEGRAPH_DIR` to the shared directory itself. A plugin install cannot do
  that, because the plugin's own commands set `SIDEGRAPH_DIR`.
  What stays trusted: your own configuration (`~/.gitconfig`, the repository's `.git/config`)
  and your environment. A filter or signing program you configured there still runs when git
  needs it. Git still enters a submodule you checked out; that submodule's git directory is
  written on your machine, not by the clone, so a command configured there is outside this
  model. So is a `.git` entry (a directory or a gitfile) delivered inside an archive: git
  never checks one out. A committed `.claude/settings.json` is your agent host's to gate, with
  one exception: when your environment sets no `SIDEGRAPH_RATIFY_POLICY`, `sidegraph-mcp` reads
  that one value from the file's `env` block. A repository can therefore choose its own
  ratification policy, `auto-all` included, under any host. A value in your environment wins
  over the file.

## Data boundary — what this tool touches

The Sidegraph runtime operates on local files and exposes tools to its MCP client.
The client can pass tool results to its configured model provider; choose the host and
provider according to your repository's data policy. The runtime data boundary is:

- **Reads:** your repository's files and the graph engine's `graphify-out/graph.json`
  (strictly read-only — Sidegraph never writes into engine output).
- **Writes:** primarily to a configured store directory
  (`.sidegraph/` inside the target repository by default) — small, human-readable JSON record files meant to be
  committed to your repo, a derived and gitignored `index.db` (a local SQLite index
  rebuilt from those files for fast queries, never itself committed), a committed format
  marker, and a `.gitignore` the store writes for itself. Opening a legacy single-file
  store (`decisions.db`, from before store schema 0.4.0) triggers a one-time migration
  that renames the legacy file to `decisions.db.migrated-backup` (kept, never deleted)
  alongside writing the new
  directory layout. The store is the default persistence location, not the only one:
  `sidegraph-init` can write `.claude/settings.json` (the ratification-policy env entry);
  the visualization, OKF export and bootstrap-report commands write to the output paths you
  give them; and the optional `sidegraph-prepare-commit-msg` git hook, once you install it,
  adds commented decision trailers to the commit message file git hands it
  (normally `.git/COMMIT_EDITMSG`). Three more writes sit outside the repository's own
  files. `sidegraph-init --hooks` (or a yes to its refresh question) writes a helper script
  and a marked block in three git hooks (`post-commit`, `post-merge`, `post-checkout`) under
  `.git/hooks/`. When `core.hooksPath` is set, `sidegraph-init` writes only the helper (to
  `.git/hooks/`) and prints the lines to add to your hooks by hand. While it runs, the helper
  also keeps a lock, a flag file and a log in the git directory. `sidegraph-init --no-hooks`
  writes the git config key `sidegraph.graphRefresh` to the repository's own config, and
  `sidegraph-init --remove-hooks` takes the blocks, the helper and that key out again. The
  SessionStart hook writes one 40-character commit id to
  `${XDG_CACHE_HOME:-~/.cache}/sidegraph/launch-commit`, and only when the running package was
  installed from the public repository's `main` branch. That covers plugin installs older than
  0.24.1, which launched `@main`, and a manual `git+…@main` install. Current plugins, a PyPI, tag, SHA or editable
  install never write it. Beyond those, the only filesystem writes are the standard config
  snippets you install yourself (`.mcp.json`, hook entries). See
  [`docs/reference/store-format.md`](docs/reference/store-format.md) for the full layout.
- **Network:** the Sidegraph runtime makes no outbound network calls and sends no remote
  telemetry. Package installation, plugin installation, and updates download dependencies
  from their configured registries or repositories. What a plugin fetches, and
  when: on the first start after an install or an update, the Sidegraph package and its
  exact-pinned dependencies from PyPI, into uv's cache; on a machine without Python 3.13, also a
  uv-managed CPython 3.13 from python-build-standalone on GitHub, which the pins do not cover;
  nothing on later starts. Your MCP client controls onward use of
  tool results. Local retrieval telemetry is recorded in the gitignored `index.db` by default (which records
  were shown and which files were touched, never uploaded); set `SIDEGRAPH_TELEMETRY=off` to
  stop new recording. The *optional* semantic documentation pass is executed by the
  separate Graphify CLI against the LLM provider you configure. Review that provider's
  data policy before using the pass on private documents.

## Security internals

The exact runtime rules are in
[Security internals](docs/reference/security-internals.md). In short:

- `record_drift_check` writes inspection history with redacted rationale, paths and Git blob
  ids, never source bodies or transcripts.
- Redaction is best-effort pattern matching and errs toward removing too much; it rewrites
  neither earlier records nor Git history, so rotate any credential that was ever captured.
- MCP calls are rate-limited, and every write request has fixed size, depth and count limits
  with no silent truncation.
- Write diagnostics never echo input values.
- Git runs through one guarded function, the MCP server reads no `.env` file, and a store link
  that leaves its repository is not followed; see
  [Security internals](docs/reference/security-internals.md#content-a-clone-brings).
- `--against` baselines are resolved to one commit before Git reads them, and bootstrap reads
  regular files only.

## Supported versions

| Version | Security fixes |
|---|---|
| Latest release | Yes |
| Any earlier release | No: upgrade to the latest release |

Sidegraph is pre-1.0, so fixes land only in a new release. Plugin installs pick a
fix up once you update the plugin after the release that contains it; installs pinned to a PyPI
version, tag or commit must move their pin.
