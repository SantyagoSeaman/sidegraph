# Graph refresh and worktree contract

> **Start here:** Use this page to inspect the implementation contract. For installation, follow the [host setup paths](../README.md). [Glossary](../concepts/glossary.md).

## Linked worktrees read the main checkout's graph

`graphify-out/` is gitignored, so a `git worktree add` checkout has the tracked store and no
graph. Without one nothing resolves through the graph: `get_task_context` finds no entity for a seed
unless it matches an anchor stored on a live binding. When the
store's own graph is missing and its repository is a linked worktree of a main checkout, the
read tools (`get_task_context`, `retrieve_decisions`, `query_structure`, `query_decisions`, `drill_down`) and the
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
and is not re-run automatically: `graphify update .` refreshes the Markdown structure but does not
repeat the semantic pass, so when the docs change re-run `graphify extract . --backend <name>` (or
the `/graphify` skill's `--update` form).

- **Which events.** After a commit, after a merge (a fast-forward `git pull` fires
  `post-merge`) and after a branch switch. A file
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
- **In the background, one at a time.** Git never waits for the rebuild. An exclusively created PID file keeps
  one rebuild running; a hook that fires while it runs makes it go once more when it finishes, so
  the graph never settles on a tree older than the last request. A lock left by a job that died
  is detected and cleared: by its pid, or, when the job died before writing one (a full disk, a
  quota), by its age, ten minutes. Re-running `sidegraph-init --hooks` refreshes the helper and clears a dead lock;
  a fresh lock without a PID is kept until the ten-minute grace period expires. One risk
  remains: after a reboot a pid can belong to a live, unrelated process, and the hook then waits
  until that process exits.
  Plain `sidegraph-init` also repairs an outdated helper in use, keeping the recorded choice.
  Older helpers used lock directories; the new helper respects a live old runner and can
  recover its stale directory. Both the lock and the stale-breaker gate now use shell
  exclusive creation, so locking does not depend on external `mkdir`.
  Installer cleanup takes the same breaker gate before removing a dead runner lock. A small
  native OS lock on `sidegraph-graph-refresh.repair` serializes installers while they recover
  a dead gate; the coordination file stays in the common git directory to keep a stable inode.
  Where native locks are unsupported, helper publication still proceeds and optional installer
  cleanup is skipped. On Windows, PID-bearing locks are left to the POSIX helper to recover,
  because its PIDs need not be native Windows PIDs. Installing or repairing a helper does not
  itself start a rebuild.
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
installs. It rebuilds in every linked worktree that commits, also installs a `post-merge`
hook since 0.9.90, and appends its block after any existing content. If both are installed,
the main checkout rebuilds twice on every commit (Graphify's lock serialises them), and linked worktrees rebuild too. Init
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
