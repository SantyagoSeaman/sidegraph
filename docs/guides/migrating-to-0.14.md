# Migrate to Sidegraph 0.14

Sidegraph stores decision history beside your code. Domains name the project areas that
memory covers. An anchor binding links a stored record to code or a document; an orphaned
binding is a link that no longer resolves.

Use this guide **after Sidegraph 0.14.0 is released** for the tested Graphify
0.9.6 → 0.9.90 upgrade. Other starting versions need their own baseline verification.
Newer Graphify builds regroup code and documents. A committed domain snapshot preserves
which domains each file and node belongs to across that change.

The procedure updates clients and protects installed skills, refreshes each clone's hook
helper, builds the old graph and snapshots its domains, commits that snapshot and its
version requirement, upgrades Graphify and rebuilds, then verifies the result—in that
order. Anchors and imports heal on the first sync without a separate migration.

Coordinate the six steps with the team. Pause domain edits until verification is complete.
Every teammate runs the local builds, syncs and diagnostics in their own clone. Choose
one maintainer to run the snapshot command in step 3 and commit it in step 4; everyone
else pulls those two files in step 4 instead of taking another first snapshot.
Run commands at the repository root. Examples use `.sidegraph/` and
`graphify-out/graph.json`; if you use custom paths, check `SIDEGRAPH_DIR` and
`SIDEGRAPH_GRAPH` and adapt the two paths staged in step 4.

