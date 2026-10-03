# Troubleshooting: what Sidegraph tells you, and what to do

Sidegraph used to degrade without a word. The graph fell hundreds of commits behind, records
were written with every anchor orphaned, a merge conflict in `.sidegraph/` switched memory off,
and the only place any of it showed was a line in the model's context. Now one list of integrity
checks runs at every session start and says what needs fixing, to the person as well as to the
model.

- **The model** gets one status line per problem, at every session start, in the context the hook
  injects. The model forgets between sessions, so these are not rationed.
- **You** get a notice, shown by the host as a warning, for the problems that need you. A notice
  comes at most once a day for each check, comes again at once when a problem gets worse, and
  stops when the check finds nothing. The text names the fix.

`sidegraph-doctor` lists the same problems record by record, and `sidegraph-stats` leads with a
`HEALTH` line. Both read the store without writing to it.

## The checks

| Check | Severity | What it means | Shows in | Fix |
|---|---|---|---|---|
| `store-unreadable` | broken | The store cannot be opened, so memory is off for the session | session notice and line | `sidegraph-verify`, or remove the damaged index |
| `pending-ratification` | advisory | Proposals await a human's verdict | session line; a notice once the oldest is 30 days old | `sidegraph-ratify` |
| `code-drift` | advisory | Records are anchored to code that changed after their capture | session line | `sidegraph-doctor` |
| `graph-borrowed` | advisory | A linked worktree reads the main checkout's graph | session line | none needed |
| `graph-stale` | degraded | The graph was built at a commit `HEAD` has moved past | session line and notice, doctor, stats | `graphify update .` |
| `stray-store` | degraded | A subdirectory launch created a second, empty store | session line and notice | remove the empty store |
| `graph-missing` | advisory | There is no graph, or it cannot be read | session line | `graphify update .` |
| `orphaned-records` | degraded or advisory | Open records whose every code anchor is orphaned | session line; notice and stats when degraded; doctor | `sidegraph-doctor` |
| `store-files-skipped` | degraded | Store files the last reload could not index | session line and notice, stats | `sidegraph-verify` |
| `refresh-hook-missing` | advisory | No git hook keeps the code graph fresh | session line and notice | `sidegraph-init --hooks`, or `--no-hooks` to stop the reminder |
| `store-uncommitted` | degraded | Store files nobody committed for a day | session line and notice, doctor, stats | commit `.sidegraph/` in a pull request |
| `branch-only-records` | advisory | Records that exist only on unmerged branches | session line; a notice when a branch is a week old | merge the branch |

Severity decides who is told. **Broken** means memory does not work now. **Degraded** means it
works worse than it should, so you get a notice and `sidegraph-stats` shows it. **Advisory**
means something worth knowing, which the model's line and the commands above report. Three
advisory checks also send a notice, because the fix is yours to decide: a proposal queue that
has waited 30 days, a missing refresh hook, and records on a branch that has not moved for a
week.

### store-unreadable

Sidegraph could not open `.sidegraph/`. The notice names the store path, the exception and a fix
that depends on the exception:

- A merge conflict left in a record file, or a record that does not validate: run
  `sidegraph-verify` from the repository root to find the file, then resolve the conflict.
- A damaged or read-only `index.db`: it is derived and safe to delete. Remove
  `.sidegraph/index.db` and start a new session; it is rebuilt from the committed files. When
  there is no `index.db` to remove, or the database could not be opened at all (a read-only store
  directory, a `.sidegraph` that is a plain file), the advice is the generic one below instead.
- A store written by a different Sidegraph version: upgrade Sidegraph, or use the version that
  wrote it. Migration tooling is deferred.
- Anything else: `sidegraph-verify` gives the details.

A lock that another process holds is not a broken store. The hook stays silent for it, and the
next session start opens the store.

### pending-ratification

Proposals nobody has ratified. The model's line gives the counts and the age of the oldest. When
the oldest is 30 days old, you also get a notice. Review the queue with `sidegraph-ratify`, or ask
the agent to use the ratify tool. `SIDEGRAPH_RATIFY_NUDGE=off` silences both the line and the
notice.

### code-drift

