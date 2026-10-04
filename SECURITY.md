# Security Policy

## Reporting a vulnerability

Please report vulnerabilities privately via GitHub's **"Report a vulnerability"** button
(Security tab) on this repository — do not open a public issue for security problems.
You should get a first response within a few days.

## Data boundary — what this tool touches

Sidegraph is a local-only tool. Its entire data surface:

- **Reads:** your repository's files and the graph engine's `graphify-out/graph.json`
  (strictly read-only — Sidegraph never writes into engine output).
- **Writes:** confined to one store directory inside the target repository
  (`.sidegraph/` by convention) — small, human-readable JSON record files meant to be
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
  installed from the canonical `git+https://github.com/SantyagoSeaman/sidegraph.git@main`. That
  covers the plugin and the manual `uvx --from …@main` recipe. A PyPI, tag, SHA or editable
  install never writes it. Beyond those, the only filesystem writes are the standard config
  snippets you install yourself (`.mcp.json`, hook entries). See
  [`docs/reference/store-format.md`](docs/reference/store-format.md) for the full layout.
- **Network:** none. No remote telemetry, no phone-home, nothing leaves your machine. Local
  retrieval telemetry is recorded in the gitignored `index.db` by default (which records
  were shown and which files were touched, never uploaded); set `SIDEGRAPH_TELEMETRY=off` to
  stop new recording. The only
  network-using feature is the *optional* semantic documentation pass, which is executed
  by the separate Graphify CLI against the LLM provider you configure — not by Sidegraph.

## Secret hygiene

Text captured into decisions passes a redaction step before it is stored (API keys,
tokens, `key=value` credentials). Redaction is best-effort pattern matching — review
drafts at ratification time, and treat the committed store like any other repo content
in your secret-scanning setup.

## Supported versions

Pre-1.0: only the latest released version receives fixes.
