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
  directory layout. Outside `.sidegraph/`, the only filesystem writes are the standard
  config snippets you install yourself (`.mcp.json`, hook entries) — Sidegraph touches
  nothing else. See [`docs/reference/store-format.md`](docs/reference/store-format.md)
  for the full layout.
- **Network:** none. No telemetry, no phone-home, nothing leaves your machine. The only
  network-using feature is the *optional* semantic documentation pass, which is executed
  by the separate Graphify CLI against the LLM provider you configure — not by Sidegraph.

## Secret hygiene

Text captured into decisions passes a redaction step before it is stored (API keys,
tokens, `key=value` credentials). Redaction is best-effort pattern matching — review
drafts at ratification time, and treat the committed store like any other repo content
in your secret-scanning setup.

## Supported versions

Pre-1.0: only the latest released version receives fixes.
