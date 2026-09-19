# Semantic docs layer

Sidegraph's founding question is the mind map: **what is in this project and how is it
connected?** — over source code *and* over the documentation that explains it. Graphify
answers that question with two passes over the same corpus, and Sidegraph anchors decisions
to whatever either pass produces.

## The story: structure, then meaning

- The **AST pass** (`graphify update .`, LLM-free) gives you *structure*: functions, classes,
  and modules for code; files and headings for Markdown/ADR-SAD docs. It also already emits
  `rationale` nodes wherever a docstring or comment explains *why* — see
  [Bootstrap import](#bootstrap-import-sidegraph-import), below.
- The **semantic pass** (`graphify extract . --backend <b>`, one API key) reads the prose
  itself and gives you *meaning*: `concept` nodes (the ideas your docs are actually about),
  `rationale` nodes extracted from argumentative prose (not just docstrings), and thematic
  Leiden communities that group concepts by topic rather than by file.

Both passes write the same `graphify-out/graph.json`; Sidegraph reads it read-only either
way (see [`integrations/graphify.md`](../integrations/graphify.md)). Nothing about the
decision store changes depending on which pass produced a node — anchoring and retrieval
treat a `concept` exactly like a code symbol.

## Two-pass workflow

```bash
# Fast path — every commit, free, no API key
graphify update .

# Semantic pass — when docs/diagrams change, or periodically
graphify extract . --backend claude
```

`graphify update .` stays your per-commit default (wire it into the post-commit hook per
[`integrations/graphify.md`](../integrations/graphify.md#keeping-the-graph-fresh-git-hooks)).
`graphify extract` is a separate, explicit step — run it after documentation changes
meaningfully, not on every commit.

`--backend` needs exactly one of these environment variables set: `ANTHROPIC_API_KEY`,
`GEMINI_API_KEY` (or `GOOGLE_API_KEY`), `OPENAI_API_KEY`, `DEEPSEEK_API_KEY` — or point it at
a local Ollama model, no key required. The backend's extra must be installed alongside
Graphify itself, e.g. for Claude:

```bash
uv tool install "graphifyy[anthropic]==<ver>" --force
```

**Model choice.** Backend defaults change faster than this guide. Pin `--model` (or the
backend's model environment variable) when reproducibility matters. Start with a low-cost
model and move up only if the extracted concepts look shallow on your corpus.

**Cost and cache expectations.** Graphify keeps a per-file SHA cache: re-running `extract`
only pays for files that actually changed, so the first run is the expensive one and every
later run is proportional to your edits. Observed on a real ADR/SAD corpus of 27 source files
(15 prose docs + 12 diagrams): **192 nodes** — 137 `concept`, 23 `document` (the engine emits
one `document` node per prose file plus each diagram's companion doc, not a 1:1 map onto every
source file — hence 23, not 27), 20 `rationale`, 12 `image` — with `references`,
`conceptually_related_to`, and `shares_data_with` relations; 7 of 16 detected communities
spanned multiple files (real thematic groupings, not just document structure). First-run cost:
**~$1.20**, ~3 minutes. Unchanged docs keep byte-stable `concept` nodes across reruns, so
re-extraction after a small edit is cheap.

Either build's output is read by the same `sidegraph-sync` content-hash gate — a rebuild from
`update` or from `extract` both bump `graph_version`, so sync (and the lazy sync on every
`get_task_context`/`SessionStart` call) picks up either one automatically.

### When your docs reference your code heavily

Internal design docs (plans, specs, runbooks) that name code entities line-by-line are a
poor input for the semantic pass: the engine's node ids are name-derived, so an LLM node
for a *mention* of `trader/order_router.py` collides with the AST node for the code
itself, and the engine drops one of the two (arrival order decides — sometimes the code
node loses; you'll see `cross-chunk ID collision` warnings). Sidegraph degrades safely
(an anchor to a dropped code node orphans rather than mis-binding), but the graph gets
poorer.

**The right answer for decision-dense docs: import them as memory, don't extract them as
structure.** A plan, spec, or ADR that names code line-by-line is already a *decision
record*, not a structural artifact the graph needs to model — that's exactly what
`sidegraph-import --docs <path>` is for (see
[the other importer](#the-other-importer---docs) below): it parses the document directly,
with no LLM and no collision-prone extraction step, and writes it straight into the decision
store, anchored via its own backtick-quoted mentions. Skip the semantic pass for this class of
doc entirely — docstring rationale already comes free from the AST pass, and code-referencing
prose adds mostly collisions with little upside.

For genuinely semantic folders that don't name code line-by-line (architecture overviews,
design rationale, glossaries), the semantic pass is still the right tool — just scope it, per
the engine's own advice: extract only that subfolder and merge, e.g.
`graphify extract docs/architecture --backend claude` followed by `graphify merge-graphs`.

To rebuild after a noisy extract, use Graphify's supported overwrite path:

```bash
graphify update . --force
```

This preserves Graphify's managed output structure and avoids a broad recursive delete.

## What changes for anchoring

`ANCHORABLE_FILE_TYPES` covers `{code, document, concept, rationale}` — `image` stays
unanchorable (not name-anchorable; no use case yet). A `concept` node anchors exactly like a
code symbol or a doc heading: a `name` + `file_path` descriptor, resolved to a Tier-2 leaf
plus its Tier-1 community (see [`concepts/anchoring.md`](../concepts/anchoring.md)).

**Multi-anchor (concept + file) is the recommended capture pattern** on semantic corpora, for
the same reason heading+file is recommended on AST-only doc corpora: a concept has no stable
id of its own, so anchoring only to the concept leaves the decision exposed to one kind of
churn. Anchoring to both the concept and its containing file means the file anchor's
surviving community keeps the decision retrievable even if the concept itself is renamed or
dropped on the next extraction.

**Migrating a doc corpus from the AST build to the semantic build orphans heading anchors —
temporarily.** Switching from `graphify update .` to `graphify extract .` on the same docs
replaces heading nodes with concept nodes — any decision anchored only to a heading (Tier-2)
loses that leaf on the next sync — but a later `graphify update .` **merges** the two layers
back together (headings return alongside concepts, healing the heading anchor), so the window
is exactly one orphaned sync between the first pure-semantic extract and the next update; file
anchors survive throughout, since both builds keep a file-level node, but its **label**
differs by build mode — the filename in AST/merged builds vs. the document title in
pure-semantic builds — so for the "file" half of the recommended concept+file multi-anchor,
anchor the **filename** form (it exists in AST and merged builds and heals on merge), not the
title form (which only exists in the pure-semantic build and would itself orphan on the next
`update .`). This is the ordinary orphan/staleness flow described in
[`guides/surviving-refactors.md`](surviving-refactors.md) — no special handling, just the
usual advice to multi-anchor, plus the filename-not-title choice above.

Edited (not deleted) doc files behave like an ordinary code refactor for concept churn:
re-extracting a changed file can rename or drop the concepts inside it, and the rebind ladder
handles that the same way it handles a renamed function — exact match, then unique name-only
match, then orphaned, never a guess.

## Bootstrap import (`sidegraph-import`)

`sidegraph-import` is two independent importers behind one command. This section covers the
default, **rationale-from-graph** mode; see
[the other importer](#the-other-importer---docs) below for the second one. You don't have to
drive either from the terminal yourself: saying "import our ADRs" in a session invokes the
[`sidegraph:import-adrs`](../../plugin/sidegraph/skills/import-adrs/SKILL.md) skill, which
walks the dry-run → review → import sequence below
conversationally (the command still runs underneath — import is deliberately a deterministic,
human-supervised CLI job).

The AST pass already extracts *why*, not just *what*: code docstrings/comments become
`rationale` nodes pointing at the entity they explain, with **zero LLM setup**. The semantic
pass extends the same idea to prose — ADR/SAD reasoning becomes `rationale` nodes too.
`sidegraph-import` (default mode) turns either into decisions in one pass, so you don't start
the decision store empty on a codebase or doc corpus that already has months of recorded
reasoning in it.

**Always dry-run first** — it costs nothing and shows you the volume before anything is
written:

```bash
uv run sidegraph-import --dry-run
```

This lists every candidate rationale (file, title), a summary line (`would import N
decision(s)`), and a per-file breakdown — read [`reference/cli.md`](../reference/cli.md) for
the exact output shape.

**Code repos are the zero-setup case.** Run `graphify update .` (no API key) and the AST pass
alone already yields real volume — the reference trading-bot repo produced **492** rationale
nodes from docstrings and comments. That's a lot in one sitting; on a repo that size, filter
before importing for real:

```bash
uv run sidegraph-import --dry-run                 # see the volume first
uv run sidegraph-import --path src/execution/ --limit 50
```

`--path PREFIX` is repeatable (only rationales whose `file_path` starts with one of the given
prefixes); `--limit N` caps how many are processed after filtering.

**Doc corpora need the semantic pass first**, since `rationale` nodes from prose only show up
after `graphify extract .`. Volume there tends to be far smaller — the reference ADR corpus
produced **20** rationale nodes — so a full unfiltered import is usually fine:

```bash
graphify extract . --backend claude
uv run sidegraph-import
```

**Ratification.** Imported decisions land `accepted` by default — most teams don't want to
gate hundreds of docstring-derived facts through manual review. Pass `--propose` if you want
them to land `proposed` instead, subject to the normal `sidegraph-ratify` gate — or
auto-ratified at write time when eligible under a non-`manual` `SIDEGRAPH_RATIFY_POLICY` (see
[`guides/capturing-decisions.md`](capturing-decisions.md#3-ratification)).

**Kind and retrieval placement.** Imported decisions default to `kind=adr` — recorded
reasoning, not a mistake — which by design keeps a bulk import out of the mistakes-first
block in `get_task_context`/`SessionStart`. They still surface as ordinary task-scoped
context (ADRs on seeds) when a session touches the file or entity they're anchored to;
override with `--kind` if you'd rather import as `lesson`/`constraint`/`gotcha`.

**Re-running is safe.** Import is idempotent per rationale: a rerun after re-extraction only
imports rationales that are actually new (a decision with the same canonicalized title, the
same origin file, and `provenance.source="import"` is skipped). Identical rationale text in
two different files still imports both — the idempotency key includes the file, not just the
title.

### The other importer: `--docs`

(The [`sidegraph:import-adrs`](../../plugin/sidegraph/skills/import-adrs/SKILL.md) skill
drives this mode conversationally too — same dry-run gate.)

Rationale-from-graph mode (above) needs the graph — it reads `rationale` nodes Graphify
already extracted. `sidegraph-import --docs <path>` is a different importer entirely: a
**deterministic markdown parser** (`src/sidegraph/doc_import.py`) that reads decision-shaped
`.md` files directly — no LLM, no API key. The *parsing* itself is pure text processing and
never touches the graph, but the command as a whole still requires one: a current,
readable `graph.json` is what every mention gets resolved against, and a document with
nothing anchorable at all (no resolvable mention, no matching file node) is skipped rather
than imported — see [`reference/cli.md`](../reference/cli.md#importing-decision-shaped-markdown---docs)
for the exact anchor-resolution rules. **Run `graphify update .` right before a `--docs`
import** — a missing graph fails the
command outright, and a *stale* one (new docs added since the last build) silently skips the
new files as unanchorable rather than erroring. Where rationale-mode turns *reasoning already
extracted into the graph* into decisions, `--docs` turns *markdown files on disk* into
decisions, one per qualifying document — the natural fit for an existing folder of ADRs or
specs that were never going through Graphify's semantic pass at all.

```bash
graphify update .                              # make sure the graph is current first
uv run sidegraph-import --docs docs/adr --dry-run
uv run sidegraph-import --docs docs/adr
```

See [`reference/cli.md`](../reference/cli.md#importing-decision-shaped-markdown---docs) for
the full flag reference (qualifying-document rules, anchor resolution, idempotency, output
shape) and [`guides/capturing-decisions.md`](capturing-decisions.md#already-have-adrs-import-them)
for when to reach for it instead of capturing decisions one at a time.

## See also

- [`reference/cli.md`](../reference/cli.md) — full `sidegraph-import` flag reference, output
  shapes, and exit codes.
- [`concepts/anchoring.md`](../concepts/anchoring.md) — tiers, multi-anchor, and the
  never-guess resolve ladder that anchoring (and import) both go through.
- [`integrations/graphify.md`](../integrations/graphify.md) — build modes, corpus types, and
  version pinning for the engine itself.
- [`guides/surviving-refactors.md`](surviving-refactors.md) — what happens to any anchor,
  concept or otherwise, when the graph is rebuilt.
