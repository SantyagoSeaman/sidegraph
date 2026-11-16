# Operations reference

> **Start here:** Use the section matching the command or payload you are checking. For everyday work, begin with the [documentation paths](../README.md). [Glossary](../concepts/glossary.md).

What actually runs, where, and what it costs. Every number here was measured on this
repository (216 Python files / 2,035 tracked files, and a store holding 239 decisions
across 1,426 canonical JSON files) on an Apple-silicon laptop, measured 2026-09-17. They
are **order-of-magnitude guidance, not a performance contract**, and they scale with your
corpus, not with ours. Re-measure before quoting these numbers elsewhere. The hook and
MCP-call rows were re-measured on 2026-10-03 (a subprocess median of nine, a store of about
1,500 record files).

The timings below predate optional delivery-evidence journaling. Current covered MCP
outcomes and delivering read/brief hooks add at most one independent best-effort commit
when telemetry and the optional tables are available. It never waits for a SQLite lock;
legacy claim/show transactions are unchanged. No new connection, daemon or retry is added.
See [the evidence contract](store-format.md#delivery-evidence-derived-index-only).

A small telemetry-enabled comparison on 2026-10-18 measured the added write separately:
20 calls per surface in a warm Python 3.13.7 process, one synthetic store with two anchored
decisions, one anchored fact and one global decision. Read-hook median was 1.119 → 1.588 ms
(about +0.47 ms); the `query_decisions` core was 0.782 → 1.107 ms (about +0.32 ms).
Output stayed 437 and 192 UTF-8 bytes respectively; canonical JSON stayed byte-identical.
This excludes interpreter startup, transport and the tool shell's freshness check. It is
one tiny local fixture and establishes no statistical significance, production latency or
usefulness; the final sample varies from the preliminary sample.

## What runs when

| Trigger | What runs | Cost here | Notes |
|---|---|---|---|
| Every hook launch (the launcher of every hook below) | `uvx --from sidegraph==X.Y.Z --python 3.13 --offline <entry point>` first, and the same launch without `--offline` only when that fails | **~85 ms** for a warm offline launch; **~4 ms** is the shell overhead of the offline-then-online form; **~45 ms** for the offline attempt that fails on a cold cache (first run), before the online launch installs the package | Wall time of the whole `sh -c` command, a `PreToolUse` `Read` in a scratch repository with no store, on macOS arm64 with uv 0.12.0 (median of 30 after 2 warm-ups; the cold-cache figure is 5 runs, 41 to 52 ms), so it includes that hook's own small handler. Without `--offline`, uv revalidates its package index once the cached pages are ten minutes old: **220 to 460 ms** with a network, and **5.8 s** and **10 s** per hook measured with an aged index and no network |
| Every agent session start | `sidegraph-session-start` hook: opens the store, best-effort sync, refreshes the code-drift cache, renders the memory map | **~1 s** warm on a store of about 140 capture commits and a 65 MB graph (python launched directly, on a loaded machine); **~1.2 s** when a commit touched files that records anchor to; **~4.7 s** (4.65-4.80 s measured) for the first start after a pull that changes many anchored files, and **~5 s** (4.6-5.1 s) without an index | The drift refresh runs one `git diff` per distinct capture commit, and for a record with no capture commit one `git merge-base` and one `git log` per distinct graph build commit. It remembers each answer in `index.db` until the commit's anchored paths change: the cost grows with the number of capture and build commits, and a pull or branch switch that changes many anchored files reruns most of them. A graph answer is extended, not rerun, when HEAD moves forward: one shared `git log` of the commits since the remembered HEAD serves every build commit. Measured on a store with 145 capture and 8 build commits (the drift refresh alone, a 65 MB graph): cold 1.5 s before graph baselines, 2.2 s with them (8 `merge-base`, 8 `git log`); warm after one new commit 0.25 s before, 0.28 s with them. Degrades to a shorter map without a graph; a failure never blocks the session |
| Every `Read`/`Grep`/`Edit`/`Write` tool call, and every Bash line that contains `sed`, `grep`, `rg` or `cat` | `sidegraph-pre-tool-use` hook: one scan of the entities, the records of at most three files, one claim per file | **~26 ms** when it prints nothing; **~35 ms** for a `Read` that delivers the busiest file here (48 records, 2 shown); **~38 ms** for a Bash line that names three anchored files | Reads and writes `index.db` directly, without opening a `Store`; may write local touch/per-file claim telemetry; failure is swallowed by design. In process the same three calls cost under 0.1 ms, 4.5 ms and 6.3 ms; the rest is the interpreter starting |
| Every `Agent` (`Task`) call that spawns a subagent, on Claude Code and Grok Build | `sidegraph-pre-tool-use` hook, `Agent\|Task` group: reads the files the brief names (and, one hop away, the files named in a plan or notes document it names), looks up their records, and appends a block to the brief | Not measured separately here: the same raw-SQLite path as the row above, plus reading up to 64 KB for each document the brief names | Once per spawn, never per tool call. No `Store`; legacy brief shows and optional occurrence evidence write only the existing index; any failure leaves the brief as the parent wrote it |
| Every subagent start (the host may fire it again on resume, but injects the brief only when the subagent's context lacks it) | `sidegraph-subagent-start` hook: one pass over the index, to print a short brief that says decision memory exists and how many decisions are anchored to code | Not measured separately here: it reads the index like the PreToolUse hook and loads nothing beyond the standard library | Prints `{}` when no decision is anchored to a file; writes nothing; Claude Code, Codex and Muse (Grok Build runs it but does not pass the output to the model) |
| Every agent session end | `sidegraph-stop` hook: capture nudge | **~35-40 ms** for a Stop that exits before the capture check (hook already active, session already captured, not yet substantial); **~45-50 ms** for a Stop of a captured session past the 30-minute re-arm gap but below its commit threshold (one `git log`); **~175 ms** for a Stop that reaches the first nudge, and **~0.6-0.7 s** for a re-armed nudge, at most once per 30 minutes per session (opens the store, refreshes the drift cache) | Same failure posture |
| Every MCP tool call | The server's freshness check before the tool runs: a digest walk over the record files, and a rebuild only if they changed | **+11 ms** per call (17 ms against 6 ms without the check); **~90 ms** when a record changed | A filesystem error during the walk does not fail the call: it answers from the previous index |
| First store open after a `git pull` (or any change to canonical files) | SQLite index rebuild from the canonical JSON | **~160 ms** (1,106 files) | Automatic, no command to run; the index is gitignored and derived |
| Store open with a fresh index | open + full decision scan | **~13 ms** | |
| Code-graph rebuild (`graphify update .`) | The **engine's** job, not Sidegraph's | **~8 s** (5,508 nodes) | Optional layer; runs when you choose (commit hook / CI / manually). without it new records get no code anchors and file or symbol retrieval finds nothing |
| After a commit, a merge or a branch switch, only if you installed the graph refresh hook (`sidegraph-init --hooks`) | The hook's helper script starts the rebuild above in the background and returns at once | The commit does not wait; the rebuild is the **~8 s** above | The one thing Sidegraph leaves running after a hook returns. One rebuild at a time; the main checkout only. See [the Graphify integration](../integrations/graphify.md#keeping-the-graph-fresh-git-hooks) |
| CI (recommended) | `sidegraph-verify` (strict) and `sidegraph-doctor` (advisory) | < 1 s on this store | `verify` gates; `doctor --check` escalates advisories if you want that |

**Sidegraph starts no background daemon, scheduler, or watcher.** Each hook and CLI entry
above does one pass and exits. A host may keep the stdio MCP server process alive for the
session; work still happens only when the host calls a tool. One optional exception: if you
install the graph refresh git hook, its helper starts `graphify update .` in the background after
a commit, a merge or a branch switch. That process outlives the git hook that started it, runs
one at a time, and exits when the rebuild is done. Without the hook, nothing runs in the
background.

**Do the multiplication before you adopt.** The PreToolUse number is per *tool call*, and a
heavy session makes hundreds: at ~35 ms, 200 calls is ~7 s and 500 calls is ~18 s of added
wall clock per session, per developer. That is the number to weigh, not the 35 ms — and it
is the strongest argument for keeping the hook matcher narrow (it fires on Read/Grep/Edit/
Write, and on a Bash call only when the line contains `sed`, `grep`, `rg` or `cat`: one `Bash`
entry whose command is a shell filter, so any other line costs the few milliseconds of a shell,
not a hook process) or dropping the PreToolUse hook entirely while keeping
SessionStart and the MCP tools, which costs you the point-of-read channel (measured for its
earlier, request form in the whitepaper's §8.3) and nothing else. Exact per-session attribution of MCP writes and retrievals needs Claude Code 2.1.154
or later (2.1.163 to keep one id across `--resume`); older hosts fall back to the one fresh
stamp of their family or to `unattributed`, see [hooks](hooks.md#session-attribution).

## Disk

| Item | Size here | Grows with |
|---|---|---|
| Canonical store (`.sidegraph/**/*.json`, committed) | **968 KiB / 1,426 files, ~695 B per record** | Records. ~695 B/record means 10,000 records ≈ 7 MiB of git-friendly text |
| Derived index (`.sidegraph/index.db`, gitignored) | 2.5 MiB | Records + telemetry rows; disposable, rebuilt on demand |
| Code graph (`graphify-out/graph.json`, gitignored) | 10.5 MiB | Your codebase, not your store |

Records are written one file each, so a store grows linearly and diffs per record — that is the
property that makes two branches ratifying different decisions merge without conflict. A
format-2 store ([packed shards](store-format.md#format-2-packed-shards)) folds settled records
into 16, 64 or 256 shard files by an explicit maintenance command; the figures above are of
the one-file-per-record layout and were not re-measured for shards.

## The graph dependency, stated plainly

The code-graph engine is **optional for the store, but required for task-seeded structure and
name/file resolution**. Without it, the SessionStart TOC, proposal queue, raw store listings,
and global memory still work; `get_task_context` cannot resolve file/name seeds against the graph and
can still find a decision whose stored descriptor matches the seed. `query_structure` has no
map to return. With it, Sidegraph adds symbol-level anchors, task-scoped retrieval,
community-derived domain candidates, and rebind support. Its rebuild cost (≈8 s here) is on
your commit or CI path, not on ordinary tool calls. A rebuild may update Graphify's own output,
but ordinary Sidegraph sync does not dirty canonical store files except for a durable leaf-file
move recorded in an entity descriptor.

## Session cost

Each memory-carrying session pays a flat injected-map tax (measured: ~1,138 prompt tokens
on a 15-domain map; ~4.5–6.3k on a 24-domain map — it scales with the rendered map, which
is why the map is budget-bounded), plus retrieval output when the agent actually calls a
tool. The full cost model, including the corpus kinds where this does *not* pay, is in the
whitepaper's §8.5 — read that before assuming a saving.

## CI wiring

```yaml
- run: uvx --from sidegraph==X.Y.Z --python 3.13 sidegraph-verify   # gate
- run: uvx --from sidegraph==X.Y.Z --python 3.13 sidegraph-doctor    # advisory
```

Pin a release, not the latest, so CI runs the version you reviewed. The store format and
documented CLI surface are committed contracts; pre-1.0 minor releases may introduce
breaking changes (see [`stability.md`](stability.md)).

## What this page does not tell you

Unmeasured here: performance at monorepo scale (thousands of
files, hundreds of domains); no multi-developer concurrency numbers; no fleet aggregation
of the local diagnostics; no growth curve for stores an order of magnitude larger than
this one. If you run a pilot, these are the numbers worth capturing — see the
[pilot kit](../pilot-kit/README.md).

## Historical pilot retrieval proxy

The earlier reminder-based protocol used a **40%** ceiling on sessions without a
retrieval call (the measured post-improvement pooled share was 26%). Current hooks can
deliver records without that call, so the old proxy cannot serve as the delivery gate.
