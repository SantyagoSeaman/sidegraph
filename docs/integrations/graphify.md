# Graphify integration

Sidegraph **rents** [Graphify](https://github.com/safishamsi/graphify) for entity extraction
and graph construction instead of building a second one. This page documents the engine
relationship: what Sidegraph reads, how corpora and versions work, and how to keep the graph
fresh.

## graph.json is read-only input

**Sidegraph never writes into `graphify-out/graph.json`.** Graphify regenerates it from cache
on every rebuild — anything Sidegraph wrote there would be erased on the next commit. This is
the project's first non-negotiable invariant (see [`CLAUDE.md`](../../CLAUDE.md)): the
decision store is a **separate, repo-committed sidecar**; `graph.json` is strictly read-only
input to it.

The `GraphifyReader` (the engine seam, `src/sidegraph/engine/reader.py`) is the only module
that parses `graph.json`. It reads the NetworkX node-link file directly — no write path exists
anywhere in Sidegraph for it. Reads are defensive: a parse failure (the write is non-atomic)
is retried a few times before raising, so a reader constructed mid-write does not corrupt
anything or wedge the caller.

## Building the graph

```bash
cd /ABSOLUTE/PATH/TO/your-repo
graphify update .
```

`graphify update` is the **LLM-free** build: no API key required. It works on **both**
corpora Sidegraph anchors to:

- **Code** — functions, classes, modules become nodes; `contains`/`references` edges link
  them; Leiden clustering assigns each node a community.
- **Markdown / ADR-SAD docs** — every file and every heading becomes a node the same way,
  with the same file/community metadata. Sidegraph anchors decisions to doc headings exactly
  like code symbols.

Output lands in `graphify-out/` next to where you ran it. What you get depends on the build
mode:

```
graphify-out/
├── graph.json       # the graph Sidegraph reads — read-only to us
├── manifest.json    # per-file state (hashes etc.) `--update` diffs against next run
├── GRAPH_REPORT.md   # god nodes, surprising connections
├── graph.html        # interactive view
└── cache/            # SHA256 cache — only changed files are re-processed on the next build
```

This is what the **LLM-free `graphify update .`** build above produces — no `wiki/` directory.
Community-article generation (`wiki/`, agent-crawlable `index.md` + one article per community)
is a separate, explicit export that needs an LLM pass: run `graphify export wiki` (or the
initial non-`--update` build with `--wiki`) to add it. Don't expect `wiki/` after a plain
`graphify update .` — its absence there is correct, not a broken build.

A third build mode, `graphify extract . --backend <claude|gemini|openai|deepseek|ollama>`,
adds the **semantic pass**: it reads prose instead of just structure, producing `concept` and
`rationale` nodes (plus thematic Leiden communities) in the same `graph.json`. It needs one
API key and costs real money per run (cached per file, so re-runs only pay for changed docs).
Anchoring and `sidegraph-import` both treat its output identically to the AST pass's nodes —
see [`guides/semantic-docs.md`](../guides/semantic-docs.md) for the full two-pass workflow,
cost/cache expectations, and the bootstrap-import walkthrough.

Point Sidegraph at it with `SIDEGRAPH_GRAPH` (default `graphify-out/graph.json`, resolved
relative to the process's working directory).

## Non-git and doc-only corpora

A corpus that is **not a git repository** works fine — for example a standalone ADR/SAD
folder you point Graphify at directly. Two things follow:

- `graph.json` carries no `built_at_commit` field in that case. Sidegraph falls back to a
  **content-hash graph version** (`content:<sha256 prefix>` of the raw file), so
  [sync](../getting-started/quickstart.md) still detects every rebuild even without commits
  to key off.
- The `.sidegraph/` store still works, but can't itself be *repo-committed* unless the corpus
  is under git — put the corpus under version control if you want the decision store to
  travel with it.

## Engine version pinning

Pin the Graphify version you build against, and re-verify after upgrading:

```bash
uv tool install "graphifyy==0.9.6"   # pin to a known-good version
graphify --version
```

(`[mcp]` is an optional extra — see below — and pins the same way: `"graphifyy[mcp]==0.9.6"`.)

Known-good range: **0.9.6–0.9.8** — 0.9.8 was live-verified (build + sync + heal on a doc
corpus) as part of the release gate. Re-verify the same way after upgrading past 0.9.8.

### Graphify's own MCP server (`[mcp]` extra) is optional

`graphifyy[mcp]` adds Graphify's *own* MCP server — a deeper structure-query layer (`path`,
`explain`, and friends) over the same `graph.json`. It's entirely optional and coexists
cleanly with Sidegraph: both read `graph.json` read-only, and Sidegraph never needs Graphify's
server to function — it only ever reads the file directly via `GraphifyReader`. One honest
caveat if you do register both: Sidegraph's `PreToolUse` nudge only intercepts Claude Code's
own `Read`/`Grep` tool calls (see [`claude-code.md`](claude-code.md)), so a direct call to one
of Graphify's own MCP tools bypasses that nudge entirely — it's not redirected toward
`get_task_context`/`drill_down` the way a blind `Read`/`Grep` is.

### `graphify claude install` is redundant with the Sidegraph plugin — skip it

Graphify ships its own Claude Code wiring via `graphify claude install`. That command writes
a `## graphify` section to `CLAUDE.md` **and two `PreToolUse` hooks into the project's
`.claude/settings.json`** — a `Bash` matcher (fires when a shell command looks like
`grep`/`rg`/`find`) and a `Read|Glob` matcher (fires on reads of source/doc files) — both
injecting a "you MUST run `graphify query` first" nudge. This overlaps Sidegraph's own
`PreToolUse` nudge, so the interaction is worth being explicit about.

It is **not a destructive conflict.** The two live in different files (Graphify's in
`.claude/settings.json`, Sidegraph's in the plugin's `hooks/hooks.json`), which Claude Code
merges rather than overwrites; both hooks only add `additionalContext` and neither ever
denies a tool call, so there is no blocking or deadlock; and `graphify claude uninstall`
filters strictly on Graphify's own matchers (`Bash`, `Read|Glob`, `Glob|Grep`), so it never
removes Sidegraph's `Read|Grep` hook. Safe to run — but redundant.

Redundant because the **Sidegraph plugin already fronts the graph for Claude Code**: its
`SessionStart` hook injects the graph-derived top-tier map, its `PreToolUse` nudge already
redirects blind `Read`/`Grep` toward retrieval (non-blocking, **once per session**), and its
MCP server exposes the query surface. Graphify's `Read|Glob` hook, by contrast, fires on
**every** qualifying read and pushes toward the *code* graph (`graphify query`) rather than
the *decision* memory — so on a plain read you get two nudges pulling different directions,
and Graphify's is the noisier, unbounded one.

**Recommendation:** with the Sidegraph plugin installed, **don't run `graphify claude
install`** — you already have the graph-orientation and read-nudge behaviour, done more
conservatively. If you specifically want Graphify's own `graphify query` reflex on top, keep
its hooks but set `SIDEGRAPH_GREP_NUDGE=off` (see [`claude-code.md`](claude-code.md)) so the
two don't double-nudge on reads. This is separate from `graphify hook install` (the
[post-commit git hook](#keeping-the-graph-fresh-git-hooks) that rebuilds `graph.json`) — that
one is orthogonal and fine to keep.

`graphify update .` prints "Re-extracting code files" / "Code graph updated" even on a
pure-markdown corpus with no code files at all — expected engine chatter, not a sign the
build failed or silently skipped your docs.

`GraphifyReader` is versioned against the `graph.json` shape it expects (node/link fields,
`built_at_commit`); a Graphify release that changes that shape breaks at most this one file —
by design, it is the only place Graphify specifics may live in Sidegraph.

## Keeping the graph fresh: git hooks

Install Graphify's own hook once, in the target repo:

```bash
graphify hook install    # post-commit (and post-checkout) → rebuilds graph.json
```

This installs a **code-only AST rebuild** (no LLM) into `.git/hooks/post-commit` as a marked
block. Prose/ADR extraction is a model pass and is not re-run automatically — re-run
`graphify update .` (or the `/graphify` skill's `--update` form) whenever the docs change.

Chain Sidegraph's sync **after** Graphify's block, in the same hook, so the graph rebuild
always finishes first:

```bash
# .git/hooks/post-commit, appended after Graphify's marked block:
sidegraph-sync
```

This is a convenience, not a dependency: `sidegraph-sync` re-resolves anchors and is a cheap
no-op when the graph version hasn't changed. Because the same `graph_version` check also runs
lazily on every read path (`get_task_context`, `SessionStart`), a missed or skipped hook
invocation is self-healing — the next read catches the stale mapping and re-syncs before
serving context.

## Troubleshooting

- **`cross-chunk ID collision` warnings during `graphify extract`** — your docs mention
  code entities by name and the LLM's mention-nodes collide with the AST's code nodes;
  the engine keeps whichever arrived first. See the "When your docs reference your code
  heavily" note in [the semantic-docs guide](../guides/semantic-docs.md) for the
  per-subfolder extract + merge pattern, or reset with `rm -rf graphify-out && graphify
  update .`.

- **Graph looks stale** — re-run `graphify update .`; the git hook only rebuilds the
  **code** graph, not prose/ADR extraction, so doc changes need a manual re-run.
- **`graphify` command missing after install** — the `uv tool` bin directory isn't on `PATH`.
  Run `uv tool update-shell`, open a new shell, and retry.
- **Sidegraph sees no anchors / graph absent** — Sidegraph degrades silently when
  `graphify-out/graph.json` (or the path in `SIDEGRAPH_GRAPH`) doesn't exist yet; decisions
  still write, just without anchors, until a graph is built.
- **Sync reports `orphaned` after a rename** — expected and correct: Sidegraph never guesses a
  rebind. Heal it one of two ways: **restore the name** (undo the rename, or re-add the removed
  symbol/heading), then `graphify update .` followed by `sidegraph-sync` — the orphaned anchor
  heals back to `rebound`/`unchanged` once an exact match exists again; or, if the code the
  decision described is genuinely gone for good, **supersede the decision**
  (`supersede_decision`) instead of trying to heal the anchor. See
  [surviving refactors](../guides/surviving-refactors.md#reading-and-healing-stale-decisions-warnings)
  for the full rebind ladder and both healing paths.
- **`built_at_commit` looks one commit behind** — if the graph was already rebuilt against a
  dirty working tree (e.g. a pre-commit hook ran `graphify update .` before the commit landed),
  the next `graphify update .` you run right after committing can be a pure cache no-op (no
  file content changed since that pre-commit build), which leaves `built_at_commit` stamped to
  the *previous* commit. Sidegraph's own sync gate is unaffected — the content hash folded into
  `graph_version` (see [`reference/configuration.md`](../reference/configuration.md#graph-version-semantics))
  still matches either way, so sync correctly no-ops too — but any decision captured in that
  window has `provenance.graph_version` recorded with the stale commit id, not the new HEAD. If
  exact provenance commits matter for that window, touch a tracked file (or otherwise force a
  real rebuild) after committing so `built_at_commit` catches up before capturing more
  decisions.
