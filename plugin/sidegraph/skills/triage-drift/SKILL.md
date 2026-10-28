---
name: triage-drift
description: >-
  Use when SessionStart reports records needing content review, retrieval carries
  [drifted] tags, sidegraph-doctor reports code-drift, or the user asks to triage
  drifted records, periodic maintenance, "разбери drifted записи", or whether decisions
  still hold. Also reachable directly as /sidegraph:triage-drift.
---

# Triage drift

`code-drift` is a *signal*, not a verdict: a live decision's anchored file(s) changed
since capture, or a previous scoped check no longer applies. The store cannot tell whether the change
invalidated the record — file hashes move on every refactor while the decision stays
true, and (the dangerous converse) logic can change semantics without the drift flag
firing at all. This skill is the judgment pass the flag exists to trigger: read the
record, read the code as it is now, decide.

Distinct from `sidegraph:heal-anchors`: that skill repairs *bindings* (orphaned/ambiguous
anchors after a graph rebuild). This one judges *content* — the anchors still resolve;
the question is whether what the record says survived what the code became.

## Get the list

The CLI runs through uvx at the session's version, never the copy on `PATH` ([why](../../../../docs/reference/cli.md#running-the-clis-beside-the-plugin)).

```bash
uvx --from git+https://github.com/SantyagoSeaman/sidegraph.git@main sidegraph-doctor --json    # drift_groups.groups[] and findings[] with "code": "code-drift"
```

Read `drift_groups.groups` first: one group per drifted file, each listing the records whose
anchored files changed. Build one record list keyed by `record_id` across all groups. A record
anchored in several files appears in each of their groups with the same `files`; judge it
**once**, after every one of its `files` has been read. When the `drift_groups` key is absent
(an older doctor, `--flat`, or no per-record drift) fall back to the `findings[]` entries
with `"code": "code-drift"` and triage per record as before.

Per record entry:

- `diff.command` is `git diff <old blob> <new blob>`: read it once per file, not once per
  record. A null `command` means read the file at HEAD and nothing more. `diff.unavailable`
  says why no diff exists (for example the baseline object is not in this clone).
- `diff.change` says what kind of change it is. `mode` means the executable bit moved and the
  content is identical. `type` means the entry kind changed (file, symlink, submodule or
  directory) and the content may well differ: read the file at HEAD, do not assume nothing
  changed. `submodule` means a submodule pointer moved (`<old8>→<new8>` in the text): read the
  submodule's own log, there is no `git diff` here. `content` is a real blob diff; if
  `old_mode` and `new_mode` differ it also changed the mode.
- `basis: "scope"` reads "scope changed; the file may also have changed". If a `command` is
  present, read that diff; otherwise judge the record against the current anchored code.
- `hint: "region-unchanged"` (text: `anchored region unchanged; sibling sections of the file
  may still matter`) shortens the reading, never the judging: still read the current
  implementation and any sibling the claim leans on.

For a per-decision `code-drift` finding, use `record_id` and `record_type` ("decision")
to identify the subject. `path` is its physical source: several records may share one shard.
Never infer identity from the shard, capture commit or human-readable `detail`. Global notes
(commit stamping unavailable, Git unavailable, check skipped) have null record fields and
sit in `drift_groups.notes`; they are not records to triage. `detail` names changed files or
why a prior check no longer applies.
Original `provenance.commit` stays history. Doctor is a pure read.

Each `code-drift` entry in `findings[]` has `first_seen`: the time Sidegraph first saw the
record drifted, or `null` when its age is unknown. Group records carry no `first_seen`: look
each one up in `findings[]` by `record_id`. Triage the records whose `first_seen` is within
the last 7 days first, newest first, then the `null` ones in doctor order.

The SessionStart count and `[drifted]` tags in detailed formatted `get_task_context`,
`query_decisions` and `drill_down` output use the lazily refreshed drift cache. Budgets can
omit records, and compact related-record lines do not all show the marker. Raw
`retrieve_decisions` JSON has no drift field or tag in either storage layout; use doctor
for the explicit current list.

Triage in batches — a 35-record queue does not need 35 interrupts. Group verdicts and
**put every question to the human into one message** (same discipline as
`sidegraph:heal-anchors`). Make exactly one `record_drift_check` or one supersession per
record, each with its own rationale and `files` scope: two records never share a rationale or
a call, even when one file change re-armed both.

## Per record: read both sides, then route

1. **Read the record** — `get_entity_history(entity_id)` via `find_entity`, or read the
   doctor finding's record by id with `retrieve_decisions(ids=["<record id>"])`. Note what
   it *claims*: the `choice`, the `consequences`, any evidence facts.
2. **Read the anchored code at HEAD** — the actual current implementation, not the diff.
   `git diff <provenance.commit> HEAD -- <file>` helps locate *what* changed, but the
   verdict comes from whether the record's claims still hold, not from how big the diff is.
