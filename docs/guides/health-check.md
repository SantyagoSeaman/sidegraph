# Check a Sidegraph installation

[Glossary](../concepts/glossary.md)

Use `/sidegraph:health-check` to collect one read-only report for the current repository.
It covers plugin wiring, runtime versions, Graphify, store integrity, anchor advisories, and
inventory and usage. `UNKNOWN` means the evidence could not be established; it remains a
warning and never becomes a clean result.

For example, a real scratch run returned `overall: warning`. Its sections reported plugin wiring, runtime and Graphify warnings. The collector could not safely establish the active MCP identity or the installed Graphify version. It also found a known version difference: the PATH install reported 0.22.0 while the running collector reported 0.23.0. This calls for checking the missing evidence. It does not show that the store is broken. See the [captured report](../reference/examples.md#health-report).

Read the six sections, choose the highest-priority recommendation and run it explicitly. Then repeat the health check to see whether the evidence changed.

## Run the check

1. Start your coding agent in the repository where Sidegraph is installed.
2. Invoke the health-check skill, or ask: "Run the Sidegraph health check for this repository and explain the highest-priority recommendation."
3. Read the report's plugin wiring, runtime, Graphify, store integrity, anchor advisories and inventory/usage sections.
4. Run a recommended follow-up only after reviewing its scope, then repeat the check.

A clean result requires evidence. `UNKNOWN` stays a warning. If the skill cannot identify an existing suitable Python interpreter, it reports limited coverage and manual recommendations instead of installing anything. Supply a known existing interpreter or use the manual commands below.

For a manual store check, run these from the repository with the optional CLI installed:

```bash
sidegraph-verify
sidegraph-doctor
```

`sidegraph-verify` checks store contracts. `sidegraph-doctor` adds curation and advisory findings. These commands do not prove that the host's live MCP connection answered.

This is the section-status excerpt from the real scratch report captured on 2026-11-02:

| Section | Result |
|---|---|
| `plugin_and_wiring` | `warning` |
| `versions` | `warning` |
| `graphify` | `warning` |
| `store_integrity` | `OK` |
| `anchors_and_entities` | `OK` |
| `inventory_and_usage` | `OK` |

The report's `overall` was `warning`. Plugin wiring checks registrations, runtime compares package evidence, Graphify checks the recorded engine, integrity checks store contracts, anchor advisories flag targets needing review, and inventory/usage counts stored records and recorded activity. `broken` means a reported failure. `warning` can mean missing evidence. Read the reason before choosing a recommendation.

## Runtime requirement

The collector needs an identified Python environment that can import Sidegraph. Sidegraph requires Python 3.13 or later. The skill identifies an existing interpreter from the checkout or attached installation metadata. If it cannot establish one, it shows limited coverage and manual recommendations; it does not install a runtime.

See the [detailed contract](../reference/health-check.md#runtime-requirement).

## Evidence and limits

A configured MCP server is not proof that it answered. If active-server evidence is unknown, confirm the host connection separately. Usage counts show recorded activity, not usefulness or saved time. If another process changed protected inputs during collection, repeat the report after that work finishes.

See the [detailed contract](../reference/health-check.md#evidence-and-limits).

## Manual follow-up

Review the prioritized recommendations. Each supplies a reason, scope, command arguments and working directory. Run only the follow-up you choose, then collect again. The collector never executes these fixes itself.

See the [detailed contract](../reference/health-check.md#manual-follow-up).
