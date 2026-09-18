# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning follows
[SemVer](https://semver.org/) (pre-1.0: minor bumps may break interfaces). Which
interfaces, exactly, and what each one promises: [`docs/reference/stability.md`](docs/reference/stability.md).

## [Unreleased]

## [0.2.0] — 2026-09-18

### Added

- **`sidegraph-init` asks before auto-ratifying, instead of writing the policy silently.**
  In an interactive terminal it now asks one question, ratify low-risk records
  automatically, default answer yes (`auto-low-risk`), and commits whichever answer the
  person gives to the project's `.claude/settings.json` explicitly, so the choice is
  visible and changeable later. Outside a terminal (CI, a script, an agent-driven session)
  it asks nothing and writes nothing, printing the one line to add by hand instead: a
  silent write with nobody to answer is exactly what this avoids. An already-set policy,
  or an unparseable settings file, is reported directly with no prompt, since any write
  would be a no-op regardless of the answer. Two new flags make a scripted setup possible
  with no prompt: `--ratify-policy VALUE` sets an explicit value, and `--no-settings`
  skips the step entirely (the two are mutually exclusive). An existing value is never
  overwritten, other keys and file shape are preserved, and no settings problem can fail
  store creation. The README also drops its `sidegraph-import` mentions: that command's
  rationale-node import is undocumented for now, pending a review of the comment junk it
  currently writes into the store. See
  [`docs/reference/cli.md`](docs/reference/cli.md) and
  [`docs/reference/configuration.md`](docs/reference/configuration.md).
- **`sidegraph-doctor` gains a `graph-root-mismatch` finding and a `--graph` flag.** When
  `graphify update` runs from a subdirectory instead of the repo root, every anchor
  descriptor stops matching the graph's own `source_file` values, and sync used to mass
  orphan the whole store with no explanation. `sidegraph-doctor --graph <path>` samples the
  graph's anchorable paths and, only when a high fraction are missing relative to the repo
  root and one subdirectory resolves them all, reports the mismatch by name instead of
  leaving a person to hunt through false orphans.
- **`.github/secret_scanning.yml`** excludes the redaction regression suite's seeded fake
  secrets from GitHub's own secret scanning, so the fixtures that prove Sidegraph's
  redaction works stop tripping GitHub's scanner on every push.
- **Test suite hermeticity**: `tests/conftest.py` now strips every `SIDEGRAPH_`-prefixed
  environment variable before each test, so a developer's own shell (an exported
  `SIDEGRAPH_RATIFY_POLICY`, `SIDEGRAPH_TRUST_DIRTY_TREE`, and so on) can no longer change
  what the suite reports.
- **Session-link gate**: a `commit-msg`-stage pre-commit hook (`no-session-links`, backed
  by `tools/check_no_session_links.py`) rejects a commit message carrying a
  `Claude-Session:` trailer, a claude.ai/chatgpt.com session URL, or a bare `session_<id>`
  token. `default_install_hook_types` now wires both the `pre-commit` and `commit-msg` git
  hook types on a plain `pre-commit install`, and CI's new `pr-description-link-gate` job
  applies the same rule to a pull request's description, which a commit hook cannot see.
  Ordinary links (a CVE/GHSA advisory, an issue or PR, vendor docs) stay allowed.

### Changed

- **`sidegraph-sync`'s "moved" rung now requires committed evidence before it rewrites an
  entity's descriptor.** The rung used to decide a symbol moved from the working tree
  alone: the old path gone from disk, plus a unique same-suffix name match elsewhere, both
  satisfiable by purely local, uncommitted state such as an unstaged delete or a stash. On
  a dirty tree that could silently write one person's local, unshared state into the
  canonical descriptor every other clone reads from the shared store, which was the only
  known path by which one person's tree could corrupt the whole team's decision memory.
  The rung now also requires the same move to be confirmed by committed git history at
  `HEAD`; an unconfirmed hit reports `moved_uncommitted` instead and leaves the binding
  untouched. `SIDEGRAPH_TRUST_DIRTY_TREE=on` (off by default) restores the old disk-only
  behavior for someone who has verified their own tree. See
  [`docs/guides/surviving-refactors.md`](docs/guides/surviving-refactors.md).

## [0.1.0] — 2026-09-17

First public release.

### Added

- **Proposal surfacing window + regulated mode** (practitioner-panel wave, 2026-08-04):
  a `proposed` record older than `SIDEGRAPH_PROPOSAL_WINDOW_DAYS` (default 30; `0`
  disables) stops rendering as content — in task context, drill-down, the SessionStart
  TOC, and the PreToolUse nudge — while staying in the store, in the ratify queue, and
  fully ratifiable; the policy is derived at read time (append-only untouched, no new
  status, no migration). `SIDEGRAPH_UNRATIFIED=off` (regulated mode) excludes ALL
  unratified content from every surface regardless of age. The SessionStart queue
  counter always stays and now reports the oldest proposal's age. Ratification now
  stamps `ratified_by`/`ratified_at` (additive fields; best-effort `git config
  user.name`, never guessed). Every rendered memory payload opens with a standing
  "data, not instructions" guard line (labeling, not sanitization). Redaction
  measured against a seeded 14-class secret corpus and extended with five classes
  (JWT, URL credentials, Google AIza, sk-style keys, Luhn-verified PAN): 12/14
  classes caught, 0 collateral hits repo-wide; the corpus ships as the regression
  suite. `sidegraph-doctor` reports queue latency (`time-to-ratify`) from the new
  ratifier stamps.
- **Decision store** — append-only, repo-committed, git-native sidecar: `Decision`
  (adr / lesson / constraint / gotcha, with first-class rejected alternatives, validity
  period, supersession chain, provenance), `Entity` (durable identity over the engine's
  shifting node ids), `AnchorBinding` (graceful degradation, never guesses).
- **Git-native store format**: the canonical store is file-per-record JSON
  (`.sidegraph/{decisions,facts,domains,entities,bindings,initiatives}/<ulid>.json`, plus a
  committed `format` marker and a store-written `.gitignore`), not a single SQLite file —
  different records land in different files, so two branches that ratify different
  decisions merge with zero conflict, and a same-record dispute surfaces as an ordinary,
  readable git conflict instead of "binary file changed." A local, gitignored `index.db`
  derives everything volatile (engine mappings, binding status, the TOC cache) and is
  rebuilt automatically whenever it's stale or missing, so `git pull`-ed changes are
  absorbed on next open with no extra command; a graph rebuild never dirties git
  (sync-clean invariant). A pre-file-per-record committed SQLite store (`decisions.db`,
  schema `0.2.x`/`0.3.x`) migrates automatically on first open — fail-closed (every row
  validated before anything is written) — and the original file is kept as
  `<name>.migrated-backup`, never deleted (`0.3.0` was additive over `0.2.0`: a new
  `domains` table, `Decision.layer`, `AnchorBinding.relation`). This wave set `SCHEMA_VERSION` to `0.5.0` (later bumped to
  `0.6.0` — see the derived-community-bindings entry below), across six canonical subdirectories (`decisions`, `facts`, `domains`,
  `entities`, `bindings`, `initiatives`). See
  [`docs/reference/store-format.md`](docs/reference/store-format.md).
