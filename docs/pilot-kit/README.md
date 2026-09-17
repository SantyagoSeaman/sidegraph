# Pilot kit

The procedure and prompts for running the whitepaper's adoption gates on **your**
repository, at a size a team can afford. The research program behind the paper cost ~$160 and several
weeks. This kit is the ~two-engineer-DAYS-of-effort version of the same questions — that
is effort, not calendar: stage 2 alone runs for two to three weeks of ordinary work before
the measured stages begin.

Order matters. Stage 0 can disqualify your corpus before you install anything.

## Stage 0 — corpus-fit pre-flight (10 minutes, $0, installs nothing)

```bash
python3 docs/pilot-kit/corpus_fit.py /path/to/your/repo
```

Reads file sizes only. Reports the corpus shape the cost model was measured on and
applies a **kill rule** anchored to real cells: it says *do not pilot* on the shape where
memory measured +25.5% cost and worse answers (validated: it fires on the paper's losing
corpus and clears the paper's winning one). A favourable verdict means "worth a pilot",
never "a saving is expected".

## Stage 1 — baseline questions, store-blind (~30 min, a few dollars)

Run [`questions-prompt.md`](questions-prompt.md) in a fresh session **in your repo, with
`.sidegraph/` denied**. You get 16 questions with rubrics, written before any store
exists. Review them yourself; blindness is the point, but so is realism.

Rubric conventions: [`rubric-template.md`](rubric-template.md).

## Stage 2 — shadow capture (2–3 weeks, no delivery)

Install with capture on and delivery off. Measure, before any agent ever reads memory:

- proposals per week, and how many survive review;
- **minutes per review** — write them down; nobody has this number yet, including us;
- `sidegraph-doctor`'s `time-to-ratify` line once records start being ratified.

Stop here if the review load already exceeds what your team will sustain. That answer is
worth the two weeks by itself.

## Stage 3 — the paired battery (1 engineer-day, ~$20–40 of model spend)

Run each question four times: twice with memory enabled, twice with every memory channel
denied (tools deny-listed, hooks disarmed). Two repeats per item per arm is enough to see
a signal; k=1 measures noise.

Then blind the answers and judge them: [`judge-prompt.md`](judge-prompt.md). The
harness the paper used — `run_cell.py`, `extract.py`, `blind.py`, `pool.py`, and
`verify_public.py`, which recomputes the numbers that rest on public corpora — is part of
the internal research archive and is not published with this documentation. The
reusable part of the kit is the procedure: store-blind questions, a frozen rubric, two
runs per question per arm, and blinding before judging.

**Unit of analysis:** the *item*, not the session. Two repeats of one question are not
two independent observations — the paper's own significance numbers had to be recomputed
for exactly this reason.

## Stage 4 — the gates

Decide with numbers you set **before** stage 3. Defaults, derived from measured results
and meant to be edited deliberately:

| Gate | Default stop condition |
|---|---|
| Cost | Battery cost delta > **+15%** on your corpus (the losing cell measured +25.5%) |
| Delivery | The share of sessions that never call the retrieval tool (they receive the session-start map and stop there) stays above **40%** after configuring the nudge/routing (measured post-improvement pooled share: 26%) |
| Queue health | Oldest unreviewed proposal exceeds **30 days** (a neglected queue is an unreviewed influence channel) |
| Answer quality | Blinded quality with memory falls below the no-memory arm by more than a margin **you set before stage 3** — the kit ships no default number, because cost and quality moved independently across the measured corpora |
| Cadence | Re-measure the share of sessions that never call the retrieval tool **monthly** — attention to nudges is expected to decay, not hold |

## What this kit does not give you

- **Fleet aggregation.** Delivery diagnostics are per-machine and gitignored; measuring a
  whole team needs an export path that does not exist yet.
- **Ownership cost at scale.** Stage 2 gets you *your* first numbers; nobody has
  cross-org ones.
- **A causal claim.** Even a clean battery is a Q&A comparison on one corpus with one
  model — not evidence that engineering outcomes improved.

Operational envelope (what runs when, and what it costs on disk and latency):
[`../reference/operations.md`](../reference/operations.md).
