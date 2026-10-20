---
name: health-check
description: Use when diagnosing a Sidegraph installation, plugin wiring, store integrity, stale graph, anchor warnings, or uncertain Sidegraph versions.
---

# Sidegraph health check

Use this skill to produce one read-only diagnosis of the Sidegraph installation for the
current repository. The collector reports six evidence sections and prioritized manual
recommendations. Its `OK`, `warning`, and `broken` labels describe only the evidence it
measured. `UNKNOWN` always remains a warning and names the missing evidence.

## Choose an existing interpreter

Resolve the project root first with the host's existing Git context so you can identify its
existing interpreter, and retain the original launch directory for Claude scope reporting.
Pass that original directory as `--project`; the collector resolves Git root itself. Use a
directly identified, already-existing Python interpreter:

- In development, use `<project>/.venv/bin/python` when that file exists, or
  `<project>/.venv/Scripts/python.exe` on Windows.
- For an installed plugin, use a Python path supplied by attached host launch metadata or by
  metadata for an identified Sidegraph environment. Do not run the foreign Sidegraph command
  to discover its version.

Run the helper by its path relative to this installed `SKILL.md`, with Python's bytecode
writing disabled. For example, after safely assigning the known interpreter, project root,
and this skill directory to quoted shell arguments:

```bash
"$PYTHON" -B "$SKILL_DIR/scripts/collect.py" --project "$LAUNCH_DIR" --host "$KNOWN_HOST"
```

Pass `--store`, `--graph`, or `--plugin-root` only when the user or host has identified those
paths. The collector does not scan user profiles or guess a Python from `PATH`. Do not use
`uv`, `uvx`, a package installer, or a cached distribution as the launcher: an offline
launcher can still provision an environment. Never create or repair an environment to make
this check run.

If no existing interpreter with the inspection package can be identified, or the helper exits
nonzero, prints error JSON, or cannot import its inspection helpers, present the same six
section names as `warning`. State that collection is limited because a known compatible runtime
is unavailable; include only safe static evidence already visible in the session and manual
next steps. Do not repeat exception text, install, download, sync, provision, or substitute
another Python. A cached wheel or uv cache without its known runtime does not count as an
interpreter.

## Render the report

Parse the helper's JSON and render a single chat report in this order:

1. Overall status and preservation status.
2. The six sections: Plugin and wiring; CLI and runtime versions; Graphify; Store integrity;
   Anchors and entities; Inventory and usage.
3. Findings with measured values, followed by each `UNKNOWN` and its reason.
4. Prioritized, deduplicated manual recommendations with the stated scope and reason. Render a
   structured recommendation as a manual command for later use, using only its `argv`, `cwd`,
   and `env`, and the attached shell's native quoting (including PowerShell syntax). Empty
   `argv` means path/settings inspection instructions only. Never interpolate paths into a
   shell command string and never execute recommendations during this check.
5. Evidence limitations, including the unmerged-branch coverage limit and the fact that
   observed usage does not measure usefulness or saved effort.

Keep the active MCP identity separate from the collector runtime, plugin manifest, PATH
install, Graphify-recorded version, installed engine version, and this skill. Static wiring
does not prove that the active server answered. You may append a separately labeled,
source-and-time-cited MCP response already visible in the session; never change the
collector's section or overall status from it. Do not start an MCP server or call memory
retrieval for this check.

The collector brackets its own process with input fingerprints. If inputs changed or could
not be read, report preservation as unstable or unverified without blaming the collector.
Host hooks around tool invocation can write independently. The collector never opens a
`Store`, runs sync or init, updates Graphify, edits settings, ratifies records, repairs
anchors, rebuilds an index, or probes telemetry. When an existing index has WAL/SHM files, an
unrecognized header, or a nonempty rollback journal, doctor advisories and stats are
`UNKNOWN`; do not ask the collector to retry. With no index, canonical-only doctor curation
can still run while index-dependent checks and stats are explicitly skipped.

Recommendations are instructions for a later, separate manual action. Do not execute them
during this health check. Ordinary doctor or stats inspection outside this byte-preserving
collector can create SQLite sidecars for a WAL index; report that limitation and make no
claim that ending a session changes its journal mode. [CLI command guidance](../../../../docs/reference/cli.md#running-the-clis-beside-the-plugin) applies to manual CLI recommendations.
