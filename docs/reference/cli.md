# CLI reference

17 console scripts are registered in `pyproject.toml` (`[project.scripts]`): thirteen
commands documented on this page — twelve in [`src/sidegraph/cli.py`](../../src/sidegraph/cli.py)
(including `sidegraph-prepare-commit-msg`, which doubles as a git hook — see
[`reference/git-bindings.md`](git-bindings.md) for its installation and mechanism) and the
guided `sidegraph-bootstrap` in
[`src/sidegraph/bootstrap/cli.py`](../../src/sidegraph/bootstrap/cli.py) — plus the MCP server
(`sidegraph-mcp`) and the three Claude Code hook entry points (`sidegraph-session-start`,
`sidegraph-stop`, `sidegraph-pre-tool-use`; see [`reference/hooks.md`](hooks.md)).
`sidegraph-ratify`, `sidegraph-sync`,
`sidegraph-import`, `sidegraph-domains`, `sidegraph-compact`, `sidegraph-verify`,
`sidegraph-doctor`, `sidegraph-viz`, `sidegraph-export-okf`, and `sidegraph-blame` take
`--db`, naming the
store **directory** (the canonical `.sidegraph/` layout — see
[`reference/store-format.md`](store-format.md)); a
legacy single-file `*.db` path is
still accepted there too, and triggers a one-time migration into the directory layout. When `--db` is
omitted, all of them resolve it through the same precedence — an explicit `--db` wins outright,
otherwise `$SIDEGRAPH_DIR` if set, otherwise the deprecated `$SIDEGRAPH_DB` (a one-line
deprecation notice prints to stderr the first time it's actually used), otherwise an existing
`.sidegraph/` if present, otherwise the default `.sidegraph/` — printing a one-line warning to
stderr if nothing resolved and a brand-new store is about to be created (only commands that
would actually create one print it; the never-creating four below error out instead).
`sidegraph-init` takes
`--db` too but resolves the same way — see its section below for what differs (idempotent
"already initialized" reporting). `sidegraph-verify`, `sidegraph-doctor`, `sidegraph-viz`,
and `sidegraph-export-okf` also resolve `--db` the same way but differ in the *opposite*
direction from every other command here:
none of them auto-creates a missing store — see [`sidegraph-verify`](#sidegraph-verify) (and
[`sidegraph-doctor`](#sidegraph-doctor), which composes it) for why;
[`sidegraph-viz`](#sidegraph-viz) and [`sidegraph-export-okf`](#sidegraph-export-okf) apply
the same rule (rendering or exporting a store that isn't there helps no one). `sidegraph-init`,
`sidegraph-sync`, `sidegraph-import`, and `sidegraph-domains bootstrap` additionally take
`--graph`, defaulting to `$SIDEGRAPH_GRAPH` or `graphify-out/graph.json`. See
[`configuration.md`](configuration.md) for full path-resolution semantics.

## `sidegraph-bootstrap`

```
sidegraph-bootstrap [--root PATH] [--db PATH] [--graph PATH] [--profile NAME]
                    [--docs PATH ...] [--include FILE ...]
                    [--host {claude-code,codex}] [--codex-config PATH]
                    [--candidate KEY] [--task PATH] [--report PATH] [--resume]
```

Guided, preview-first onboarding for existing ADR/spec rationale. Bootstrap auto-detects or
accepts one of exactly six profiles (`generic-adr`, `superpowers`, `genkovich-sdd`,
`spec-kit`, `bmad`, `openspec`), scans inside one repository root, produces a redacted
immutable plan, reviews candidates, writes only after literal confirmation, reopens and
verifies the store, checks the selected host, and calls production task-context retrieval
for proof. See the
[Bootstrap guide](../getting-started/bootstrap.md) for the exact profile globs, scan
exclusions, review/state table, host matrix, and recovery semantics.

| Flag | Default | Meaning |
|---|---|---|
| `--root` | `.` | repository root and confinement boundary |
| `--db` | shared store-path resolution | canonical Sidegraph store directory |
| `--graph` | `$SIDEGRAPH_GRAPH` or `graphify-out/graph.json` | read-only Graphify input; missing permits preview but stops before review |
| `--profile` | auto-detect | one supported profile; required when detection is empty or ambiguous |
| `--docs` | profile globs only | additional in-repository document or directory; repeatable |
| `--include` | none | explicitly include one text file otherwise excluded by directory/size policy; repeatable |
| `--host` | `claude-code` | host integration to inspect |
| `--codex-config` | `.codex/config.toml` | alternate Codex MCP config path |
| `--candidate` | all candidates | review only the displayed candidate key |
| `--task` | deterministic proof selection | additional repository path for production retrieval proof |
| `--report` | none | write an aggregate, source-content-free Markdown report; never uploaded |
| `--resume` | off | marker only, for the resume command a failed run prints — it changes no behavior. The scan/review/reconcile flow is idempotent, so reissuing the same command without it behaves identically |

Output keeps `STORE`, `ANCHORS`, `INTEGRATION`, and `PROOF` independent. Claude Code can
complete the full v1 flow. Codex verifies MCP, SessionStart, and Stop but reports Read/Grep
PreToolUse as unsupported; a green supported subset plus store, anchor, and proof completion
is `best-effort` and exits `0`, but is never labeled fully supported. A missing supported
check exits `2`.

**Exit codes:** `0` for complete activation, Codex best-effort completion, or a safe
no-write diagnostic; `1` for usage or operational failure, unreachable once anything has been
durably written (a post-write I/O failure is at worst `2`, never `1`); `2` for actionable
`incomplete` or `partial-recoverable` activation. The 10–15 minute path is an unmeasured
launch target, not a measured CLI guarantee.

```bash
graphify update .
sidegraph-bootstrap --profile generic-adr --host claude-code --report bootstrap-report.md
sidegraph-bootstrap --resume --profile generic-adr --host claude-code
```

## `sidegraph-init`

```
sidegraph-init [--db PATH] [--graph PATH]
```

| Flag | Default | Meaning |
|---|---|---|
| `--db` | `$SIDEGRAPH_DIR` if set, else existing `.sidegraph/` if present, else `.sidegraph` | store directory to create — the recommended in-repo location; `$SIDEGRAPH_DB` is honored for back-compat (deprecated). Passing a legacy `*.db` file path still works via one-time migration |
| `--graph` | `$SIDEGRAPH_GRAPH` or `graphify-out/graph.json` | `graph.json` path to check for (read-only; never created) |

Bootstraps a target repo for Sidegraph. Run it once, in the repo you want memory over (not
the Sidegraph checkout):

1. Creates the store's directory (the canonical `decisions/`/`domains/`/`entities/`/
   `bindings/`/`initiatives/` layout, a committed `format` marker, and a store-written
   `.sidegraph/.gitignore` ignoring the derived `index.db`) at `--db` (via `Store(...)`, the
   same call every other entry point makes — inherently idempotent). If the directory already
   looks initialized (an existing `format` marker, an un-migrated legacy `decisions.db`, or a
   legacy `*.db` file path), prints `already initialized: <path> exists.` instead of touching
   it again; a legacy path that isn't yet migrated is migrated in place, and the (accurate)
   "already initialized" message still applies since that's pre-existing data, not a fresh
   store.
2. Checks whether `--graph` exists. Prints `found graph: <path>` if so, or `missing graph:
   <path> — run \`graphify update .\`, then \`sidegraph-sync\`, to anchor decisions to code
   (optional; Sidegraph works without it).` if not. The graph is optional at init time —
   `sidegraph-init` typically runs before the first `graphify update .`.
3. Prints the setup instructions: the plugin install path first (`/plugin marketplace add
   SantyagoSeaman/sidegraph` + `/plugin install sidegraph@sidegraph` — installs the MCP server
   and all three hooks automatically), then the no-plugin alternative (a single
   `claude mcp add sidegraph -s project … -- uvx --from git+…` command that writes a
   repo-committed `.mcp.json`), and a pointer to
   [`claude-code-setup.md`](../getting-started/claude-code-setup.md) /
   [`codex-setup.md`](../getting-started/codex-setup.md) for hook-by-hook manual wiring.
   Printed on every run, not just the first, so it's easy to re-fetch.

**Exit code:** `0` on success, including when the graph is missing (expected and non-fatal)
and when re-run on an already-initialized repo (idempotent). Non-zero (`1`) only if the store
itself can't be created/opened (e.g. its parent directory isn't writable, or an existing store
has an incompatible format/`schema_version`) — printed as `store not writable (<path>):
<error>`.

**Examples:**

```bash
uv run sidegraph-init
uv run sidegraph-init --db .sidegraph --graph graphify-out/graph.json
```

## `sidegraph-ratify`

```
sidegraph-ratify [--db PATH] [--accept ID ...] [--drop ID ...] [--all]
```

| Flag | Default | Meaning |
|---|---|---|
| `--db` | `$SIDEGRAPH_DIR` → existing `.sidegraph/` → `.sidegraph` | store directory; `$SIDEGRAPH_DB` honored for back-compat (deprecated); warns on stderr if the resolved path doesn't exist yet (see above) |
| `--accept ID ...` | `[]` | ratify these ids — decisions AND domains, routed by lookup (decision id checked first, then domain) |
| `--drop ID ...` | `[]` | reject these ids — same routing as `--accept` |
| `--all` | off | accept every currently pending proposal, decisions **and** domains (overrides `--accept`, computed from `store.iter_proposed()` + `store.iter_domains(status=proposed)` at run time — see the [naming guide](../guides/naming-your-domains.md#why-selective-not---all) for why `--all` on a large auto-bootstrapped domain batch is usually the wrong call) |
| `--graph` | `$SIDEGRAPH_GRAPH` → `graphify-out/graph.json` | read-only input used to resolve an accepted **domain's** membership immediately, the way the MCP `ratify` tool does. A relative path resolves against the STORE's project root, never the shell's CWD. Missing or unreadable is fine — the accept still lands and the membership heal is scheduled for the next `sidegraph-sync` |

One gate for both authoring surfaces (decisions and [domains](../concepts/mind-model.md)) — see
[mind model](../concepts/mind-model.md#domain-lifecycle).

**No flags** (or `--accept`/`--drop`/`--all` all absent): lists every pending proposal
human-readably, sectioned **"Decisions:"** then **"Domains:"** (each printed only when
non-empty; domains render as `<id>  [domain] <slug> — <title>` with `summary:`/`paths:`/
`communities:`/`from:` lines always present, plus a `parent:` line only when the domain has a
parent (`parent_id` set) — `paths:`/`communities:` always render, `(none)` when empty rather
than being omitted, so an over-broad membership rule is visible at this gate), or
prints `No proposed decisions, facts, or domains pending ratification.` if there are none, followed by
`N pending. Use --accept ID... / --drop ID... / --all.` (`N` = decisions + domains combined)
when there are.

**With `--accept`/`--drop`/`--all`:** prints one line per id — `accepted <id>` / `dropped
<id>` on success, `error <id>: <message>` if the id isn't in a state that action allows, or
doesn't resolve to either a decision or a domain at all. `--accept` always requires
`proposed`, for either kind. `--drop` requires `proposed` for a **decision** (unchanged —
an accepted decision is memory, not something you retire by dropping it; see
[`concepts/mind-model.md`](../concepts/mind-model.md#domain-lifecycle) for why domains are
different) but accepts `proposed` **or accepted** for a **domain** — retiring an
already-accepted domain (e.g. to resolve a [cross-branch slug
conflict](store-format.md#merge-semantics-git-resolves-it-not-the-store)) is a legitimate,
append-only-safe operation (the file stays, only its status flips to `dropped`). An id in
both lists (or repeated) still just gets processed once per list, in `--accept` order then
`--drop` order. Accepting a domain also mints its paired `domain:<slug>` abstract entity;
dropping one mints nothing (an entity already minted by a prior accept is left as-is).

**TOC refresh:** whenever at least one *domain* id was actually accepted or dropped by this
run, the `SessionStart` TOC cache is rebuilt immediately (no need to wait for the next
`sidegraph-sync`) — see
[retrieval: when the TOC goes live](../concepts/retrieval.md#when-the-toc-goes-live). A
decisions-only run leaves the cache untouched.

**Exit code:** `0` on success — including the no-flags listing case and the case where every
id passed to `--accept`/`--drop` succeeds. Returns `1` if the store can't be opened (e.g. an
incompatible `schema_version`, printed as `store not readable (<path>): <error>`), or if any
id passed to `--accept`/`--drop` errors (unknown, not in an eligible status for that action,
or wrong-kind). The per-id
`error <id>: ...` lines still print and the rest of the ids are still processed either way —
the exit code just lets scripts detect the failure via `$?` instead of parsing stdout.

**Examples:**

```bash
uv run sidegraph-ratify                              # review what's pending (decisions + domains)
uv run sidegraph-ratify --accept 01J8Z2Q... 01J8Z2R...
uv run sidegraph-ratify --drop 01J8Z2S...
uv run sidegraph-ratify --all                         # accept everything pending — see the caveat above for domains
uv run sidegraph-ratify --db .sidegraph --accept 01J8Z2Q...
```

## `sidegraph-sync`

```
sidegraph-sync [--db PATH] [--graph PATH] [--force] [--json] [--check]
```

| Flag | Default | Meaning |
|---|---|---|
| `--db` | `$SIDEGRAPH_DIR` → existing `.sidegraph/` → `.sidegraph` | store directory; `$SIDEGRAPH_DB` honored for back-compat (deprecated); warns on stderr if the resolved path doesn't exist yet (see above) |
| `--graph` | `$SIDEGRAPH_GRAPH` or `graphify-out/graph.json` | graph.json path |
| `--force` | off | re-run the full rebind pass even if `graph_version` matches the last sync |
| `--json` | off | print the report as one JSON object to stdout (nothing else) — same shape as the `sync_anchors` MCP tool's return value (`sync.report_as_dict`); see [CI-consumable output](#ci-consumable-output) below |
| `--check` | off | exit `2` when the report has an attention finding — for CI; see [CI-consumable output](#ci-consumable-output) below |

Re-resolves every tracked concrete entity through the rebind ladder (see
[`guides/surviving-refactors.md`](../guides/surviving-refactors.md)), re-points Tier-1
community bindings where Leiden renumbered communities, refreshes every **accepted** domain's
`communities` mapping (see
[mind model](../concepts/mind-model.md#how-domains-relate-to-engine-communities)), and
recomputes the `SessionStart` TOC cache.

**Output, in order:**

1. If the graph can't be read: `graph not readable (<path>): <error>` and stop.
2. If the store's last-synced `graph_version` already matches the current graph, `--force`
   wasn't given, and the store hasn't just been reloaded from a canonical change (`git pull`,
   merge, branch switch) that left its volatile state cold: `up to date (graph <version>)` and
   stop — the rebind pass does not run.
3. Otherwise: `synced <from_version or '<never>'> -> <to_version>: <counts>`, where `<counts>`
   is a `{status: count}` map (e.g. `{'unchanged': 3, 'orphaned': 1}`) or `no tracked entities`
   if the store has none yet.
4. If any Tier-1 community bindings were re-pointed (the count is per-anchor bindings, not
   distinct decisions): `re-pointed N community binding(s)`.
5. If any accepted domain's `communities` field actually changed this pass:
   `refreshed community mapping for N domain(s)`.
6. One line per entity whose outcome was `moved`, `orphaned`, `ambiguous`, or `error`:
   `  <status>: <canonical_name> (<detail or 'no match'>)`. `unchanged`/`rebound` entities are
   not listed individually (only counted above) since nothing needs attention there.
7. If any decisions are now "stale" (every leaf anchor orphaned):
   `possibly stale decisions (all anchors gone — verify):` followed by one
   `  <id>  <title>` line per decision.
8. If any accepted domain recomputed to zero current communities *and* its `path_prefixes`
   matched nothing in the current graph: `possibly empty domains — re-scope or supersede:`
   followed by one `  <slug>  <title>` line per domain. A domain is never auto-changed
   (dropped/superseded) here — it's routed to a human, same "never guess" convention as stale
   decisions.
9. If any accepted domain's `path_prefixes` recomputed to a candidate that would newly claim
   more than 20% of all current communities (a candidate that resolves to a single community is
   never capped, regardless of ratio — one community can't swallow anything else), the path
   contribution is **rejected and dropped** — but any `seed_anchors` the domain carries are
   still resolved and applied, so a domain with both an overbroad path rule and precise
   `seed_anchors` has its `communities` mapping **replaced** by the anchor-resolved set; only a
   domain whose overbroad path had no `seed_anchors` to rescue it keeps its previous
   `communities` mapping, unchanged. Either way it's reported:
   `path rule too broad — path contribution dropped (seed_anchors, if any, still applied);
   narrow path_prefixes or re-scope:` followed by one
   `  <slug>  <title>  (<matched>/<total> communities)` line per domain (`<matched>` = the
   rejected path candidate's community count, `<total>` = all current communities). This is
   unconditional — it applies to a `path_prefixes` rule regardless of whether it was
   auto-derived (`sidegraph-domains bootstrap`) or hand-authored (`sidegraph-domains add
   --path`) — see the [naming guide's guardrails](../guides/naming-your-domains.md#guardrails-against-an-over-broad-path-rule).
10. If any accepted domain's refresh itself raised (a malformed `seed_anchors` descriptor, a
    domain that vanished mid-pass, a lock-contended write): `domains that FAILED to refresh
    (fix, then re-run with --force):` followed by one `  <slug>  <title>  (<error>)` line per
    domain. Isolated per domain — one broken domain never costs any other domain its heal.
    A completed pass stamps `last_synced_graph_version` **and** clears the volatile-reload
    flag regardless of whether this domain's own refresh succeeded, so the failure is
    **not** auto-retried on the next ordinary pass — the gate closes again until the graph
    version moves or you pass `--force`. Fix the underlying cause (the descriptor, the
    domain, or the graph), then re-run with `--force` to confirm it cleared.
11. If any slug is held by more than one **live** (`proposed`/`accepted`) domain at once — the
    shape a cross-branch merge race leaves behind (see
    [`reference/store-format.md`](store-format.md#merge-semantics-git-resolves-it-not-the-store)):
    `slug conflicts — drop one (sidegraph-ratify --drop <loser-id>):` followed by one
    `  slug conflict: '<slug>' held by N live domains (<id>, <id>, ...) — drop one` line
    per conflicting slug. This is recomputed live by every `sidegraph-sync` pass (a single
    indexed query — see `Store.domain_slug_conflicts`, not cached anywhere), so it's always
    current, but like the findings above it is never auto-resolved.

### CI-consumable output

`--json` prints `sync.report_as_dict(report)` as one JSON object to stdout — nothing else —
the exact shape the `sync_anchors` MCP tool returns (see
[`reference/mcp-tools.md#sync_anchors`](mcp-tools.md#sync_anchors) for every key): `{"synced",
"from_version", "to_version", "counts", "repointed", "outcomes", "stale_decisions",
"empty_domains", "overbroad_domains", "slug_conflicts", "domains_refreshed",
"domain_failures"}`. `outcomes` is already filtered to `moved`/`orphaned`/`ambiguous`/`error`
(never `unchanged`/`rebound`), same as the prose printer above. `domain_failures` is a list of
`{"slug", "title", "error"}` — one entry per domain whose refresh raised (item 10 above).

`--check` exits **2** when the report has an *attention finding* — an `error` outcome, a
non-empty `stale_decisions`, a non-empty `slug_conflicts`, or a non-empty `domain_failures`
(`sync.report_has_findings`). `orphaned`/`ambiguous` outcomes and `empty_domains`/
`overbroad_domains` are deliberately excluded — informational only, never fail the check on
their own (they're still listed in
`outcomes` in the JSON report for a human to triage on their own schedule, not because CI
is red). The `orphaned`/`ambiguous` exclusion is a live-experiment refinement (design/
superpowers/specs/2026-07-11-ci-live-findings-design.md ruling 1): a legitimate rename+heal
leaves the renamed-away entity's leaf orphaned for good — an append-only store has no
retirement path — and that residue must not keep a healing PR (or the default branch after
it merges) red forever. When an orphaned/ambiguous anchor actually costs reachability (it
was a decision's only live anchor), the decision goes stale and `stale_decisions` already
fires, so the failure signal is covered there instead. `--json --check` compose —
the exit code reflects `--check`'s verdict regardless of whether `--json` was also passed.
`--check` implies `force=True` (`cli.sync_main` passes `force=args.force or args.check`), so
a `--check` run never takes the skip branch — it always runs a real pass against the current
graph, never the empty default `SyncReport(skipped=True)` shape. This matters because the
volatile-reload heal is one-shot per canonical reload: without forcing, an earlier caller
(a `SessionStart` hook, or any retrieval MCP call via `maybe_sync`) could have already
consumed that heal and discarded its report (both lazy callers wrap the call in
`suppress(Exception)`), leaving a plain `sidegraph-sync --check` gated on `graph_version`
and reporting an empty, misleadingly-clean skip. Forcing costs one full rebind pass
(measured ~4.6 ms on this repo's corpus) and closes that hole. A **plain** `sidegraph-sync`
(or `--json` without `--check`) can still legitimately report `"synced": false` when
`graph_version` already matches and no cold-reload flag is pending — that skip is fine there
because nothing is asserting on the report. See
[`guides/ci-cd-maintenance.md`](../guides/ci-cd-maintenance.md) for the full GitHub Actions
recipe (anchor-health required check).

**Exit code:** `0` on success, including the plain (no-`--check`) "up to date" (skipped)
branch, and (with `--check`, which always runs a real pass — see above) a clean report.
Returns `1` if `graph.json` can't be read (`graph not
readable (<path>): <error>`), if the store can't be opened (`store not readable (<path>):
<error>`), or if the rebind pass itself raises an unexpected exception (`sync failed:
<error>`) — these operational-error paths are unchanged by `--json`/`--check`. Returns `2`
with `--check` when the report has an attention finding (see above). Per-entity `error`
outcomes inside an otherwise-completed sync report (step 6 above) do **not**, on their own,
override a clean exit *without* `--check` — that's a report finding about one entity, not a
process failure; the plain (no-`--check`) exit code stays `0` as long as the sync pass
completed and got to stamp `last_synced_graph_version` (an `error` outcome DOES count as an
attention finding under `--check`, per the paragraph above).

**Examples:**

```bash
uv run sidegraph-sync                                 # normal post-rebuild sync
uv run sidegraph-sync --force                         # re-run even if unchanged
uv run sidegraph-sync --db .sidegraph --graph graphify-out/graph.json
uv run sidegraph-sync --json --force                  # CI-consumable report
uv run sidegraph-sync --json --check --force          # exit 2 on any attention finding
```

## `sidegraph-import`

```
sidegraph-import [--db PATH] [--graph PATH] [--kind {adr,lesson,constraint,gotcha}]
                  [--propose] [--dry-run] [--limit N] [--path PREFIX ...]
sidegraph-import --docs PATH [--docs PATH ...] [--profile NAME] [--db PATH] [--graph PATH]
                  [--kind {adr,lesson,constraint,gotcha}] [--propose] [--dry-run]
                  [--limit N] [--tag TAG ...] [--section-limit N] [--any-doc]
sidegraph-import --profile NAME [--db PATH] [--graph PATH]
                  [--kind {adr,lesson,constraint,gotcha}] [--propose] [--dry-run]
                  [--limit N] [--tag TAG ...] [--section-limit N] [--any-doc]
```

Two independent importers behind one command, switched by `--docs` and/or `--profile`:

- **Default mode** bootstraps decisions from Graphify **rationale nodes** — reasoning already
  recorded in your sources, extracted with zero LLM setup from code docstrings/comments (the
  AST pass) or, after a semantic pass, from ADR/SAD prose. See
  [`guides/semantic-docs.md`](../guides/semantic-docs.md) for the full walkthrough and the
  underlying `import_rationales` (`src/sidegraph/importer.py`).
- **`--docs` mode** parses decision-shaped markdown files directly — the parser itself is
  deterministic and LLM-free — and turns each into one anchored `Decision`. The command as a
  whole still requires a current, readable graph: every anchor is resolved against it, and a
  document with nothing anchorable at all is skipped rather than imported (see below). Run
  `graphify update .` right before a `--docs` import so newly-added documents can actually
  anchor. See [Importing decision-shaped markdown](#importing-decision-shaped-markdown---docs)
  below and the underlying `doc_import.import_docs` (`src/sidegraph/doc_import.py`).

| Flag | Default | Meaning |
|---|---|---|
| `--db` | `$SIDEGRAPH_DIR` → existing `.sidegraph/` → `.sidegraph` | store directory; `$SIDEGRAPH_DB` honored for back-compat (deprecated); warns on stderr if the resolved path doesn't exist yet (see above) |
| `--graph` | `$SIDEGRAPH_GRAPH` or `graphify-out/graph.json` | graph.json path |
| `--kind` | `adr` (rationale mode); auto per document (`--docs` mode — see below) | one of `adr`/`lesson`/`constraint`/`gotcha`; passed explicitly, pins every decision imported this run to that kind |
| `--propose` | off | write as `proposed` (ratify gate) instead of `accepted` by default; a `--docs`-mode document whose own status reads as draft/pending review lands `proposed` regardless (see below); under a non-`manual` `SIDEGRAPH_RATIFY_POLICY` an eligible `--propose` write is auto-ratified at write time (see [configuration](configuration.md)) |
| `--dry-run` | off | list what would be imported; write nothing to the store |
| `--limit N` | unlimited | cap the number processed (rationale nodes after `--path` filtering, or markdown files in `--docs` mode); must be `>= 0` |
| `--path PREFIX` | none | only import rationales whose `file_path` starts with `PREFIX` (repeatable); **rationale mode only** — combining with `--docs` is a hard error |
| `--docs PATH` | none | switch to markdown-import mode: `PATH` is a file or a directory (recursed for `*.md`); repeatable. Bare (no `PATH`) imports the active profile's ingest globs — `generic-adr`'s by default, or the profile named by `--profile` — see below |
| `--profile NAME` | `generic-adr` | flow-profile selecting the reader dialect and default ingest globs; also switches to `--docs` mode on its own (repeatable is N/A — a single name); **`--docs` mode only** — see below |
| `--tag TAG` | none | tag every imported/superseded decision with a durable `tag:<slug>` entity (repeatable); **`--docs` mode only** |
| `--section-limit N` | `2000` | per-field char cap for `context`/`choice`/`rejected`/`consequences`, applied after redaction at a word boundary (never mid-word); must be `>= 200`; **`--docs` mode only** |
| `--any-doc` | off | import every enumerated `*.md` file regardless of the active profile's `ingest_globs`; **`--docs` mode only** — see the scope filter below |

**The profile's globs filter explicit `--docs` paths too.** By default a file that matches
none of the active profile's `ingest_globs` is skipped without being parsed and counted as
`outside-profile` in the summary line — including a file you named explicitly. That is easy to
mistake for "nothing here is decision-shaped":

```console
$ sidegraph-import --docs design/0002-some-adr.md --dry-run

would import 0 decision(s), supersede 0 (skipped: 0 existing, 0 unanchorable,
0 not-decision-shaped, 0 unparseable, 0 superseded-frontmatter, 1 outside-profile)
```

A non-zero `outside-profile` count means the filter fired, not that the documents are
unusable. Either name a profile whose globs cover the path (`--profile`, table above), or pass
`--any-doc` to lift the check for the run:

```console
$ sidegraph-import --docs design/0002-some-adr.md --dry-run --any-doc
design/0002-some-adr.md: [imported] 2. Use a content hash as the parser cache key
```

**`--docs` mode's auto `--kind`:** when `--kind` is omitted, each document is classified individually — a doc whose qualifying sections include "Root cause" lands `kind=lesson` (a root-cause doc IS a lesson); every other document lands `kind=adr`. Passing `--kind` explicitly (including `--kind adr`) always overrides this for every document in the run, same as before this behavior existed.

### What gets written (rationale mode)

One `Decision` per rationale node, `kind`/`status` as above:

- **`title`** — the rationale text's first line, redacted, capped at 120 characters.
- **`choice`** — the full rationale text verbatim (redacted) — it *is* the recorded reasoning,
  not a re-summarization.
- **`context`** — `"imported from <file_path> (<node_id>)"`, or `"imported from <node_id>"`
  when the rationale has no `file_path` (cross-cutting rationale with no source file).
- **`provenance`** — `source="import"`, `author="sidegraph-import"`, `ref` = the rationale's
  `file_path` (or its `node_id` when there is no `file_path`), `graph_version` stamped from
  the current graph.

**Anchors:** up to 3 of the rationale node's `rationale_for`/`references` targets, each
independently confirmed `resolved` against the graph (never guess — ambiguous or unresolved
targets are skipped, not orphan-bound). Falls back to the rationale's own source-file node
only when zero targets resolved. A rationale with nothing anchorable at all (no resolvable
target and no resolvable file node) is skipped entirely and counted as unanchorable — no
`Decision` is written for it.

### Idempotency (rationale mode)

Re-running (e.g. after re-extraction adds new rationales) skips a rationale when a
non-superseded decision already exists with the same canonicalized `title`, the same
`provenance.ref`, **and** `provenance.source == "import"`. All three must match: `ref` keys on
the rationale's origin file (or node id), so identical rationale text recorded in *two
different files* is treated as two distinct memories and both import — only a genuine rerun
over the same file's same rationale is deduplicated.

### Output (rationale mode)

**Real run:** `imported N decision(s) (skipped: X existing, Y unanchorable)`. Under a
non-`manual` `SIDEGRAPH_RATIFY_POLICY`: `imported N decision(s), auto-ratified K (skipped: X
existing, Y unanchorable)`, and each failed auto-ratify attempt prints `auto-ratify failure:
<id>: <reason>` on stderr; `--dry-run` never carries the segment.

**`--dry-run`:** one line per candidate — `<file_path or node_id>: <title>` — followed by a
blank line, `would import N decision(s) (skipped: X existing, Y unanchorable, Z filtered)`,
and a per-file breakdown (`  <file_path or '<unknown>'>: <count>`, sorted by path). Nothing is
written to the store; counts reflect exactly what a real run would do.

**Exit code:** `0` on success, including an empty import (nothing new to import) and every
`--dry-run` invocation. Returns `1` before touching the graph or store if `--limit` is
negative (`--limit must be >= 0 (got N)`); returns `1` if `graph.json` can't be read (`graph
not readable (<path>): <error>`) or the store can't be opened (`store not readable (<path>):
<error>`).

**Examples (rationale mode):**

```bash
uv run sidegraph-import --dry-run                              # see volume before writing anything
uv run sidegraph-import --path src/execution/ --limit 50        # filtered import on a large repo
uv run sidegraph-import --propose                               # land as proposed, ratify later
uv run sidegraph-import --kind lesson --db .sidegraph
```

### Importing decision-shaped markdown (`--docs`)

`sidegraph-import --docs <path> [--docs <path> ...]` switches to the second importer
(`doc_import.import_docs`): a deterministic parser (no LLM, no API key) that recognizes
decision-shaped markdown and turns each qualifying document into one anchored `Decision`. The
parsing itself never touches the graph, but the command still requires one — a `GraphifyReader`
is constructed up front, so a missing/unreadable `graph.json` fails the whole run, not just
anchor resolution — and every mention is resolved against whatever graph it reads, so a stale
graph (new docs added since the last build) silently skips those new docs as unanchorable.
**Run `graphify update .` right before a `--docs` import.** `--path` (the rationale-mode
graph-prefix filter) has no meaning against markdown files and is a hard error if combined
with `--docs`.

**`--profile NAME`** selects a *flow-profile* — the per-flow reader dialect (which heading
vocabulary maps onto a decision's context/choice/rejected/consequences) plus its default
ingest globs. Six profiles ship: `generic-adr` (the default) reads ADR/Nygard-style docs and
is intentionally conservative. `superpowers` reads a superpowers spec's free-form `Goal`/
`Architecture`/`Approaches considered`/`Risks` sections and is scoped to
`docs/superpowers/specs/*.md`. `genkovich-sdd` reads a genkovich/sdd feature's `adr/*.md` and
`sad.md` (its `spec.md` is a requirements document and is deliberately not ingested) and is
scoped to `docs/features/*/adr/*.md` and `docs/features/*/sad.md`. `spec-kit` reads a
spec-kit feature's `plan.md` (its `spec.md` is likewise excluded) and is scoped to
`specs/*/plan.md`. `bmad` reads a BMAD architecture run's `ARCHITECTURE-SPINE.md` (its PRD
is a requirements document — excluded like the two `spec.md`s — and the run's `.memlog.md`,
a flat headingless log where BMAD keeps rationale, is deliberately not ingested either) and
is scoped to `_bmad-output/planning-artifacts/architecture/*/ARCHITECTURE-SPINE.md`.
`openspec` reads an OpenSpec change's `proposal.md` (`## Why`/`## What Changes`/`## Impact`)
and `design.md` (`## Context`/`## Decisions`/`## Risks / Trade-offs`) — both live and
archived copies (its main capability specs and delta specs under `changes/*/specs/` are
requirements documents and are deliberately not ingested; `tasks.md` is a checklist) — and
is scoped to `openspec/changes/*/proposal.md`, `openspec/changes/*/design.md`, and their
`openspec/changes/archive/*/` counterparts. Every record parsed from the **live** (not yet
archived) tree gets an extra line appended to its `context`, flagging that it describes an
in-flight change and its task/spec claims should be verified against the code before being
trusted — archived-copy records carry no such note. When `--profile` is given with no
explicit path, the profile's ingest globs (relative to the current directory) are imported;
an explicit path always wins. An unknown name is a usage error (exit 1).

Bare `--docs` with no PATH is legal even without `--profile`: it imports the **active**
profile's ingest globs — `generic-adr`'s (`docs/adr/*.md`, `docs/decisions/*.md`) when
`--profile` is omitted too. The paths it discovers this way are always repo-relative (never
an absolute cwd-anchored path), so `provenance.ref` stays deterministic across machines and
a later equivalent relative `--docs <path>` run dedups against it instead of duplicating a
proposed draft.

**Absolute `--docs` paths need the repo root as cwd.** Doc paths are resolved relative to the
current working directory so they can match `graph.json`'s root-relative `source_file`
entries — so an absolute `--docs` path run from anywhere but the repo root can make every
anchor miss, yielding a silent-looking `0 imported, N unanchorable`. When at least half of
anchor-attempted documents come back unanchorable *and* an absolute path was passed, the
command warns instead of leaving that unexplained:

```
warning: N doc(s) unanchorable — if you passed an absolute --docs path, run sidegraph-import
from the repo root (doc paths are resolved relative to the current directory, so they only
match graph.json's root-relative source_file entries when the current directory IS the repo
root).
```

Run `sidegraph-import` from the repo root to avoid this.

**What qualifies as decision-shaped:** an H1 title, plus either a known decision-section
heading (ADR-style: Context/Decision(s)/Status/Consequences/Rejected/Alternatives/Root cause;
spec-style: Trigger/Design/Residuals/Scope notes/User decisions — matched by case-insensitive
prefix, so a decorated real-world heading like "Design — track the observed…" still matches)
or a bold top metadata line (`**Trigger:**` or `**Status:**`). A document with
`**Status:** superseded`/`deprecated` in its frontmatter is skipped, not imported as live
history. A file that doesn't qualify is counted `skipped_not_decision`, not treated as an
error.

**Mapping:** `title` = the H1 (plain 120-char slice); `context` = the Context/Trigger section
(or the bold `**Trigger:**` line when no such section exists); `choice` = the Decision/Design
section, or the first paragraph after the H1 when neither exists; `rejected` = the
Rejected/Alternatives/Considered-alternatives section, falling back to bold pseudo-heading
prose (e.g. a bare `**Rejected — ...**` paragraph or a `- **Rejected (antipattern) — ...**`
list item) when no real heading exists — `None` when neither is found; `consequences` = the
Consequences section, with the same bold pseudo-heading fallback — `None` when absent. Every
field is redacted before use, same as every other write path, then capped at `--section-limit`
chars (default 2000; `title` is exempt) at a word boundary — a cut field ends with
`" …[truncated]"` so a downstream reader always knows it was cut, never a silent mid-word
amputation. Table cells are not parsed as a source for any field (deferred).

**Status-derived `proposed`:** when the doc's own extracted status (YAML frontmatter
`status:` or a bold `**Status:**` line) reads as draft/proposed/pending/under-review-like
(case-insensitive substring match), the imported decision lands `proposed` **regardless of
`--propose`** — an explicit accepted/approved status, or an absent/unrecognized one, follows
the normal `--propose`-controlled default. The real-run/`--dry-run` report lines say how many
landed `proposed` for this reason specifically (e.g. `N landed proposed (source status:
draft/proposed/pending/under review)`), printed only when `N > 0`.

**Status-derived `rejected`:** when that same extracted status reads as *turned down* — a
word-boundary `reject`/`rejected`, with parenthesised asides stripped and `not`/`un`
negations excluded, so `Rejected in favour of ADR-9999` matches while `Accepted (rejected
alternative: gRPC)` and `rejection criteria defined` do not — the decision lands
`rejected`, **regardless of `--propose`**. It is deliberately not `proposed`: a document
the team refused must not enter the human ratification queue as if it were awaiting
review. It is not skipped either — a rejected proposal, with its reasons, is exactly the
"tried before, abandoned because…" the store exists to keep. The report line is `N landed
rejected (source status: rejected)`. A status that reads as *both* (`proposed, then
rejected`) is terminal, and terminal wins; `superseded`/`deprecated` still skips the doc
entirely, as before.

If a document is re-imported after its status flips to rejected, the adopted record is
**superseded by a rejected successor** rather than edited in place — `accepted → rejected`
is not a legal status transition, and the store's rule is "reversal = `valid_to` on the old
record plus a new one with `supersedes`". Both records stay at the same `ref`, so the run
after that is a clean `skipped_existing`.

**Anchors:** backtick-quoted mentions in the document — a path-like mention (contains `/` or
ends with a known source extension) resolves via the file's own node; an identifier-like
mention (CamelCase/snake_case, >= 4 chars) resolves via the normal entity resolver; ambiguous
or unresolved mentions are skipped, never guessed. Frequency-ranked, capped at 3 resolved
mention-anchors, **plus** the source document's own file node when the graph has one — so a
decision surfaces whenever someone works on the doc it came from, even with zero code
mentions.

**Idempotency:** keyed on `provenance.source == "doc-import"` + `provenance.ref` (the doc
path), comparing `title`/`context`/`choice`/`rejected`/`consequences` against every record at
that ref that is `accepted`, `proposed` **or** `rejected`. An unchanged re-import is skipped
(`skipped_existing`) — including a rejected one, which before this was invisible to the
comparison and so re-imported as a brand-new record on every single run; an edited doc (parsed content changed — including a `consequences`-only
change, or the SAME file re-parsed under a different `--section-limit`) supersedes the old
record, keeping its history retrievable. With `--propose` (or the status-derived override
above), the supersession happens at ratification time instead of immediately.

**Templates are detected and skipped, never imported as decisions.** A document TEMPLATE — a
skeleton meant to be copied and filled in — is recognized by a filename stem of `template` (as
a whole word: `template.md`, `ADR-template.md`, `_ADR-template.md`; a real doc that merely
mentions "template" in its name, e.g. `ADR-012-email-template-engine.md`, does not match), a
`type: template` frontmatter field, or a placeholder-dominated Context/Decision body. A
template is counted separately from `skipped_not_decision` and is never written as a decision,
even when it carries its own `**Status:** APPROVED`-looking line. Both the real run and
`--dry-run` print an extra line whenever `skipped_template > 0`:
`N skipped as template(s) (not decisions)`.

**A degenerate split-produced parent is suppressed, never written (E3).** When a
`split_choice`-capable profile (e.g. `openspec`, `bmad`) splits a section into per-heading
children, the PARENT record is skipped — not written at all — when its own `choice` is
either an echo of its `context` or empty even after every fallback; the children still
import on their own. Both the real run and `--dry-run` print an extra line whenever
`skipped_degenerate_parent > 0`: `N split parent(s) skipped as degenerate (echoed context
or empty choice) — children imported on their own`.

**Real run:** `imported N decision(s), superseded M (skipped: A existing, B unanchorable,
C not-decision-shaped, D unparseable, E superseded-frontmatter, F outside-profile)`, followed
by the status-derived-proposed line above when applicable, followed by the template-skip line
and the degenerate-parent line above when applicable. A non-zero `F` is the profile scope
filter, not a parse failure — see the `--any-doc` note under the flag table above. Under a
non-`manual` `SIDEGRAPH_RATIFY_POLICY`: `imported N decision(s), superseded M, auto-ratified K
(skipped: …)` — only fresh `--propose` writes that land as a new `proposed` record are
eligible; status-derived and `superseded` re-imports never auto-ratify; each failed
auto-ratify attempt prints `auto-ratify failure: <id>: <reason>` on stderr; `--dry-run`
never carries the segment.

**`--dry-run`:** one line per candidate — `<path>: [imported|superseded] <title>`, with any
skipped-anchor reasons indented underneath (`    anchor skipped: <name> (<reason>)`) —
followed by a blank line, `would import N decision(s), supersede M (...)` (same skip breakdown
as the real-run line above), the status-derived-proposed line when applicable (`N would land
proposed (...)`), the template-skip line and the degenerate-parent line above when
applicable, and a per-file breakdown. Nothing is written to the store.

**Exit code:** `0` on success, including an empty run and every `--dry-run` invocation.
Returns `1` before touching the graph or store if `--profile` names an unknown profile
(`unknown flow profile '<name>' (valid: ...)`), if any explicit `--docs` path doesn't exist, if
`--docs`/`--profile` and `--path` are combined, or if `--section-limit` is `< 200`; returns `1`
if `graph.json` can't be read or the store can't be opened.

**Examples (`--docs` mode):**

```bash
uv run sidegraph-import --docs docs/adr --dry-run                # see what would import first
uv run sidegraph-import --docs docs/adr --docs design/            # repeatable, file or directory
uv run sidegraph-import --docs docs/adr --propose --tag adr-backfill
uv run sidegraph-import --docs docs/adr --section-limit 4000      # keep more of each section
uv run sidegraph-import --profile superpowers --dry-run           # profile's own ingest globs
uv run sidegraph-import --docs design/ --any-doc --dry-run        # ignore the profile's globs
```

## `sidegraph-domains`

```
sidegraph-domains bootstrap [--db PATH] [--graph PATH] [--min-members N] [--paths PREFIX ...]
                             [--limit N] [--dry-run]
sidegraph-domains add [--db PATH] --slug SLUG --title TITLE --summary SUMMARY
                       [--parent SLUG] [--path PREFIX ...]
```

Authors [`Domain`](../concepts/data-model.md#domain--the-owned-abstraction) proposals — see the
[naming-your-domains guide](../guides/naming-your-domains.md) for the practical walkthrough.
Two subcommands, the first two of the [three authoring paths](../concepts/mind-model.md#domain-lifecycle):
`bootstrap` (from graph communities) and `add` (manual — the third path, `propose_domains`, is
MCP-only, for in-session agent proposals). Both land `status=proposed`; `sidegraph-ratify` is
the one gate for every authoring path, `sidegraph-domains` included — except that `bootstrap`
under `SIDEGRAPH_RATIFY_POLICY=auto-all` ratifies an eligible candidate at write time (`add`
never does).

### `sidegraph-domains bootstrap`

| Flag | Default | Meaning |
|---|---|---|
| `--db` | `$SIDEGRAPH_DIR` → existing `.sidegraph/` → `.sidegraph` | store directory; `$SIDEGRAPH_DB` honored for back-compat (deprecated); warns on stderr if the resolved path doesn't exist yet |
| `--graph` | `$SIDEGRAPH_GRAPH` or `graphify-out/graph.json` | graph.json path |
| `--min-members N` | `5` | minimum anchorable member count for a community to be proposed; must be `>= 1` |
| `--paths PREFIX` | none | only consider communities with >= 1 anchorable member whose `file_path` starts with `PREFIX` (repeatable) |
| `--limit N` | `100` | cap the number of *significant* communities considered, after threshold/path filtering, in deterministic community-id order; must be `>= 0`; `0` means unlimited — the full list |
| `--dry-run` | off | list what would be proposed; write nothing |

One `Domain` draft per community at or above `--min-members` (counting only anchorable
members): slug/title/summary come from the engine's community label
(`.graphify_labels.json` sidecar) when present, else the community's god-node name, else a
deterministic member digest. Redaction runs on the title/summary/slug regardless of origin.
**Idempotent**: a community already claimed by any non-superseded domain (proposed, accepted,
*or dropped* — see the [naming guide's caveat](../guides/naming-your-domains.md#why-selective-not---all))
is skipped, never re-proposed.

`path_prefixes` is derived only when >= 80% of a community's anchorable members share one
top-level directory, **and** that directory is neither a well-known shared/infra name (`tests`,
`src`, `lib`, `docs`, …) nor already shared (the community's own membership included) by more
than 20% of *all* communities in the graph — either guard tripping leaves `path_prefixes` empty
rather than a stabilizer that could let a later `sidegraph-sync` silently expand the domain onto
unrelated communities. See the
[naming guide's guardrails](../guides/naming-your-domains.md#guardrails-against-an-over-broad-path-rule)
for the full rationale.

**Scale-aware default `--limit` (100):** on a monorepo-scale corpus, considering every
significant community is expensive and the resulting batch is unreviewable (a real
cross-project finding: Apache Airflow has 2,578 significant communities). `--limit` defaults
to `100` — the same default the `list_domain_candidates` MCP tool applies, so the two never
disagree about what "a normal run" proposes — capping both a real run's write and
`--dry-run`'s listing to the top 100 communities in deterministic community-id order. Pass an
explicit `--limit N` to widen it, or **`--limit 0` for the full, unbounded list** (the "all"
convention). Re-running under the same default/explicit limit only ever reaches the same
community-id-sorted window — communities past it are skipped as "already proposed" only once
you've actually widened `--limit` (or narrowed with `--min-members`/`--paths`) far enough to
reach them.

**Output, non-dry-run:** `proposed N domain(s) (skipped: X existing)`. Under
`SIDEGRAPH_RATIFY_POLICY` other than `manual`: `proposed N domain(s), auto-ratified K
(skipped: X existing)` (`N` still counts every write), plus `auto-ratify failure: <id>:
<reason>` on stderr per failed attempt. Two independent stderr
notes can precede the write (both computed from a read-only pre-check, before anything is
written — see below): when `--limit` (default or explicit) actually cut candidates, `note: M
significant communities found — showing only the top N (community-id order); pass --limit 0
for the full list, a higher --limit N, or narrow with --min-members/--paths`; separately, when
the number about to be proposed exceeds 50, `note: about to propose N domains — consider
--min-members/--limit and ratify selectively`.

**Output, `--dry-run`:** one line per candidate — `<community_id>: <slug> — <title>` — followed
by `would propose N domain(s) (skipped: X existing, Y below threshold, Z filtered)`. Nothing is
written. The same `--limit`-truncation note as above prints on stderr when it applies; the
"ratify selectively" nag does not (dry runs are for tuning `--min-members`/`--limit` before
committing to a real run — see the
[naming guide](../guides/naming-your-domains.md#2-bootstrap-as-a-dry-run-first)).

**Before-write ordering:** both stderr notes are computed from a separate, read-only
`collect_domain_candidates` call made *before* `bootstrap_domains` writes a single record —
never printed only after the fact once thousands of records are already on disk.

**Exit code:** `0` on success, including every `--dry-run` invocation. Returns `1` before
touching the graph or store if `--min-members < 1` or `--limit < 0`; returns `1` if
`graph.json` can't be read or the store can't be opened.

### `sidegraph-domains add`

| Flag | Default | Meaning |
|---|---|---|
| `--db` | `$SIDEGRAPH_DIR` → existing `.sidegraph/` → `.sidegraph` | store directory; `$SIDEGRAPH_DB` honored for back-compat (deprecated) |
| `--slug` | required | kebab-case (`^[a-z0-9][a-z0-9-]*$`) |
| `--title` | required | short label, non-empty |
| `--summary` | required | the WHY-IT-EXISTS prose, non-empty |
| `--parent SLUG` | none | must resolve to an existing (non-superseded) domain via `find_domain_by_slug`; unresolvable → hard error |
| `--path PREFIX` | none | `path_prefixes` stabilizer/bootstrap membership rule (repeatable) |

Mirrors the `add_domain` MCP tool — always lands `status=proposed`. Draft shape (slug/title/
summary validity) is validated *before* any store is opened, so a bad `--slug` never creates an
empty store as a side effect. A slug collision against a *live* (proposed/accepted) domain is
reported as a skip, not a hard failure.

**Output:** `proposed 1 domain(s) (skipped: 0 existing)` followed by `<domain_id>  <slug>` on
success; `proposed 0 domain(s) (skipped: 1 existing — <error>)` on a slug collision (exit `0`
— an expected outcome, not a failure, same treatment as bootstrap's idempotency skip).

**Exit code:** `0` on success or a slug-collision skip. Returns `1` only for an unreadable
store, an unresolvable `--parent`, or an invalid draft (e.g. non-kebab-case `--slug`).

**Examples:**

```bash
uv run sidegraph-domains bootstrap --dry-run                          # tune before committing
uv run sidegraph-domains bootstrap --min-members 15 --paths src/execution
uv run sidegraph-domains bootstrap --limit 0                          # "all" -- no default cap
uv run sidegraph-domains add --slug risk-gate --title "Risk gate" \
  --summary "Pre-trade checks that block an order before it reaches the exchange." \
  --path src/risk
uv run sidegraph-domains add --slug order-lifecycle --parent risk-gate \
  --title "Order lifecycle" --summary "State machine from submit to fill/cancel."
```

## `sidegraph-compact`

```
sidegraph-compact [--db PATH] [--older-than N] [--dry-run]
```

| Flag | Default | Meaning |
|---|---|---|
| `--db` | `$SIDEGRAPH_DIR` → existing `.sidegraph/` → `.sidegraph` | store directory; `$SIDEGRAPH_DB` honored for back-compat (deprecated) |
| `--older-than N` | unlimited | only compact records that have been in a terminal state for at least `N` days (uses a decision's `valid_to`; domains carry no terminal timestamp at all and are conservatively excluded — kept hot — whenever this flag is set); must be `>= 0` |
| `--dry-run` | off | list what would be compacted (and any interrupted-prior-run leftovers that would be cleaned up); write nothing |

Packs every **terminal**-status record — decisions `superseded`/`rejected`/`deprecated`,
domains `superseded`/`dropped` — into a new immutable
`archive/<date>-<seq>-<hash12>.jsonl` segment and removes their now-redundant hot files, in
one operation. `proposed`/`accepted` records are never touched. Explicit, human-run
maintenance: recommended periodically on the default branch; never invoked by
sync/retrieval/ratify. See
[`reference/store-format.md`](store-format.md#archive-segments-sidegraph-compact) for the
segment format and the append-only guarantee this preserves (records *move*, never disappear).

**Crash/interrupt safety:** a prior run that durably wrote its segment but was interrupted
before removing the now-redundant hot files leaves those as harmless leftover duplicates. This
run detects and removes them without writing a second, duplicate segment — counted separately
in the output as "leftover hot file(s)".

**`--dry-run` output:** one line per candidate — `<ulid>  <kind>  <status>  <title>` — or a bare
`nothing to compact` if there is nothing eligible at all (no candidates, nothing age-filtered
out, no leftovers). Otherwise: `would compact N record(s) (X decisions, Y domains); Z skipped
(not terminal enough)`; if `--older-than` excluded any domains outright (no terminal timestamp
to check), an additional line reports `M domain(s) excluded: terminal age unknown`; if a prior
interrupted run left redundant hot files behind, an additional line reports `would also remove
K leftover hot file(s) already durably archived by a prior, interrupted compact run`.

**Real-run output:** when there was nothing to do at all, a single line —
`nothing to compact` (bare), or `nothing to compact; Z skipped (not terminal enough)` and/or
`; M domain(s) excluded: terminal age unknown` appended when either applies. Otherwise:
`compacted N record(s) into <segment_path> (X decisions, Y domains); Z skipped (not terminal
enough)` — or `compacted 0 record(s); Z skipped (not terminal enough)` when only leftover
cleanup happened and no new segment was written — followed by an `M domain(s) excluded:
terminal age unknown` line and/or a `cleaned up K leftover hot file(s) from an interrupted
prior compact run` line, each only when applicable.

**Exit code:** `0` on success, including a no-op run and every `--dry-run` invocation. Returns
`1` before touching the store if `--older-than` is negative (`--older-than must be >= 0 (got
N)`); returns `1` if the store can't be opened (`store not readable (<path>): <error>`).

**Examples:**

```bash
uv run sidegraph-compact --dry-run                 # see what would move before writing anything
uv run sidegraph-compact                            # pack every terminal record
uv run sidegraph-compact --older-than 90             # only records terminal for 90+ days
```

## `sidegraph-verify`

```
sidegraph-verify [--db PATH] [--against GIT_REF] [--json]
```

| Flag | Default | Meaning |
|---|---|---|
| `--db` | `$SIDEGRAPH_DIR` → existing `.sidegraph/` → `.sidegraph` | store directory; `$SIDEGRAPH_DB` honored for back-compat (deprecated) — but see "Exit code" below: unlike every other command on this page, a missing directory here is a hard failure, never something this command creates |
| `--against GIT_REF` | none | also run the transition layer: classify every store file that changed vs `GIT_REF` against the store's own write-path rules (git plumbing — `git diff`/`git show`; CI mode) |
| `--json` | off | print `{"clean": bool, "violations": [{"code", "path", "detail"}, ...]}` as one JSON object to stdout (nothing else) |

Lints the store's canonical files against the invariants `store.py`'s write API enforces at
write time. **Pure read** — opens the canonical JSON files directly and never constructs a
`Store`, so a lint run can never itself write to `index.db`, migrate a legacy store, or
mutate anything it inspects (see [`reference/store-format.md`](store-format.md) for the
on-disk layout this walks). Two layers, combined into one report:

- **Snapshot layer** (always runs): every *hot* record file parses against its schema (a
  `Model.model_validate`, not just JSON decoding) — archived entries are JSON-parsed and
  cross-referenced (supersedes chains, ULID uniqueness) but the lint stops short of
  schema-validating them; a schema-invalid archived payload would instead surface at the
  next cold `Store` reload, which does model-validate archived records; the `format` marker's
  `schema_version` is present and known; `valid_to >= valid_from` on every decision/fact; a
  `superseded` record's `supersedes` chain resolves to a real successor; every `supersedes`
  target exists; every binding references an existing entity; every fact's `supports`
  references an existing *decision* (never a fact — `Fact.supports` only ever holds decision
  ids); internal ids (ULIDs) are unique across hot files *and* archive segments; archive
  segments parse as JSONL; every hot record file is named `<its own internal id>.json` (the
  store always writes that).
- **Transition layer** (`--against GIT_REF`, additive): classifies every store file that
  *changed* vs `GIT_REF` (compared against the current working tree — not two fixed
  points) against the store's own write-path rules, derived from `store.py` rather than a
  blanket "committed files never change" lint, which would be wrong: a real supersede
  legally closes a predecessor, `ratify` legally flips statuses, `sidegraph-sync` legally
  refreshes an entity's `descriptor`/community mapping, `sidegraph-compact` legally deletes
  a hot file once archived. See "What's legally mutable" below for the exact table.

**Violation codes** (the `code` field in `--json`'s `violations`, or the first column of a
plain-text line):

| Code | Layer | Meaning |
|---|---|---|
| `parse-error` | snapshot | a record file doesn't decode as JSON, isn't an object, or fails schema validation (a `valid_to < valid_from` failure is reported as `bad-validity-window` instead — see below) |
| `unknown-schema-version` | snapshot | the `format` marker is missing, unreadable, malformed, or names a `schema_version` this version of the code doesn't recognize |
| `bad-validity-window` | snapshot | `valid_to < valid_from` on a decision or fact |
| `superseded-without-successor` | snapshot | a `status=superseded` record has no other record's `supersedes` pointing back at it |
| `dangling-supersedes` | snapshot | a `supersedes` id doesn't resolve to any record, hot or archived |
| `dangling-binding-entity` | snapshot | a binding's `entity_id` doesn't resolve to any `entities/<id>.json` |
| `dangling-fact-support` | snapshot | a fact's `supports` id doesn't resolve to any decision |
| `duplicate-ulid` | snapshot | the same internal id appears in more than one canonical location — two hot files, a hot file plus any archive copy, or two archive segments whose payloads for that id actually differ. **Exempt:** two or more archive segments carrying byte-*identical* payloads for the same id — a sanctioned cross-branch `sidegraph-compact` merge (independent compaction on two branches, later merged) — see [`reference/store-format.md#archive-segments-sidegraph-compact`](store-format.md#archive-segments-sidegraph-compact) |
| `bad-archive-segment` | snapshot | an `archive/*.jsonl` line doesn't parse as JSON, or isn't a JSON object |
| `filename-id-mismatch` | snapshot | a hot record file's name doesn't match its own internal id |
| `illegal-field-change` | transition | an immutable field changed between `GIT_REF` and the working tree (the detail names the field); also covers a modified already-published archive segment (write-once) |
| `illegal-status-jump` | transition | `status` changed to something that isn't a real write-path transition for that record kind |
| `valid-to-unset` | transition | `valid_to` reverted from a value back to `null` |
| `valid-to-changed` | transition | `valid_to` changed from one value to a *different* value (only `null → value`, once, is legal) |
| `illegal-deletion` | transition | a record file — or an already-published archive segment — was deleted with no sanctioned reason. The one exception: a decision/domain whose id is present in an `archive/*.jsonl` segment in the new tree (a legitimate `sidegraph-compact`) |

**What's legally mutable** (transition layer only; derived from `store.py`'s write methods,
not invented — see `verify.py`'s module docstring for the line-numbered derivation):

- **Decisions:** `status` (only along a real transition — `proposed→accepted`,
  `proposed→rejected`, `proposed→superseded`, `accepted→superseded`, `accepted→deprecated`),
  `valid_to` (`null → value`, once). Everything else (`title`, `kind`, `context`, `choice`,
  `rejected`, `consequences`, `layer`, `valid_from`, `supersedes`, `provenance`) is immutable.
- **Facts:** the same shape, `status`/`valid_to`, but **without** `accepted→deprecated`
  — no live write path ever sets a fact `deprecated` (`schema.py`: "DEPRECATED unused for
  facts", unlike the decision-side forward-compat carve-out above), so that jump is *not*
  legal here and is flagged `illegal-status-jump` if it appears. Legal: `proposed→accepted`,
  `proposed→rejected`, `proposed→superseded`, `accepted→superseded`. Everything else
  (`statement`, `source`, `supports`, `valid_from`, `supersedes`, `provenance`) is immutable.
- **Domains:** `status` (`proposed→accepted`, `proposed→dropped`, `accepted→dropped`,
  `proposed→superseded`, `accepted→superseded`, `dropped→superseded`), `communities`.
  Everything else (`domain_id`, `slug`, `title`, `summary`, `parent_id`, `path_prefixes`,
  `seed_anchors`, `provenance`) is immutable.
- **Entities:** `descriptor`, `last_seen_node_id`, `last_seen_graph_version`,
  `last_seen_community`. Everything else (`entity_id`, `canonical_name`, `kind`) is
  immutable.
- **Bindings:** referential integrity only — any change to a `bindings/<record_id>.json`
  file, including deletion, is never flagged here (machine-managed payload; the snapshot
  layer's `dangling-binding-entity` check covers correctness of the new state).
- **Initiatives:** no append-only guard in `store.py` at all (`Store.upsert_initiative` is a
  bare, unconditional upsert) — only a *deletion* is flagged (`illegal-deletion`); no
  field-level check runs for initiatives.
- **Archive segments** (`archive/*.jsonl`): write-once — a brand-new segment (git status
  `A`) is legal; any modification or deletion of an already-published one is always
  illegal.

**`--against <ref>` diffs the CURRENT WORKING TREE against `<ref>`'s snapshot — not two fixed
points in history.** Passing a moving branch name directly (`--against origin/main`) is only
correct when your checkout's `HEAD` actually sits at that branch's tip; on a PR branch that's
behind it, the diff also picks up main-side transitions the PR branch never touched, which
show up as illegal-looking reversals. Pass a merge base instead in CI —
`--against "$(git merge-base origin/main HEAD)"` — see
[`guides/ci-cd-maintenance.md`](../guides/ci-cd-maintenance.md) for the full recipe (store
lint on PR) and why the false positive happens.

**Exit code:** `0` clean (snapshot layer, and the transition layer too when `--against` is
given, report zero violations). `1` **operational error** — the store directory doesn't
exist or isn't readable, `--against` names an unresolvable ref, or `--db` isn't inside any
git repository at all (never a violation — see design ruling 2: "not-a-git-repo / unknown
ref → operational error"). **This is the one command on this page that does NOT
auto-create a missing store:** every other CLI here opens the store via `Store(...)`, which
idiomatically creates an empty, schema-stamped store on first use; `sidegraph-verify` never
constructs a `Store` at all, so a missing/non-directory `--db` raises instead — a lint has
nothing to lint if there's nothing to open, and silently reporting "clean" against a store
it just created would be actively misleading in CI. `2` violations found (printed one line
each as `<code>  <path>  <detail>`, or in `--json`'s `violations` list).

**Examples:**

```bash
uv run sidegraph-verify                                                 # snapshot layer only
uv run sidegraph-verify --json                                          # CI-consumable
uv run sidegraph-verify --against HEAD~5                                # local: last 5 commits' store edits
uv run sidegraph-verify --against "$(git merge-base origin/main HEAD)" --json   # CI-safe transition check
```

## `sidegraph-doctor`

When any record carries the `ratified_at` stamp (2026-08-04+), the human output adds an informational `time-to-ratify: median N days, max M days (K stamped record(s))` line — queue latency derived from `ratified_at − valid_from`; never a finding, never in `--json` (its key set is pinned), absent when no record is stamped. Records stamped `auto:<policy>` are excluded (their `ratified_at` ≈ `valid_from` would collapse the median), so `K` counts human stamps only.

Once any record, hot or archived, carries an `auto:` stamp, two more informational lines follow: `auto share: decisions A/B (P%), facts A/B (P%), domains A/B (P%) (E stamp-less record(s) after the first stamp excluded)` — records ever auto-ratified, whatever their status now, over all classified records of the kind — and `auto supersede rate: decisions+facts superseded n/d (p%) vs human n/d (p%); domains retired n/d (p%) vs human n/d (p%) (E stamp-less record(s) after the first stamp excluded)` — auto-ratified records later superseded (domains: dropped or superseded) beside the human-ratified baseline. A zero denominator prints `n/a`; percentages round down. An unstamped accepted or retired record counts as human baseline only if created before the store's earliest stamp; later ones are excluded and counted in the parenthetical. Both lines read hot files plus `archive/*.jsonl`, are never findings, never change the exit code, and never appear in `--json`.

```
sidegraph-doctor [--db PATH] [--against GIT_REF] [--check] [--stale-days N] [--json]
```

| Flag | Default | Meaning |
|---|---|---|
| `--db` | `$SIDEGRAPH_DIR` → existing `.sidegraph/` → `.sidegraph` | store directory; `$SIDEGRAPH_DB` honored for back-compat (deprecated) — same never-auto-create rule as `sidegraph-verify` (see above) |
| `--against GIT_REF` | none | also run `sidegraph-verify`'s transition layer vs `GIT_REF` (identical semantics — same operational-error rules) |
| `--check` | off | advisory findings also exit `2` (default: report only; a *skipped* check never fails, even with `--check`) |
| `--stale-days N` | `30` | flag a proposed decision/domain whose id timestamp is strictly older than `N` days; must be `>= 0` |
| `--json` | off | print `{"clean", "violations", "findings", "skipped"}` as one JSON object to stdout (nothing else) |

One-stop store health: composes `sidegraph-verify`'s strict snapshot (+ optional
`--against` transition layer) with an advisory curation lint, rather than reimplementing
either. **Pure read**, same guarantee as `sidegraph-verify` — never writes to the store,
never constructs a `Store`.

- **Strict section** — `verify.verify_snapshot` (+ `verify.verify_against` when
  `--against` is given): exactly what `sidegraph-verify` runs, same violation codes (see
  [`sidegraph-verify`](#sidegraph-verify) above) and the same operational-error rules — a
  missing store is never auto-created; a bad git ref or a store outside any git repo is
  exit `1`, never a violation.
- **Advisory section** — `doctor.curate`: curation findings that never gate the exit code
  by default; `--check` escalates them to exit `2`, mirroring `sidegraph-sync --check`.

**Finding codes** (the `code` field in `--json`'s `findings`, or the first column of a
plain-text line):

| Code | Meaning |
|---|---|
| `dangling-record` | an open decision (`proposed`/`accepted`) has no committed anchor set; an open fact (`valid_to` unset) has no anchor AND no `supports` id resolving to a LIVE (`proposed`/`accepted`) decision — an anchor alone, or a supports id resolving to a live decision, is reachability for a fact |
| `degraded-binding` | a binding's last recorded status (from `index.db`) is `degraded` |
| `orphaned-binding` | a binding's last recorded status (from `index.db`) is `orphaned` |
| `stale-proposal` | a proposed decision/domain whose ULID timestamp is strictly older than `--stale-days` |
| `unreferenced-entity` | a hot entity no committed anchor set references (structural `domain:*`/`community:*` entities are exempt) |
| `expired-open-validity` | a decision's `valid_to` is strictly past but its status is still open (`proposed`/`accepted`) |
| `code-drift` | a live (`proposed`/`accepted`) decision's anchored file(s) changed since the commit its `provenance.commit` was captured at; also carries the "N record(s) predate commit stamping — not checkable" and git-unavailable notes, in place, when either applies |
| `never-surfaced` | an accepted decision has `0` shows against `N > 0` queries on its anchored file(s) — anchored where people repeatedly work, never once rendered |
| `stale-instructions` | an auto-injected agent-instructions file beside the store (`CLAUDE.md`, `AGENTS.md`, `GEMINI.md`, `.cursorrules`) still carries the distinctive wording of a **superseded** decision — that file reaches the agent at turn zero, before retrieval, so an abandoned rule surviving there outranks the store's correction by default. Verbatim-ish match on normalized text with length/word floors; superseded records only; no git call. Advisory: it reports a quotation, not a proven contradiction — the file may be narrating history |
| `unratified-accept` | an **accepted** record whose provenance says `agent` and which carries no ratifier stamp — the signature `SIDEGRAPH_AUTO_ACCEPT=on` leaves in a shared store (one environment's setting bypassing the ratification gate for everyone). Scoped to records created at/after the EARLIER of the store's earliest ratifier stamp and its creation marker (`.sidegraph/stamping_live_since`, written once when a store is genuinely new — never backfilled onto an existing one), so a store predating both reports nothing rather than everything; a store that has never ratified anything but was created by a marker-writing version is still in scope from its own creation, closing the blind window an unratified store would otherwise sit in forever. A stamped record — including one stamped `auto:<policy>` by an auto-ratification policy — and a human-sourced one are never flagged. A **supersession successor** (`supersedes` set — `supersede_decision`/`supersede_fact`) is never flagged either: reversal has no ratification queue to bypass and lands accepted immediately by design, so it reproduces the signature with no relation to the env var this check hunts. Narrowed exception: a `supersedes`-bearing record IS still flagged if it carries a `layer`, a `provenance.ref`, or a tier-0 `tag:`/`initiative:` binding absent from its predecessor — neither `supersede_decision` nor `supersede_fact` can produce any of the three, so one present proves the `propose_decisions`/auto-accept path wrote it. (The fact half is defensive only: `supersede_fact` always stamps `source="human"`, so a fact successor never reaches this agent-sourced check, and no draft shape carries `supersedes` on a fact.) Only a MINIMAL superseding draft (none of the three) is indistinguishable from a legitimate successor by the record's own fields, and stays excluded — a known, accepted, narrower trade-off than treating every supersession alike. Advisory: the signature is not proof — verify before concluding |
| `duplicate-entity` | two or more committed entity files share one logical identity (legal — see below — but ambiguous); reported once per group, at the winner's (lowest-`entity_id`) file, naming every id in the group and its binding count |

`degraded-binding`/`orphaned-binding` come from `index.db`, opened strictly read-only — it
is the only source for binding status (canonical `bindings/*.json` files carry identity,
not status; see [`reference/store-format.md`](store-format.md)) — so they read "as of last
sync." When `index.db` is missing or unusable (a fresh clone before the first
`sidegraph-sync`, or a corrupt index), that one check is **skipped**, not failed: its name
(`binding-status`) is reported in `skipped` and it affects neither the exit code nor
`clean`, even under `--check`.

`never-surfaced` reads the same read-only `index.db` for two more derived, gitignored
tables — `retrieval_shows`/`retrieval_seeds`, retrieval telemetry (see [Retrieval
telemetry](store-format.md#retrieval-telemetry)) — and is **skipped** the same way, under
its own name, when `index.db` is missing or unusable: a store nobody has queried yet has no
evidence of dead memory, and reporting one would flag a healthy store on its first day. Only
`0 shows` against `N > 0` queries is ever reported; `0` shows against `0` queries means
nobody worked in that area (an absence of occasion, not a defect) and stays silent. Set
`SIDEGRAPH_TELEMETRY=off` to stop recording the counts this check reads (see the
environment variable list below).

`duplicate-entity` is a LEGAL state, not corruption: two branches that each mint the same
logical entity (e.g. the same abstract tag, or the same concrete name + file) produce two
different ULIDs, so two different files, which git merges cleanly — the store format is
built to make that merge conflict-free. A UNIQUE index on identity was considered and
rejected for exactly this reason: its rebuild runs inside `Store`'s own open, so it would
raise on a legally-merged store and make it unopenable. Instead, every lookup
(`find_entity`/`find_abstract_entity`/get-or-create) resolves a duplicate deterministically
— the lowest `entity_id` wins, stable across processes and reopens — so this finding exists
to tell a human the group exists and let them decide (there is no automatic merge tool)
which id the others' bindings should be re-pointed at.

The denominator is **file paths**, so the check only sees decisions with at least one
file-anchored binding. A decision anchored purely to abstract entities (`domain:*`,
`initiative:*`, `tag:*`) has no path that could have been queried and is never reported —
neither flagged nor cleared. `drill_down` does record a `domain:<slug>` seed, but nothing
consumes it yet; its *shows* count normally, so surfacing only through a domain drill-down
still protects a decision from a false flag.

`clean` is `true` only when both `violations` and `findings` are empty.

**Exit code:** `0` healthy — advisory findings alone stay `0` without `--check`. `1`
operational error — same paths as `sidegraph-verify` (unreadable store, bad `--against`
ref, store outside any git repo), plus a negative `--stale-days`, rejected before the
store is touched (same hard-usage treatment as `sidegraph-import --limit`). `2` strict
violations, or — with `--check` — advisory findings too.

**Examples:**

```bash
uv run sidegraph-doctor                              # verify + curate, human-readable
uv run sidegraph-doctor --json                       # CI-consumable
uv run sidegraph-doctor --check                      # also fail on advisory findings
uv run sidegraph-doctor --stale-days 14               # tighter ratification-queue threshold
uv run sidegraph-doctor --against "$(git merge-base origin/main HEAD)"   # + transition layer
```

## `sidegraph-viz`

Render a **read-only** interactive graph of the owned decision/fact store — a diagnostic view
of what is anchored where, what is orphaned or degraded, and how supersede chains and
fact→decision links look. It writes a self-contained offline HTML (vis-network is vendored
inline — no network needed to open it) plus a machine-readable JSON sibling.

```bash
sidegraph-viz [--db PATH] [--out sidegraph-graph] [--open] \
              [--only-problems] [--no-superseded] [--max-nodes N] [--json]
```

- `--out BASE` — writes `BASE.html` and `BASE.json` (default `sidegraph-graph`).
- `--open` — open the written HTML in your browser.
- `--only-problems` — restrict to the subgraph touching a degraded/orphaned binding or a
  dangling (unanchored) record.
- `--no-superseded` — omit terminal-status records (shown dimmed by default; history is the
  product).
- `--max-nodes N` — cap the node count; excess is truncated lowest-priority-first and the drop
  is reported (never silent). Default 800.
- `--json` — print the `{nodes, edges, stats}` JSON to stdout and write no HTML.

The command never writes to the store and never touches `graph.json`. Binding status
(live/degraded/orphaned) reflects the last `sidegraph-sync`; run sync first for a fresh view.
Exit code 0 on success (a store with problems still exits 0 — the problems are the output), 2
on an operational error (uninitialized store or unwritable output path).

## `sidegraph-export-okf`

```bash
uv run sidegraph-export-okf                      # writes ./okf-bundle/
uv run sidegraph-export-okf --db .sidegraph --out /tmp/bundle
```

Projects the decision store into an [Open Knowledge Format v0.1](https://github.com/GoogleCloudPlatform/knowledge-catalog/tree/main/okf)
bundle — a directory of markdown concept files with YAML frontmatter whose cross-links
form a graph — so any OKF-aware agent or tool can read Sidegraph's decision memory
without knowing anything about the store format or MCP. Strictly **one-way**: the bundle
is a derived artifact (like `sidegraph-viz` output); the store stays the source of truth.
There is no importer and never will be.

What lands in the bundle:

- `decisions/` and `facts/` — every **ratified** record, full append-only history:
  superseded, rejected, and deprecated records ship too, with status/validity in
  frontmatter and `Supersedes` / `Superseded by` links in a `# History` section. "Tried
  before, abandoned because…" is the product; the export doesn't hide it. The one thing it
  does hide is unratified `proposed` drafts — those have not entered the shared memory yet,
  so they are never published (proposed domains are dropped the same way).
- `domains/` — one concept per slug (the latest revision row wins), with parent links
  and an `# Anchored records` backlink section.
- `entities/` — only entities actually referenced by an exported anchor; each page lists
  every decision/fact anchored to it. Bindings to `domain:*` entities link to the domain
  page instead; volatile `community:*` bindings are not exported at all.
- `index.md` per directory (progressive disclosure) and a root `log.md` chronology.

Output is **deterministic** — the same store exports byte-identically, so the bundle can
be committed and diffed. `--out` (default `okf-bundle`) is only ever cleared when it is
empty or carries the `generator: sidegraph` marker from a previous export; any other
non-empty directory is refused. Exit `0` on success, `2` on an operational error
(uninitialized store — never auto-created — or a refused/unwritable out dir).

## `sidegraph-prepare-commit-msg`

```
sidegraph-prepare-commit-msg <message-file> [<source> [<sha1>]]
```

Not something you run directly — it's wired up as git's own `prepare-commit-msg` hook
(see [`reference/git-bindings.md`](git-bindings.md) for installation, both raw-git and the
`pre-commit` framework). Takes no flags of its own: git invokes it with the message-file
path plus git's own `<source>`/`<sha1>` positional args. Comments candidate
`Sidegraph-Decision:` trailers into the commit message template — captured-this-session
records plus decisions anchored to files you've staged — for you (or your agent) to
uncomment; never auto-appends one. Acts only when `<source>` is absent (a plain `git
commit`) or `template`; every other source, and any internal error, leaves the message
file untouched. Never blocks and never stalls: always exits `0`, bounded by a 2-second
wall-clock budget and a read-only, short-timeout store open.

## `sidegraph-blame`

```
sidegraph-blame <file> [--range A,B] [--db PATH] [--json]
```

| Flag | Default | Meaning |
|---|---|---|
| `--range A,B` | whole file | blame only lines A through B (`git blame -L A,B`) |
| `--db` | shared store-path resolution | canonical Sidegraph store directory |
| `--json` | off | print the hunk/record table as one JSON object to stdout |

`git blame`, joined to the decisions and facts each hunk's commit carries — see
[`reference/git-bindings.md`](git-bindings.md) for the join mechanics (commit trailers +
`provenance.commit`), the output cap, and a worked example. Read-only over records: opens
the store's index read-only and never writes to it; a missing or unreadable store degrades
every hunk to unresolved records rather than failing. Exit `0` on success (including when
every hunk is unresolved), `1` on an operational git failure (not a git repo, unknown
file/range, git not installed, or a malformed `--range`).
