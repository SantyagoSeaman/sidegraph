---
name: compact
description: >-
  Use when someone asks to compact, pack or migrate the Sidegraph store — "compact the store", "pack the decisions", "migrate to the packed format", "sidegraph-compact", "sidegraph-migrate", "too many files in .sidegraph", or when sidegraph-doctor reports `store-not-migrated`. Also reachable directly as /sidegraph:compact. Runs the local sidegraph-compact (or, on a store in the older layout, sidegraph-migrate) command: dry run first, the report shown verbatim, the real pass only after an explicit yes, then sidegraph-verify. It never commits; the changed `.sidegraph/` files go to the human to commit in a pull request.
---

# Compact

Run the CLI, never an MCP tool: the MCP server, sync and `SessionStart` never run maintenance or
migration. CLI commands below run through
`uvx --from git+https://github.com/SantyagoSeaman/sidegraph.git@main <command>` at the session's version, never the copy on `PATH` ([why](../../../../docs/reference/cli.md#running-the-clis-beside-the-plugin)).

Work from the repository root, on the default branch.

## 1. Dry run

Which layout does the store have? On a store in the older layout (a format marker below major
2, or `sidegraph-doctor` reports `store-not-migrated`), the command is `sidegraph-migrate`,
and **before** running even its dry run, show the human the **team checklist** in the
[0.17 migration guide](https://github.com/SantyagoSeaman/sidegraph/blob/main/docs/guides/migrating-to-0.17.md)
(everyone on the same version, open branches landed or rebased) and let them read it.
Otherwise the command is `sidegraph-compact`.

```bash
uvx --from git+https://github.com/SantyagoSeaman/sidegraph.git@main sidegraph-compact --dry-run   # packed store
uvx --from git+https://github.com/SantyagoSeaman/sidegraph.git@main sidegraph-migrate --dry-run   # older layout
```

Show the report **verbatim** in a fenced block. A dry run writes nothing tracked.

## 2. Ask

Summarise in one or two lines what the report says would change, then ask the human to
confirm. **Never run the real pass without an explicit yes.** A "looks fine" about the report
is not a yes to the run; ask again plainly.

## 3. Run and verify

Run the same command without `--dry-run`, then `sidegraph-verify`:

```bash
uvx --from git+https://github.com/SantyagoSeaman/sidegraph.git@main sidegraph-verify
```

Show both outputs verbatim. A non-zero exit from either is reported as it is: do not retry,
do not edit store files by hand.

## 4. Hand back

Say which files changed (`git status --short .sidegraph/`) and that `.sidegraph/` needs a
commit in a pull request. **Never commit**, and never push; the human does both.

## Refusals

Both commands refuse some states (not on the default branch, a dirty store, a conflict in the
tree). Relay the refusal text and its remedy as printed. Add `--any-branch` only when the
human asks for it, never on your own to get past a refusal.

## See also

- [`docs/reference/cli.md`](../../../../docs/reference/cli.md) — flags and exit codes of
  `sidegraph-compact`, `sidegraph-migrate` and `sidegraph-verify`.
- `sidegraph:health-check` — diagnosing the installation before maintenance.
