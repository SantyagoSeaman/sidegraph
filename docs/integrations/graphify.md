# Graphify integration

> **Start here:** Install the host plugin, start a new session in your repository, then [verify it](../guides/verifying-your-setup.md). [Glossary](../concepts/glossary.md).

Sidegraph uses [Graphify](https://github.com/Graphify-Labs/graphify) for entity extraction
and graph construction instead of building a second one. This page documents the engine
relationship: what Sidegraph reads, how corpora and versions work, and how to keep the graph
fresh.

For an existing store, follow the [Sidegraph 0.14 migration guide](../guides/migrating-to-0.14.md)
before upgrading Graphify. It preserves accepted domain membership with a committed snapshot
and coordinates the minimum client version across the team.

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

macOS or Linux:

```bash
cd /ABSOLUTE/PATH/TO/your-repo
export GRAPHIFY_NO_AUTO_REFRESH=1        # macOS, Linux: keeps Graphify off your agent skills
graphify update .
```

Windows PowerShell:

```powershell
cd /ABSOLUTE/PATH/TO/your-repo
$env:GRAPHIFY_NO_AUTO_REFRESH = "1"
graphify update .
```

Graphify 0.9.87 and later refresh the agent skills they installed (such as `~/.claude/skills/graphify/`) on the first run after a Graphify upgrade, unless the environment variable `GRAPHIFY_NO_AUTO_REFRESH=1` is set; any Graphify command triggers it, `graphify --version` included, and a skill you edited by hand is kept as `SKILL.md.bak`. Set the variable persistently instead of per shell: add `export GRAPHIFY_NO_AUTO_REFRESH=1` to your shell profile, or run `setx GRAPHIFY_NO_AUTO_REFRESH 1` on Windows. The hook `sidegraph-init` installs and the CI recipes set it, and the rebuild instructions Sidegraph prints name it. Graphify 0.9.6 has no such refresh and ignores the variable.

`graphify update` is the **LLM-free** build: no API key required. It works on **both**
corpora Sidegraph anchors to:

- **Code** — functions, classes, modules become nodes; `contains`/`references` edges link
  them; community detection (Leiden with Graphify's `leiden` extra, otherwise Louvain) assigns
  each node a community.
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
is a separate, explicit export: run `graphify export wiki` (or the
initial non-`--update` build with `--wiki`) to add it. The export itself runs locally from the
finished graph and its communities and needs no LLM call; only the semantic pass below (or an
LLM-enriched build) does. Don't expect `wiki/` after a plain
`graphify update .` — its absence there is correct, not a broken build.

A third build mode, `graphify extract . --backend <claude|gemini|openai|deepseek|ollama>`,
adds the **semantic pass**: it reads prose instead of just structure, producing `concept` and
`rationale` nodes (plus thematic communities) in the same `graph.json`. Every backend
except a local `ollama` needs an API key and costs real money per run; `ollama` talks to a local
server and needs no real key. Runs are cached per file, so re-runs only pay for changed docs.
Anchoring and `sidegraph-import` both treat its output identically to the AST pass's nodes —
see [`guides/semantic-docs.md`](../guides/semantic-docs.md) for the full two-pass workflow,
cost/cache expectations, and the bootstrap-import walkthrough.

Point Sidegraph at it with `SIDEGRAPH_GRAPH` (default `graphify-out/graph.json`). The CLI
commands resolve a relative default against the store's project, and a `--graph` you type
against the shell's directory; `sidegraph-bootstrap` resolves both against its `--root`, so pass
it an absolute `--graph` when the graph lives elsewhere. The MCP server resolves it the same way, against the
project of the store it serves, and so do the hooks: a relative `SIDEGRAPH_GRAPH` is looked up in the
store's project, not under `$CLAUDE_PROJECT_DIR`. See [Graph path resolution](../reference/cli.md).

## Linked worktrees read the main checkout's graph

A linked worktree without its own graph can borrow the main checkout's graph for read tools. Branch-only code is absent from that graph. Explicit sync and CLI defaults do not borrow it. Build a local graph when you need the branch's own structure.

See the [full contract](../reference/graphify-internals.md#linked-worktrees-read-the-main-checkouts-graph).

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

macOS or Linux:

```bash
uv tool install "graphifyy==0.9.90"   # pin to a known-good version
export GRAPHIFY_NO_AUTO_REFRESH=1        # macOS, Linux: keeps Graphify off your agent skills
graphify --version
```

Windows PowerShell:

```powershell
uv tool install "graphifyy==0.9.90"   # pin to a known-good version
$env:GRAPHIFY_NO_AUTO_REFRESH = "1"
graphify --version
```

(`[mcp]` is an optional extra — see below — and pins the same way: `"graphifyy[mcp]==0.9.90"`.)

Known-good: **0.9.6 and 0.9.90**. 0.9.90 was verified for the 0.14.0 release on this
repository and on the openspec corpus (build, sync and domain snapshot). Re-verify after
upgrading to another version.

### Graphify's own MCP server (`[mcp]` extra) is optional

`graphifyy[mcp]` adds Graphify's *own* MCP server — a deeper structure-query layer (`path`,
`explain`, and friends) over the same `graph.json`. It's entirely optional and coexists
cleanly with Sidegraph: both read `graph.json` read-only, and Sidegraph never needs Graphify's
server to function — it only ever reads the file directly via `GraphifyReader`. If you
register both, Sidegraph's `PreToolUse` delivery of records only intercepts
Claude Code's own `Read`/`Grep`/`Edit`/`Write` calls and Bash `sed`/`grep`/`rg`/`cat` lines (see
[`claude-code.md`](claude-code.md)), so a direct call to one of Graphify's own MCP tools bypasses
it entirely — the records of the files that call touches are not handed over the way they are
for a `Read`.

### `graphify claude install` is redundant with the Sidegraph plugin — skip it

Graphify ships its own Claude Code wiring via `graphify claude install`. That command writes
a `## graphify` section to `CLAUDE.md` **and two `PreToolUse` hooks into the project's
`.claude/settings.json`** — a `Bash|Grep` matcher (fires on a Grep call or a shell command that looks like
`grep`/`rg`/`find`) and a `Read|Glob` matcher (fires on reads of source/doc files) — both
injecting a "you MUST run `graphify query` first" nudge. This overlaps Sidegraph's own
`PreToolUse` records block, so the interaction is worth being explicit about.

It is **not a destructive conflict.** The two live in different files (Graphify's in
`.claude/settings.json`, Sidegraph's in the plugin's `hooks/hooks.json`), which Claude Code
merges rather than overwrites; by default both hooks only add `additionalContext` and neither
denies a tool call, so there is no blocking or deadlock (Graphify's opt-in strict mode,
`graphify claude install --strict` or `GRAPHIFY_HOOK_STRICT=1`, can deny the first raw read of
a session); and `graphify claude uninstall`
filters on Graphify's own matchers (`Bash`, `Read|Glob`, `Glob|Grep`) plus the text `graphify`
anywhere in the entry, so it never removes Sidegraph's `Read|Grep|Edit|Write` hook. Safe to run
with the plugin — but redundant.

**For a hand-wired setup,** the same filter would remove a Sidegraph `Bash` group whose command
carries the word `graphify`. The commands in the
[setup guide's snippet](../getting-started/claude-code-setup.md#2-add-the-hooks) do not: the hook
reads no graph path, so `graphify claude install` and `uninstall` leave them alone (checked
against `graphifyy` 0.9.6 and 0.9.90). Keep it that way when you edit them. A `PreToolUse`
command that carries `SIDEGRAPH_GRAPH=graphify-out/graph.json` goes with the group, and Sidegraph then stops
delivering records on Bash lines without a word.

The **Sidegraph plugin already supplies graph context for Claude Code**: its
`SessionStart` hook injects the graph-derived top-tier map, its `PreToolUse` hook already
hands over the records of a file when it is read or edited (non-blocking, each file at most
**once per agent**, and again after the agent's context is compacted or cleared), and its MCP server exposes the query surface. Graphify's `Read|Glob` hook, by contrast, fires on
**every** qualifying read and asks for `graphify query`. With both installed, a read can
receive both the graph-query instruction and Sidegraph's decision-memory block.

**Recommendation:** with the Sidegraph plugin installed, **don't run `graphify claude
install`** — Sidegraph already supplies graph orientation and records when files are read.
If you also want Graphify's `graphify query` instruction, keep
its hooks but set `SIDEGRAPH_GREP_NUDGE=off` (see [`claude-code.md`](claude-code.md)) so the
two don't double-nudge on reads. This is separate from the git hook that rebuilds `graph.json`: Sidegraph installs its own
([below](#keeping-the-graph-fresh-git-hooks)), which differs from Graphify's `graphify hook
install` in rebuilding in the main checkout only.

`graphify update .` prints "Re-extracting code files" / "Code graph updated" even on a
pure-markdown corpus with no code files at all — expected engine chatter, not a sign the
build failed or silently skipped your docs.

`GraphifyReader` is versioned against the `graph.json` shape it expects (node/link fields,
`built_at_commit`); a Graphify release that changes that shape breaks at most this one file —
by design, it is the only place Graphify specifics may live in Sidegraph.

## Keeping the graph fresh: git hooks

Run `sidegraph-init --hooks` in the target repository to install the refresh helper after deciding to enable it. It refreshes the main checkout's graph in the background after commits, merges and branch switches. If a hooks manager owns the hooks, add the line init prints. Read the log in the common Git directory if the graph stays stale. Re-run `sidegraph-init --hooks` to refresh the helper.

See the [full contract](../reference/graphify-internals.md#keeping-the-graph-fresh-git-hooks).

## Troubleshooting

- **`cross-chunk ID collision` warnings during `graphify extract`** — your docs mention
  code entities by name and the LLM's mention-nodes collide with the AST's code nodes;
  the engine keeps whichever arrived first. See the "When your docs reference your code
  heavily" note in [the semantic-docs guide](../guides/semantic-docs.md) for the
  per-subfolder extract + merge pattern. (`graphify update . --force` does not help: it only
  overwrites `graph.json` when the rebuild has fewer nodes and does not repeat the semantic pass.)

- **Graph looks stale** — re-run `graphify update .`; the git hook only rebuilds the
  **code** graph, not prose/ADR extraction, so doc changes need a manual
  `graphify extract . --backend <name>` for the semantic layer.
- **`graphify` command missing after install** — the `uv tool` bin directory isn't on `PATH`.
  Run `uv tool update-shell`, open a new shell, and retry.
- **Sidegraph sees no anchors / graph absent** — Sidegraph degrades silently when
  `graphify-out/graph.json` (or the path in `SIDEGRAPH_GRAPH`) doesn't exist yet; decisions
  still write, but an anchor that needs the graph cannot resolve: `add_decision`, `supersede_decision` and
  a proposal keep their code anchors as `orphaned` (reported in `anchors_orphaned` with the
  reason `no-graph`), and an initiative binding is still made.
- **Sync reports `orphaned` after a rename** — inspect the current target and follow
  [status-sensitive anchor recovery](../guides/surviving-refactors.md#reading-and-healing-stale-decisions-warnings).
  Add precise anchors when the rule still holds. Use a meaningful successor only for an
  accepted, open predecessor whose claim changed. An obsolete proposal is dropped after
  human review, and a decision with no replacement can be retired under its guards.
- **`built_at_commit` looks one commit behind** — if the graph was already rebuilt against a
  dirty working tree (e.g. a pre-commit hook ran `graphify update .` before the commit landed),
  the next `graphify update .` you run right after committing can be a pure cache no-op (no
  file content changed since that pre-commit build), which leaves `built_at_commit` stamped to
  the *previous* commit. Sidegraph's own sync gate is unaffected — the content hash folded into
  `graph_version` (see [`reference/configuration.md`](../reference/configuration.md#graph-version-semantics))
  still matches either way, so the full pass is correctly skipped too (a move it left as `moved_uncommitted`
  is remembered and re-verified once `HEAD` moves, without a rebuild) — but any decision
  captured in that window has `provenance.graph_version` recorded with the stale commit id, not the new HEAD. If
  exact provenance commits matter for that window, touch a tracked file (or otherwise force a
  real rebuild) after committing so `built_at_commit` catches up before capturing more
  decisions.