3. **Route to one of three verdicts:**

   - **(a) Still true.** Inspect the current implementation and any dependencies that
     support the claim. Record the explicit assertion using the current full Git HEAD:

     ```python
     record_drift_check(
         decision_id="<existing decision id>",
         checked_commit="<full inspected HEAD>",
         files=["src/component.py", "src/inspected_dependency.py"],
         rationale="What was inspected and why the original claims still hold.",
     )
     ```

     Include every current checkable concrete anchor file and each additional dependency
     actually inspected. If `missing_required_files` is returned, inspect the local configured
     Store: `bindings_for_record(decision_id)`, then `get_entity(binding.entity_id).descriptor`
     for each binding. Include every current checkable concrete file, not just the files
     named as changed by doctor. The files must be tracked regular files and clean relative to
     that HEAD; unrelated dirt is allowed. The target must be open, commit-stamped and
     concretely anchored. With no Git HEAD, dirty inspected files, incomplete scope or an
     ineligible target, report the judgment and why no check was recorded; retain the
     advisory. Do not omit a required dirty file or substitute another revision.

     This immutable agent assertion preserves the decision and its capture provenance.
     It does not certify semantic truth, ratify a proposal, or permanently mute warnings.
     Changed checked files or required scope re-arm review (for a markdown file checked with
     declared sections, only a change to a declared or also-watched section or to the file's
     link definitions); unchanged file bytes after unrelated commits remain covered. A known wrong claim belongs in route (b).
     Never create a content-free successor just to refresh a stamp.
     **Scoping a check on a document.** Pass `sections` to `record_drift_check` when:

     - the file is markdown, the claim rests on identifiable sections, and you read those
       sections in this session at the recorded HEAD. When unsure, make a plain whole-file check.
     - Declare the narrowest sections that carry the claim (`[["55.2 Rules"]]`, not the
       chapter). The result echoes each section's `first_line` and `lines` (and the file's
       `total_lines`): a section spanning most of the file gains nothing, and a `first_line`
       that is not where the heading sits in your viewer means the parser disagrees with your
       renderer, so make a whole-file check.
     - Add `also_watch` when the claim depends on content elsewhere in the same file, such as
       a rule restated in another chapter, a table that must stay consistent or a "the only X"
       statement. Read the watched section too.
     - Never declare a section you did not read: the declaration is an attributed assertion,
       and a wrong narrow scope hides a real change.
     - Do not declare sections that transclude other files (include directives, snippets).
     - Refusals are normal. `section_file_unsupported` means a heading sits inside a list
       item or quote, or a footnote reference is in a declared span; a file over 20,000 lines
       is `section_file_too_large`. Make a plain whole-file check.
     - Not for code files. Declared sections are for markdown only.
     - Report the declared sections per file in your summary line.
   - **(b) Content now wrong or too broad.** The code change genuinely invalidated or
     narrowed the record. Propose the successor — never write it directly on your own
     initiative:

     ```python
     propose_decisions(drafts=[{
         "title": "...", "kind": "...", "context": "what changed and when",
         "choice": "what is true now",
         "rejected": "what the old record claimed, and why it stopped holding",
         "supersedes": "<old decision id>",
         "anchors": [{"name": "...", "file_path": "..."}],  # anchor to the NEW code
     }])
     ```

     The direct `supersede_decision` path is legitimate only when the human has dictated
     or approved the successor text in this conversation. For an invalidated *fact*,
     the same split applies with `supersede_fact`.
   - **(c) The anchored code is gone entirely** — deleted, not changed. Run
     `sidegraph:heal-anchors` to distinguish a rename from a lost subject. For accepted
     decisions, propose a meaningful supersession that explains the obsolete scope and
     anchors what remains true. `ratify(drop=...)` only rejects proposed decisions/facts;
     it cannot retire an accepted decision. If there is no meaningful successor, report
     that limitation to the human and retain history; do not invent an empty successor
     or edit status/valid_to in canonical files. Accepted domains have their own supported
     drop path; do not generalize that exception to decisions.

## Calibrate the effort

Mistakes first, same as retrieval: a drifted `gotcha`/`lesson`/`constraint` that no
longer holds actively misleads every future session — triage those before drifted `adr`s.
Within a batch, records whose anchored files changed *most recently* are likelier to be
genuinely affected than ones flagged by an old mechanical rename.

A formatting-only or unrelated diff helps prioritize the review. Still read the current
required implementation before recording a check; the diff alone does not establish that
the claim holds. Spend more judgment where the change touches the claim or dependencies.

**Prime suspects: records born during a review whose implementation has since landed.**
A gotcha captured while reviewing spec N describes the code *before* N's fix — correct at
capture, false within days, by design. When an implementation wave ships, re-check the
records its spec review produced (the first maintenance pass found exactly two of these,
and they were the only genuine invalidations in 42 flags); ideally this re-check is part
of the wave's own definition of done, not a later triage's discovery.

## Report shape

End with one summary the human can act on in a single pass: checked (decision id, returned check id, revision, inspected scope and cache-clear outcome), still true but
check unavailable (id and reason), proposed supersessions (old id → new draft title, awaiting
`sidegraph:ratify-decisions`), handed to heal-anchors (ids), and unresolved obsolete subjects
(ids + current status + reason). Recommend `ratify(drop=[...])` only for proposed
decisions/facts, or domains whose drop path explicitly permits it.

## See also

- [`docs/reference/cli.md`](../../../../docs/reference/cli.md#sidegraph-doctor) —
  `code-drift` and its sibling finding codes, exact `--json` shape.
- `sidegraph:heal-anchors` — binding damage (orphaned/ambiguous), and the headless/CI
  flag discipline if this triage runs unattended (proposals only; CI never ratifies).
- `sidegraph:ratify-decisions` — where every proposed supersession from (b) lands next.