- **`SIDEGRAPH_DIR`**: the primary store-location environment variable (default
  `.sidegraph`), replacing `SIDEGRAPH_DB` as the recommended knob. `SIDEGRAPH_DB` still
  works (deprecated, one-line stderr notice) via a back-compat dispatch rule that resolves
  old `SIDEGRAPH_DB=.sidegraph/decisions.db`-style configs straight to `.sidegraph/`. See
  [`docs/reference/configuration.md`](docs/reference/configuration.md).
- **`sidegraph-compact`**: packs terminal-status (superseded/rejected/deprecated
  decisions; superseded/dropped domains) records into immutable, write-once
  `archive/<date>-<seq>-<hash12>.jsonl` segments and removes their now-redundant hot
  files — explicit, human-run maintenance (recommended on the default branch) that keeps
  every record retrievable while trimming the individual-file count for records that can
  never change again.
- **`sidegraph-import --docs`**: a second, deterministic importer (no LLM, no API key) that
  parses decision-shaped markdown files directly — ADR/Nygard and spec-style documents — and
  writes one anchored `Decision` per qualifying document, idempotent per source file
  (an edited doc supersedes its prior import). Complements the existing rationale-node
  importer for teams with an existing ADR/spec corpus.
- **Mind-model layer**: `Domain` — a named, described area of the system (title +
  required WHY-IT-EXISTS summary, optional `parent_id`/subdomains), authored via three
  paths (`sidegraph-domains bootstrap` from graph communities, agent-in-session
  `propose_domains`, manual `add_domain`/`sidegraph-domains add`) through one unified
  ratification gate (`ratify`, covering decisions and domains together; the old
  decisions-only `ratify_decisions` name is kept as a deprecated alias). Cross-cutting
  `tag:<slug>` labels and `Decision.layer`/`AnchorBinding.relation` fields. Domain-aware
  Tier-1 anchoring (an accepted domain covering a community wins over the bare
  `community:<id>` entity). `sidegraph-sync` refreshes each accepted domain's community
  mapping (REPLACE, not union — Leiden ids are recycled across rebuilds) and flags
  domains that recompute to empty.
- **Named table of contents**: `SessionStart` now renders accepted domains (title,
  summary, mistake/subdomain counts) instead of a bare community listing the moment the
  first domain is ratified — precomputed on the sync path, no extra read-time cost.
  `drill_down(domain_slug)` walks one domain's summary, subdomains, member sample, and
  decisions — where `decisions` is the union of the domain's directly-tagged decisions and
  the decisions anchored to any code/doc entity in one of the domain's communities (a
  bounded, deduplicated community-membership join), so imported ADRs and other memory that
  anchored to the code rather than the domain abstraction surface under the area they belong
  to instead of showing zero. Budget-tight `get_task_context` now falls back to domain summary lines
  instead of hard-truncating the structural map. `query_structure`/`query_decisions`
  expose the two halves of `get_task_context` as standalone thin tools.
- **Domain onboarding, agent-curated**: a read-only `list_domain_candidates` MCP tool exposes
  bootstrap's own candidate selection (path-grouped, every Gate-5 guard already applied,
  each candidate carrying a durable `anchor`) without writing anything, and `DraftDomain`
  (the `propose_domains` path) gained a `seed_anchors` field — durable, entity-anchored
  membership (resolved into `communities` by `ratify`/`sidegraph-sync`, never populated
  directly) — so an agent-curated merge with no single shared path prefix can still land
  through the ratify gate, and its membership survives a fresh clone or a graph rebuild
  (an earlier raw community-id seed did not: Leiden renumbers communities every rebuild).
  Two new MCP tools round out the surface: `list_domains(status=None)` — a full listing of
  every domain (any status), with membership/lineage counts (`member_count`,
  `seed_anchor_count`, `parent_slug`/`child_slugs`) — and `supersede_domain`, which exposes
  the existing `Store.supersede_domain` primitive (previously reachable only via direct
  `Store` access) as the lineage-correct rename/re-scope path: closes the old domain, writes
  a `status=proposed` successor with `supersedes` set, and also directly fixes the mass-drop
  community-recovery case the naming guide documents. Two plugin skills build on all of
  this: `sidegraph:name-domains` (primary onboarding — studies the candidates, offers 2–3
  domain sets of different granularity built from `seed_anchors`, and writes only after an
  explicit human pick) and `sidegraph:manage-domains` (the escape hatch: add/rename/drop a
  domain by hand, now via `list_domains`/`supersede_domain` instead of "no tool for this").
  Replaces hand-curating a raw `sidegraph-domains bootstrap` listing as the recommended
  onboarding path; the CLI stays for scripted/CI use. See
  [naming your domains](docs/guides/naming-your-domains.md).
- **Five lifecycle plugin skills**: beyond the two domain skills, the plugin now carries
  the whole memory lifecycle as procedural skills — `sidegraph:setup` (end-to-end first
  run: engine → graph → store → wiring check, then hand-offs to the other skills),
  `sidegraph:record-decision` (the authoring craft: the write-path rule — human-asked
  `add_decision` vs agent-initiated `propose_decisions`, never `add_decision` on the
  agent's own initiative — the kind matrix, `rejected` as the highest-value field, anchor
  discipline), `sidegraph:ratify-decisions` (the in-session human gate over the pending
  queue: present each proposal with a recommendation, wait for explicit verdicts, one
  `ratify` call), `sidegraph:import-adrs` (drives `sidegraph-import` dry-run-first, with
  report triage and a human-gated real run), and `sidegraph:heal-anchors` (routes every
  `sidegraph-sync` finding — stale decisions, ambiguous/orphaned anchors, empty domains,
  overbroad path rules, slug conflicts — to its correct heal; supersede-with-fresh-anchors
  over binding inheritance, never a guessed rebind).
- **Redaction on the direct write paths**: `add_decision` and `supersede_decision` now run
  the same secret redaction as the propose/import pipelines (title/context/choice/rejected/
  consequences, plus tag text before slugification; an all-secret tag is skipped, never
  minted as `tag:redacted`), and their results carry a `redactions` count. Previously the
  direct MCP path committed its text verbatim into the repo-committed store — a gap against
  the redact-first rule, surfaced by the skills-wave blind audit.
- **`GraphifyReader` no longer hangs on large graphs**: `neighbors()`, `resolve()`, and
  `nodes_in_file()` are backed by prebuilt indexes instead of re-scanning every edge/node on
  each call. On an Apache Airflow-scale graph (23K nodes / 233K edges ≈ 5.4B comparisons) the
  old per-call O(E)/O(V) scans made `sidegraph-import` hang indefinitely (killed after 90s of
  CPU with zero output); the indexed reader returns promptly. A correctness/reliability fix
  for any large corpus, not just a speed-up.