Records whose anchored files changed after their capture. The line says how many. It is the
agent's work to supersede the ones that no longer hold, so there is no notice. `sidegraph-doctor`
lists them as `code-drift` findings, and the `sidegraph:triage-drift` skill walks them.
`SIDEGRAPH_DRIFT_NUDGE=off` silences the line.

### graph-borrowed

A linked worktree has no graph of its own and reads the main checkout's. Code that exists only on
the worktree's branch is not in that graph. Nothing is wrong, and nothing needs doing unless you
want the graph to include the branch: build one in the worktree with `graphify update .`.

### graph-stale

The graph's build commit is behind `HEAD`, and a file the graph should hold has changed or
appeared since. Memory cannot see or anchor to code added after the build. Run
`graphify update .` from the repository root. In a worktree that borrows the main checkout's
graph, rebuild it there. A graph that cannot be compared with `HEAD` (no recorded build commit, a
graph outside a git repository, git unavailable) is not reported, and a recorded notice survives
the start where it could not be compared.

### stray-store

An older Sidegraph created an empty store beside the directory a session was started in, and the
repository's own store, with the records, sits above it. The session uses the empty one. Remove
the empty store, as the notice names it, to use the repository's.

### graph-missing

No code graph where Sidegraph looked, or one that could not be read. Memory still works from the
store, but it cannot match files to records or anchor new ones. Build the graph from the
repository root: `graphify update .`. This is advisory, because a corpus of documents only runs
without a graph. It is a model line only, and `sidegraph-doctor` reports it as a skipped check,
never as a finding.

### orphaned-records

Open decisions and facts whose every leaf anchor is orphaned: retrieval reaches them only through
their file or domain. If the graph is stale, rebuilding it re-anchors them. Otherwise
`sidegraph-doctor` lists each record as an `orphaned-record` finding, and the
`sidegraph:heal-anchors` skill repairs them, by re-anchoring with `add_anchors` or by superseding a
record whose code is gone.

The severity depends on how old the records are. **Three or more written in the last 14 days** is
degraded: anchoring is failing now, as when a batch of proposals lands with its anchors orphaned
at once. Anything older is advisory, curation debt that does not need a notice. The thresholds are
unmeasured beyond two points.

The statuses live in the derived index and are recomputed by a sync. Right after a reload, such as
a `git pull` that changed a record, every anchor reads live until the next sync, so the check does
not run then and a recorded notice is kept. `sidegraph-doctor` lists `orphaned-records` among its
skipped checks until then; `sidegraph-sync` recomputes the statuses.

### store-files-skipped

Files under `.sidegraph/` that the last reload could not index, usually because a record's id does
not match its file name. They are left out of memory. The notice names the first file and the
reason. Run `sidegraph-verify` to list them all, then fix or restore them with git.

### refresh-hook-missing

