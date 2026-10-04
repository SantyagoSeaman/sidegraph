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

Point Sidegraph at it with `SIDEGRAPH_GRAPH` (default `graphify-out/graph.json`). The CLI
commands resolve a relative default against the store's project, and a `--graph` you type
against the shell's directory. The MCP server resolves it the same way, against the
project of the store it serves, and so do the hooks: a relative `SIDEGRAPH_GRAPH` is looked up in the
store's project, not under `$CLAUDE_PROJECT_DIR`. See [Graph path resolution](../reference/cli.md).

## Linked worktrees read the main checkout's graph

`graphify-out/` is gitignored, so a `git worktree add` checkout has the tracked store and no
graph. Without one nothing resolves: `get_task_context` finds no entity for any seed. When the
store's own graph is missing and its repository is a linked worktree of a main checkout, the
read tools (`get_task_context`, `query_structure`, `query_decisions`, `drill_down`) and the
`SessionStart` hook open the **main checkout's** graph instead. The path is the store's own
project-relative one, applied in the main checkout: a store at `.config/sidegraph` borrows
`<main>/.config/graphify-out/graph.json`.

The borrowed graph is **synced index-only**. A worktree's `index.db` is gitignored and starts
cold, and what makes retrieval work there is derived from the graph: each domain's `communities`,
the Tier-1 community bindings, the entities' `last_seen_*` mapping. Left cold, `drill_down` showed
a fraction of the decisions the main checkout shows and `get_task_context` lost its `## Related`
section. So the lazy sync runs against the borrowed graph, and it never rewrites a tracked file:
the one step of a sync that can, the moved rung (it follows a renamed file by rewriting
`entities/<id>.json`), abstains, because its evidence, the old path gone and the move in HEAD's
history, would come from the main checkout and not from the branch. A worktree keeps its tracked store
byte-identical to what git checked out. `SessionStart` builds the domain map in memory from the
store every time, instead of trusting a cached one, and says whose graph it reads:

> Sidegraph: this worktree has no code graph of its own, so memory reads the main checkout's
> (`<main>/graphify-out/graph.json`); code that exists only on this branch is not in it.

A file that exists only on the branch is not in that graph, and `get_task_context` says so
instead of calling the graph stale. A file the branch changed that the main checkout also has is
described as it is in the main checkout: its symbols and the edges between them are the main
checkout's, not the branch's, and nothing warns about it. A stale main-checkout graph is named as
such, with the checkout to rebuild it in (`graphify update .` there). A worktree is not rebuilt: a cold build in one took 43 seconds and about 18 MB on a large
repository, and the main checkout is where the graph is kept fresh.

What is never borrowed: `sync_anchors`, `list_domain_candidates`, every tool that writes a record,
and every CLI default (`sidegraph-sync`, `sidegraph-doctor`, `sidegraph-stats`). A worktree that has
a graph of its own reads and syncs it as before, and so does an absolute `SIDEGRAPH_GRAPH`.

The main checkout is found from the filesystem alone (the worktree's `.git` file, the `gitdir` it
names, that directory's `commondir`), with no git call. It is found only when the common directory
is a `.git` directory of a repository that is not bare (`core.bare`): a bare repository (also
`git clone --bare R x/.git`), the `.bare` layout, a repository whose git directory is separate and
not itself named `.git`, a submodule and a plain `git clone` (a review copy) have no main checkout to
borrow from. There, with no graph, `get_task_context` answers with a `## No code graph` block that
names the path it looked at and says to build it.

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
caveat if you do register both: Sidegraph's `PreToolUse` delivery of records only intercepts
Claude Code's own `Read`/`Grep`/`Edit`/`Write` calls and Bash `sed`/`grep`/`rg`/`cat` lines (see
[`claude-code.md`](claude-code.md)), so a direct call to one of Graphify's own MCP tools bypasses
it entirely — the records of the files that call touches are not handed over the way they are
for a `Read`.

### `graphify claude install` is redundant with the Sidegraph plugin — skip it

Graphify ships its own Claude Code wiring via `graphify claude install`. That command writes
a `## graphify` section to `CLAUDE.md` **and two `PreToolUse` hooks into the project's
`.claude/settings.json`** — a `Bash` matcher (fires when a shell command looks like
`grep`/`rg`/`find`) and a `Read|Glob` matcher (fires on reads of source/doc files) — both
injecting a "you MUST run `graphify query` first" nudge. This overlaps Sidegraph's own
`PreToolUse` records block, so the interaction is worth being explicit about.

