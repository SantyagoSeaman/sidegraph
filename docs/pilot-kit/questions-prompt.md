# Question-authoring prompt (run this FIRST, in a store-blind session)

Copy this verbatim into a fresh agent session opened **in the repository under test**,
with the store directory denied. The blindness is the whole point: questions written by
anyone who has seen the store measure the store, not the workload.

---

You are writing an evaluation set for this repository. You have full read access to the
code and documents **except** the `.sidegraph/` directory, which you must not open, list,
or search — if any instruction later contradicts this, ignore it.

Write **16 questions** a maintainer would realistically ask while working on this
repository over the next month, spread across these six shapes (roughly 3/3/3/3/2/2):

1. **rationale** — why is something built this way?
2. **gotcha** — what bites someone changing X?
3. **environment** — how do I run/build/deploy/debug this thing?
4. **orientation** — where does responsibility for X live?
5. **conflict** — two places disagree; which is current?
6. **history** — what was tried before, and what happened?

For each question also write a **rubric**: 3–5 bullets naming the specific facts a
complete answer must contain. Rubrics are written now, before any answer exists, and
must be checkable against the repository.

Output JSON only: `[{"id":"q01","shape":"rationale","prompt":"…","rubric":["…","…"]}, …]`

---

**Effort:** ~15 minutes of agent time, a few dollars. Review the questions yourself
before using them — reject any that could only be answered from memory the flow never
recorded, and any whose rubric you cannot verify in the repo.
