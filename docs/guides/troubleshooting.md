# Troubleshooting: what Sidegraph tells you, and what to do

> **Start here:** Match the message or check code to the table below, read its cause and run the suggested fix. A store is the committed record data. The index is a disposable local cache. Do not delete the store to clear a warning. [Glossary](../concepts/glossary.md).

For pinning a version in your own commands, see [pinned releases](../getting-started/installation.md#mutable-development-references).

Sidegraph reports store, graph and host problems separately. First match the symptom,
then inspect the diagnostic that can establish its cause. Keep the committed store.
The SQLite index is derived and can be rebuilt when it is the damaged component.

| Symptom | Likely cause | First action |
|---|---|---|
| No Sidegraph tools or opening context | Plugin scope, hook trust, executable discovery or repository selection | Follow [host verification](verifying-your-setup.md#case-1--host-wiring). OpenCode has no opening map. |
| Tools answer but no record appears for a file | No matching record, an unresolved anchor or the wrong store | Inspect the capture result and [verify anchored retrieval](verifying-your-setup.md#case-2--durable-capture-and-retrieval). |
| A host says Sidegraph could not start | A launcher failed before the hook entry point | Run the [hook diagnostic](../reference/hooks.md#if-you-see-this-message) in the host environment. |
| A graph or anchor warning appears | Missing, stale or differently rooted graph, or a changed target | Find the exact code in [the checks](#the-checks), rebuild from the root when needed, then sync. |
| `rg` or Grep finds nothing in `.sidegraph/` | The project-root `.ignore` hides the store from recursive search | Search it by path (`rg PATTERN .sidegraph/`), use the MCP or `sidegraph-doctor`, or read [the root `.ignore`](../reference/store-format.md#searching-the-repository-the-root-ignore) to opt out. |
| Doctor reports strict violations | Canonical data or transition verification failed | Read `sidegraph-verify` before attempting curation. Restore the named data from Git when appropriate. |
| Doctor lists skipped checks | Required index or graph evidence was unavailable, or the advisory pass stopped early | Restore that evidence and repeat the check. Skipped evidence does not prove health. |

SessionStart gives the agent status lines. Problems needing a person can also produce a
notice, normally at most once a day per check. The [hooks reference](../reference/hooks.md#notices-for-the-human)
owns the notice rules. `sidegraph-doctor` gives record-level diagnostics, while
`sidegraph-stats` provides a health overview.

## Windows plugin launch

If the package CLI works but the plugin's MCP server cannot start or no memory appears
at session start, check the [Windows setup boundary](../getting-started/installation.md#windows).
The bundled MCP launchers start `uvx` directly (`uv` in the development manifest),
so they need `uv` on the PATH the host gives its subprocesses; Claude Code runs its hooks through
Git Bash, so they need Git for Windows at its default options (the plugin does not start on
native Windows without it). Codex hooks use a PowerShell command on Windows, which parses
in both Windows PowerShell 5.1 and PowerShell 7. PowerShell 7 is optional. The MCP launchers and Codex hooks do not require an
additional POSIX shell; executable discovery still depends on the host PATH.

From the Windows environment used to launch the host, inspect executable discovery:

```powershell
Get-Command git, uv, uvx -ErrorAction SilentlyContinue |
    Select-Object Name, Source
```

Missing entries identify PATH gaps. After changing PATH, restart the agent host so its
subprocesses inherit the new environment. A GUI host may inherit a different PATH from a
newly opened terminal. If Codex lists no `sidegraph` tools, run its launcher by hand to see the
error (Ctrl+C stops it):

```powershell
uvx --from sidegraph --python 3.13 sidegraph-mcp --repo-root
```

If `claude mcp list` shows `sidegraph` as failed to connect, see
[The plugin's first start](#the-plugins-first-start): the Claude plugin runs the same command with
its own version, `sidegraph==X.Y.Z`.

For Codex, `codex mcp list` should show the `sidegraph` server, and `/hooks` should list its
hooks as trusted. If the hooks fail to start on Windows, update Codex: they need a release that
supports `commandWindows`.

Inside the Git Bash that runs the Claude Code hooks, check again:

```bash
command -v git uv uvx
```

All three should resolve in that environment. Reproduce a failed hook there with the
[SessionStart diagnostic command](../reference/hooks.md#if-you-see-this-message),
including its stdin redirection. POSIX assignments and `</dev/null` are not PowerShell
syntax; the Codex hooks' PowerShell form is the `commandWindows` key of the plugin's
`codex/hooks.json`.

If tools resolve but hooks stay silent, check the host guide's
[Claude plugin scope](../getting-started/claude-code-setup.md#option-a--plugin-recommended)
or [Codex hook trust](../getting-started/codex-setup.md#verify), and verify that the MCP
server and hooks resolve the same repository's store. Native plugin integration is not
established by core/installed-wheel CI; the native Windows route is verified with Claude Code
2.1.296 and Codex CLI 0.162.1, and WSL with a single Linux toolchain is the other supported route.

## The plugin's first start

Every plugin (Claude Code, Grok Build, Codex, Muse and OpenCode's MCP server) launches the
published package at the plugin's own version, `uvx --from sidegraph==X.Y.Z --python 3.13
<entry point>`, and that package pins its dependencies. Nothing floats between sessions. What can
go wrong is the first start after each plugin update, or the environment that start needs. When a
hook cannot start, its guard hides the error: `SessionStart` shows "Sidegraph: the SessionStart
hook could not start", the other hooks answer `{}`, and the MCP server shows as failed in
`claude mcp list`. To see the error, run the hook command in a terminal, in the project, with
stdin closed. `X.Y.Z` is the version in the plugin's manifest:

```bash
uvx --from sidegraph==X.Y.Z --python 3.13 sidegraph-session-start --repo-root </dev/null
```

- **The first start downloads.** The first session after each plugin update installs that
  version's package and its pinned dependencies (about 70 packages) from PyPI into uv's cache,
  unless uv already has them. On a machine with no Python 3.13, uv also downloads a managed
  CPython 3.13 from python-build-standalone on GitHub. Expect that start to take seconds to a
  minute on a slow connection. Grok waits only about two seconds for an MCP server, so its first
  session after an update may start without Sidegraph tools; start a new session. Later starts
  need no network. An offline machine, a proxy that blocks PyPI or GitHub, or an exported
  `UV_OFFLINE` or index setting can make the first start fail.
- **uv is set not to download Python.** The launch names Python 3.13 (`--python 3.13`). With
  `python-downloads = "never"` or `"manual"` in uv's settings, or `UV_PYTHON_DOWNLOADS=never`,
  and no Python 3.13 installed, the start fails with a message that no interpreter was found.
  Install one (`uv python install 3.13`, or any 3.13 on the PATH) or allow downloads. A newer
  Python on the machine does not stand in for it: the pinned dependencies have no builds for a
  Python released after the version you run.
- **uv older than 0.6.10.** The launch needs uv 0.6.10 or newer; the guard hides a refusal from
  an older uv. `uv --version` shows yours, and `uv self update` (or the command of the way you
  installed it) moves it.
- **An exported `UV_*` setting.** The `--python 3.13` flag outranks an exported `UV_PYTHON`, so
  that one has no effect. Any other `UV_*` setting still applies, because it is your own choice
  of where uv looks or what it may do: `UV_OFFLINE`, `UV_CACHE_DIR`, `UV_INDEX_URL` and
  `UV_DEFAULT_INDEX` can all change the result. Unset it for the session to test.
- **A store that refuses the plugin's version.** A teammate raised the store's minimum Sidegraph
  version (`sidegraph-require`), and the plugin is older. The hooks and the server refuse the
  store until you update the plugin. The message reads "This store requires Sidegraph >= X; this is Y."
- **Windows needs Git Bash.** The Claude Code hook commands are shell commands. Install Git for
  Windows (see [Windows plugin launch](#windows-plugin-launch)).
- **Platforms.** The pinned package has builds for macOS on Apple silicon, Linux (glibc) on x86-64
  and arm64, and Windows on x86-64. Intel Macs and musl Linux (Alpine) are not supported by any
  install route: the pinned `cryptography` has no wheel for an Intel Mac, `uvx` would try a source
  build that needs a Rust toolchain, and an exact pin leaves no room for a constraint of your own.

## The checks

Run `sidegraph-doctor` in the repository whose store you are inspecting. Start with its
strict verification section, then use the exact finding code below. Doctor reports these
findings without repairing the store. The [CLI contract](../reference/cli.md#sidegraph-doctor)
explains JSON output, exit codes and skipped evidence.

| Doctor finding | Likely cause | Next action |
|---|---|---|
| `dangling-record` | An open record has no committed anchors. A fact also has no live supporting decision. | Inspect the write result, then add accurate anchors with `add_anchors`. Restore a skipped binding file before adding replacements. |
| `degraded-binding` | A binding's last recorded status is degraded. This can come from ambiguous leaf resolution or its shared-community fallback. | Choose a precise target name and file path, then use [`add_anchors`](../reference/mcp-tools.md#add_anchors). An ambiguous sync outcome reports a candidate count. Doctor reports the recorded status rather than a candidate list. New anchors retain older bindings. A damaged binding stays reported until sync re-resolves its original target. See the [resolution ladder](../reference/anchoring-internals.md#the-rebind-ladder). |
| `orphaned-binding` | The binding's last recorded status is orphaned. Capture or sync could not resolve its target. | Check whether the target moved or disappeared. Adding an anchor keeps the old binding as history. A damaged binding stays reported until sync re-resolves its original target. Rebuild and use the [healing guide](surviving-refactors.md#reading-and-healing-stale-decisions-warnings). |
| `orphaned-record` | Every code anchor of an open record is orphaned. | If the graph is stale, rebuild it first. Otherwise inspect the record and heal its anchors. See [orphaned records](#orphaned-records). |
| `stale-proposal` | A proposed decision or domain has waited beyond the selected age threshold. | Review it with `list_proposed` or `sidegraph-ratify`, then accept it or drop it explicitly. |
| `unreferenced-entity` | No committed binding refers to the entity. | Inspect its identity and history before further capture. This is curation advice, not permission to delete canonical history. There is no automatic doctor repair. |
| `duplicate-entity` | Several entity identities describe the same target. | Inspect the ids and binding counts in the finding, and check precise name/file anchors before further writes. Keep historical records. See the [identity contract](../reference/cli.md#sidegraph-doctor). |
| `expired-open-validity` | A decision has passed its validity end while its status is still open. | Review its current meaning. Follow the linked version and refusal guards before [`retire_decision`](../reference/mcp-tools.md#retire_decision) for an accepted decision that has ended. Use [`ratify(drop=[...])`](../reference/mcp-tools.md#ratify) for an obsolete proposal. |
| `superseded-no-live-successor` | Every successor of a superseded decision or fact was rejected. | Restate the knowledge as a fresh anchored record. The old finding stays as history. See [recovery](#superseded-no-live-successor). |
| `code-drift` | A record-specific change, or a store-level note about unavailable comparison evidence. | First distinguish the record finding from the store-level evidence note. Follow [content review](#code-drift) for the appropriate action. |
| `never-surfaced` | An accepted decision has no recorded showing despite queries on its files. | Call `get_task_context` with those files. Inspect its anchors and the supplied `memory_budget` and `structure_budget` parameters. Retry with a larger memory budget when appropriate before changing the record. See [retrieval](retrieval-in-sessions.md). |
| `stale-instructions` | An injected instructions file quotes wording from a superseded decision. | Compare the text with the successor and update instructions if they still teach the abandoned rule. A historical quotation alone is not proof of a contradiction. |
| `unratified-accept` | An accepted agent-authored record has no ratifier stamp within the checkable period. | Inspect provenance and the capture policy before judging it. Replace deprecated auto-accept configuration if present. Do not backfill the store creation marker. |
| `graph-root-mismatch` | Graph paths appear relative to a subdirectory instead of the repository root. | Rebuild from the repository root with `GRAPHIFY_NO_AUTO_REFRESH=1`, then run `sidegraph-sync`. |
| `snapshot-split` | Several accepted domains inherit one frozen snapshot area. | Run the refresh command printed by doctor for every named part, review the snapshot change and commit it. See [snapshot reference](../reference/cli.md#sidegraph-domains-snapshot). |
| `domain-split` | One domain is replaced by several live domains. | Nothing is wrong by itself, so `--check` does not fail on it. If it should be a single replacement, drop the extra parts with `sidegraph-ratify --drop <id>`. |
| `domain-not-closed` | An accepted domain is still open although another domain replaced it: a Sidegraph older than 0.24 accepted the replacement without closing it. | Run the `sidegraph-ratify --drop <id>` doctor prints. To stop older clients from writing to the store, run `sidegraph-require --sidegraph 0.24.0` and commit `.sidegraph/format`. `--check` fails on it. |
| `domain-slug-conflict` | One slug is held by more than one live domain, usually after two branches each added a domain under it. | Keep one domain: drop the others with `sidegraph-ratify --drop <id>`, or supersede one under a different slug. `--check` fails on it. |
| `graph-stale` | HEAD differs from the graph build commit and a covered file changed or appeared. | [Rebuild the graph](#graph-stale) from the root with the refresh guard, then sync. |
| `engine-version` | The graph uses an untested engine version, or its store needs the matching migration. | Read the finding, pin a tested Graphify release and follow the [engine guidance](#engine-version). |
| `store-uncommitted` | Canonical store files have remained outside commits. | Review the store diff and commit the canonical changes in the same pull request as the work. Keep the derived index ignored. See [commit guidance](#store-uncommitted). |
| `plugin-off-in-subdirectories` | Claude project scope leaves nested launches without Sidegraph. | Enable the plugin in root local settings or user settings after reviewing the scope. See [scope recovery](#plugin-off-in-subdirectories). |
| `refresh-helper-outdated` | A Git hook uses an older refresh helper. | Run `sidegraph-init` to refresh the helper while preserving the recorded hook choice. See [helper recovery](#refresh-helper-outdated). |
| `store-not-migrated` | A nonempty store uses the earlier file layout. | Follow the team migration checklist, then run `sidegraph-migrate` on the default branch. See [migration](#store-not-migrated). |
| `maintenance-due` | A packed store has routine packing or cleanup work. | Run `sidegraph-compact` on the default branch and review the diff. This finding does not escalate the doctor check exit code. See [maintenance](#maintenance-due). |
| `deprecated-auto-accept` | The legacy `SIDEGRAPH_AUTO_ACCEPT=on` switch is set. | Choose the intended ratification policy, then unset the legacy switch where it is configured. See [policy migration](#deprecated-auto-accept). |
| `record-fields-requirement` | The store requirement predates fields the running client can write. | Upgrade all writers to the version named in the finding, run `sidegraph-require --sidegraph X.Y.Z` and commit the format marker. |

An entry in the JSON `skipped` list means that check did not run. It is not a clean result
for that check. Restore the evidence or resolve the reason the advisory pass stopped, then
inspect the report again. Store-level `code-drift` notes saying "check skipped" are findings,
not entries in that list. See [content review](#code-drift).

For `binding-status`, `never-surfaced` or `orphaned-records`, restore the derived index and
current binding evidence with `sidegraph-sync`. If `graph` is also skipped, build the graph
from the repository root first, then sync. Sync needs a readable graph to recompute binding
statuses. Doctor can still inspect previously computed statuses without that graph. For
`advisory-checks`, resolve the reported error, symlink or version requirement before
repeating doctor. Follow the report's reason when it names a narrower condition.

Strict violations such as `parse-error` are a separate namespace. Use the
[verification-code reference](../reference/cli.md#sidegraph-verify) for those.

SessionStart also uses the notice codes below. Some aggregate several doctor findings,
and others concern the live host rather than stored records.

| Session notice or line | Likely cause | Next action |
|---|---|---|
| `store-unreadable` | The selected store cannot open | Follow the [named recovery](#store-unreadable). Remove only a damaged derived index when that is the diagnosed cause. |
| `pending-ratification` | Proposals await review | Review with `sidegraph-ratify` or the ratify tool. |
| `graph-borrowed` | This worktree uses the main checkout graph | No repair is required. Build a worktree graph when branch-only code needs to appear. |
| `stray-store` | A nested empty store hides the intended repository store | Verify that it is empty, then remove only that extra store. See [stray-store](#stray-store). |
| `graph-missing` | No readable graph was found | Build the graph from the repository root. See [graph-missing](#graph-missing). |
| `orphaned-records` | One or more open records lost all concrete anchors | Inspect the corresponding `orphaned-record` doctor findings. |
| `store-files-skipped` | Canonical files could not be indexed | Run `sidegraph-verify`, resolve the named files and reopen the store. |
| `refresh-hook-missing` | No Git hook keeps a graph current | Choose `sidegraph-init --hooks` or `--no-hooks`. See [hook setup](#refresh-hook-missing). |
| `branch-only-records` | Records remain only on unmerged branches | Merge the intended branch through the normal pull request flow. |
| `version-skew` | Plugin and runtime versions differ | Follow the direction-specific [version checks](#version-skew), then start a new session. |
| `path-skew` | The optional CLI install differs from the session runtime | Follow the [CLI update or removal path](#path-skew). |

### store-unreadable

Sidegraph could not open `.sidegraph/`. The notice names the store path, the exception and a fix
that depends on the exception:

- A record file that does not parse (a merge conflict left markers in it) or does not validate no
  longer lands here: the store opens without that file and `store-files-skipped` names it. A file
  that cannot be read at all (a permission or I/O error) still does, and `sidegraph-verify` gives
  the details.
- A damaged or read-only `index.db`: it is derived and safe to delete. Remove
  `.sidegraph/index.db` and start a new session; it is rebuilt from the committed files. When
  there is no `index.db` to remove, or the database could not be opened at all (a read-only store
  directory, a `.sidegraph` that is a plain file), the advice is the generic one below instead.
- A store that requires a newer Sidegraph (`This store requires Sidegraph >= X.Y.Z; this is
  A.B.C.`): upgrade to at least that version and start a new session. A teammate raised it
  with `sidegraph-require`. On the Claude plugin, a new session alone changes nothing: update
  the Sidegraph plugin from its marketplace first, and the message says so.
- A missing format marker (`the store format marker … is missing`, or
  `store format marker is missing and git did not answer in time`): if a git command is
  running (a pull or checkout), start a new session when it finishes. Otherwise restore a
  committed marker with `git -C .sidegraph checkout HEAD -- format`. If the message names an
  index lock and no git command is running, remove that named lock and open again. For a
  repeated timeout with no git command running, check `git -C .sidegraph status`.
- A store written by a different Sidegraph version, for example a packed (format 2) store opened
  by 0.14 to 0.16, whose message ends `migration tooling is deferred — use a fresh store`:
  upgrade Sidegraph (on the Claude plugin, update the plugin), or use the version that wrote it. The message predates `sidegraph-migrate`
  and does not mean you need a fresh store.
- Anything else: `sidegraph-verify` gives the details.

An ordinary store write lock held by another process is transient. The hook stays silent for
it, and the next session start opens the store. The marker-specific git refusal above instead
names its recovery step.

### pending-ratification

Proposals nobody has ratified. The model's line gives the counts and the age of the oldest. When
the oldest is 30 days old, you also get a notice. Review the queue with `sidegraph-ratify`, or ask
the agent to use the ratify tool. `SIDEGRAPH_RATIFY_NUDGE=off` silences both the line and the
notice.

### code-drift

A record-specific finding means anchored code changed or a previous scoped inspection no
longer applies. Inspect the current claim. Use `record_drift_check` if it still holds, or
follow the [status-sensitive recovery](surviving-refactors.md#reading-and-healing-stale-decisions-warnings)
if it changed. The drift-check tool requires a stamped, open
subject and an exact inspected Git revision. See its [write contract](../reference/mcp-tools.md#record_drift_check).

A store-path line saying "not checkable" or "check skipped" has no decision id to inspect.
It reports unavailable comparison evidence, such as missing Git, an unavailable scoped
check or records that predate commit stamping. It does not prove a claim changed. Restore
Git or the missing comparison evidence where possible and rerun doctor.

For records that predate commit stamping, the capture revision is absent historical
provenance. There is no comparison evidence to restore. That note remains while the check
counts those open legacy records. Close them only for a genuine content replacement,
retirement or proposal-drop reason. Do not backfill provenance or invent a content-free
successor to clear the note.

These store-level notes are `code-drift` findings, so `sidegraph-doctor --check` exits 2
on them. They are separate from the JSON `skipped` list. See the
[doctor output contract](../reference/cli.md#sidegraph-doctor).

The default text groups record findings by drifted file. `--flat` prints one line per
record. The `sidegraph:triage-drift` skill walks the content checks.
`SIDEGRAPH_DRIFT_NUDGE=off` silences the session line.

### graph-borrowed

A linked worktree has no graph of its own and reads the main checkout's. Code that exists only on
the worktree's branch is not in that graph. Nothing is wrong, and nothing needs doing unless you
want the graph to include the branch: build one in the worktree with `graphify update .`.

### graph-stale

The graph's build commit is behind `HEAD`, and a file the graph should hold has changed or
appeared since. Memory cannot see or anchor to code added after the build. Run
`graphify update .` with the environment variable `GRAPHIFY_NO_AUTO_REFRESH=1` set,
from the repository root. In a worktree that borrows the main checkout's
graph, rebuild it there. A graph that cannot be compared with `HEAD` (no recorded build commit, a
graph outside a git repository, git unavailable) is not reported, and a recorded notice survives
the start where it could not be compared.

### stray-store

An older Sidegraph created an empty store beside the directory a session was started in, and the
repository's own store, with the records, sits above it. The session uses the empty one. Remove
the empty store, as the notice names it, to use the repository's.

### graph-missing

No code graph where Sidegraph looked, or one that could not be read. Memory still works from the
store, but graph-based matching and concrete target resolution are unavailable. Build the graph from the
repository root by running `graphify update .` with the environment variable `GRAPHIFY_NO_AUTO_REFRESH=1` set. This is advisory, because a corpus of documents only runs
without a graph. It is a model line only, and `sidegraph-doctor` reports it as a skipped check,
never as a finding.

### engine-version

The code graph was built by a Graphify release this Sidegraph was not tested with, read from
its `graphify_version` stamp. Anchors and domains may read differently from a tested build.
Install a tested release listed under [Engine version pinning](../integrations/graphify.md#engine-version-pinning),
then rebuild the graph. An absent stamp (a legacy graph or one written by
`update --no-cluster`) counts as tested and does not produce this advisory. Doctor exits 0 by default and 2 under `--check` when this finding is present.

The second case is a mention-edge graph (Graphify 0.9.62 or later) over a store without a
minimum Sidegraph version, so the store has not been migrated. Follow the
[0.14 migration guide](https://github.com/SantyagoSeaman/sidegraph/blob/main/docs/guides/migrating-to-0.14.md).
Its domain snapshot also sets the minimum version, which clears this line.

### orphaned-records

Open decisions and facts whose every leaf anchor is orphaned: retrieval reaches them only through
their file or domain. If the graph is stale, rebuilding it re-anchors them. Otherwise
`sidegraph-doctor` lists each record as an `orphaned-record` finding, and the
`sidegraph:heal-anchors` skill walks the
[status-sensitive recovery](surviving-refactors.md#reading-and-healing-stale-decisions-warnings):
add precise anchors when the claim still holds, propose a meaningful successor for an
accepted, open record whose claim changed, or review retirement when a decision's subject
is gone. Invalidated proposals go to human review for dropping.

The severity depends on how old the records are. **Three or more written in the last 14 days** is
degraded: anchoring is failing now, as when a batch of proposals lands with its anchors orphaned
at once. Anything older is advisory, curation debt that does not need a notice. The thresholds are
unmeasured beyond two points.

The statuses live in the derived index and are recomputed by a sync. Right after a reload, such as
a `git pull` that changed a record, every anchor reads live until the next sync, so the check does
not run then and a recorded notice is kept. `sidegraph-doctor` lists `binding-status` and `orphaned-records`
among its skipped checks until then; `sidegraph-sync` recomputes the statuses.

### superseded-no-live-successor

A decision or fact is `superseded`, it has successors, and every one of them is `rejected`, so
retrieval shows nothing for it. Stores written before the live-head rule can hold this: under
manual ratification a successor proposal closed the old record when it was written, and
dropping the proposal later left the old record closed with nothing to continue it. That
propose-then-drop sequence no longer produces it. A successor that is `accepted`, still
`proposed`, `deprecated` (retired on purpose) or itself `superseded` keeps the chain healthy.
A superseded record with no successor at all is `sidegraph-verify`'s
`superseded-without-successor`, not this finding.

The advice restates a record, so doctor gives it only when the successors are fully known. While
`sidegraph-verify` reports a record it could not read in full (`parse-error`,
`bad-validity-window`, `filename-id-mismatch`, `bad-archive-segment`, `bad-shard-line`,
`unsafe-record-id`, `shard-conflict`, `duplicate-packed-record` or `symlinked-store-entry`), or
cannot run, the finding is withheld
for the whole store. Heal what verify reports, and the finding appears if it still applies.

The record cannot be superseded again, because `supersede_decision` and `supersede_fact` take
only a live head. Restate it as a fresh record with `add_decision` or `add_fact`, copying the
context, choice and rejected alternatives (or the statement and source) from the superseded
record and anchoring it the same way. The new record has no link to the old one, so the finding
stays on the old record as history. It is advisory and never fails `sidegraph-doctor --check`,
which would otherwise fail forever. Doctor never rewrites a record.

### store-files-skipped

Files under `.sidegraph/` that the last reload could not index. They are left out of memory and the
rest of the store loads. The usual causes are a merge that left conflict markers in a record file
(reason `parse-error`, also for a file that parses but does not validate) and a record whose id
does not match its file name. An archive segment with a line that cannot be read is listed as
`bad-archive-segment`: that line is left out and the rest of the segment loads. The notice names
the first file and the reason; for a segment it says what the segment needs (the lines that could
not be read are left out, restore it with git, never delete it). Run `sidegraph-verify` to list
them all, naming each file and, for a segment, each line, then restore them with git or fix them
by hand, and reopen the store. Two reasons are not fixed by a restore: `not a regular file` (a
directory, FIFO, socket or broken symlink named like a record or segment) means remove or
rename the entry, or replace it with the real file; `unsafe filename` means rename the file to
`<its id>.json`. Do not delete a record file, and never an archive segment: its
records have no other copy.

While a record file is left out, Sidegraph refuses to write it. A tool that would rewrite it, such
as `add_anchors` on a record whose bindings file is the skipped one, fails with an error naming the
file and leaves it as it is, so the conflicted content is still there to resolve. Also while an
entity file is skipped, an anchor to the same name mints a second entity; once the file is fixed,
`sidegraph-doctor`'s `duplicate-entity` check shows the pair.

### refresh-helper-outdated

A git hook runs an older graph refresh helper that uses directory-based locking, or starts
Graphify without `GRAPHIFY_NO_AUTO_REFRESH=1`.
The directory lock can admit overlapping rebuilds with uutils `mkdir`; the replacement uses
exclusive PID files for both the runner lock and the stale-breaker gate.
Graphify 0.9.87 and later refresh the agent skills they installed,
such as `~/.claude/skills/graphify/`, on the first run after a Graphify upgrade unless that
variable is set. One hook that runs the helper is enough for the check to fire, and a recorded "no"
does not silence it. Run `sidegraph-init` with no flag: it rewrites the helper and keeps your
recorded hook choice. The helper lives in `.git/hooks`, so each clone needs this once. `sidegraph-init --hooks` would also reset a recorded "no".

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

### store-not-migrated

The store keeps one file per record, the layout before 0.17, and holds at least one record. The
store works as it is; the packed layout is the one 0.17 is built for. The notice names
`sidegraph-migrate`, to run on the default branch with `.sidegraph/` committed, or the
`sidegraph:compact` skill, after the team checklist in the [0.17 migration guide](https://github.com/SantyagoSeaman/sidegraph/blob/main/docs/guides/migrating-to-0.17.md). An empty store
has nothing to migrate and says nothing. Doctor lists one finding at the store path, and
`--check` exits `2` on it.

### maintenance-due

A packed (format 2) store has work for `sidegraph-compact`: more than 200 per-record files (drift-check files count), a
legacy `archive/` segment that maintenance would delete, a shard line in the wrong shard file or
repeated with an equal body, or a stray shard file. Nothing is broken, and `sidegraph-verify`
does not report these states. A segment that stays because one of its lines is not safe to
delete does not count. Run `sidegraph-compact` on the default branch. Doctor lists one finding
at the store path; `--check` reports it but does not exit `2` on it. A legacy segment that maintenance would delete counts, whether its records are already in shard
lines or maintenance would pack them. The check does not consult git, so a record file that
still has to settle (or is untracked) is not predicted. It reads the shard files once; a store
with more than 200 per-record files (drift-check files included) is decided from the directory listing.

### deprecated-auto-accept

`SIDEGRAPH_AUTO_ACCEPT=on` is set in the environment of the session or of `sidegraph-doctor` (an MCP server started with its own `env` block is not seen here; its `propose_decisions` results carry a `deprecation` note instead). The
variable still works exactly as before (agent captures land `accepted`, with no ratifier stamp,
whether or not they have a live anchor) but it is deprecated and will be removed in a later release, announced in the
CHANGELOG. The replacement is `SIDEGRAPH_RATIFY_POLICY=auto-all`: eligible captures
self-ratify with a `ratified_by` stamp, and a capture with no live anchor stays `proposed` for a
human to review. Set the new variable where you set the old one and unset the old one. Doctor
lists one finding, at `SIDEGRAPH_AUTO_ACCEPT`; `--check` reports it but does not exit `2` on it.

### version-skew

The package that runs the hooks and the MCP server is not the version of the plugin that wired
them. Every SessionStart prints `Sidegraph <version>` right after the standing instruction, so a
session always shows which package it runs; this check compares that version with the plugin's
manifest (`plugin.json` under `$CLAUDE_PLUGIN_ROOT`, or else `$PLUGIN_ROOT`, which Codex sets
too). It compares numeric parts, so `0.10.0` is above `0.9.0`, and ignores a local label such as
`+local`.

- **The package is newer than the plugin.** The installed plugin copy is stale: its skills and
  hook wiring come from an older release. Update the plugin from its marketplace.
- **The plugin is newer than the package.** The session started before the plugin update, or
  Sidegraph is launched from a manual registration (`claude mcp add`, a hand-written config) that
  pins an older version. Restart the session, or update that registration.
- **What it can catch.** With the internal manifests the package and `plugin.json` come from one
  checkout and are bumped in one commit, so it never fires there. A plugin launch names the
  plugin's own version, so it does not fire there either. It fires when a manual registration pins
  another version than the plugin, or when the installed plugin copy is older than the package that
  runs. An old binary cannot report itself,
  so a skew is visible only once the binary that checks is new enough; the version line is what
  an older install lacks.
- **Where it runs.** The session surface only, and not at all without a plugin root, a manifest,
  or a manifest named `sidegraph` with a version. A session's Bash tool has no plugin root, so
  the setup skill cannot run it.

### path-skew

A `sidegraph-*` command on `PATH` is a second Sidegraph beside the plugin, typically a
`uv tool install`. That is fine while it stays current, but a `uv tool` install never
updates itself: after a release it keeps answering your own shell commands at the version it was
installed at, while the plugin's hooks, MCP server and skills run the session's version
(`uvx --from sidegraph==X.Y.Z --python 3.13` on every host). Old copies can also lack commands or flags (`sidegraph-init --hooks`, `sidegraph-stats`).

The SessionStart line names the command, the `PATH` directory and both versions. It reads the
install's `sidegraph-<version>.dist-info` and never runs the command.

- **You use only the plugin.** Remove it: `uv tool uninstall sidegraph`.
- **You use the CLI on purpose.** Update it: `uv tool upgrade sidegraph` for an unpinned install
  from PyPI. An install pinned to a version, or from `git+…@main`, moves to the session's version
  with `uv tool install --reinstall --python 3.13 sidegraph==X.Y.Z`, the command the line offers
  because it works for either. The line is shown whenever the versions differ, so a deliberately pinned or newer copy
  shows it daily.
- **Not a `uv tool` install** (pipx, pip). The line says "upgrade or remove" without a command.
- **Where it runs.** The session surface only. It does not run when no foreign `sidegraph-*` is
  on `PATH`, when the install has no or two dist-infos, or on a Windows layout.

### plugin-off-in-subdirectories

Sidegraph is on for a session started at the repository root and off for one started below it.
Claude Code reads the project settings (`.claude/settings.json`) of the launch directory only,
never of a parent, and the repository root's `.claude/settings.local.json` for every launch inside
the repository. `enabledPlugins` merges key by key, user settings first, then project, then
local. The check applies that model to the directories of the repository and reports two
problems:

- **(a) Enabled only in the root's project settings.** The plugin is on for a root launch through
  `.claude/settings.json` alone, so every directory below the root without its own enabling
  settings runs without it. Enable it in `.claude/settings.local.json` at the root (it applies to
  every directory) or in your user settings. Every collaborator of a project-scoped install has
  this, and the fix is per person, so (a) alone is a model line and a doctor finding with no
  notice: the line tells the model to tell you and not to change your settings unasked.
- **(b) A nested directory whose own settings leave it off.** A directory with its own
  `.claude/settings.json` or `.claude/settings.local.json` that sets `enabledPlugins` and whose
  merge with your user settings and the root's local file holds no enabling `sidegraph` key, or
  an explicit `false`. A nested settings file without `enabledPlugins` cannot change the merge,
  so it is not counted: case (a) already covers its directory. The line names the first such
  directory and how many there are. Enable it there, or at the root as above. A nested
  `enabledPlugins` is evidence that someone launches in that directory, so (b) also sends the
  once-a-day notice.

The plugin counts as on when the merged map holds a key whose plugin part, before the `@`, is
`sidegraph` with the value `true`. A plugin that is not enabled anywhere for a root launch is
not reported: nothing was switched off.

- **How it looks.** It lists every directory that holds a tracked file and looks for the two
  settings files in each directly, so a repository that ignores `.claude/` is covered. That is
  one `git ls-files` over the index, a few tens of milliseconds on a large repository; a
  directory that holds no tracked file yet is not walked. User settings are read from
  `$CLAUDE_CONFIG_DIR/settings.json` when that variable is set, else from
  `~/.claude/settings.json`.
- **Where it runs.** The session surface and doctor. Not run outside a git repository, with no
  `git`, or when `git ls-files` takes longer than five seconds. Doctor lists the finding with the
  code `plugin-off-in-subdirectories`, and `--check` exits `2` on it.
- **Why only a root launch tells you.** A session started in a directory where the plugin is off
  runs no SessionStart hook, so nothing speaks there. The line (and the notice, with case (b))
  comes from a session at the root, and the finding from doctor and the setup skill.

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

## The launch-commit file

Only plugin installs older than 0.25.0, which launched `@main`, use it; no current plugin
does. If a hook uses a stale or invalid cached commit, start a new session or remove the launch-commit file. See the [launch-commit contract](../reference/hooks.md#the-launch-commit-file) for its path, validation and recovery steps.
