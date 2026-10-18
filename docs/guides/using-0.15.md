# Using Sidegraph 0.15

0.15 adds ADR export and maintenance, auditable delivery statistics, explicit drift
inspections, eligible fact delivery, and fixes for repeated automatic context and exact-file
memory outside the graph. File/entity/domain selection still applies; task-query ranking
is future work. Release changes are listed in the [changelog](../../CHANGELOG.md#0150--2026-10-18).

## Update

Keep a backup or commit of your authored `.sidegraph/` files. For a CLI installation:

```bash
uv tool install --upgrade sidegraph==0.15.0
```

For plugins, update the marketplace/package and installed plugin using your
[host's installation instructions](../getting-started/installation.md), then start a new
session so the MCP server and hooks use the new release. Public plugins intentionally
follow `main`; an explicit RC pin stays pinned until you change it.

## Collect cases during the first week

Use ordinary working tasks. Before a task-specific Sidegraph lookup, save the task brief,
expected files/entities and any constraints or facts you already expect to help. Unknown
expectations are valid. After lookup, retain the exact tool arguments and full response
locally, then note what the agent actually did with the memory.

```text
Date, project, host, Sidegraph version:
Task and intended result (before lookup):
Files/entities and known constraints (before lookup):
Tool arguments and response (saved locally):
Useful / off-task / missing records, with IDs if known:
Observed action influenced by memory, or UNKNOWN:
```

A few ordinary cases across the week are enough for an initial diagnosis. Include cases
where memory did not help. Keep real records and responses private; redact credentials
before sharing a report and exclude whole dialogue exports. Record an observed action
rather than estimating time saved. Inspected cases can inform private or synthetic regression examples; real records and
responses stay out of repositories. An inspected case cannot serve as an unseen test for
a later ranking change.