Every Sidegraph command below uses `uvx`, so installing the terminal CLI is optional.
The public `@main` source moves with releases; see
[mutable development references](../getting-started/installation.md#mutable-development-references).

## 1. Update every Sidegraph client and protect installed skills

Set `GRAPHIFY_NO_AUTO_REFRESH=1` **before any Graphify command**. Otherwise newer Graphify
may replace the agent skills it installed, including local edits. Set it both for this
shell and for future shells; persistent changes alone do not affect existing processes.

macOS/Linux, and Git Bash (choose the profile your shell actually loads):

```bash
export GRAPHIFY_NO_AUTO_REFRESH=1
# Bash example; use ~/.zshrc instead for zsh. Add the line only once.
printf '%s\n' 'export GRAPHIFY_NO_AUTO_REFRESH=1' >> ~/.bashrc
```

PowerShell:

```powershell
$env:GRAPHIFY_NO_AUTO_REFRESH = "1"
[Environment]::SetEnvironmentVariable("GRAPHIFY_NO_AUTO_REFRESH", "1", "User")
```

Windows Command Prompt:

```bat
set GRAPHIFY_NO_AUTO_REFRESH=1
setx GRAPHIFY_NO_AUTO_REFRESH 1
```

Record the current plugin session's Sidegraph version, the current CI source ref, and
any installed CLI version **before updating or removing them**. With the protection now
set, record the current Graphify version too:

```bash
export GRAPHIFY_NO_AUTO_REFRESH=1        # PowerShell/cmd: use the process setting above
graphify --version
uv tool list                            # includes versions of optional installed tools
```

Keep these values privately for rollback. The version check in step 5 will show the new
engine; it is not a record of your previous version.

Update the plugin in every host and start a fresh session. Check that its startup context
reports Sidegraph 0.14.0 or later. This terminal check verifies the command source used by
the rest of the guide; it does not update a running plugin session:

```bash
uvx --from git+https://github.com/SantyagoSeaman/sidegraph.git@main python -c "import sidegraph; print(sidegraph.__version__)"
```

If you installed the [optional terminal CLI](../getting-started/installation.md#option-a--the-cli-on-your-path),
update it too, or remove it if you only use the plugin and `uvx`:

```bash
uv tool install --reinstall git+https://github.com/SantyagoSeaman/sidegraph.git@main
# Alternative: uv tool uninstall sidegraph
```

Move CI's Sidegraph source to the released `v0.14.0` ref before the migration commit.
Do not leave an older client writing the store while the team migrates.

Keep the protection setting after migration.

## 2. Refresh the git-hook helper in every clone

Every teammate runs plain init with the updated Sidegraph:

```bash
uvx --from git+https://github.com/SantyagoSeaman/sidegraph.git@main sidegraph-init
```

This refreshes an installed or wired graph-refresh helper so its builds also protect
installed skills. The helper is local to each clone. A recorded decision to decline hooks
stays declined; you do not need to install hooks to migrate.

## 3. Rebuild with the current engine and snapshot the domains

Keep the current Graphify version for now. Explicitly rebuild and sync, then save doctor's
current diagnostics privately, including existing orphaned bindings. Sync is needed so
doctor checks binding statuses against this graph rather than skipping that check:

Every teammate runs this baseline block in their own clone:

```bash
export GRAPHIFY_NO_AUTO_REFRESH=1        # PowerShell/cmd: use the step 1 process setting
graphify update .
uvx --from git+https://github.com/SantyagoSeaman/sidegraph.git@main sidegraph-sync
uvx --from git+https://github.com/SantyagoSeaman/sidegraph.git@main sidegraph-doctor
```

Only the chosen maintainer takes the snapshot. Everyone else waits for the commit in
step 4 and pulls it instead:

```bash
uvx --from git+https://github.com/SantyagoSeaman/sidegraph.git@main sidegraph-domains snapshot
```

Retain the snapshot's printed per-domain file counts. Its `files` entries preserve the
baseline inventory of anchorable paths: file defaults together with node exceptions.
It includes paths with no domain. You can compare this inventory after the old graph is
overwritten; you do not need to keep the generated graph under version control.

**Already upgraded Graphify?** A first plain snapshot refuses a graph that links documents
to mentioned code, or whose generation cannot be identified. Rebuild the committed `HEAD`
with the old engine for this first snapshot instead:

```bash
uvx --from git+https://github.com/SantyagoSeaman/sidegraph.git@main sidegraph-domains snapshot --build-with 0.9.6
```

Commit relevant source and domain changes first: `--build-with` uses an isolated archive
of `HEAD`, not your working files. It selects the requested engine itself and removes its
temporary graph afterward. Do not use `--accept-current-engine` to recover the old domain
membership: that explicitly accepts the new engine's regrouping.

## 4. Commit the snapshot and the minimum-version requirement

Taking the snapshot also raises the store's minimum Sidegraph requirement to 0.14.0.
Review and commit both files together:

```bash
git add .sidegraph/format .sidegraph/domain-snapshot.json
git commit -m "store: preserve domains before the Graphify upgrade"
```

Have teammates update Sidegraph **before pulling this commit**, then restart their
sessions after pulling. The snapshot travels with the store; generated graphs and the
local index do not. Do not lower or hand-edit the requirement to let an old client in.

If the commit or pull started a background graph-refresh hook, wait for that build to
finish before step 5. Otherwise an old-engine build could finish after your new-engine
build and replace its output. See the [hook log and status](../integrations/graphify.md#keeping-the-graph-fresh-git-hooks).

## 5. Upgrade Graphify and rebuild

Replace the old tool, then rebuild under the protection set in step 1:

```bash
uv tool install --reinstall "graphifyy==0.9.90"
export GRAPHIFY_NO_AUTO_REFRESH=1        # PowerShell/cmd: use the step 1 process setting
graphify --version
graphify update .
```

`graphifyy` has two y's; the command is `graphify`. This is the engine version verified
for this migration. Re-verify before choosing a different one.

## 6. Sync and compare with the baseline

```bash
uvx --from git+https://github.com/SantyagoSeaman/sidegraph.git@main sidegraph-sync
uvx --from git+https://github.com/SantyagoSeaman/sidegraph.git@main sidegraph-doctor
uvx --from git+https://github.com/SantyagoSeaman/sidegraph.git@main sidegraph-domains snapshot --dry-run
```

There should be no additional orphaned bindings and no `engine-version` advisory. Investigate
other doctor findings on their own merits; the migration does not repair unrelated damage.
The dry run computes current membership using the saved snapshot; it does not just print
the saved counts and does not publish a replacement.

Common anchorable paths must keep the same domain sets. Newer extraction can add or remove
anchorable paths, changing a domain's file count. The following diagnostic prints the
added/removed paths per domain and fails on a common-path change, an unexplained count
delta, or no matching baseline paths. It uses the same snapshot computation as the dry run,
without publishing it.
Run this block in a POSIX shell, including Git Bash on Windows:

```bash
uvx --from git+https://github.com/SantyagoSeaman/sidegraph.git@main python - <<'PY'
from sidegraph import domain_snapshot
from sidegraph.config import default_graph_path, resolve_store_path
from sidegraph.engine.reader import GraphifyReader
from sidegraph.store import Store
from sidegraph.sync import take_snapshot

store_path = resolve_store_path()
before, signature = domain_snapshot.read_snapshot(store_path)
if before is None:
    raise SystemExit("No baseline snapshot: complete step 3 first")
with Store(store_path) as store:
    after = take_snapshot(store, GraphifyReader(str(default_graph_path(store_path))),
                          refresh=True, based_on=signature)

def inventory(snapshot):
    return {path: set(entry.domains).union(*(n.domains for n in entry.nodes))
            for path, entry in snapshot.files.items()}

old, new = inventory(before), inventory(after)
common = old.keys() & new.keys()
print(f"Paths: {len(common)} common; {len(new.keys() - old.keys())} added; "
      f"{len(old.keys() - new.keys())} removed")
if old and not common:
    raise SystemExit("STOP: no baseline paths match; check the graph's project root")
changed = sorted(p for p in common if old[p] != new[p])
for path in changed:
    print("CHANGED", path, sorted(old[path]), "->", sorted(new[path]))
old_counts = domain_snapshot.file_counts(before)
new_counts = domain_snapshot.file_counts(after)
unexplained = []
for slug in sorted(old_counts.keys() | new_counts.keys()):
    added = sorted(p for p in new.keys() - old.keys() if slug in new[p])
    removed = sorted(p for p in old.keys() - new.keys() if slug in old[p])
    start, end = old_counts.get(slug, 0), new_counts.get(slug, 0)
    print(f"{slug}: {start} -> {end}; added={added}; removed={removed}")
    if end != start + len(added) - len(removed):
        unexplained.append(slug)
if changed or unexplained:
    raise SystemExit("STOP: inspect the differences before continuing")
print("Domain comparison passed")
PY
```

Keep the baseline doctor output private and compare the actual orphaned bindings, as well
as the total. Stop if a new binding is orphaned, a common path changes domain sets, a count
delta is unexplained, or sync reports a failed domain refresh. Keep the committed snapshot;
`--refresh --domain SLUG` deliberately rederives that domain and is a later maintenance
operation, not a way to make a failed migration check pass.

## New projects

A project with no old domains has no membership to preserve. After installing the tested
Graphify version and setting the protection in step 1, build its graph, initialize the
store and explicitly require the new Sidegraph:

```bash
export GRAPHIFY_NO_AUTO_REFRESH=1        # PowerShell/cmd: use the step 1 process setting
graphify update .
uvx --from git+https://github.com/SantyagoSeaman/sidegraph.git@main sidegraph-init
uvx --from git+https://github.com/SantyagoSeaman/sidegraph.git@main sidegraph-require --sidegraph 0.14.0
```

This clears the migration advisory without taking a snapshot. Once you have accepted
domains, `sidegraph-domains snapshot --accept-current-engine` through the same `uvx` source
can instead freeze those new-engine memberships and raise the requirement. Do not use
this new-project shortcut for a store whose old domains you want to preserve.

## If you skip the snapshot or keep old clients

Without a snapshot, domains follow the new engine's communities; updating Sidegraph alone
does not freeze them. Recover through step 3's old-engine build if that was unintended.
Existing imports need no separate replay; compatible rationale labels are deduplicated.

Before a minimum requirement is raised, mixed client versions can repeatedly rebuild each
other's local index. After it is raised, Sidegraph 0.13 refuses a new session and asks for
an upgrade. An old session already running may keep serving its previous context until
restart. Some of its hooks stop delivering context after a new client rebuilds the index.
An old process kept running during the upgrade was also observed rebuilding that index
once, then refusing a later refresh.

Old raw CLI commands, Stop and commit-message hooks do not all enforce the same check.
Update or remove every old CLI and restart every session. The team must finish updating
clients before work resumes on the migrated store.

## Rollback

Stop sessions that use the store. Revert the migration commit so the previous format and
absence of a snapshot are restored together; also revert any separately committed
requirement change. Restore the exact previous Sidegraph and Graphify versions recorded
in step 1, including plugin/CI sources and optional CLI tools. Keep
`GRAPHIFY_NO_AUTO_REFRESH=1`, rebuild with that previous engine, and restart sessions.
Run sync and doctor through the restored Sidegraph source. Downgrading a tool without
reverting the store files leaves the requirement in place, so the old client still refuses.
