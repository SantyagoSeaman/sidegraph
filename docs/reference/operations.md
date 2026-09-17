# Operations reference

What actually runs, where, and what it costs. Every number here was measured on this
repository (216 Python files / 2,035 tracked files, and a store holding 239 decisions
across 1,426 canonical JSON files) on an Apple-silicon laptop, measured 2026-09-17. They
are **order-of-magnitude guidance, not a performance contract**, and they scale with your
corpus, not with ours. Re-measure before quoting these numbers elsewhere.

## What runs when

| Trigger | What runs | Cost here | Notes |
|---|---|---|---|
| Every agent session start | `sidegraph-session-start` hook: opens the store, best-effort sync, renders the memory map | **~350 ms** | Degrades to a shorter map without a graph; a failure never blocks the session |
| Every `Read`/`Grep`/`Edit`/`Write` tool call | `sidegraph-pre-tool-use` hook: path lookup + at most one nudge per session per kind | **~110 ms** | Pure read; failure is swallowed by design |
| Every agent session end | `sidegraph-stop` hook: capture nudge | ~110 ms | Same failure posture |
| First store open after a `git pull` (or any change to canonical files) | SQLite index rebuild from the canonical JSON | **~160 ms** (1,106 files) | Automatic, no command to run; the index is gitignored and derived |
| Store open with a fresh index | open + full decision scan | **~13 ms** | |
| Code-graph rebuild (`graphify update .`) | The **engine's** job, not Sidegraph's | **~8 s** (5,508 nodes) | Optional layer; runs when you choose (commit hook / CI / manually). Sidegraph degrades to file-path anchors without it |
| CI (recommended) | `sidegraph-verify` (strict) and `sidegraph-doctor` (advisory) | < 1 s on this store | `verify` gates; `doctor --check` escalates advisories if you want that |

**Nothing runs in the background.** There is no daemon, no scheduler, no watcher — every
number above is a process that starts, does one pass, and exits.

**Do the multiplication before you adopt.** The PreToolUse number is per *tool call*, and a
heavy session makes hundreds: at ~110 ms, 200 calls is ~22 s and 500 calls is ~55 s of added
wall clock per session, per developer. That is the number to weigh, not the 110 ms — and it
is the strongest argument for keeping the hook matcher narrow (it fires on Read/Grep/Edit/
Write only) or dropping the PreToolUse hook entirely while keeping SessionStart and the MCP
tools, which costs you the nudge channel measured in the whitepaper's §8.3 and nothing
else.

## Disk

| Item | Size here | Grows with |
|---|---|---|
| Canonical store (`.sidegraph/**/*.json`, committed) | **968 KiB / 1,426 files, ~695 B per record** | Records. ~695 B/record means 10,000 records ≈ 7 MiB of git-friendly text |
| Derived index (`.sidegraph/index.db`, gitignored) | 2.5 MiB | Records + telemetry rows; disposable, rebuilt on demand |
| Code graph (`graphify-out/graph.json`, gitignored) | 10.5 MiB | Your codebase, not your store |

Records are one file each, so a store grows linearly and diffs per record — that is the
property that makes two branches ratifying different decisions merge without conflict.

## The graph dependency, stated plainly

The code-graph engine is **optional**. Without it: entity resolution falls back to file
paths and domains, the SessionStart map renders from records alone, and retrieval works.
With it: symbol-level anchors, community-derived domain candidates, and drift detection
against moved code. If you adopt it, its rebuild cost (≈8 s here) is on your commit or CI
path, not on your agents' sessions — and a rebuild never dirties git (verified by the
sync-clean invariant: `graphify update .` followed by `git status` shows no store change).

## Session cost

Each memory-carrying session pays a flat injected-map tax (measured: ~1,138 prompt tokens
on a 15-domain map; ~4.5–6.3k on a 24-domain map — it scales with the rendered map, which
is why the map is budget-bounded), plus retrieval output when the agent actually calls a
tool. The full cost model, including the corpus kinds where this does *not* pay, is in the
whitepaper's §8.5 — read that before assuming a saving.

## CI wiring

```yaml
- run: uvx --from git+https://github.com/<org>/sidegraph@<sha> sidegraph-verify   # gate
- run: uvx --from git+https://github.com/<org>/sidegraph@<sha> sidegraph-doctor    # advisory
```

Pin a SHA or a tag, not a branch: the store format is the public contract, the CLI surface
is provisional (see [`stability.md`](stability.md)).

## What this page does not tell you

Reviewer-flagged gaps, honestly named: no measurements at monorepo scale (thousands of
files, hundreds of domains); no multi-developer concurrency numbers; no fleet aggregation
of the local diagnostics; no growth curve for stores an order of magnitude larger than
this one. If you run a pilot, these are the numbers worth capturing — see the
[pilot kit](../pilot-kit/README.md).
