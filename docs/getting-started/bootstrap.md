# Bootstrap existing project rationale

`sidegraph-bootstrap` turns supported ADR/spec documents into reviewed Sidegraph memory and
then asks production retrieval to surface one accepted, live-anchored decision. It runs
locally and does not require an account or model API.

Commands use the `uvx` prefix the plugin uses; with the CLI installed ([Option A](installation.md#option-a--the-cli-on-your-path)) the bare names work too. (`@main` is a mutable ref: see [mutable development references](installation.md#mutable-development-references).)

The 10–15 minute path is a launch target that has not yet been measured across first-user
sessions.

```bash
export GRAPHIFY_NO_AUTO_REFRESH=1        # macOS, Linux: keeps Graphify off your agent skills
$env:GRAPHIFY_NO_AUTO_REFRESH = "1"      # Windows PowerShell: use this line instead
graphify update .
uvx --from git+https://github.com/SantyagoSeaman/sidegraph.git@main sidegraph-bootstrap --host claude-code
```

Bootstrap scans, previews, reviews, writes, verifies, checks the selected host, and proves
retrieval in one guided command. Preview and review use the same immutable plan. Nothing is
written until the user reviews the redacted action summary and types the literal `confirm`.

## Supported document profiles

Seven profiles are supported in v1; the six in the table below are auto-detected, and `madr`
(the same ADR dialect and globs as `generic-adr`, with its own ADR export template) is selected
only by naming it. Pass `--profile NAME` to select one; an explicit
choice always wins. Auto-detection selects a specific profile by its marker path alone, and `generic-adr` by its input globs. When several
specific profiles match, Bootstrap asks for an explicit choice rather than merging dialects.
`generic-adr` is selected automatically only when its own globs match and no specific marker
wins.

| Profile | Default input |
| --- | --- |
| `generic-adr` | `docs/adr/*.md`, `docs/decisions/*.md` |
| `superpowers` | `docs/superpowers/specs/*.md` |
| `genkovich-sdd` | `docs/features/*/adr/*.md`, `docs/features/*/sad.md` |
| `spec-kit` | `specs/*/plan.md` |
| `bmad` | `_bmad-output/planning-artifacts/architecture/*/ARCHITECTURE-SPINE.md` |
| `openspec` | `openspec/changes/*/proposal.md`, `openspec/changes/*/design.md`, `openspec/changes/archive/*/proposal.md`, `openspec/changes/archive/*/design.md` |

Permissive arbitrary-document extraction is not supported in v1. Use repeatable `--docs
PATH` for additional files or directories that still use the selected profile's dialect.

## Scan boundary and preview

Bootstrap resolves the repository root once and reads only the selected profile's globs,
explicit `--docs` paths inside that root, and explicit `--include` files. It rejects directly
supplied outside paths and resolved symlink targets outside the root. Reports use stable
repository-relative paths; an outside path is redacted to `<outside-repository>/<name>`.

The default size limit is 512,000 bytes. Bootstrap excludes `.git/`, `.sidegraph/`, `.venv/`,
`venv/`, `node_modules/`, `vendor/`, `dist/`, `build/`, `target/`, `graphify-out/`, `_build/`,
and `coverage/`, plus binary files and unreadable or over-limit files. Binary detection reads
only a 4,096-byte prefix. `--include FILE` may override the directory or size exclusion for a
known text file; it does not widen the rest of the scan. Only regular files are eligible,
including in-repository aliases to regular files. FIFO, socket, device and directory
inputs are excluded as nonregular even when included. The scanner rechecks the frozen
resolved target against the root and excluded directories before opening.

Source validation uses one unbuffered descriptor and strict incremental UTF-8 decoding.
Ordinary files consume at most the size limit plus one overflow byte; an included file is
bounded by its opened size plus one, and observed growth beyond that size is excluded as
`changed-during-scan`. Binary sniffing consumes at most the first 4,096 bytes. Available
nonblocking and no-follow open flags defend final-component FIFO/link replacement on
platforms that provide them. This does not promise atomic ancestor-directory containment
or an atomic content snapshot; planning/apply reads later remain separate.

The preview lists a candidate count
followed by each candidate's title, source, context, choice, rejected alternatives,
consequences, warnings, and anchors; it does not enumerate the files read or the exclusions
applied during the scan (those are tracked internally but not rendered).

Planning parses, redacts, validates, deduplicates, and reports anchor intents without opening
a writable store. A missing graph still permits preview, but Bootstrap prints the rebuild
instruction (run `graphify update .` with the environment variable `GRAPHIFY_NO_AUTO_REFRESH=1` set, in the project directory) and the resume commands, and stops before interactive review. Zero useful
candidates is `diagnostic`: it exits safely without creating canonical memory and does not
claim activation.

Candidate warnings distinguish missing choice, missing rejected alternatives, unresolved or
ambiguous anchors, a document with no anchor at all, likely current-state summaries,
duplicates within the plan, and duplicates of live canonical memory. Warnings inform review; they never silently accept a candidate. Each
warning code is rendered beside the consequence of accepting it anyway:

| Warning code | Consequence of accepting |
| --- | --- |
| `generated-projection` | generated ADRs are projections of existing memory, not new capture evidence |
| `missing-choice` | the document states no decision; the record would carry only context |
| `missing-rejected-alternatives` | no rejected alternatives were found; the most valuable field stays empty |
| `unresolved-anchor` | the anchor does not resolve; the record would not surface for that code |
| `ambiguous-anchor` | several entities match; Sidegraph never guesses, so the anchor stays degraded |
| `no-anchor` | the document has no anchor in the graph, so apply writes no record without one: if a record with identical content is already stored for this source, it is ratified, or a rejected one is revived as a new record that inherits its bindings; otherwise the candidate is skipped and listed on an UNANCHORED line. Rebuild the graph (for the default one, run `graphify update .` with the environment variable `GRAPHIFY_NO_AUTO_REFRESH=1` set) so the document has a node, or add a backticked mention of a real symbol, then rerun |
| `likely-current-state-summary` | this reads as a current-state summary, not a decision with a fork |
| `duplicate-within-plan` | another candidate in this same plan carries identical content |
| `duplicate-canonical-memory` | a record already exists for this source; accepting identical content leaves an accepted record unchanged, ratifies a pending proposal, or revives a rejected record — accepting changed content writes a replacement (closing any open record at this source), and edits made in an earlier review are not carried over |

## Review actions and canonical state

The available action stays visible at every line-oriented prompt:

| Review action | Canonical result | May affect retrieval after write |
| --- | --- | --- |
| accept | `accepted` | yes |
| edit | no result until reconfirmed | no |
| keep proposed | `proposed` | only through the quarantine tier |
| skip | no stored record | no |
| batch `N,M` | the listed candidates are accepted; every other remaining candidate stays `proposed` | accepted ones; the rest only through the quarantine tier |

`edit` opens one mode-`0600` temporary file through `$VISUAL`, then `$EDITOR`. Edited content
is parsed, redacted, validated, re-anchored, shown as a new diff, and confirmed again. If no
editor is configured, the candidate remains proposed. `skip` is run-local state and is never
stored. There is no default accept-all action: `batch` accepts only the numbers you list.

## Completion, recovery, and hosts

Bootstrap reports four outcomes independently: store, anchors, host integration, and
production-path proof. Proof selects an accepted, currently valid decision with a resolved
tier-2 anchor and calls the same `get_task_context` production retrieval path used by agent
sessions. File-bound applicability remains useful retrieval, but does not satisfy this
current-graph anchor proof. Optional domain suggestions do not block that proof.

| Host | MCP | SessionStart | Stop | Read/Grep PreToolUse | Completion label |
| --- | --- | --- | --- | --- | --- |
| Claude Code | verified | verified | verified | verified | complete v1 flow |
| Codex | verified when configured | verified when configured | verified when configured | unsupported | best-effort only |

The host check reads the manual registration files first: `.mcp.json` and the `hooks` of
`.claude/settings.json` for Claude Code, `.codex/config.toml` and `.codex/hooks.json` for Codex.
When they are not complete, an enabled Sidegraph plugin satisfies the check.

For Claude Code, the `enabledPlugins` merge must enable `sidegraph@<marketplace>` for a session
launched at the bootstrap root: user settings, that directory's `.claude/settings.json` and
`.claude/settings.local.json`, then the repository root's `.claude/settings.local.json` when
launching below it. `$CLAUDE_CONFIG_DIR` replaces `~/.claude` for user settings when set.
The terminal prints `INTEGRATION  Claude Code plugin sidegraph@sidegraph enabled (MCP + hooks)`.
For Codex, `[plugins."sidegraph@<marketplace>"]` with `enabled = true` in
`$CODEX_HOME/config.toml` (default `~/.codex/config.toml`) satisfies its supported checks.
Under `INTEGRATION  Codex best-effort`, it prints
`plugin sidegraph@sidegraph enabled; its hooks run once trusted in Codex (/hooks)`.
Bootstrap cannot see Codex hook trust.

Claude Code has the complete v1 flow. Codex has MCP, SessionStart, and Stop support, but no
Read/Grep PreToolUse equivalent. When store, anchors, all three supported Codex checks, and
production proof are complete, the command reports `best-effort`. In that state, best-effort completion exits 0
with an explicit warning. The terminal never prints `fully supported` for Codex (the optional
`--report` markdown does write `- fully supported: false` for Codex). A missing or invalid
supported check is actionable incomplete and exits `2`, unless an enabled Sidegraph plugin
provides MCP and hooks for this host.

Exit `0` also covers full Claude Code activation and safe diagnostics with no required write.
A run whose approved candidates were all skipped as unanchored (see below) is such a
diagnostic: it writes no record, so it needs no anchor and no retrieval proof (no `ANCHORS` or
`PROOF` line, and no proof section in `--report`), but it still verifies the host integration,
reopens and strictly verifies the store, and writes `--report`. When the integration is not
ready, or verification finds a violation, it is `incomplete` instead: it prints `STORE
incomplete`, the `VERIFY` lines when verification failed, the `INTEGRATION` lines and exits `2`.
Its `RESUME` is the exact `--resume` command when the integration is the problem, and
`sidegraph-verify --db <store>` when the store is, since a resume cannot repair a store this
run did not break.
Exit `1` is a usage or operational error, and is unreachable once anything has been durably
written — a broken output pipe or similar I/O failure after a confirmed write is at worst
`partial-recoverable`, never `1`, so a `1` never leaves partial memory behind for a resume to
reconcile. Exit `2` is `incomplete` or `partial-recoverable`, or a `complete` store whose
`ANCHORS`, `INTEGRATION` or `PROOF` check is incomplete. The `status` in the output and in
`--report` is the apply status: it covers the canonical writes, not anchors, host integration
or the retrieval proof, so a stored record the graph cannot reach, or a run that only kept
proposals, can exit `2` with `status: complete`. The run status has these meanings:

| Status | Meaning |
| --- | --- |
| `complete` | Confirmed writes reopened, reconciled with the plan and strictly verified; anchors, host integration and proof are checked separately. |
| `partial-recoverable` | At least one canonical file is durable, but later write, index, reopen, reconciliation, or verification work failed; activation is not claimed. |
| `incomplete` | No durable candidate completed the confirmed plan, or an actionable prerequisite/check remains. |
| `diagnostic` | No useful candidate required a write; the scan and explanation succeeded, but activation did not. A run whose approved candidates were all skipped as unanchored is a `diagnostic` only while its store verifies and its host integration is ready; otherwise it is `incomplete`. |

The canonical store and its rebuildable SQLite index are not one filesystem transaction.
After a post-confirmation failure, Bootstrap preserves known-durable files, reports the failed
source and pending candidates, and prints an exact `--resume` command. Reopening rebuilds a
mismatched index from canonical files; an unchanged rerun converges without duplicate records
or bindings. A candidate you accept or keep as a proposal that has no anchor in the graph
gets no new record: Bootstrap skips it, lists it on an `UNANCHORED` line with its key and
source path, and does not count it as pending, so an unchanged rerun does not stay
partial-recoverable on it. The `UNANCHORED` line names the rebuild step: once `graphify update .`
(or, with `--graph` pointing elsewhere, a rebuild of that graph file) gives the document a node,
the next run writes it. A record that was ratified in place or revived for such a document is judged by
whether production retrieval can still surface it from its own stored bindings. When the current
graph no longer reaches it, the run exits 2 with an `ANCHORS` line that names the stored record
and its source (``ANCHORS      incomplete: stored record <id> for <source> has no anchor in the
current graph; run `graphify update .` with the environment variable `GRAPHIFY_NO_AUTO_REFRESH=1` set and rerun``) and a `NEXT` line with that command, and
prints no `RESUME`, because a resume cannot change the graph. If the run also has a reason a
resume can fix, such as a failed write or missing host wiring, the exact `RESUME` stays. With
`--graph` pointing elsewhere, `ANCHORS` names that graph file to rebuild and there is no `NEXT`,
because `graphify update .` rebuilds only the default location. Once the graph has the node
again, the same rerun exits 0. Existing records and unrelated working-tree files are preserved.
`--resume` is a marker only — the underlying scan/review/reconcile flow is idempotent, so reissuing the same
command without it behaves identically.

## Privacy and optional report

Documents, canonical memory, and reports are not sent to a Sidegraph service. Local,
git-ignored retrieval diagnostics are enabled by default and failure-tolerant; set
`SIDEGRAPH_TELEMETRY=off` to stop recording. The selected coding-agent host's own transmission
policy is separate.

`--report bootstrap-report.md` is written after a confirmed apply, including the
all-unanchored diagnostic above. The pre-apply diagnostics (no candidates, every candidate
skipped in review, a changed plan with no writes) exit without writing it. The report holds
aggregate counts, separate edit/action rates, candidate
precision, per-action and CLI-to-proof elapsed seconds, anchor coverage, proof state, review
debt, and the canonical paths changed (including the `stamping_live_since` creation marker
of a fresh store). It excludes source content and candidate text and is
never uploaded automatically. The report never overwrites any host's config file, the graph, a scanned
source or a store file, hard links included. The reviewed action summary starts with the
resolved `store:` path, and names a symlink when the path or a directory above it in the
repository is one. A symlinked directory or store file *inside* the store stops the run
before review. These local metrics are operator/session observations, not a
first-user cohort or task-benefit result. Inspect the report before sharing.

## Reproduce the dogfood path

A release may include a `demo` branch with Sidegraph's own sample store. When that branch
is available, run the supported-document path against it:

> **Check that the release includes `demo` before running this clone.** The clone below
> fails when the branch is not published. Check with
> `git ls-remote --heads https://github.com/SantyagoSeaman/sidegraph.git demo`: run the
> clone only if that prints a line. If it prints nothing, use a repository containing one of
> the supported document profiles for the same bootstrap and retrieval loop.

This command installs from the public `main`, which advances at releases. To pin a version, see
[how to pin mutable development references](installation.md#mutable-development-references).

```bash
git clone --branch demo https://github.com/SantyagoSeaman/sidegraph.git sidegraph-demo
cd sidegraph-demo
export GRAPHIFY_NO_AUTO_REFRESH=1        # macOS, Linux: keeps Graphify off your agent skills
$env:GRAPHIFY_NO_AUTO_REFRESH = "1"      # Windows PowerShell: use this line instead
graphify update .
uvx --from git+https://github.com/SantyagoSeaman/sidegraph.git@main sidegraph-bootstrap \
  --profile superpowers --host claude-code --report bootstrap-report.md
```

Continue through the maintenance loop:

1. Start Claude Code on the demo checkout and use the Bootstrap copyable prompt to retrieve
   the imported record before touching its anchor.
2. Complete one real repository task and ask the agent to call `propose_decisions` for the
   durable decision or lesson produced by that task.
3. Run `sidegraph-ratify` to inspect the proposal, then
   `sidegraph-ratify --accept <the displayed decision ID>`.
4. Start a fresh session and call `get_task_context` for the proposal's anchored path; record
   whether the accepted record surfaces organically.

This loop checks import, review, retrieval proof, new-session capture, and later retrieval
as separate steps. Claude Code's existing PreToolUse delivery remains available alongside
the explicit task-context calls.
