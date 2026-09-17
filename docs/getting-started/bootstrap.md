# Bootstrap existing project rationale

`sidegraph-bootstrap` turns supported ADR/spec documents into reviewed Sidegraph memory and
then asks production retrieval to surface one accepted, live-anchored decision. It runs
locally and does not require an account or model API.

The 10–15 minute path is a launch target that has not yet been measured across first-user
sessions.

```bash
graphify update .
sidegraph-bootstrap --host claude-code
```

Bootstrap scans, previews, reviews, writes, verifies, checks the selected host, and proves
retrieval in one guided command. Preview and review use the same immutable plan. Nothing is
written until the user reviews the redacted action summary and types the literal `confirm`.

## Supported document profiles

Exactly six profiles are supported in v1. Pass `--profile NAME` to select one; an explicit
choice always wins. Auto-detection uses the profile marker and input globs. When several
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
known text file; it does not widen the rest of the scan. The preview lists a candidate count
followed by each candidate's title, source, context, choice, rejected alternatives,
consequences, warnings, and anchors; it does not enumerate the files read or the exclusions
applied during the scan (those are tracked internally but not rendered).

Planning parses, redacts, validates, deduplicates, and reports anchor intents without opening
a writable store. A missing graph still permits preview, but Bootstrap prints the exact
`graphify update .` and resume commands and stops before interactive review. Zero useful
candidates is `diagnostic`: it exits safely without creating canonical memory and does not
claim activation.

Candidate warnings distinguish missing choice, missing rejected alternatives, unresolved or
ambiguous anchors, likely current-state summaries, duplicates within the plan, and duplicates
of live canonical memory. Warnings inform review; they never silently accept a candidate. Each
warning code is rendered beside the consequence of accepting it anyway:

| Warning code | Consequence of accepting |
| --- | --- |
| `missing-choice` | the document states no decision; the record would carry only context |
| `missing-rejected-alternatives` | no rejected alternatives were found; the most valuable field stays empty |
| `unresolved-anchor` | the anchor does not resolve; the record would not surface for that code |
| `ambiguous-anchor` | several entities match; Sidegraph never guesses, so the anchor stays degraded |
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

`edit` opens one mode-`0600` temporary file through `$VISUAL`, then `$EDITOR`. Edited content
is parsed, redacted, validated, re-anchored, shown as a new diff, and confirmed again. If no
editor is configured, the candidate remains proposed. `skip` is run-local state and is never
stored. There is no default accept-all action.

## Completion, recovery, and hosts

Bootstrap reports four outcomes independently: store, anchors, host integration, and
production-path proof. Proof selects an accepted, currently valid decision with a resolved
tier-2 anchor and calls the same `get_task_context` production retrieval path used by agent
sessions. Optional domain suggestions do not block that proof.

| Host | MCP | SessionStart | Stop | Read/Grep PreToolUse | Completion label |
| --- | --- | --- | --- | --- | --- |
| Claude Code | verified | verified | verified | verified | complete v1 flow |
| Codex | verified when configured | verified when configured | verified when configured | unsupported | best-effort only |

Claude Code has the complete v1 flow. Codex has MCP, SessionStart, and Stop support, but no
Read/Grep PreToolUse equivalent. When store, anchors, all three supported Codex checks, and
production proof are complete, the command reports `best-effort`. In that state, best-effort completion exits 0
with an explicit warning. The terminal never prints `fully supported` for Codex (the optional
`--report` markdown does write `- fully supported: false` for Codex). A missing or invalid
supported check is actionable incomplete and exits `2`.

Exit `0` also covers full Claude Code activation and safe diagnostics with no required write.
Exit `1` is a usage or operational error, and is unreachable once anything has been durably
written — a broken output pipe or similar I/O failure after a confirmed write is at worst
`partial-recoverable`, never `1`, so a `1` never leaves partial memory behind for a resume to
reconcile. Exit `2` is `incomplete` or `partial-recoverable`. The final run status has these
meanings:

| Status | Meaning |
| --- | --- |
| `complete` | Confirmed writes reopened, reconciled with the plan, strictly verified, anchored, and ready for host/proof checks. |
| `partial-recoverable` | At least one canonical file is durable, but later write, index, reopen, reconciliation, or verification work failed; activation is not claimed. |
| `incomplete` | No durable candidate completed the confirmed plan, or an actionable prerequisite/check remains. |
| `diagnostic` | No useful candidate required a write; the scan and explanation succeeded, but activation did not. |

The canonical store and its rebuildable SQLite index are not one filesystem transaction.
After a post-confirmation failure, Bootstrap preserves known-durable files, reports the failed
source and pending candidates, and prints an exact `--resume` command. Reopening rebuilds a
mismatched index from canonical files; an unchanged rerun converges without duplicate records
or bindings. Existing records and unrelated working-tree files are preserved. `--resume` is a
marker only — the underlying scan/review/reconcile flow is idempotent, so reissuing the same
command without it behaves identically.

## Privacy and optional report

Documents, canonical memory, and reports are not sent to a Sidegraph service. Local,
git-ignored retrieval diagnostics are enabled by default and failure-tolerant; set
`SIDEGRAPH_TELEMETRY=off` to stop recording. The selected coding-agent host's own transmission
policy is separate.

`--report bootstrap-report.md` writes aggregate counts, separate edit/action rates, candidate
precision, per-action and CLI-to-proof elapsed seconds, anchor coverage, proof state, review
debt, and the canonical paths changed. It excludes source content and candidate text and is
never uploaded automatically. These local metrics are operator/session observations, not a
first-user cohort or task-benefit result. Inspect the report before sharing.

## Reproduce the dogfood path

A `demo` branch will carry Sidegraph's own dogfood store. Once it ships, run the
supported-document path against it:

> **The `demo` branch ships with a later release.** It does not exist yet, so the clone below
> cannot run today. This is the intended reproduction path once it ships; there is no working
> substitute before then.

> **`@main` is a mutable ref.** Every `git+…@main` command on this page tracks the
> branch: what you install today is not what you installed yesterday, and a `uvx` cache
> refresh can change it under you. Fine for trying Sidegraph out; for anything you depend
> on — CI, a shared team setup, a pilot you intend to measure — replace `@main` with a
> commit SHA (`git+https://github.com/SantyagoSeaman/sidegraph@<sha>`) so the version is a
> decision you made rather than whatever HEAD happened to be. See [`reference/stability.md`](../reference/stability.md) for what each surface promises.

```bash
git clone --branch demo https://github.com/SantyagoSeaman/sidegraph.git sidegraph-demo
cd sidegraph-demo
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

The visual launch demo must show discovery and review, production proof, new-session capture
and ratification, and later organic retrieval. An import-only recording does not satisfy the
release package. The existing concrete-title PreToolUse nudge is part of normal delivery; its
effect is measured separately and is not new Bootstrap functionality.
