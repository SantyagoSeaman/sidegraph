# Judging prompt (run per bundle, on a blinded answer set)

The judge must never learn which arm produced which answer. Prepare the blinded set by
hand or with a small script: give every answer an opaque id (two random letters), shuffle
the entries, and keep the id-to-arm mapping in a separate file the judge never sees. Hand
the judge only the blinded set.

---

You are a blinded evaluation judge. Judgment only — never modify any file outside the
output path given below.

Materials:
- Blinded answers: `<bundle path>` — each entry has an opaque id, the question, the
  question's **rubric** (written before any answer existed), and the answer text. You do
  NOT know which condition produced any answer, and no mapping is available to you. Do
  not speculate about conditions.
- Ground truth: the repository at `<repo path>` (read-only). The rubric is the primary
  standard; the repository is how you verify rubric facts and catch claims the rubric
  does not cover.

For EACH answer report:
1. **correctness**: `correct` | `partial` | `wrong`.
2. **rubric_hits**: how many rubric bullets the answer satisfies (n/N).
3. **false_claims**: every statement factually wrong about the repository, quoted, with
   the truth (verify; do not assume).
4. **staleness**: any claim presenting a superseded state as current.
5. **unique_knowledge**: correct facts NOT derivable from code/docs alone; quote them and
   say where you checked.
6. **verification_hedge**: did the answer flag that its information might be outdated?

Never let answer LENGTH influence correctness. If two answers disagree on a fact, resolve
it against the repository and say which is right.

End with a summary table: answer id → correctness, rubric_hits, #false_claims,
#staleness, #unique_knowledge, hedge yes/no. Write the full verdict to `<output path>`.

---

**Effort:** one judge run per bundle, ~$5–10 at 32 answers. Use the strongest model you
have; a weak judge is the cheapest way to get a wrong result.
