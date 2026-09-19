---
name: stats
description: Use when someone asks how much Sidegraph memory is actually being used — "sidegraph stats", "is the memory being used", "how often does memory get asked for", "which files carry decisions", "покажи статистику sidegraph". Also reachable directly as /sidegraph:stats. Runs the local, read-only sidegraph-stats command and shows its one-screen report verbatim — how often memory was asked for, how much of the code worked on has memory anchored to it, what the store holds, and anchor health. It states what was shown, asked and touched — never what was improved, prevented or saved.
---

# Stats

Run `sidegraph-stats` and show its output **verbatim** in a fenced block. Everything in the
report is read from the local, gitignored index; nothing is sent anywhere. If the command is
not on `PATH`, run it the way the other CLI skills do:
`uvx --from git+https://github.com/SantyagoSeaman/sidegraph.git@main sidegraph-stats`.

Do not restate the numbers in prose, do not round them, do not reorder the blocks, and do
not add a summary line. The report is written to be read as printed.

**Never** describe the numbers causally. The report says what was shown, asked and touched.
It does not say what was improved, prevented, saved or caused, and neither do you — no
timeline here can separate "memory sent the agent there" from "the agent was going anyway".

If the report says `too little to summarize yet`, say that and stop; do not compute a
ratio it deliberately withheld.

Follow-ups worth offering, only if the numbers invite them:

- many silent domains → `/sidegraph:record-decision` or `/sidegraph:import-adrs`
- degraded or orphaned anchors → `/sidegraph:heal-anchors`
- a large no-recorded-showing count beside a nonzero `dropped by it` on the budget line → offer to
  look at whether the budget is cutting records the store holds. That is something to
  investigate, not something to state: the report does not establish it

## See also

- [`docs/reference/cli.md`](../../../../docs/reference/cli.md#sidegraph-stats) — the flags
  (`--window`, `--json`, `--db`, `--graph`) and exit codes.
- [`docs/guides/verifying-your-setup.md`](../../../../docs/guides/verifying-your-setup.md#case-9--usage-statistics)
  — what each block of the report answers.
