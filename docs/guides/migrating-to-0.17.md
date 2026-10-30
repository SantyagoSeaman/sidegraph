# Migrate to Sidegraph 0.17

Sidegraph keeps its memory in `.sidegraph/`, a folder you commit beside your code. Until 0.17
every record was its own small JSON file, so a store with a year of history held thousands of
files. From 0.17 a store can be **packed**: records that have settled are folded, byte for
byte, into shard files (`shards/00.shard` and so on; 64 by default, 16, 64 or 256 supported), and new records are still
written as one file each, exactly as before. The packed layout is called **format 2**. Nothing
is rewritten, summarised or dropped: a record's content is the same in a shard line as it was in
its file.

Your store keeps working as it is. 0.17 reads the old layout fully, and shows a notice at the
start of each session until you migrate. This guide is for the day you do.

Migration is one command, run once, by one person, on the default branch. The rest of this
page is what to do around it so that nobody on the team loses work. If you only read one part,
read the [team checklist](#team-checklist).

Terms used below. A **record** is one decision, fact, domain, entity or anchor list. A
**per-record file** is the one-file-per-record form. A **shard line** is one record inside a
shard file. **Maintenance** is `sidegraph-compact` on a packed store: it folds settled
per-record files into the shards. **The target branch** is the branch a pull request merges
into, normally `main`.

## Team checklist

Do these in order. The first four prevent every problem the later sections describe.

1. **Land or close open pull requests that edit existing records.** A pull request that edits a
   record main is about to fold meets a modify/delete conflict, which GitHub's web editor
   cannot resolve. Pull requests that only add new records are not affected.
2. **Update every Sidegraph client to 0.17 or later** (see [plugin and CLI](#plugin-and-cli)),
   and move your CI's Sidegraph source to the release.
3. **Stop every process older than 0.17 that touches the checkout**: MCP servers and editor
   sessions started before the update. Hooks are per call and need no stopping. Anything older
   than 0.14 matters most: a server of that age that was already running does not recheck
   the layout, and after the pull it can answer from an almost empty memory until restarted.
4. **Do not run `sidegraph-compact` on main while the migration pull request is open, and do
   not run it on the old layout on any branch that stays open.** The old command moves
   finished records (superseded, rejected or dropped ones) into `archive/` files, and a
   migration folding the same records meets that badly (see
   [two migrations](#two-migrations-or-a-migration-and-an-older-compaction)).
5. Check out the default branch, up to date, with nothing uncommitted under `.sidegraph/`.
6. Run `sidegraph-migrate --dry-run`, read the list of files it would fold, then run
   `sidegraph-migrate`. It never commits.
7. Run `sidegraph-verify`, then commit everything under `.sidegraph/` in **one** pull request.
   Review it: the diff is large because every per-record file disappears and the shards appear,
   but each shard line is the content of a file you already had.
8. Merge it. Everyone then pulls, and [should not pull while an agent is writing](#the-pull-window).
   Open branches merge or rebase main as usual; see [open branches](#open-branches).

An agent can run steps 6 and 7 for you: the `sidegraph:compact` skill runs the dry run, shows it,
asks for a yes, runs the real pass and verifies. It never commits.

## If you see "use a fresh store"

A teammate on 0.14, 0.15 or 0.16 who pulls a migrated store sees a message like
`migration tooling is deferred — use a fresh store`. For a format-2 store that advice is wrong.
It means: **upgrade to 0.17**. Do not delete `.sidegraph/`. Releases before 0.14 refuse the
store too, or, for a server already running, may serve an empty memory until it restarts.
After the upgrade restart every running session.

## Plugin and CLI

The plugin, and every command the plugin's skills run, uses `uvx` at the session's version, so
updating the plugin and starting a new session is enough. The optional terminal CLI on your
`PATH` is a separate copy and is the one most often left behind:

```bash
uv tool install --reinstall git+https://github.com/SantyagoSeaman/sidegraph.git@main
# or, if you only use the plugin:
uv tool uninstall sidegraph
```

The public `@main` source moves with releases; see
[mutable development references](../getting-started/installation.md#mutable-development-references).
The commands in this guide use the `uvx` form, which works for plugin-only users with no CLI
installed. Check that the command exists (it does only from 0.17):

```bash
uvx --from git+https://github.com/SantyagoSeaman/sidegraph.git@main sidegraph-migrate --help
```

If you installed the CLI (0.17 or later), the bare command names work the same. A fresh store
created by 0.17 is already format 2 and requires `>= 0.17.0`.

## Run the migration

From the repository root, on the default branch:

```bash
uvx --from git+https://github.com/SantyagoSeaman/sidegraph.git@main sidegraph-migrate --dry-run     # conditions + what would be folded; writes nothing
uvx --from git+https://github.com/SantyagoSeaman/sidegraph.git@main sidegraph-migrate               # 64 shard files by default
uvx --from git+https://github.com/SantyagoSeaman/sidegraph.git@main sidegraph-verify
git status --short .sidegraph/
```

Other commands on this page are written by name alone; give them the same `uvx --from …` prefix
if you have no CLI installed.

Use `--shards 256` for a store you expect to grow past about 25,000 records (shards stay
small); `--shards 16` for a tiny one. The number is chosen once; a store keeps the count its
shard files show.

The command refuses, with exit code 2 and the reason, when the checkout is not on the default
branch (pass `--any-branch` to override), when anything under `.sidegraph/` is uncommitted,
when a record file is unreadable or its name does not match its id, or when a symlink sits
inside the store. It first writes the new `format` marker, then folds; if it is interrupted,
run it again, or after the marker has been committed run `sidegraph-compact --settled-days 0`.
Outside a git work tree there is no default branch and no clock, and no git history to revert
to: back up the store first (copy the folder), then run it.

The marker requires Sidegraph `>= 0.17.0` from now on, so nobody on an older version can write
to the store by accident.

## After the migration

New records are written as per-record files, as before. They fold into the shards later, when
you run maintenance:

```bash
uvx --from git+https://github.com/SantyagoSeaman/sidegraph.git@main sidegraph-compact --dry-run
uvx --from git+https://github.com/SantyagoSeaman/sidegraph.git@main sidegraph-compact
```

Maintenance only folds a file that has been on your branch for at least **7 days** (change it
with `--settled-days`), runs on the default branch, and never commits. The 7-day window exists
so that a record still being ratified or edited stays a plain file where merges handle it
well. After a run, commit `.sidegraph/` in a pull request. Both SessionStart and
`sidegraph-doctor` say when maintenance is due: more than 200 per-record files (drift-check files count), a legacy
`archive/` segment that maintenance would delete, a shard line in the wrong shard file or
repeated, or a stray shard file.

Two rules keep the pull request clean:

- **A record edit lands on the target branch before the maintenance or migration pull request
  that folds it.** A maintenance or migration pull request carries no other record edit. CI
  compares each shard line with the target branch's tip and reports `shard-body-without-source`
  for a line whose body does not exist there: an edit and its fold in one pull request is
  that finding.
- **Do not hand-edit a shard.** Edit a record through Sidegraph; the edit appears as a
  per-record file, which wins over the shard line and is folded back later.

Things that look like problems and are not: `sidegraph-doctor` says `maintenance-due`; a record
exists both as a file and as a shard line (the normal state until the file is folded); a shard
line sits in the wrong shard file or twice with equal content (maintenance moves and collapses
these; verify does not report them).

## CI

Point your verify job at the release. The job from [CI/CD maintenance](ci-cd-maintenance.md)
needs no other change. Three things to know:

- **CI's answer is the one that counts.** Every comparison against "the target branch" means
  the target branch's tip, as CI computes it: `git merge-base origin/<target> HEAD` on GitHub's
  pull-request checkout gives that tip. A local `sidegraph-verify --against` run against the
  fork point can differ; CI's answer is the one that gates the merge.
- **`sidegraph-doctor --check` exits 2 on an unmigrated store.** While a store is
  unmigrated, doctor reports `store-not-migrated`, and `--check` fails on it. A CI job that
  runs `doctor --check` turns red until you migrate. Migrate in the same window you upgrade the
  job, or, since doctor has no flag to exclude a finding, run it with `--json` and filter that
  finding name out (for example with `jq`) before deciding. On a packed store with work
  pending doctor reports `maintenance-due`; since 0.19.0 `--check` does not fail on it.
- **`sidegraph-verify` has new findings**, all errors: `bad-shard-line`, `shard-conflict`,
  `duplicate-packed-record`, `shadow-divergence`, `binding-entry-dropped`, `shard-name-case`
  and `shards-without-format2` (these on the tree itself), and `shard-body-without-source` and
  `format-downgrade` (against a ref). The sections below say what each one asks of you. Full list:
  [`sidegraph-verify`](../reference/cli.md#sidegraph-verify).

A tool that reads the store files directly, not through Sidegraph, now has to read shard lines
too: [store format](../reference/store-format.md#format-2-packed-shards) says how. ADR export
works as before. Before 0.19.0, each ADR's `Record:` link pointed at the record's file or
shard, so an export after migration or maintenance rewrote every ADR document; since 0.19.0
generated documents name store records by id and stay byte-identical.

## Open branches

A branch cut before the migration meets it when it merges main, or when you rebase.

- A branch that only **adds** records merges clean.
- A branch that **edited an existing record** (ratified it, dropped it, superseded it, added an
  anchor) meets a modify/delete conflict on that record's file: main deleted the file when it
  folded it. If main folded the file **unchanged** (nobody edited the record on main since your
  branch was cut), **keep the file**: run `git add` on it. The file is a legal edit of the shard
  line, the merge is clean, and the next maintenance folds it. If you delete the file instead,
  your edit is lost and nothing reports it. If main **edited the record and then folded it**,
  keeping the file is not clean: `sidegraph-verify` reports `shadow-divergence` (and
  `illegal-field-change` against the target's tip) unless your file is a legal edit of main's
  version. Then delete the kept file (main's version stands and your edit is dropped, which
  you accept knowingly), or rewrite the file as a legal edit of main's version. GitHub's web editor cannot resolve a
  modify/delete: resolve it on the command line.
- Before you run maintenance, merge or rebase open branches, and read the dry run's list of
  files it would fold: each one is a file some open branch may be editing.
- A branch that raised the requirement with `sidegraph-require` or took a domain snapshot meets
  a conflict on the `format` file. **Resolve it to the major-2 marker** (the first line
  `sidegraph-store 2.0.0`, the `shards N` line kept) with the higher
  `requires-sidegraph` of the two. A marker resolved to major 1 while shards exist is refused
  by 0.17: run `sidegraph-migrate`, which finishes it, or restore the major-2 marker. Releases
  before 0.17 would serve that tree without its packed records.
- Two branches that each only raise the requirement: keep the higher one and keep `shards N`.

### The recipes

**Keep the file.** Only for a fold of an unedited file (see above). The file then differs from
the shard line only as a legal edit would, so `sidegraph-verify --against` the target's tip is
clean. For a record main edited and folded, delete the kept file or rewrite it as a legal edit.

**Binding union.** Two branches add different anchors to one record: git reports an add/add
conflict on `bindings/<id>.json`. Keep the **union**: every entry from both sides, no entry
lost. Anchor lists are order-free.

**Sanctioned removal.** Verify reports `shadow-divergence` when a per-record file is not a
legal edit of its shard line (a status moving backwards, for example), and `duplicate-ulid`
when two differing copies of one record exist (a file and an `archive/` line, for example; not
to be confused with `duplicate-packed-record`, which is two differing shard lines). You decide which copy is the
record. Delete the other (the file, or the archive segment once maintenance has packed its
other lines). Verify accepts that removal when the result is exactly one of the old copies.
Removing both sides at once is not accepted.

**`binding-entry-dropped`.** A bindings file lacks an anchor its shard line has: a merge
dropped it. The finding names the missing entity id and the shard line that binds it; read
that entity's name and file path from its own line in `shards/` (or with `find_entity` /
`get_entity_history`). CI fails until you put the anchor back, with the `add_anchors` tool (or
`sidegraph:heal-anchors`), giving the **same name and file path** the anchor had: Sidegraph then
reuses the existing entity (same id) and the finding clears. A different name or another path
mints a new entity, and the finding stays. Do not delete the shard line.

**`duplicate-ulid`.** Two copies of one record that differ: pick the copy that is the
record. Keeping the archive's copy means deleting the file; keeping the file's means deleting
the segment, after maintenance has packed the segment's other lines. Maintenance skips such a
record and tells you; it never picks a side.

## The limits: what a clean merge can lose

One case merges **clean** and can lose an edit, because git cannot see it. It needs four things
at once: main edited record A and folded it, a branch **older than the 7-day window** also
edits A, and the branch merges afterwards. The branch's version wins.

- For a bindings list, the loss of a whole entry is no longer silent: `binding-entry-dropped`
  fails CI and names the missing anchor. A changed entry is not detected: if main changed an
  anchor's relation and folded it, an older branch that adds another anchor restores the old
  relation, the entity is still present, and nothing reports it.
- For a decision edit that is not a legal transition of the shard line, `shadow-divergence`
  fails CI.
- For a legal-but-different edit (a domain's status, an entity's descriptor) nothing reports it,
  and neither does a bindings relation change, as above.

This is accepted, and is rare: in 161 merges with store changes across the stores of two projects, none
had both sides touch one record. The 7-day window is why it is rare: it covers ratification, the common
case. Two limits of that window. The window starts when a change lands through a **merge commit, a
squash or a rebase**, which are GitHub's three merge buttons. A fast-forward or a direct push
keeps the original commit's date, so a record can fold the day it lands; branches older than
that day are exposed. And `--any-branch` lets you fold on a feature branch: its own edit
and fold in one pull request are `shard-body-without-source`, so that case is caught.

## The pull window

While git checks out a maintenance commit it removes the folded per-record files a moment
before it writes the new shard. A 0.17 writer that touches one of those records in that moment
builds the record from the half-checked-out tree. It is rare: it needs a pull of a maintenance
commit (weekly or rarer) to coincide with a write to a record that commit folded. It is not
guarded. **Do not pull while agents are writing, and do not pull while you run maintenance.**
The same window exists while maintenance publishes: a pull landing then makes the run's commit
lack a record, which CI reports as `illegal-deletion`; the record is still in git history.

## Two migrations, or a migration and an older compaction

- **Migration pull request open, main edits a record the pull request folds.** Modify/delete on
  that record's file. CI compares with main's tip, which holds the record only as main's edit,
  so the pull request's shard line is `shard-body-without-source`. Drop the pull request and
  migrate again on main. Alternatively merge main into it and rerun
  `sidegraph-compact --any-branch --settled-days 0`, which folds everything, so it gives up the
  7-day window for the other files.
- **Main runs `sidegraph-compact` on the old layout while the migration pull request is open.**
  Both sides delete the compacted files, and merging is clean, but the pull request's shard
  line for such a record can differ from the archive's copy, and maintenance cannot fix it.
  Drop the pull request and migrate again on main. This is why the checklist says no
  `sidegraph-compact` on main while it is open.
- **Two branches migrated.** With identical flags and the same store the result is clean.
  Otherwise (different `--shards`, or different records added first) the two share shard names
  with different bytes. Revert one branch's migration and maintenance commits before merging:
  that is the first remedy. If both are already merged, resolve each shared shard file by
  keeping **both sides' lines** (order does not matter), keep one `format` marker, and run
  maintenance: it moves every line to its bucket and removes stray files. Differing copies of
  one record are `duplicate-packed-record`: a person chooses. A record the pull request created
  and folded is `shard-body-without-source` until you write it back to its per-record file and
  remove its shard line.
- **Two maintenance runs on two branches.** Disjoint folds merge clean; two runs folding the
  newest records of one type usually conflict at the same sorted gap. Drop one side's
  maintenance commit and run maintenance again on the default branch. Keeping one side of each
  conflict is not a remedy: the other side's lines may be the only copy of a record.
- **A GitHub Revert of a packed edit.** An edit of a packed entity or bindings list is a
  per-record file in the pull request that made it. GitHub's Revert button deletes that file.
  The shard line still holds the older content, so the deletion reverts the record without
  saying so, and CI reports it as `illegal-deletion` (for a bindings list the deleted file's entry relations
  can also show as `illegal-field-change`). Instead of deleting the file,
  **rewrite it to the shard line's body**: copy the body out of the shard line into
  `entities/<id>.json` or `bindings/<id>.json`. Verify then sees the record at its older,
  legal state, and the next maintenance folds it.

## Rollback

Before anyone writes in format 2: revert the migration commit. The store is back to
per-record files, every record complete. After a maintenance run, revert **both** commits
(the migration and every maintenance commit). A revert that leaves a shard file behind is
`format-downgrade` in CI.

Your checkout keeps its local `index.db`, which is stamped for format 2. A 0.14–0.16 `Store`
opens the rolled-back store and re-stamps the index, but their hooks read the index directly and
serve nothing while it carries the format-2 stamp. After a rollback, delete `.sidegraph/index.db`
(it is derived, gitignored and rebuilt on the next open) before relying on a 0.14–0.16 client's
hooks.

## Checking afterwards

```bash
uvx --from git+https://github.com/SantyagoSeaman/sidegraph.git@main sidegraph-verify
# the transition check, as CI computes it: against the merge base with the target
uvx --from git+https://github.com/SantyagoSeaman/sidegraph.git@main sidegraph-verify --against "$(git merge-base origin/main HEAD)" --json
uvx --from git+https://github.com/SantyagoSeaman/sidegraph.git@main sidegraph-doctor
```

Only CI's answer counts. On your own machine the merge base is the target's tip only after you
have merged the target into your branch; before that, the local run can report an
`illegal-deletion` for a file main folded that CI would not.

If a refusal or finding here is not in this page, [troubleshooting](troubleshooting.md) lists
the notices (`store-not-migrated`, `maintenance-due`), and the
[CLI reference](../reference/cli.md#sidegraph-migrate) lists every flag, exit code and
finding.