No git hook rebuilds the code graph when it goes stale, so it will fall behind as code changes.
The helper `sidegraph-graph-refresh` is missing, or one of `post-commit`, `post-merge` and
`post-checkout` does not call it (in the `core.hooksPath` directory when that is set). The
model's line tells the agent to ask you and not to install anything unasked. Run
`sidegraph-init --hooks` to install the hook, or `sidegraph-init --no-hooks` to stop the
reminder (it records `sidegraph.graphRefresh=false` in the repository's git config; a global
`false` works too). What the hook does and where it goes:
[keeping the graph fresh](../integrations/graphify.md#keeping-the-graph-fresh-git-hooks).

If the hook is installed and the graph still goes stale, read `sidegraph-graph-refresh.log` in
the repository's git directory (the output of the last rebuild), then re-run
`sidegraph-init --hooks`: it rewrites the helper and clears a lock that a job which died left
behind (the helper also clears one by itself, by its pid or, when the job never wrote one, after
ten minutes). One case is not detected: after a reboot a recorded pid can belong to a live,
unrelated process, and the hook then waits until that process exits.

The check does not run in a repository without a graph, so a corpus of documents is never
nagged, nor when the graph is somewhere other than `graphify-out/graph.json` at the main
checkout's root, the only one the hook rebuilds. A linked worktree that borrows the main
checkout's graph is checked, because the hooks are shared; one with a graph of its own, a store
in a subdirectory, and a bare repository with worktrees (no main checkout) are not. A call added
by hand to a hook counts as installed, which
is how a hooks-manager setup (Husky, lefthook, pre-commit) clears it. A hook counts only when git
would run it and the call would run: the file is executable and the helper's name is on a line
that does not start with `#`, so a comment that names it, or a hook without the executable bit,
leaves the reminder on. It has no doctor finding, because a CI checkout has no hooks.

### store-uncommitted

Files under `.sidegraph/` that nobody committed for 24 hours. A record follows the branch and the
checkout it was written on, so one that stays uncommitted is invisible to every other checkout and
teammate, and a `git clean` or a re-clone loses it. The text gives how many files there are, how
old the oldest is and what kinds they are (decisions, facts, domains, bindings, entities, other),
and names the path to commit. Commit `.sidegraph/` in a pull request, in the same change as the
work that produced the records. The write tools remind the agent with a
[`commit_hint`](../reference/mcp-tools.md#commit-hint).

- **Age.** A new record or entity file is aged by the ULID in its name, which `git stash -u` and
  `pop` cannot rewrite; a modified file, or anything under `bindings/`, by its modification time;
  a deleted file by the modification time of its subdirectory, which is a lower bound. A name
  that is not a plausible ULID (dated before 2020, or more than a day ahead of the clock) is aged
  by its modification time too. New `archive/` files beside deleted records read as "a
  compaction" (`sidegraph-compact`).
- **Fully staged files do not count.** They are being committed now, so a
  `sidegraph-doctor --check` in a pre-commit hook never blocks the commit that fixes the problem.
  A file that was staged and then edited again still counts.
- **Where it runs.** Not run outside a git repository, when `git` is missing or slower than two
  seconds. Doctor lists one `store-uncommitted` finding at the store path, and `--check` exits `2`
  on it. A fresh CI checkout has nothing uncommitted.

### branch-only-records

Open records that exist only on local branches that are not merged into the default branch. They
reach it when the branch merges, which is normal for work in progress. The line names the number
of records, how many await ratification, and up to three branches by their counts. A notice
comes only when a branch holding records has a tip older than a week: that is work that may
never merge. The fix is to merge the branch. Ratifying the records there does not help, because
an accepted record on an unmerged branch is as stranded as a proposed one.

- **The default branch** is `origin/HEAD` when it resolves (in a pull-request flow the local
  `main` lags), otherwise the first of `origin/main`, `origin/master`, `main` and `master` that
  exists. With none of them the check does not run.
- **What counts.** Every record a branch adds under `decisions/`, `facts/` and `domains/`
  whose status is not terminal, and which the default branch does not already hold (a branch that
  was squash- or rebase-merged stays "unmerged" to git while its records are on the default
  branch). The branch you are on is left out. A record counts once, for the newest branch that
  holds it (a branch cut from another carries the first one's records too), and that branch's
  copy decides its status.
- **Delete branches you have squash-merged.** If the records were compacted away on the default
  branch afterwards (`sidegraph-compact`), they no longer exist there, and the leftover branch
  reads as holding records that never arrived.
- **Cost.** Every unmerged branch is scanned, oldest first, within two seconds. A scan that runs
  out of time says "at least" before the count and that the scan ran out of time; a partial scan
  that found nothing is not run, so a recorded notice is kept.
- It has no doctor finding, because a CI checkout has no local branches, and no stats item.

## How a notice behaves

- It comes **at most once every 24 hours** for each check. A second session start inside the day
  still carries the model's line, and no notice.
- It comes **again at once** when the check's severity is higher than the one last reported.
- It **stops** when the check runs and finds nothing, and a problem that comes back after a fix is
  reported at once. A check that did not run, because its evidence was missing or a switch is
  off, changes nothing.
- Two registrations of the hook for one session do not both notify.

There is no switch for notices. The existing `SIDEGRAPH_RATIFY_NUDGE` and `SIDEGRAPH_DRIFT_NUDGE`
still silence their own checks. The record of what was reported lives in the derived `index.db`,
as `integrity_notice:<check>` meta rows, so deleting the index repeats the notices once.

See [`hooks.md`](../reference/hooks.md#notices-for-the-human) for the exact output,
[`cli.md`](../reference/cli.md#sidegraph-doctor) for the doctor findings and the stats `HEALTH`
line.
