# What is stable, and what is not

> **Start here:** Use the section matching the command or payload you are checking. For everyday work, begin with the [documentation paths](../README.md). [Glossary](../concepts/glossary.md).

Sidegraph is pre-1.0. This page identifies the interfaces covered by compatibility
commitments and those that may still change. Use it when deciding what your integration
can depend on.

Read it before you write a script, a CI job, a prompt, or a tool against Sidegraph.

Use the compatibility categories below when building an integration. They describe API promises and are separate from the numbered anchor tiers in the glossary. **Committed** surfaces have migration or deprecation protection. **Provisional** surfaces require a version pin. **Not a contract** surfaces are unsuitable for parsers or assertions.

For a team upgrade, update every writer and reader before raising the store's minimum required version. Inspect the `.sidegraph/format` marker to learn that minimum. A client below the requirement refuses to open the store. Check the release's migration guide before running a command that raises it. Do not lower the marker by hand to force an old client to open new data.

## The three tiers

| Tier | The promise |
|---|---|
| **Committed** | Will not break in 0.x without either an automatic migration or a deprecation window with the old form still working. Breaking changes are always a `CHANGELOG` entry. |
| **Provisional** | May gain fields and entries without notice. May change or lose them with a `CHANGELOG` entry — but no migration and no compatibility shim. Build on it, pin your version. |
| **Not a contract** | May change in any release, silently. Do not parse it, do not assert on it, do not build tooling against it. |

A surface moves **up** a tier when it gains a mechanical guard, never because it merely feels
settled. That rule is the lesson of a real drift: `sidegraph-bootstrap --resume` had a test
asserting its help text was accurate, so the help text stayed right — and the reference page
described a mode that had not existed for weeks, because no test looked there.

## Where each surface sits

### Committed