- **Scale-aware default `limit` for domain candidates**: on a monorepo-scale corpus (a
  cross-project test found Apache Airflow returning 2,578 candidates, ~276K tokens),
  `list_domain_candidates` and `sidegraph-domains bootstrap` now default `limit`/`--limit`
  to 100 (the measured sweet spot, ~10K tokens) instead of unlimited — `limit=0`/`--limit 0`
  is the "all" convention when you really want everything. The response/output signals a
  truncation explicitly (`"truncated"`, `"total_significant"` on the tool; a stderr note
  naming the full count on the CLI) instead of silently showing a partial list as if it were
  everything. `sidegraph-domains bootstrap` also moved its over-50-domains nag to a
  read-only pre-check printed *before* a real run writes anything, instead of after —
  a large, unbounded run could previously flood the store with thousands of
  proposed-domain records before the nag ever printed.
- **Document templates are never imported as decisions**: `sidegraph-import --docs` detects a
  document TEMPLATE — a filename stem of `template` (`ADR-template.md`, `_ADR-template.md`,
  …), a `type: template` frontmatter field, or a placeholder-dominated Context/Decision body —
  and skips it outright, counted separately as `skipped_template` (`N skipped as template(s)
  (not decisions)` on both a real run and `--dry-run`) instead of importing it as a
  false-positive decision (a blank template's own `**Status:** APPROVED` line used to be
  enough to land it accepted).
- **`sidegraph-import --docs` warns on a likely wrong-cwd absolute path**: when at least half
  of anchor-attempted documents come back unanchorable and an absolute `--docs` path was
  passed, the command now prints a stderr warning naming the actual cause (doc paths resolve
  against the current working directory to match `graph.json`'s root-relative `source_file`
  entries, so an absolute path run from anywhere but the repo root misses every anchor)
  instead of leaving a silent `0 imported, N unanchorable` unexplained.
- **Overbroad `path_prefixes` no longer zeroes a domain's `seed_anchors`**: `sidegraph-sync`'s
  20%-of-communities cap on a domain's `path_prefixes` now applies to that path contribution
  alone — a domain with both an over-broad path rule and precise `seed_anchors` keeps its
  anchor-resolved membership (the path contribution is dropped and flagged, the anchors still
  replace `communities`); only a domain whose overbroad path had no `seed_anchors` to rescue it
  keeps its previous mapping, unchanged. A path candidate that resolves to a single community
  is never capped, regardless of ratio.
- **`drill_down` surfaces imported decisions on doc corpora**: a real-corpus accuracy eval
  found `drill_down` missing 8/9 imported ADR decisions under their covering domains on a
  pure-document corpus, even though the community-membership join (above) already existed.
  Root cause: `sidegraph-import --docs` anchors an ADR decision to the document's OWN
  file-level node, but Graphify clusters ALL doc file-level nodes into one hub community —
  so that entity's community is essentially never among a domain's `communities`, which come
  from the document's HEADING nodes in per-document communities instead. `decisions` is now
  the union of three sources, not two: (a) domain-tagged, (b) the community-membership join,
  and (c) a decision anchored to a whole-document entity whose file_path is covered by one of
  the domain's member nodes (i.e. the domain covers at least one heading from that same
  file). Branch (c) is scoped to whole-document anchors only — a code entity's node is never
  `file_type == "document"` — so a code corpus, where one file spans many communities, still
  can't have a decision anchored to an unrelated function in that file falsely surface.
  Verified against a real 3-domain/9-decision ADR corpus: decisions surfaced per domain went
  0/0/1 → 3/3/2 (8/9 total; the ninth ADR isn't a `seed_anchor` of any of the three domains,
  so it correctly stays unsurfaced everywhere).
- **`PreToolUse` hook**: a non-blocking nudge toward `get_task_context` on a blind
  `Read`/`Grep` of a source file, when the store has memory to offer
  (`SIDEGRAPH_GREP_NUDGE=off` to disable). It **names what memory holds about the path
  being opened** — up to two anchored record titles, mistakes first then newest first,
  clipped, `[unratified]` tagged when still proposed — falling back to domain/decision
  counts when the path carries nothing or a `Grep` has no path. The two forms hold
  SEPARATE once-per-session keys, so the generic form cannot consume the specific one.
  The counting-only form was measured across 168 sessions: it fires and agents read the
  file anyway (`design/testing/2026-07-31-whitepaper-evidence-results.md` §4.4).
- **`SessionStart` standing search instruction**: the injected context now leads with an
  unconditional instruction to call `get_task_context` before any search — bash
  `grep`/`rg`/`find` and MCP structure-query tools included, not just `Read`/`Grep` (the
  `PreToolUse` nudge above stays a just-in-time backstop for those two tools only).
  Prepended once at the hook-assembly level, so `render_toc`/`top_tier_map` themselves stay
  pure content formatters.
- **`Stop` hook, calmer by default**: the block-to-distill nudge now fires only once a
  session has produced >= 2 real user prompts (a substance gate over the transcript,
  distinguishing real prompts from tool-result noise) — it no longer fires at the end of
  a session's very first turn — and the nudge text itself shrank to a compact one-liner
  (`suppressOutput: true` set on the block response); `SIDEGRAPH_CAPTURE_NUDGE=off` to
  disable it entirely.
- **MCP server** (`sidegraph-mcp`, 22 tools as of this bullet — see the CI integrity toolkit
  bullet below for the 22 → 24 that followed later in this same 0.1.0 section):
  `get_task_context`, `query_structure`,
  `query_decisions` (budgeted, mistakes-first), `drill_down`, `list_domain_candidates`,
  `list_domains`, `add_decision`, `supersede_decision` (anchor inheritance), `add_fact`,
  `supersede_fact` (evidence layer — see below), `find_entity`, `get_entity_history`,
  `retrieve_decisions`, `list_facts`, `propose_decisions`, `propose_domains`, `add_domain`,
  `supersede_domain`, `list_proposed`, `ratify`, `ratify_decisions` (deprecated alias),
  `sync_anchors` (see below).
- **Claude Code integration**: SessionStart context map + Stop-hook capture/domain
  nudge + PreToolUse redirect nudge; secret redaction on drafts; human ratification
  gate. Codex CLI supported at configuration level (same hook contract).
- **Refactor-surviving sync** (`sidegraph-sync`): deterministic rebind ladder with
  cross-type suffix guard, community re-pointing, content-hash graph versioning
  (dirty-tree and non-git corpora covered).
- **Documentation corpora**: anchor decisions to markdown headings and files; LLM-free
  graph build; non-git folders supported.
- **Semantic docs layer** (optional, one API key): concept/rationale anchoring over
  Graphify's semantic pass; thematic cross-file retrieval.
- **Bootstrap import** (`sidegraph-import`): seed the store from rationale already in
  your sources (docstrings — no LLM required; document prose after the semantic pass),
  with dry-run, path/limit filters, file-aware idempotency, and an optional
  ratification gate.
