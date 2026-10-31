# Check a Sidegraph installation

Use `/sidegraph:health-check` to collect one read-only report for the current repository.
It covers plugin wiring, runtime versions, Graphify, store integrity, anchor advisories, and
inventory and usage. `UNKNOWN` means the evidence could not be established; it remains a
warning and never becomes a clean result.

## Runtime requirement

The skill runs its local `scripts/collect.py` helper with `-B` under an already identified
Python interpreter. In a development checkout, the supported paths are:

```bash
"<project>/.venv/bin/python" -B "<skill-directory>/scripts/collect.py" --project "<launch-directory>" --host claude
```

For Codex, pass `--host codex`; use `unknown` when the attached host is not known. On Windows,
use the existing `<project>/.venv/Scripts/python.exe`. Installed use requires a
known interpreter from attached host launch metadata or metadata for an identified Sidegraph
environment. The collector does not guess a Python on `PATH`, run an unknown Sidegraph
executable, or use `uv`/`uvx` as its launcher. If no suitable interpreter is known, the skill
shows all six sections as `warning`, states that collection is limited, includes safe static evidence already available, lists
manual recommendations, and stops. The same fallback applies when the helper exits nonzero,
prints error JSON, or cannot import required inspection helpers; show only the error class and
do not echo exception text. Cached package files alone do not authorize provisioning.

The skill resolves the Git root to select the interpreter, then passes the original launch
directory as `--project` so Claude plugin-scope checks retain the launch context. The optional
`--store`, `--graph`, and `--plugin-root` arguments are for
paths already known to the host or user; the collector does not search unrelated profiles.

## Evidence and limits

The report separates the collector package, plugin manifest, optional PATH installation,
Graphify-recorded engine version, and any already observed active MCP evidence. Static
registration is not proof that a running server answered. The workflow never starts a server
or calls retrieval tools to test connectivity.

The collector fingerprints the canonical store, index and sidecars, settings, plugin
manifests, graph inputs, and actual Git index around its process. This is a stable-source
check, not an atomic snapshot; concurrent writes make preservation evidence unstable without
identifying which process changed it. Host hooks surrounding tool invocation are outside the
collector's preservation boundary.

If the index is absent, canonical-only doctor curation still runs, and index-dependent
findings and stats are marked skipped. If an existing index has WAL/SHM sidecars, an
unrecognized header, an unreadable header, or a nonempty rollback journal, all doctor
advisory and stats paths that could open it are skipped. A regular rollback-mode index allows
the existing doctor and stats checks to run. Cold or stale index qualifications are retained.

The six report sections distinguish contract violations from advisory findings and missing
coverage. Inventory counts come from canonical readers. Pending age is unknown for malformed
record identifiers. Usage reports summarize recorded activity only; delivery counts do not
show usefulness, prevented errors, or saved time. Records on unmerged or remote-only branches
are not exhaustively checked.

## Manual follow-up

Recommendations are prioritized and deduplicated, with a reason, explicit scope, argv, working
directory, and environment for a manual command or skill to run after the report. Render argv
with the attached shell's native quoting; paths are never embedded into shell snippets. The
health check never executes recommendations. Ordinary doctor and stats reads
outside this collector can create SQLite sidecars for a WAL database, so inspect such an
index separately after the writing session. Ending a session does not guarantee a persistent
journal-mode change. Do not recommend an upgrade from an unknown version or install the
optional PATH CLI solely because it is absent.