| Surface | What is fixed | What guards it |
|---|---|---|
| **The store on disk** — `.sidegraph/` layout (one JSON file per record, and from 0.17 optionally packed `shards/`), the `format` marker, `schema_version` | The current version is **0.6.0** (a format-2 store's index is stamped `0.7.0+format2`). Single-file SQLite stores (`decisions.db`, stamped `0.2.0`/`0.3.0`) predate 0.1.0 and are refused on open; open one once with 0.23.x to migrate it, then upgrade. `0.4.0`/`0.5.0` reload in place. A future bump does one of those two or, as format 2 did, an explicit migration command (`sidegraph-migrate`) while the old format stays served — it never orphans a store. A store can require a minimum Sidegraph version (`sidegraph-require`): its marker becomes `sidegraph-store 1.0.0` plus `requires-sidegraph >= X.Y.Z`, and older versions refuse it. A packed store's marker is `sidegraph-store 2.0.0` plus a `requires-sidegraph` line and a `shards N` line; 0.14–0.16 refuse it, and the [0.17 migration guide](../guides/migrating-to-0.17.md) covers the switch. From 0.22.0, `sidegraph-compact` may also pack drift checks into `verifications/packed.shard`; the first run that does raises the store's requirement to `0.22.0`, so 0.17–0.21 refuse it. From 0.24 a new store has no `initiatives/` directory and the `Initiative` model is gone; a store that still holds `initiatives/*.json` from an older release opens, verifies, syncs, compacts and migrates as before, and those files are left untouched. | `sidegraph-verify` lints every canonical file against the write-path invariants; a drift-check capsule may carry an optional `declared_sections` field inside `schema_version` 1 (added in 0.20.0, before the [mixed-version rule](#mixed-versions); readers that do not know it ignore it, and any field added since raises the store requirement); `--against <ref>` classifies every changed file against the store's own transition rules |
| **CLI commands, flags, exit codes** | 22 entry points, 100 flags. A flag is not removed without a deprecation window; documented exit codes keep their meaning (`0` ok, `1` operational error or refusal with the message on stderr, `2` findings or an argparse usage error: [`cli.md`](cli.md#exit-codes-and-streams)) | every parser option must appear in its `cli.md` section (`tests/test_docs_claims.py`) |
| **Environment variables** | The 16 in [`configuration.md`](configuration.md), and the store-path precedence between them. `SIDEGRAPH_DB` stays honoured as the documented deprecated form. `SIDEGRAPH_AUTO_ACCEPT` is deprecated the same way: it keeps its exact behavior (including landing records with no ratifier stamp), SessionStart and `sidegraph-doctor` warn when it is set in their environment, `propose_decisions` results carry a `deprecation` note while it is `on`, and it will be removed in a later release, announced in the CHANGELOG; `SIDEGRAPH_RATIFY_POLICY=auto-all` replaces it and still requires a live anchor. `SIDEGRAPH_RATIFY_POLICY`'s name and its `manual` default are committed; its auto values and their gates are Provisional (below). | bidirectional test: the documented set must equal what `src/` actually reads |
| **Hook process contract** | Read one JSON payload on stdin, write one JSON object on stdout, never block the host, never crash it — any internal failure exits normally, printing `{}`, or for SessionStart a store that cannot be opened prints a `systemMessage` naming the cause. Hosts call the hooks without arguments, or with `--repo-root` alone (the Claude and Grok plugins pass it; it moves the hook to the project root first): `-h`/`--help` prints usage on stdout and exits `0`, and any other argument is a usage error on stderr that exits `2` (keep the shell guard on hand-written wiring) | the hook tests, and a live host run per release |

The *payload schema* inside the hook contract is **Claude Code's**, not ours. If the host
changes it, we follow the host; that is the point of keeping the host seam thin.

### Provisional

| Surface | Why it is not committed |
|---|---|
| **The MCP tool set and return shapes** (25 tools) | Fields and tools may still be added, deprecated or removed. Pin a version if a client depends on an exact shape. |
| **Flow profiles** — the seven names and their ingest globs, ADR output settings and templates | `spec-kit`'s glob reads `specs/*/plan.md`, while that flow writes its rejected alternatives to `research.md`, which nothing reads. Fixing that changes a glob. |
| **`doctor` finding codes** and the `drift_groups` key of `sidegraph-doctor --json` | Advisory lint; the set grows as checks are added. Guarded against the docs, not frozen. The `drift_groups` shape (and the grouped default text) may still change. |
| **Integrity check ids and severities** — `store-unreadable`, `graph-stale`, `orphaned-records` and the rest of the [troubleshooting](../guides/troubleshooting.md) table, as they appear in `sidegraph-stats --json`'s `health[].check` and in the `integrity_notice:` meta rows | Beside the doctor codes, for the same reason: the set grows as checks are added (the host-wiring checks `version-skew`, `path-skew` and `plugin-off-in-subdirectories` came before `refresh-helper-outdated` and `engine-version`), and a severity or a threshold (such as the age that makes orphaned records degraded) may move once it is measured. |
| **Auto-ratification policy** — `SIDEGRAPH_RATIFY_POLICY`'s `auto-low-risk`/`auto-all` values and eligibility gates, the `, auto-ratified N` CLI summary segment (and the dry run's `, would auto-ratify M` and `[auto]` marker), and `sidegraph-doctor`'s `auto share`/`auto supersede rate` lines | Eligibility and reporting may tighten. `manual` remains the committed default. |

### Not a contract

| Surface | Why |
|---|---|
| **The text retrieval renders** — `[unratified]` / `[drifted]` markers, line format, budget splits, the `SessionStart` map layout, and the wording of its status lines and notices | It is unversioned prose written for a model to read. Assert on structured records or tool return values, not formatting. |
| **Archive segment format** — `archive/<date>-<seq>-<hash>.jsonl` | Implemented but still provisional; consumers should load records through Sidegraph rather than parse filenames. |
| **`index.db`** — every table and column | Derived, gitignored, rebuilt from the canonical files whenever stale. It is a cache. Read the JSON. |

## Guarantees vs. the surfaces that express them

The distinction that matters most, and the one easiest to get backwards: **several behaviours
are committed while the surface expressing them is not.** The rendered line is free to
change; what it is telling you is not.

| Committed behaviour | Expressed through (not a contract) |
|---|---|
| **Mistakes-first.** Direct gotcha, constraint, and lesson records rank ahead of direct ADRs and related/global memory. | the order of lines in `get_task_context` output |
| **Unratified memory cannot outrank accepted memory.** Proposed records render only in a final quarantine block and may be withheld entirely by the surfacing window or `SIDEGRAPH_UNRATIFIED=off`. Raw MCP listings apply the same policy. Withholding never deletes the record or removes it from the ratification queue. | the `[unratified]` tag, block placement, and default window |
| **Memory signals its own decay.** A record anchored to code that changed since capture is marked, not silently served as current. | the `[drifted]` tag |
| **Local runtime.** Sidegraph makes no outbound network calls and sends no remote telemetry. The MCP client controls where tool results go; installation and updates download dependencies. Optional local diagnostics live in the gitignored index and switch off with one variable. | `SIDEGRAPH_TELEMETRY` (the variable name is committed; the diagnostics tables are not) |

If you need one of these guarantees in a script, assert on the store or the MCP return value,
never on rendered text.

## The invariants underneath everything

These are not a tier. They are the premise — no version of Sidegraph trades them away:

1. **The engine's `graph.json` is read-only input.** Sidegraph never writes into it.
2. **The store is append-only.** No `Decision` is ever hard-deleted. A reversal closes the old
   record and appends a successor; the superseded record stays retrievable, because that
   history *is* the product.
3. **`schema_version` is stamped on the store**, always, from the first write.

## When we break something

- **Committed:** a `CHANGELOG` entry, plus a migration or a deprecation window. Never both
  absent.
- **Provisional:** a `CHANGELOG` entry.
- **Not a contract:** nothing. That is what the tier means.

## Mixed versions

A client rewrites a record through its own model, so a field it does not know is dropped
on the rewrite, silently, and a record whose field has a type or value it cannot read is
skipped. A release cannot change a team's `.sidegraph/format` marker; only
`sidegraph-require` does, and sync, SessionStart and the MCP server never run it. So the rule
is a procedure:

- A release that adds a field to a stored record (or changes what one serialises) raises
  `RECORD_FIELDS_REQUIREMENT` (`src/sidegraph/store_layout.py`) to itself.
- Its CHANGELOG entry says that all clients must upgrade, and that every team runs
  `sidegraph-require --sidegraph X.Y.Z` and commits `.sidegraph/format` before anyone on the
  new version writes. Until then an older client can still drop the field.
- `sidegraph-doctor` reports `record-fields-requirement` (advisory, `--check` exits `2`) for a
  store whose requirement is below the constant. It is silent while the constant is at its
  baseline, `0.17.0`, so no existing store is told anything before a release raises it, and
  while the running Sidegraph is older than the constant, since `sidegraph-require` refuses a
  version above the running one.
- A store this Sidegraph creates (and the marker `sidegraph-migrate` writes) requires the
  constant once the running release has reached it, so a new store has no older client to
  protect and gets no finding. A development build older than the constant keeps `0.17.0`,
  so it never creates a store it would refuse.
- 0.24.0 is the first release under the rule: it adds `Decision.retired`
  ([`retire_decision`](mcp-tools.md#retire_decision)), so the constant is `0.24.0`. For this
  field the risk is not a dropped field: a `deprecated` record is terminal, so no older write
  path rewrites it, and maintenance copies record bodies verbatim. The risk is an older
  `sidegraph-verify --against` reporting `illegal-field-change` on the commit that retires a
  decision, and older readers listing the retired record by default, without its reason. So
  `retire_decision` refuses until the store requires `0.24.0`; nothing else is gated.

A test enforces the first step: `tests/test_record_fields.py` compares the serialisation
schema of every stored model (`Decision`, `Fact`, `Domain`, `Entity`, `AnchorBinding`,
`DriftCheck` and the models nested in them: the keys the writers produce, types, defaults, default factories)
with a committed snapshot, and fails unless the constant is above the last released version.

Pin the version if any of this matters to you — `pip install 'sidegraph==X.Y.Z'`, or a commit
SHA in the `uvx --from git+…@<sha>` form. Pre-1.0, minor bumps may break provisional surfaces.
