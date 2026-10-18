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
uvx --from git+https://github.com/SantyagoSeaman/sidegraph.git@main sidegraph-doctor --json    # findings[] entries with "code": "code-drift"
```

Each finding names the record and changed files or why its prior check no longer
applies (including subject/anchor-scope changes). Original `provenance.commit` stays history. `sidegraph-doctor` is a pure read — safe to run any time. From
inside a session, the drifted set also announces itself: the SessionStart line and
`[drifted]` tags in retrieval output are the same population, surfaced lazily.

Triage in batches — a 35-record queue does not need 35 interrupts. Group verdicts and
**put every question to the human into one message** (same discipline as
`sidegraph:heal-anchors`).

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
     actually inspected. The files must be tracked regular files and clean relative to
     that HEAD; unrelated dirt is allowed. The target must be open, commit-stamped and
     concretely anchored. With no Git HEAD, dirty inspected files, incomplete scope or an
     ineligible target, report the judgment and why no check was recorded; retain the
     advisory. Do not omit a required dirty file or substitute another revision.

     This immutable agent assertion preserves the decision and its capture provenance.
     It does not certify semantic truth, ratify a proposal, or permanently mute warnings.
     Changed checked files or required scope re-arm review; unchanged file bytes after
     unrelated commits remain covered. A known wrong claim belongs in route (b).
     Never create a content-free successor just to refresh a stamp.
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
   - **(c) The anchored code is gone entirely** — deleted, not changed. That's binding
     damage, not content drift: run `sidegraph:heal-anchors` (its stale-decisions tree
     already covers restore-the-name vs. supersede vs. recommend-a-drop). Never drop a
     record yourself — a drop is a ratify-gate action for a human.

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
`sidegraph:ratify-decisions`), handed to heal-anchors (ids), and recommended drops
(ids + reason — recommendation only, the human runs `ratify(drop=[...])`).

## See also

- [`docs/reference/cli.md`](../../../../docs/reference/cli.md#sidegraph-doctor) —
  `code-drift` and its sibling finding codes, exact `--json` shape.
- `sidegraph:heal-anchors` — binding damage (orphaned/ambiguous), and the headless/CI
  flag discipline if this triage runs unattended (proposals only; CI never ratifies).
- `sidegraph:ratify-decisions` — where every proposed supersession from (b) lands next.