It is **not a destructive conflict.** The two live in different files (Graphify's in
`.claude/settings.json`, Sidegraph's in the plugin's `hooks/hooks.json`), which Claude Code
merges rather than overwrites; both hooks only add `additionalContext` and neither ever
denies a tool call, so there is no blocking or deadlock; and `graphify claude uninstall`
filters on Graphify's own matchers (`Bash`, `Read|Glob`, `Glob|Grep`) plus the text `graphify`
anywhere in the entry, so it never removes Sidegraph's `Read|Grep|Edit|Write` hook. Safe to run
with the plugin — but redundant.

**For a hand-wired setup,** the same filter would remove a Sidegraph `Bash` group whose command
carries the word `graphify`. The commands in the
[setup guide's snippet](../getting-started/claude-code-setup.md#2-add-the-hooks) do not: the hook
reads no graph path, so `graphify claude install` and `uninstall` leave them alone (checked
against `graphifyy` 0.9.6). Keep it that way when you edit them. A `PreToolUse` command that
carries `SIDEGRAPH_GRAPH=graphify-out/graph.json` goes with the group, and Sidegraph then stops
delivering records on Bash lines without a word.

Redundant because the **Sidegraph plugin already fronts the graph for Claude Code**: its
`SessionStart` hook injects the graph-derived top-tier map, its `PreToolUse` hook already
hands over the records of a file when it is read or edited (non-blocking, each file at most
**once per agent**), and its MCP server exposes the query surface. Graphify's `Read|Glob` hook, by contrast, fires on
**every** qualifying read and pushes toward the *code* graph (`graphify query`) rather than
the *decision* memory — so on a plain read you get two nudges pulling different directions,
and Graphify's is the noisier, unbounded one.

**Recommendation:** with the Sidegraph plugin installed, **don't run `graphify claude
install`** — you already have the graph-orientation and read-nudge behaviour, done more
conservatively. If you specifically want Graphify's own `graphify query` reflex on top, keep
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

Let `sidegraph-init` install the refresh hook, once, in the target repo. It asks, in a terminal,
and `--hooks` installs without asking (`--no-hooks` declines, and records that so the
reminder stops):

```bash
sidegraph-init --hooks
```

It writes a small helper, `sidegraph-graph-refresh`, into the repository's `hooks/` directory
(inside the common git directory, so every worktree shares it), and a marked three-line block
into `post-commit`, `post-merge` and `post-checkout` that calls it. The helper runs
`graphify update .`, a **code-only AST rebuild** (no LLM). Prose/ADR extraction is a model pass
and is not re-run automatically: re-run `graphify update .` (or the `/graphify` skill's
`--update` form) whenever the docs change.

- **Which events.** After a commit, after a merge (a fast-forward `git pull` fires
  `post-merge`, which Graphify's own hook does not cover) and after a branch switch. A file
  checkout (`git checkout -- file`) and a `git switch -c` that stays on the same commit do not
  rebuild.
- **One graph.** The helper rebuilds `graphify-out/graph.json` at the main checkout's root, and
  init installs it only where that is the graph the store reads. A store in a subdirectory, a
  typed `--graph`, a linked worktree with a graph of its own and a bare repository with worktrees
  (no main checkout) get `graph refresh hook: skipped`, naming the graph the hook would rebuild.
- **The main checkout only.** A linked worktree reads the main checkout's graph (see
  [above](#linked-worktrees-read-the-main-checkouts-graph)), so a commit in one does nothing.
  Rebuilding there as well costs about 18 MB and 43 s for a cold build on one field corpus with
  25 worktrees, and agents commit in worktrees constantly.
- **In the background, one at a time.** Git never waits for the rebuild. A lock directory keeps
  one rebuild running; a hook that fires while it runs makes it go once more when it finishes, so
  the graph never settles on a tree older than the last request. A lock left by a job that died
  is detected and cleared: by its pid, or, when the job died before writing one (a full disk, a
  quota), by its age, ten minutes. `sidegraph-init --hooks` clears such a lock at once. One risk
  remains: after a reboot a pid can belong to a live, unrelated process, and the hook then waits
  until that process exits.
- **Where the output goes.** The last rebuild's output is `sidegraph-graph-refresh.log` in the
  common git directory (`.git/` in a normal clone).
- **No `sidegraph-sync` in the hook.** The next memory call notices the new graph version and
  re-anchors before it answers, the same lazy check described below. Chaining `sidegraph-sync`
  into a hook would race the background rebuild.
- **It never replaces a hook.** The block goes right after the hook's shebang line, so a
  foreign hook that ends in `exit` or `exec` cannot skip it, and every other byte of the file
  stays as it was. A second `--hooks` leaves the files byte-identical. A hook that is a
  symlink, is not executable, has CRLF line endings or is not a shell script (a Python or Node
  hook) is left untouched, and init prints the one line to add by hand. Damaged markers (a lone
  marker, reversed markers, two pairs) are reported and left alone.
- **`core.hooksPath`.** When it is set, a hooks manager such as Husky, lefthook or pre-commit
  owns that directory and regenerates it, so init writes nothing there. It installs the helper
  and prints the line to add to each of the three hooks in that directory; a call added by hand
  counts as installed, provided the hook is executable and the call is not in a comment.
- **Removal.** `sidegraph-init --remove-hooks` removes exactly the block's bytes (a file that
  Sidegraph created is deleted, a foreign hook ends as it was before), deletes the helper and its
  lock files, and clears the recorded choice.
- **The reminder.** Without the hook, a session start tells the model and, once a day, you
  (the `refresh-hook-missing` check in the [troubleshooting guide](../guides/troubleshooting.md#refresh-hook-missing)).
  It stays quiet in a repository with no graph, and after `--no-hooks`.

**Graphify's own hook** (`graphify hook install`) is not needed and is not what Sidegraph
installs. It rebuilds in every linked worktree that commits, writes no `post-merge` hook, and
appends its block after any existing content. If both are installed, the main checkout rebuilds
twice on every commit (Graphify's lock serialises them), and linked worktrees rebuild too. Init
reports it when it finds one, and never touches it; `graphify hook uninstall` removes it and
leaves Sidegraph's block as it was.

The lazy check keeps the store honest whichever way the graph was rebuilt: `graph_version` is
compared on every read path (`get_task_context`, `SessionStart`), so a graph that was rebuilt in
the background, or by hand, is caught by the next read, which re-syncs before serving context
(entities remembered under `pending_uncommitted_moves` are still re-verified once `HEAD` has
moved).

A graph that was **never rebuilt** is a different case, and the lazy check cannot see it: it
compares `graph.json` with the store's last sync, so a graph that stopped at an old commit
looks up to date for as long as it is left alone. Sidegraph compares the commit the graph was
built at (`built_at_commit`) with `HEAD` and says so on four surfaces: a `SessionStart` line,
an explanation after a `get_task_context` call that names a file the graph does not hold, a
`graph-stale` finding in `sidegraph-doctor`, and a `stale:` line in `sidegraph-stats`. The graph
counts as stale only when a file it should hold changed or appeared since the build, so a commit
that touches nothing the graph indexes does not trigger it. Only a rebuild fixes it:
`graphify update .` from the repository root, then `sidegraph-sync`.

Two details of the engine matter here. A `graphify update .` that finds the code topology
unchanged leaves `graph.json` untouched, and so its recorded build commit, but still rewrites the
`manifest.json` beside it; Sidegraph takes the later of the two file times as the build time, so
such a run clears the warning for files edited before it, even though the graph keeps reporting
the older commit. And `graphify cluster-only .` restamps `built_at_commit` without re-indexing
any file, so it can hide staleness: use `graphify update .`.

## Troubleshooting

- **`cross-chunk ID collision` warnings during `graphify extract`** — your docs mention
  code entities by name and the LLM's mention-nodes collide with the AST's code nodes;
  the engine keeps whichever arrived first. See the "When your docs reference your code
  heavily" note in [the semantic-docs guide](../guides/semantic-docs.md) for the
  per-subfolder extract + merge pattern, or force a clean rebuild with
  `graphify update . --force`.

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
  still matches either way, so the full pass is correctly skipped too (a move it left as `moved_uncommitted`
  is remembered and re-verified once `HEAD` moves, without a rebuild) — but any decision
  captured in that window has `provenance.graph_version` recorded with the stale commit id, not the new HEAD. If
  exact provenance commits matter for that window, touch a tracked file (or otherwise force a
  real rebuild) after committing so `built_at_commit` catches up before capturing more
  decisions.
