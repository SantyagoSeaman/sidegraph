# Using Sidegraph 0.16

Sidegraph 0.16.0 adds bounded full-domain listings, honest uncomputed stats, a read-only
installation health report, lower repeated descriptor-lookup cost, immediate refusal of hook
SQLite lock contention, and clearer reporting of retained hot files during compaction. See the
[changelog](../../CHANGELOG.md#0160--2026-10-20) for details.

## Update

For a CLI installed from the public repository's `main`, run:

```bash
uv tool upgrade sidegraph
```

For a PyPI version pin, install or upgrade to the exact release:

```bash
uv tool install --upgrade sidegraph==0.16.0
```

For host plugins, update the marketplace package and installed plugin using the
[host installation instructions](../getting-started/installation.md), then start a new session
so the MCP server and hooks use the released version. Public plugin manifests intentionally
follow `main`; an explicit release-candidate pin stays pinned until you change it.

## Stores and changed responses

Existing stores from 0.15 remain compatible. This release adds no canonical store format
migration.

`list_domains` now returns a provisional JSON text envelope with `matched`, `returned`, `omitted`,
`domains`, and `hint`, instead of a bare list. Read rows from `domains`; use the counts and hint
to see whether the response was bounded and how to narrow it.

Run `/sidegraph:health-check` for a read-only report about plugin wiring, runtime versions,
Graphify, store integrity, anchor advisories, and recorded inventory and usage. Missing evidence
stays `UNKNOWN`; an unsafe existing SQLite index causes checks that might open it to be skipped.

## Limits

Retrieval can still include off-task records, and this release does not measure whether delivered
memory helped or saved time. It adds no task-query ranking or full-pool recall claim, and it does
not change active-store compaction behavior. Health-report usage counts describe recorded
activity, not usefulness.
