---
name: triage-drift
description: Use when SessionStart reports "N record(s) are anchored to code that changed after their capture", when retrieval output carries [drifted] tags, when sidegraph-doctor prints code-drift findings, or as periodic store maintenance — "разбери drifted записи", "triage the drifted records", "are these decisions still true". Also reachable directly as /sidegraph:triage-drift. Walks each drifted record against the code at HEAD and routes it to a verdict: still true (report, no write), content now wrong (propose a supersession), or code gone (hand to heal-anchors). The manual counterpart to a known limitation — drift detection sees code movement, not semantic invalidity; this skill is where the semantic judgment happens.
---

# Triage drift

`code-drift` is a *signal*, not a verdict: a live decision's anchored file(s) changed
after the commit its provenance was captured at. The store cannot tell whether the change
invalidated the record — file hashes move on every refactor while the decision stays
true, and (the dangerous converse) logic can change semantics without the drift flag
firing at all. This skill is the judgment pass the flag exists to trigger: read the
record, read the code as it is now, decide.

Distinct from `sidegraph:heal-anchors`: that skill repairs *bindings* (orphaned/ambiguous
anchors after a graph rebuild). This one judges *content* — the anchors still resolve;
the question is whether what the record says survived what the code became.

## Get the list

```bash
sidegraph-doctor --json    # findings[] entries with "code": "code-drift"
```

Each finding names the record and which anchored file(s) changed since
`provenance.commit`. `sidegraph-doctor` is a pure read — safe to run any time. From
inside a session, the drifted set also announces itself: the SessionStart line and
`[drifted]` tags in retrieval output are the same population, surfaced lazily.

Triage in batches — a 35-record queue does not need 35 interrupts. Group verdicts and
**put every question to the human into one message** (same discipline as
`sidegraph:heal-anchors`).

## Per record: read both sides, then route

1. **Read the record** — `get_entity_history(entity_id)` via `find_entity`, or match the
   doctor finding's record id against `retrieve_decisions()`. Note what it *claims*:
   the `choice`, the `consequences`, any evidence facts.
2. **Read the anchored code at HEAD** — the actual current implementation, not the diff.
   `git diff <provenance.commit> HEAD -- <file>` helps locate *what* changed, but the
   verdict comes from whether the record's claims still hold, not from how big the diff is.
3. **Route to one of three verdicts:**

   - **(a) Still true.** The code moved or grew but the decision holds. **No store write
     exists for "confirmed"** — the drift flag is derived from git at read time, not
     stored, so a confirmed record stays flagged until it is one day superseded. Don't
     fight that with a content-free supersession "to refresh the stamp" — it pollutes the
     append-only history with a successor that says nothing new (same rule as
     heal-anchors' preference for `add_anchors`). Report it as confirmed and move on;
     the cost of an honest flag is smaller than the cost of a fake successor.
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

Not every flag deserves a deep read: a record anchored to a file whose diff since
`provenance.commit` is formatting or an unrelated function can be confirmed in seconds.
Spend the judgment where the diff touches what the record is actually about.

**Prime suspects: records born during a review whose implementation has since landed.**
A gotcha captured while reviewing spec N describes the code *before* N's fix — correct at
capture, false within days, by design. When an implementation wave ships, re-check the
records its spec review produced (the first maintenance pass found exactly two of these,
and they were the only genuine invalidations in 42 flags); ideally this re-check is part
of the wave's own definition of done, not a later triage's discovery.

## Report shape

End with one summary the human can act on in a single pass: confirmed (ids, one line
each), proposed supersessions (old id → new draft title, awaiting
`sidegraph:ratify-decisions`), handed to heal-anchors (ids), and recommended drops
(ids + reason — recommendation only, the human runs `ratify(drop=[...])`).

## See also

- [`docs/reference/cli.md`](../../../../docs/reference/cli.md#sidegraph-doctor) —
  `code-drift` and its sibling finding codes, exact `--json` shape.
- `sidegraph:heal-anchors` — binding damage (orphaned/ambiguous), and the headless/CI
  flag discipline if this triage runs unattended (proposals only; CI never ratifies).
- `sidegraph:ratify-decisions` — where every proposed supersession from (b) lands next.