- **CLIs**: `sidegraph-init` (bootstrap + wiring snippets, now including the
  `PreToolUse` hook), `sidegraph-ratify` (decisions and domains together),
  `sidegraph-sync`, `sidegraph-import` (rationale nodes and `--docs` markdown),
  `sidegraph-domains` (`bootstrap`/`add`), `sidegraph-compact`; meaningful exit codes.
- **Claude Code plugin + marketplace manifest** — `/plugin marketplace add
  SantyagoSeaman/sidegraph` then `/plugin install sidegraph@sidegraph` installs the MCP
  server and all three hooks in one step, functional today: it builds straight from this
  repository via `uvx --from git+...`, no PyPI publish required.
- **Plugin-first install docs**: README and the getting-started guides now lead with the
  plugin (or a bare `uvx --from git+https://github.com/SantyagoSeaman/sidegraph.git@main
  <entrypoint>` for manual/no-plugin wiring) as the primary install path; the source-checkout
  (`git clone` + `uv sync` + `uv run --project`) form moves to a "from source (contributors)"
  section. `graphifyy` installs without the `[mcp]` extra by default — `[mcp]` is documented
  as an optional extra for Graphify's own MCP server, which coexists with Sidegraph but isn't
  required by it.
- **PyPI package + `pip install sidegraph`**: Sidegraph packages as a pure-Python PyPI
  distribution; `pip install sidegraph` (or `uv tool install sidegraph`) puts `sidegraph-mcp`
  and every `sidegraph-*` CLI on your PATH. The README/getting-started guides lead Quickstart
  with it as the primary install (a PyPI version badge and a green-tests badge sit at the top). A
  tag-triggered `publish.yml` GitHub Actions workflow builds the sdist + wheel
  and publishes via **PyPI Trusted Publishing** (OIDC — no stored token), gated on the full
  test suite (it reuses the `ci.yml` lint+type+test workflow) and a check that the git tag
  matches the `pyproject` version, so a mistyped tag never ships. The plugin and
  `uvx --from git+…@main` paths keep working unchanged for the latest/unreleased build.
- **Facts layer**: a new `Fact` record captures compact, falsifiable, non-derivable
  knowledge that informed a decision — benchmarks, external constraints, trial-learned
  lessons ("the code does X" stays out; that's the engine's job and goes stale with every
  commit). `add_fact`/`supersede_fact` mirror the decision-side direct paths (redaction, a
  `redactions` count, append-only supersession) but fix a no-graph gap rather than repeat
  it: with no reader present, an anchor still binds an ORPHANED Tier-2 leaf instead of being
  silently dropped the way `add_decision`'s anchors are. `propose_decisions` grows a `facts`
  list per draft (attached — inherits the decision's anchors unless it names its own) and a
  top-level `facts` parameter (standalone — needs its own anchor or `supports` id, or the
  draft is rejected as unreachable). Ratification gates facts the same way as decisions, but
  cascades: accepting or dropping a decision accepts or drops every still-proposed fact that
  supports it too (`list_proposed`/`sidegraph-ratify` show these nested as `evidence: ...`
  lines under the decision, one verdict covering both); standalone facts are their own queue
  rows, and `--all`/batch-accept never double-counts a nested one. Retrieval renders a live
  supporting fact inline under its decision (`evidence: <statement> [<source>]`) and
  standalone facts in a new `## Known facts` block placed right after `## Decisions`,
  within the same shared budget — mistakes never lose budget to facts, by construction (a
  two-phase mistakes bucket places every mistake decision line before any evidence line is
  spent). Facts reuse the existing `AnchorBinding` machinery — `decision_id` renamed to
  `record_id` (it now holds a decision OR a fact id), `find_entity` gains `record_type`.
  This wave is what bumps `SCHEMA_VERSION` to `0.5.0` (see the store-format bullet above):
  a store still stamped `0.4.0` reloads its index and re-stamps to `0.5.0` automatically on
  open (no hard failure — the 0.4.0 canonical layout is fully forward-compatible).
  `facts/<ulid>.json` isn't yet covered by `sidegraph-compact` (deferred — facts stay
  entirely hot for now).
