# Rubric template

[Glossary](../concepts/glossary.md)

Use this as the shape of one frozen question and its evidence checks. Replace every example claim with a claim you can verify in your repository or a named durable artifact. For each example below, replace the placeholder with the file or durable artifact that proves your own claim. Omit a bullet if you cannot locate its evidence. A bonus bullet counts in the denominator, but its absence alone does not downgrade a correct answer.


One rubric per question, written **before** any answer exists.

```json
{
  "id": "q07",
  "shape": "gotcha",
  "prompt": "What bites someone adding a new field to the export payload?",
  "rubric": [
    "Names the versioned schema check in <file>",
    "Mentions that downstream consumer X pins the field order",
    "Says the migration must ship in the same release as the writer change",
    "Bonus: names the incident that produced the rule"
  ]
}
```

Rules that keep a rubric honest:

- **Checkable.** Every bullet must be verifiable in the repository or in a durable
  artifact. "Explains the design well" is not a bullet.
- **Specific.** Name files, symbols, constraints — not topics.
- **Bonus bullets are labeled.** Mark optional depth `Bonus:`; it counts in the
  denominator but its absence alone does not downgrade a correct answer.
- **Written blind and frozen.** Once answers exist, the rubric does not change. If a
  rubric turns out to be wrong, record the affected question and faulty bullet in the pilot report as an instrument defect. Do not change the rubric or re-judge those answers.
