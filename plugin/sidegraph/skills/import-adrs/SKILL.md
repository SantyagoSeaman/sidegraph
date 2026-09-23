---
name: import-adrs
description: Use when a repo already has decision documents or recorded reasoning to seed the store from — "import our ADRs", "load the design docs into memory", "seed sidegraph from docs/adr", "импортируй наши ADR", or a fresh setup on a corpus with months of docstring rationale. Also reachable directly as /sidegraph:import-adrs. Drives sidegraph-import end to end: graph refresh first, dry-run first, triage the report with the human, gated real run, retrieval spot-check.
---

# Import ADRs

Seed the decision store from what the team already wrote — a folder of ADR/spec/runbook
markdown (`--docs` mode, a deterministic parser: no LLM, no API key) or the rationale
nodes Graphify extracted from docstrings/comments (default mode) — instead of starting
memory empty. CLI invocations below use bare names; in a repo without a Sidegraph
checkout, run them as
`uvx --from git+https://github.com/SantyagoSeaman/sidegraph.git@main <name>`.

## Preflight (both modes)

1. **Run from the repo root.** Doc paths resolve relative to the current directory and
   must match `graph.json`'s root-relative `source_file` entries — an absolute `--docs`
   path from the wrong cwd makes every anchor miss, silently.
2. **Rebuild the graph right before importing:** `graphify update .`. A *missing* graph
   fails the whole run; a *stale* one (docs added since the last build) silently skips the
   new files as unanchorable.
3. Initialize the store first if this repo was never set up (`sidegraph-init`; see
   `sidegraph:setup`) — import would otherwise create a fresh store at the default
   `.sidegraph` path, which may not be the one you meant.

## Pick the mode

- **`--docs <path>`** — for a folder of decision-shaped markdown (ADRs, specs, runbooks).
  One anchored `Decision` per qualifying document, anchored via its backtick-quoted
  mentions plus the document's own file node. This is also the right answer for
  decision-dense docs that name code line-by-line — import them as memory, do NOT run
  Graphify's semantic pass over them (mention-nodes collide with code nodes; see
  [semantic docs](../../../../docs/guides/semantic-docs.md#when-your-docs-reference-your-code-heavily)).
- **Default (rationale) mode** — for reasoning already in the graph: code docstrings/
  comments come free from the AST pass; ADR/SAD *prose* rationale needs a prior
  `graphify extract . --backend <b>` semantic pass (one API key, costs real money — see
  [semantic docs](../../../../docs/guides/semantic-docs.md)). Volume can be large (a
  reference repo yielded 492 rationale nodes) — filter with `--path PREFIX`/`--limit N`.

## Always dry-run first

```bash
sidegraph-import --docs docs/adr --dry-run     # docs mode
sidegraph-import --dry-run                      # rationale mode
```

Costs nothing, writes nothing, and shows exactly what a real run would do. Read the
report closely:

- `would import N decision(s), supersede M (skipped: A existing, B unanchorable, C
  not-decision-shaped, D unparseable, E superseded-frontmatter, F outside-profile)` — plus
  per-file breakdown and per-anchor `anchor skipped: <name> (<reason>)` lines.
- **Lots of `unanchorable` + you passed an absolute path?** That's the wrong-cwd trap from
  preflight — the command itself warns when ≥ half of anchor-attempted docs miss. Re-run
  from the repo root with a relative path.
- **`N skipped as template(s)`** — template detection (filename stem `template`,
  `type: template` frontmatter, placeholder-dominated body) working as intended, never a
  bug: a blank ADR template's own `**Status:** APPROVED` line must not become a decision.
- **`N would land proposed (source status: draft/proposed/pending/under review)`** — a
  doc whose own status reads as draft lands `proposed` regardless of `--propose`.
- **`not-decision-shaped`** — the doc has no H1 + recognizable decision sections
  (Context/Decision/Status/... or Trigger/Design/...); narrative or table-structured docs
  don't qualify (by design, for now) — record their content via
  `sidegraph:record-decision` if it matters.
- **`N file(s) skipped: not valid UTF-8, re-save as UTF-8 to import:`** (printed last, with
  the listed paths) — a file whose bytes aren't valid UTF-8 is skipped and named instead of
  aborting the run. Re-save the listed file(s) as UTF-8 and re-run to import them. No
  encoding is guessed on your behalf. A UTF-8 byte-order mark is fine: it is stripped and the
  file imports, so it never appears in this list.

## Gate the real run on the human

**Before importing for real, confirm with the human** (one message, all choices batched):

- **Volume** — what the dry run said would land.
- **`--propose` or not** — default writes `status=accepted` directly (right for a trusted
  corpus a team doesn't want to hand-review); `--propose` routes everything through the
  ratify gate instead (`sidegraph:ratify-decisions` afterwards; an eligible write
  auto-ratifies under a non-`manual` `SIDEGRAPH_RATIFY_POLICY`).
- **`--tag <t>`** (docs mode) — a durable batch marker like `--tag adr-backfill` makes the
  import auditable later.
- **`--kind`** — docs mode auto-classifies (a "Root cause" doc → `lesson`, else `adr`);
  rationale mode defaults `adr` (deliberately keeps a bulk import out of the
  mistakes-first block). Pin explicitly only when the human wants an override.
- **`--section-limit N`** (docs mode, default 2000 chars/field) — widen for long ADRs
  whose Context/Decision sections get `…[truncated]`.

Then run it without `--dry-run`.

## Verify

- Re-run the same command with `--dry-run` — everything should now count as
  `skipped: existing` (import is idempotent; an *edited* doc supersedes its old record on
  a re-import, keeping history).
- Spot-check retrieval: `get_task_context(files=["<one imported doc's path>"])` should
  surface its decision; with domains named, `drill_down` on a covering domain should list
  imported decisions for that area.
- If you imported with `--propose` (or any docs landed status-derived `proposed`), hand
  off to `sidegraph:ratify-decisions`.

## See also

- [`docs/reference/cli.md`](../../../../docs/reference/cli.md#sidegraph-import) — every
  flag, qualifying-document rule, anchor-resolution rule, and output shape.
- [`docs/guides/semantic-docs.md`](../../../../docs/guides/semantic-docs.md) — the
  two-pass engine workflow behind rationale mode, and cost/cache expectations.
- `sidegraph:record-decision` — one-at-a-time capture for what the importer can't parse.