- **Derived community bindings**: Tier-1 `community:*` bindings, and the abstract entities
  they point at, are now fully DERIVED — index-only, never written to a canonical file, at
  capture time and at sync time alike (the scope condition is the entity: any abstract entity
  whose `canonical_name` starts with `community:`; `domain:*`/`tag:*`/initiative anchors are
  unaffected and stay canonical). Closes a real sync-clean violation found live: a pure
  `get_task_context` read had rewritten 8 committed `bindings/<record>.json` files and minted
  4 new canonical `community:*` entities on a graph rebuild that only renumbered Leiden
  communities. A fresh clone self-heals via the ordinary first `sidegraph-sync` (no committed
  community baseline is needed, for any decision whose community binding has a Tier-2 leaf to
  regenerate from); a store carrying old-format committed community entries tolerates them on
  reload — the stale *binding* entry sheds lazily, the next time that record's binding file is
  legitimately rewritten for an unrelated reason, but the stale *entity* file itself has no
  decay path and persists until a future cleanup command — no proactive cleanup command in
  this wave.
  Bumps `SCHEMA_VERSION` to `0.6.0`: a `0.5.0` store reloads its index and re-stamps
  automatically on open, same forward-compatible pattern as the `0.4.0` → `0.5.0` step.
  Mixed-version teams: a teammate still on pre-`0.6.0` code will transiently re-mint
  committed community entries until they upgrade (new code tolerates and lazily decays
  them — no data loss, self-healing). See
  [`docs/reference/store-format.md`](docs/reference/store-format.md#community-bindings-are-derived-not-committed).
- **`SessionStart` surfaces the pending-ratification queue**: the injected context now ends
  with one more line, own `try/except` so a count failure never costs the map above it —
  `Sidegraph: N record(s) awaiting ratification (X decisions, Y facts, Z domains) — review
  with the ratify MCP tool or sidegraph-ratify.` — whenever the queue is non-empty (nothing
  is appended at zero). The count is a new `Store.pending_ratification_counts()` query:
  proposed decisions, standalone proposed facts (a fact riding a proposed decision's cascade
  is covered by it and never double-counted), and proposed domains. Renders with or without a
  graph — it's store-only information. `SIDEGRAPH_RATIFY_NUDGE=off` disables the line
  entirely, same convention as `SIDEGRAPH_CAPTURE_NUDGE`/`SIDEGRAPH_GREP_NUDGE`. Closes a real
  adoption risk: the proposed→ratify gate is the store's only noise filter, but with no queue
  visibility the realistic failure mode wasn't "users reject the gate," it was "users forget
  it exists."
- **`SIDEGRAPH_AUTO_ACCEPT` — sanctioned opt-out, off by default**: set it to `on` and every
  agent capture through `propose_decisions` — each decision draft, its attached facts, and
  standalone facts passed via the top-level `facts` parameter — lands `status="accepted"`
  directly instead of `"proposed"`, skipping the ratification queue entirely.
  `provenance.source` still stamps `"agent"` regardless, so history never lies about
  authorship, only about whether a human reviewed it. **Domains are always exempt**:
  `propose_domains` never consults this variable, so a domain draft always lands `"proposed"`
  — few in number, high cost of a bad name/scope, and `drill_down`/retrieval hard-gate on
  accepted domains. No TTL/auto-promote path was considered and rejected — it would silently
  legitimize noise. Recommended for solo use; not recommended for team stores, since it
  removes the store's only noise filter. See
  [`docs/guides/capturing-decisions.md#4-auto-accept-opt-in`](docs/guides/capturing-decisions.md#4-auto-accept-opt-in).
- **`list_facts` MCP tool**: the `retrieve_decisions` counterpart for the facts layer —
  previously a fact had no direct read path at all (`retrieve_decisions` returns decisions
  only). Mirrors `retrieve_decisions`'s contract: excludes `superseded`/`rejected` by
  default, `include_superseded=True` to see that history too; sorted newest-first
  (`valid_from` descending, `id` descending tiebreak — facts carry no `kind`, so there is no
  mistakes-first ranking analogue).
- **`get_entity_history` no longer drops facts**: it used to call `get_decision` per binding
  only — a fact-only binding silently vanished from an entity's history with no trace. Now
  tries a decision lookup, then a fact lookup, per binding (an unknown record kind stays
  skipped, as before); every returned dict gains an additive `"record_type": "decision" |
  "fact"` key so a caller can tell them apart, and the merged list stays sorted `valid_from`
  descending.
- **`sync_anchors` MCP tool**: the diagnostic/heal MCP counterpart to `sidegraph-sync` — runs
  the same rebind pass (`force=True` to re-run even when `graph_version` already matches) and
  returns the report as data (`synced`, `from_version`/`to_version`, `counts`, `repointed`,
  `outcomes` filtered to non-`unchanged`/`rebound` entities, `stale_decisions`,
  `empty_domains`, `overbroad_domains`, `slug_conflicts`, `domains_refreshed`) instead of only
  printing it to stdout. A `synced: false` skip (version match, no `force`) returns every
  other field as an empty default, never a stale prior report. An unreadable graph returns
  `{"synced": false, "error": "..."}` instead of crashing — explanatory, matching this tool's
  diagnostic purpose, unlike every other tool's silent best-effort degrade. Closes the last
  MCP coverage gap: anchor-health diagnostics (stale decisions, orphaned anchors, slug
  conflicts) were previously CLI-only, so the `heal-anchors` skill couldn't run MCP-only — it
  now leads with `sync_anchors`, with `sidegraph-sync` kept as the CLI fallback.
- **CI integrity toolkit**: store integrity is now a checkable CI contract, not just a
  human convention.
  - **`sidegraph-sync --json`/`--check`**: `--json` prints the full sync report as one JSON
    object (`sync.report_as_dict` — the same shape `sync_anchors` returns, so a CI script
    and an in-session tool call never disagree about the fields); `--check` exits `2` when
    the report has an attention finding (an `error` outcome, a non-empty `stale_decisions`,
    or a non-empty `slug_conflicts` — `orphaned`/`ambiguous` outcomes and
    `empty_domains`/`overbroad_domains` stay informational, never fail the check). The two
    flags compose; exit `0` still covers a clean report and a version-skip, exit `1` the
    existing operational-error paths.
  - **`sidegraph-verify`** (new CLI + `verify_store` MCP tool): a store-integrity lint with
    two layers. The **snapshot layer** (always runs, pure read — never opens a `Store`,
    never migrates, never touches `index.db`) checks schema validity, `valid_to >=
    valid_from`, supersedes-chain resolution, dangling bindings/fact-supports, ULID
    uniqueness across hot files and archive segments (byte-identical archive-archive
    duplicates from a sanctioned cross-branch `sidegraph-compact` merge are exempt), archive
    segment parsing, and filename/id agreement — 10 pinned violation codes. The
    **transition layer** (`sidegraph-verify --against <git-ref>`, CLI-only — git plumbing
    isn't exposed through MCP) classifies every store file changed vs a git ref against the
    store's OWN write-path rules (derived from `store.py`, not invented): a real
    supersede/ratify/sync/compact stays legal, a hand-edit or history rewrite doesn't.
    `verify_store` is snapshot-only in v1; its docstring points CI users wanting the
    transition layer at the CLI. Exit contract: `0` clean, `1` operational error (unreadable
    store, bad git ref, not a git repo), `2` violations found.
  - **`add_anchors` MCP tool**: append bindings to an *existing* decision or fact —
    generalizes the fact-anchoring resolve-or-orphan ladder to either record kind. This is
    the missing half of triage: "code moved, decision still valid" now heals in place
    (bindings-only — the record file itself is never touched, so it stays legal under
    `sidegraph-verify`'s transition rules) instead of forcing a content-free
    `supersede_decision`/`supersede_fact` that pollutes history with a successor saying
    nothing new. Tool count: 22 → 24.
  - **`heal-anchors` skill gains a triage decision tree**: after `sync_anchors`, walk every
    stale/orphaned finding through (a) code moved, decision still valid →
    `find_entity`/`query_structure` to locate the new home → `add_anchors`; (b) content
    genuinely outdated → `propose_decisions` with `supersedes` (still `status=proposed` — a
    human ratifies); (c) subject genuinely gone → recommend a drop in the summary, never
    perform it. Includes a headless CI prompt example
    (`claude -p --mcp-config ... --allowedTools "mcp__sidegraph__*" ...`).
  - **New guide, [`docs/guides/ci-cd-maintenance.md`](docs/guides/ci-cd-maintenance.md)**:
    three GitHub Actions recipes — an anchor-health required check
    (`graphify update .` + `sidegraph-sync --json --check`), a store lint on PR
    (`sidegraph-verify --against <merge-base> --json` — diffed against the PR's merge base,
    not the base branch's moving tip, to avoid false positives on a branch that's behind),
    and a scheduled triage job (headless Claude + the `heal-anchors` playbook, proposals
    only). States two hard rules plainly: CI never ratifies; CI never auto-pushes canonical
    files to a branch nobody reviewed (the scheduled recipe opens a PR with whatever it
    proposed instead).
  - **Live-experiment refinement: `orphaned`/`ambiguous` no longer fail `--check`.** A live
    GitHub Actions run showed the anchor-health check staying red forever after a
    legitimate rename+heal — triage adds a live anchor to the decision, but the
    renamed-away entity's own leaf has no retirement path in an append-only store, so it
    stays orphaned for good. `sync.report_has_findings`'s failing classes are now exactly
    `error` outcomes, `stale_decisions`, and `slug_conflicts`; `orphaned`/`ambiguous`
    outcomes are informational only (still listed in `outcomes` and the PR comment) — when
    one actually costs reachability, the decision goes stale and `stale_decisions` already
    fires, so the signal isn't lost, just no longer duplicated as a permanent red flag.
- **`sidegraph-viz`**: a read-only CLI that renders the owned decision/fact store as an
  interactive graph — a maintainer's diagnostic view of what is anchored where. Nodes are
  decisions (colored by kind: mistakes warm, ADRs blue), facts (diamonds), and the entities
  they anchor to (by tier); edges are anchor bindings colored by status
  (live / degraded / orphaned), supersede chains, and fact→decision (`supports`) links. A
  footer counts the things you inspect for — orphaned and degraded bindings, and *dangling*
  records (memory with no anchor binding at all). Output is a **self-contained, offline HTML
  file** with the vis-network library vendored inline (no CDN, opens with a double-click),
  plus a machine-readable `{nodes, edges, stats}` JSON sibling (`--json` prints it to stdout).
  `--only-problems` narrows to the degraded/orphaned/dangling subgraph; `--no-superseded`
  hides history (shown dimmed by default); `--max-nodes` caps with reported (never silent)
  truncation; `--open` launches a browser. Read-only and store-only: it never writes a
  record, never touches `graph.json`, and adds no schema fields — it lives entirely in the
  portable core with no engine reader. A `demo` branch, shipping with a later release, will
  carry a rendered graph of Sidegraph's own decision memory, regenerated from `.sidegraph`
  on each release of that branch.
- **Imported `status: rejected` docs land `rejected`**, not `accepted`: a document the
  team turned down was being imported as a decision the team had adopted. It is not
  labelled `proposed` either — that would push a settled no into the ratification queue —
  and not skipped, because a rejected proposal with its reasons is exactly the
  "tried before, abandoned because…" the store exists to keep. `sidegraph-import` reports
  the count (`N landed rejected (source status: rejected)`).
- **`sidegraph-ratify` resolves an accepted domain's membership immediately**, like the
  MCP `ratify` tool already did — the same accept through two doors no longer leaves
  different state (`communities: []` until the next sync). New `--graph` flag; a relative
  path resolves against the store's own project root, and any failure falls back to
  scheduling the heal rather than failing the accept.
- **`sidegraph-doctor`** — one-stop store health: verify's strict checks + advisory
  curation lint (dangling records, decayed bindings, stale proposals, unreferenced
  entities, expired-but-open validity); `--check` escalates advisory findings for CI.
- **Drift→supersede affordance** — doctor's `code-drift` signal (a live, commit-stamped
  decision anchored to files that changed since its capture commit) now surfaces where an
  agent actually acts, instead of only in `sidegraph-doctor` output: a `[drifted]` tag on
  detailed-tier retrieval lines (`get_task_context`/`query_decisions`/`drill_down`, plus a
  one-line legend; `drill_down` returns it as a new optional `"legend"` key), a
  SessionStart map line ("N record(s) are anchored to code that changed after their
  capture…"), and a conditional Stop-nudge clause (inside the pinned 800-char bound).
  Freshness rides a store-meta cache (`code_drift_cache`) refreshed by both hooks via one
  deadline-bounded git scan with per-commit merge (a transient git failure neither erases
  real markers nor freezes the cache). `SIDEGRAPH_DRIFT_NUDGE=off` suppresses the two
  prose surfaces; the refresh and the markers stay on. Records superseded mid-session are
  live-filtered out at read time. See `docs/concepts/retrieval.md` (the `[drifted]`
  marker) and `docs/reference/hooks.md`.
- **Retrieval telemetry — telling dead memory from memory nobody has needed yet**: the read
  path now records what actually surfaced and what was asked about, in two gitignored
  `index.db` tables (`retrieval_shows`, `retrieval_seeds`) that, like `capture_sessions`,
  survive a `git pull`-triggered reload. A read still never produces a git diff. On top of
  it, `sidegraph-doctor` gains a `never-surfaced` curation finding that reports only the
  actionable case — a decision anchored where people keep working that has never once
  reached a render, "check its anchors or the ranking" — and stays silent on
  `0 shows / 0 queries`, which is an absence of occasion, not dead memory. A store nobody
  has read yet is `SKIPPED`, never flagged. `query_structure` records nothing: it returns
  no decision memory, so it never offers an opportunity for one to surface, and counting it
  would inflate the denominator. The data never leaves the machine, and
  `SIDEGRAPH_TELEMETRY=off` disables recording entirely — same convention as the nudge
  knobs.
- **`sidegraph-export-okf`** — one-way projection of the store into an Open Knowledge
  Format (OKF v0.1) bundle: every ratified decision/fact with full supersession history,
  domains, and anchored entities as cross-linked markdown concepts. Unratified `proposed`
  drafts are never published (superseded/rejected/deprecated history is kept). Deterministic
  (byte-identical re-export), safe out-dir handling, no new runtime dependencies.
- **Cross-process store opens no longer destroy each other's state**: two real defects behind
  `tests/test_store_concurrent_open.py`'s ~1-in-5 flakiness, both reproduced rather than
  reasoned about. (1) The index rebuild (`_reload_index_from_canonical`) used to `DROP` and
  recreate the six record tables with bare, autocommitting DDL — a concurrent reader, a
  long-lived MCP server included, could catch the gap and see `OperationalError: no such
  table: domains` (2/1600 opens measured). The rebuild now runs as one explicit transaction
  (`BEGIN IMMEDIATE` … `COMMIT`): a reader sees the pre-rebuild rows for the entire rebuild
  and the new ones only once it commits, and a failure anywhere in the body rolls back instead
  of leaving the index half-dropped. (2) The open-time tmp-debris sweep couldn't tell crash
  debris from another process's in-flight write and deleted live buffers faster than
  `_atomic_write_text_race_tolerant`'s 3-attempt retry could absorb above 3 concurrent openers
  (`FileNotFoundError` on `os.replace`, 1/1000 opens measured). The sweep is now age-gated —
  it only removes a `*.tmp` file older than 60 seconds, old enough that it can never be a live
  buffer — and every atomic write, not just the format marker, gets a per-write-unique tmp
  name, closing the same shared-inode corruption class for the store's own canonical records
  that the marker write was already protected from.
- **Every store write now commits or rolls back**: 16 of 17 committing methods in `store.py`
  had no rollback guard, so a failed write returned control with the SQLite connection still
  holding an uncommitted transaction — a second process then got `OperationalError: database
  is locked (after 2.1s wait)`. Every public write now runs through a shared
  `Store._mutation()` helper (reentrant, so nested writes like `ratify_domains`'s entity mint
  still work) that commits on success and rolls back on any failure, including a failing
  commit itself; `ratify_domains` commits per accept/drop item instead of once for the whole
  batch, so one bad id still can't undo the rest. `sidegraph-doctor` also stopped reporting a
  standalone fact reachable only through a `supports` link to a live decision as dangling —
  the write path has always accepted that shape, and all 9 `dangling-record` findings on this
  repo's own store were exactly this false positive — and the fact write gate (`add_fact`,
  `supersede_fact`, `propose_facts`) now requires an anchor or a `supports` id resolving to a
  LIVE decision, so the product stops minting records that check would go on to flag.
- **Entity get-or-create is now atomic across processes**: `get_or_create_abstract_entity`
  guarded its check-then-create with `self._lock` — a per-instance `RLock`, so an MCP server,
  a hook, and a CLI each holding their own `Store` could race the same lookup, all see
  nothing, and each mint a different id for the same logical entity (latent: 0 duplicates
  across this repo's own entity files, which is why it was worth fixing carefully rather than
  fast). The same shape existed for concrete entities via `find_entity` + `upsert_entity`
  written out longhand at two call sites. Both now run their lookup and their mint inside one
  `BEGIN IMMEDIATE` transaction (`Store._mutation(immediate=True)`, gated on whether a
  transaction is already open — not on nesting depth, since an outer scope that has only read
  holds no lock at all to gate on), collapsed onto one new `Store.get_or_create_entity`. A
  **UNIQUE index on logical identity was considered and rejected**: file-per-record exists so
  two branches merge without a git conflict, and two branches that each mint the same name
  produce two ULIDs — two files — that merge cleanly and leave the canonical store legally
  holding a duplicate; a UNIQUE index's rebuild (inside `Store.__init__`) would then raise
  `UNIQUE constraint failed` on exactly that merge, and the store could never be opened again.
  Lookups (`find_entity`, `find_abstract_entity`, and the get-or-create's own inline check)
  instead resolve any duplicate deterministically — the lowest `entity_id` wins, stable across
  processes and reopens — and `sidegraph-doctor` gains a `duplicate-entity` finding that names
  every id in a group and each one's binding count, so a human can decide which should absorb
  the others.
- **The freshness digest now only certifies what the index actually loaded**: `_touch_digest`
  used to hash the FILESYSTEM — every canonical file it could see, not the ones THIS process
  actually indexed — so a writer that crashed right after publishing a canonical file (before
  its own index write) could have that orphan silently certified as indexed by a different,
  already-open process's next unrelated write. `stored_digest == compute_digest()` then held
  while the record was missing from the index, permanently: unlike ordinary crash debris, a
  matching digest means the next open takes the fast path and never reloads, so nothing ever
  healed it — the only one of four external-review findings whose damage is silent *and*
  permanent. A new derived, gitignored `canonical_stat(subdir, stem, size, mtime_ns)` table
  (in `index.db`, never the canonical store) records every canonical writer's own file stat —
  captured from the tmp file *before* `os.replace`, never after, so a later stat can never pick
  up a DIFFERENT writer's replace — beside its canonical write, in the same transaction, for
  all seven canonical writers (the six that publish via tmp + `os.replace`, plus the archive
  segment writer, which has no `os.replace` at all and publishes via exclusive `os.link`).
  `_touch_digest` now compares its own digest walk against this table and refuses to stamp —
  and CLEARS the existing stamp, rather than merely leaving it in place — on the first file
  with no matching row or a different one: an id-based check was tried first and rejected,
  because it only catches an ABSENT record, not a REWRITTEN one (a ratify status flip, a
  supersede, an entity rename can all die pre-commit after mutating an existing canonical file,
  and an id check would happily certify the stale original). Clearing rather than withholding
  closed a gap found during implementation: two writers rewriting the SAME record with no crash
  at all can invert `os.replace` order against commit order, and if the loser's touch merely
  *withheld* a new stamp, the winner's already-valid (and still disk-matching) stamp would be
  left in place while the index quietly drifted under it — clearing forces the next open to
  reload unconditionally instead of trusting a value comparison that shape can defeat. The
  check stays one-directional (a `canonical_stat` row whose file is now gone — compacted away,
  or a derived `community:*` entity that never had a canonical file to begin with — is normal
  and never blocks a stamp); a cross-process writer lock was considered and rejected, since it
  would still need this same check (a crash while holding the lock reintroduces the identical
  defect) while this check needs no lock at all.
- **`sidegraph-bootstrap`**: a guided onboarding command that turns an existing ADR/spec corpus
  into reviewed, anchored Sidegraph memory in one run — scan, preview, interactive review, write
  only after the user reviews a redacted action summary and types the literal `confirm`, verify
  anchors and the selected host, and prove production retrieval by calling the same
  `get_task_context` path a live session uses. Preview and review share one immutable plan, so
  what gets reviewed is exactly what gets written. Supports six document profiles in v1 —
  `generic-adr`, `superpowers`, `genkovich-sdd`, `spec-kit`, `bmad`, `openspec` — auto-detected
  from the profile marker and input globs, or pinned with `--profile` (an explicit choice always
  wins; several specific-profile matches force an explicit choice instead of merging dialects).
  `--docs PATH` adds files/directories in the same dialect; permissive arbitrary-document
  extraction (matching no profile at all) is not supported in v1. A host support matrix (in the docs, not
  printed by the command) distinguishes what is actually verified, not just attempted: Claude
  Code gets the complete v1 flow (MCP, SessionStart, Stop, and the Read/Grep `PreToolUse` nudge
  all verified), and the terminal prints `INTEGRATION  Claude Code MCP + hooks verified`; Codex
  gets MCP/SessionStart/Stop verified when configured but has no `PreToolUse` equivalent, so the
  terminal prints `INTEGRATION  Codex best-effort` instead and never `fully supported` (the
  optional `--report` markdown does write `- fully supported: false` for Codex). Exit codes are
  a real contract: `0` covers full activation and safe, no-write diagnostics; `1` is a usage or
  operational error and is unreachable once anything has been durably written (a post-write I/O
  failure during completion rendering is `2`, never `1`, so a `1` never leaves partial memory
  behind for a `--resume` to reconcile); `2` covers `incomplete` (no durable candidate completed
  the plan, or an actionable prerequisite/check remains) and `partial-recoverable` (at least one
  canonical file is durable but a later write/index/reopen/verification step failed). `--resume`
  is a marker only — the underlying scan/review/reconcile flow is idempotent, so reissuing the
  same command without it behaves identically. See
  [`docs/getting-started/bootstrap.md`](docs/getting-started/bootstrap.md).
- **`SIDEGRAPH_RATIFY_POLICY` — auto-ratification policy, `manual` by default**
  (2026-09-13): lets a deployment with no human in the loop delegate the ratification gate
  to a deterministic, stamped policy. `manual` (the default; also unset, empty, or any
  unknown value — matched exactly after trimming, so a typo fails safe) changes nothing:
  canonical state, every rendered surface, and the three CLI batch summary lines stay
  byte-identical. `auto-low-risk` lets an eligible `gotcha`/`lesson` decision or standalone
  fact ratify itself at write time; `auto-all` also admits `adr`/`constraint` decisions and
  domains. Six write surfaces read it once per invocation: `propose_decisions` (decisions
  and standalone facts), `propose_domains`, `sidegraph-import` with `--propose` (rationale
  mode, and `--docs` writes that land as a new `proposed` record — status-derived drafts,
  `superseded` re-imports and `status: rejected` documents never auto-ratify), and
  `sidegraph-domains bootstrap` (a candidate whose derived `path_prefixes` is empty stays
  proposed); a dry run performs no transition. The gates are conjunctive and LLM-free: the
  policy admits the shape (under `auto-low-risk` a superseding draft never does), at least
  one live Tier-1/Tier-2 anchor binding (domains: a graph reader, a clean path-prefix lint,
  and a resolving seed anchor or non-empty prefixes), a clean non-dry-run write, and
  provenance. An attached fact never self-ratifies — it rides its decision's cascade, and
  one ineligible attached fact keeps the decision proposed (re-checked under the write
  lock). An auto-ratified record goes through the same ratify transition a human uses,
  is stamped `ratified_by="auto:<policy>"`, surfaces as ordinary accepted memory, and
  leaves the queue by the ordinary accept path; a superseding proposal written under an
  auto policy leaves its predecessor open until the successor is ratified — automatically,
  or later by a human. Additive
  fields: `ratified_by` and `auto_ratify_error` on every `propose_decisions` result
  (nested facts included) and `propose_domains` result — `null` under `manual`, so the key
  set grows even there — and `auto_ratified`/`auto_ratify_failures` on the import and
  bootstrap reports. Under a non-`manual` policy the three CLI summaries append
  `, auto-ratified N` before `(skipped: …)` and each failure prints
  `auto-ratify failure: <id>: <reason>` on stderr. `sidegraph-doctor`'s human output gains
  two informational lines once any `auto:` stamp exists (`auto share`, `auto supersede
  rate`, hot files plus archive) and `time-to-ratify` now excludes `auto:` stamps; `--json`,
  exit codes and finding codes are unchanged, and an `auto:`-stamped record never raises
  `unratified-accept`. Out of scope: `sidegraph-bootstrap`'s keep-proposed verdicts (a human
  verdict wins), manual domain adds (`add_domain`, `sidegraph-domains add`) and
  `supersede_domain` successors. Legacy `SIDEGRAPH_AUTO_ACCEPT=on` still wins when both are
  set and stays stamp-less. See
  [`docs/reference/configuration.md`](docs/reference/configuration.md).
- **Git-native provenance: commit trailers and `sidegraph-blame`.** A
  `prepare-commit-msg` git hook (`sidegraph-prepare-commit-msg`) comments candidate
  `Sidegraph-Decision:` trailers into the commit-message template (captured-this-session
  records, plus decisions anchored to staged files) for a human or agent to uncomment,
  never auto-appended. `sidegraph-blame` joins `git blame` hunks back to the
  decisions/facts each commit carries, via commit trailers and `provenance.commit`. Both
  are read-only over records and never block or stall a `git commit`. See
  [`docs/reference/git-bindings.md`](docs/reference/git-bindings.md).
- **Codex plugin support.** A marketplace manifest (`.agents/plugins/marketplace.json`)
  and per-host Codex manifests (`plugin/sidegraph/.codex-plugin/`,
  `plugin/sidegraph/codex/`) let the same plugin install into Codex CLI. Each skill ships
  a matching `agents/openai.yaml`, and Codex gets `SessionStart` and `Stop` hooks (no
  `PreToolUse` equivalent). See [`docs/integrations/codex.md`](docs/integrations/codex.md).
- **Community files.** A pull request template (`.github/PULL_REQUEST_TEMPLATE.md`),
  `.github/CODEOWNERS`, and a Contributor Covenant 2.1 code of conduct
  (`CODE_OF_CONDUCT.md`).
- **Public release runbook**
  ([`docs/reference/releasing.md`](docs/reference/releasing.md)): the step-by-step
  procedure for cutting the allowlist-only public snapshot and tagging a release.
- **`pyproject.toml` classifiers and keywords**: PyPI discovery metadata (development
  status, license, supported Python versions, and search keywords such as `mcp`,
  `decision-log`, and `team-memory`).
- **Lint gate additions** in `.pre-commit-config.yaml`: `gitleaks` (broad secret
  detection), `detect-private-key`, `zizmor` and `actionlint` (GitHub Actions workflow
  security and correctness), `check-toml`, and a `uv.lock`/`pyproject.toml` sync check.

### Changed

- **Whitepaper rev 6.0 is the GitHub edition, and it ships alone** (rev 5.0 shipped
  2026-09-05, rewritten to rev 6.0 on 2026-09-15): `design/whitepaper/draft.md` is a
  ground-up rewrite under the leitmotif fixed with the owner the same day. Engineers
  remember decisions, not code. Agents read the same code but keep no decisions.
  Sidegraph is the missing link. The rewrite opens with the three ways teams cope and
  their ceilings, restores the related-work section with every neighbour linked, and
  walks one real decision chain from this repository's own store (`Store.ratify`, two
  supersession chains, the plan check that surfaced a twice-rejected design). Every
  measured number and its boundary from rev 5.1 survives. The claim ledger is now at rev
  2.7. The text passed three review rounds (Codex, muse, a Fable subagent, each blind to
  the others), two style rounds, and eight blind-reader passes. The public snapshot still
  carries `docs/whitepaper/index.md` only: the evidence edition, the claim ledger, the
  artifact bundle, the bibliography and the figures stay in the internal repository, and
  `docs/llms.txt` and the pilot kit no longer point at them. The pilot kit is described as
  the procedure and prompts (four runs per question, blinding done by hand). The
  measurement harness behind the paper is not published.
- **Proposed-record quarantine — a retrieval-ordering change for existing stores**: a proposed
  decision or fact (not yet ratified) now ranks after every accepted record on the five
  production delivery surfaces spec rev 3 §3.5 names — `get_task_context` and `query_decisions`
  (both via `rank_decisions`), `drill_down`, `SessionStart`/TOC, and the `PreToolUse` title
  nudge — instead of competing for mistakes-first placement the way an accepted gotcha does. It
  is still discoverable and still rendered `[unratified]`; it simply can no longer outrank a team's
  already-ratified memory on those five surfaces, so review debt now carries a visible retrieval
  cost. A store that already carries proposed records will see their retrieval position move to
  the back of the list on upgrade. `retrieve_decisions`, the raw MCP listing, is a **deliberate,
  unquarantined exception**: it keeps sorting by kind alone (gotcha, then lesson, then everything
  else), so a proposed gotcha or lesson can still rank ahead of an accepted ADR there — every row
  still renders its own `status`, so trust information is present even though ranking isn't. See
  ADR `01KZ27ZQXHAP8J8TYSAPSHSMT3`.
- **Whitepaper promoted to a public engineering release candidate**: `design/whitepaper/draft.md`
  (rev 2.2, Bootstrap publication audit, 2026-08-02) and `design/whitepaper/README.md` now carry
  "Public engineering whitepaper release candidate" status. The claim-ledger audit (58 claims: 16
  implemented property, 23 design rationale, 15 empirical finding, 4 open hypothesis, backed by
  18 publication sources) permits publishing the bounded architecture, the completed evidence
  program's measured mechanisms and negative results (E14 Phases 0–3), and the new cold-start
  Bootstrap workflow. This is not a final paper: release-candidate technical review findings
  remain open, and any claim that task-aware delivery causes better engineering outcomes, beats a
  baseline, or provides superiority stays blocked on the controlled Track B campaign and its
  raw-artifact review.
