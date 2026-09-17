# Verifying your setup

A "does Sidegraph actually work on my repo" checklist to run right after install. Eight
cases, each provable in a few minutes, each with an exact command and an observable result —
not "trust the docs." Run them in order the first time (later cases build lightly on earlier
ones); re-run any single case independently afterward whenever you want to sanity-check that
part of the loop again.

Every command below is copy-paste-runnable from your repo's root, in a plain terminal or a
Claude Code session as noted. Replace bracketed placeholders (`<...>`) with real names from
your own repo.

## Before you start

Install both pieces (full detail: [installation](../getting-started/installation.md)):

```bash
uv tool install graphifyy
cd /path/to/your-repo
graphify update .
```

Inside a Claude Code session started in that repo, install the plugin — MCP server + all
three hooks, wired automatically:

```
/plugin marketplace add SantyagoSeaman/sidegraph
/plugin install sidegraph@sidegraph
```

Bootstrap the store (a one-off CLI step — no plugin needed, and no local checkout: this runs
straight from the repository via `uvx`):

> **`@main` is a mutable ref.** Every `git+…@main` command on this page tracks the
> branch: what you install today is not what you installed yesterday, and a `uvx` cache
> refresh can change it under you. Fine for trying Sidegraph out; for anything you depend
> on — CI, a shared team setup, a pilot you intend to measure — replace `@main` with a
> commit SHA (`git+https://github.com/SantyagoSeaman/sidegraph@<sha>`) so the version is a
> decision you made rather than whatever HEAD happened to be. See [`reference/stability.md`](../reference/stability.md) for what each surface promises.

```bash
uvx --from git+https://github.com/SantyagoSeaman/sidegraph.git@main sidegraph-init
```

Expected: `created store: .sidegraph` and `found graph: graphify-out/graph.json` (or
`already initialized: .sidegraph exists.` on a re-run — safe to repeat). If you see
`missing graph: ...` instead, run `graphify update .` first.

With those three steps done, work through Cases 1–8 below.

## Case 1 — Domain onboarding

**Proves:** the graph's raw Leiden communities turn into a small, curated, named table of
contents you pick from — not a 200-line list you'd have to hand-curate yourself.

**Steps.** In a Claude Code session in the repo:

> Name my domains

(equivalently, run `/sidegraph:name-domains` directly). The agent calls the read-only
`list_domain_candidates` tool, studies the ambiguous candidates by reading the actual code
behind them, and presents 2–3 alternative sets at different granularities — Coarse (~8
domains), Medium (~15), Fine (~25) — as a compact table, then **stops and waits** for your
pick. It should not write anything before you answer.

Pick one set (e.g. "Medium"). The agent calls `propose_domains` then `ratify` in the same
turn.

**Expected observable result:** start a fresh session (`claude`) in the repo. The injected
`SessionStart` context now shows a `## Domains` section with human titles and one-line
WHY-IT-EXISTS summaries — around 15 domains for a Medium pick, not a raw community dump.

Now ask the agent to name domains again, in a new session:

> Name my domains

**Expected:** `list_domain_candidates` reports the communities you just named as
`already_claimed`; the agent reports there's essentially nothing new to name (0, or close to
it, depending on whether your repo genuinely has unclaimed areas).

**✅ Success:** `SessionStart` shows a `## Domains` section with a small, human-picked set
(not a `## Communities` fallback with raw community ids); re-running the same onboarding
prompt surfaces ~0 new candidates instead of re-proposing the same domains.

## Case 2 — Durability

**Proves:** a domain's membership is committed as durable, name-based entity anchors — not
the volatile Leiden community id that gets renumbered on every rebuild — so it survives a
fresh clone or a rebuilt local index.

**Setup:** at least one domain accepted (Case 1).

**Steps.** Inspect what actually got committed:

```bash
git status
cat .sidegraph/domains/<the-new-domain-id>.json
```

**Expected observable result:** `git status` shows small new files under `.sidegraph/domains/`
(and `.sidegraph/entities/`) — never `.sidegraph/index.db`. The domain JSON has a
`seed_anchors` field (a list of `{"name", "file_path"}` entries) and has **no `communities`
key at all** — membership is derived on read, never committed.

Now simulate a fresh clone by deleting the local, gitignored index and re-deriving it:

```bash
rm -f .sidegraph/index.db*
uvx --from git+https://github.com/SantyagoSeaman/sidegraph.git@main sidegraph-sync --force
```

**Expected observable result:** `synced <never> -> ...: {...}` followed by
`refreshed community mapping for N domain(s)` — no `possibly empty domains` warning for the
domain you just inspected.

Ask the agent, in a fresh session, to `drill_down` on that domain, or ask a task-scoped
question that touches one of its members — the members and decisions should be present,
exactly as before the wipe.

**✅ Success:** the committed domain JSON carries `seed_anchors` and no `communities` key;
after `rm .sidegraph/index.db* && sidegraph-sync`, the domain's membership resolves again
with zero manual repair.

## Case 3 — Manage domains

**Proves:** individual domains can be listed, renamed, and re-scoped without re-running the
whole onboarding pass.

**Steps.** In a session:

> Show me all domains

The agent calls `list_domains()` — every domain, any status, with member counts and
parent/child lineage.

> Rename the "`<some domain>`" domain to "`<new name>`"

The agent calls `supersede_domain(...)`, then ratifies (or asks you to run
`uvx --from git+https://github.com/SantyagoSeaman/sidegraph.git@main sidegraph-ratify --accept <id>`
yourself).

**Expected observable result:** `list_domains()` now shows the old domain as `superseded`
(still retrievable, no longer TOC-visible) and the new one as `accepted` under the new title
— never a hard rewrite of the old record.

**✅ Success:** the rename produces two rows (old `superseded`, new `accepted`), not an
in-place edit; `git log --stat` on the domain files shows a new file added, not an existing
one modified.

## Case 4 — Mistakes-first retrieval

**Proves:** the core loop — a gotcha comes back **first**, before the agent reads a single
file, complete with why it happened — this is the whole point of the tool.

**Setup (optional).** If you have an existing folder of ADRs/design docs, seed the store from
it first:

```bash
graphify update .   # make sure the graph is current right before importing
uvx --from git+https://github.com/SantyagoSeaman/sidegraph.git@main sidegraph-import --docs <path-to-your-docs> --dry-run
uvx --from git+https://github.com/SantyagoSeaman/sidegraph.git@main sidegraph-import --docs <path-to-your-docs>
```

No existing docs? Skip straight to the next step.

**Steps.** In a session, record a gotcha tied to a real file in your repo:

> Record a gotcha: `<describe a real mistake or trap in this codebase>` — anchor it to
> `<a real function or file>`.

Start a **brand-new** session (`claude`) and ask a task-scoped question about that same file:

> I'm about to edit `<that file>`. What should I know first?

**Expected observable result:** the `SessionStart` context you saw right when the session
opened led with the standing instruction:

> When you need to find or understand code in this project, call get_task_context(seeds)
> before any grep or file search — decisions, gotchas and a domain map are indexed here.

And the answer to your question opens with a `## ⚠ Known mistakes & gotchas` block —
**before** any `## Decisions` or `## Structural map` block — rendering your gotcha with its
`context`/`rejected`/`consequences`, not just a bare title.

**✅ Success:** the gotcha is the first thing the agent surfaces for that file, with its full
reasoning attached; the standing search instruction appeared at session start, ahead of the
agent's first grep.

## Case 5 — Quiet session capture

**Proves:** the `Stop`-hook nudge respects your attention — it interrupts at most once per
session, and only once the session actually did something.

**Steps.**

- **Trivial session:** open a session, ask one throwaway question ("what does this repo
  do"), end it. **Expected:** no `Stop hook error: Sidegraph: ...` banner at all — a session
  with one real prompt never nudges.
- **Substantial session:** open a session, have at least two real back-and-forth exchanges
  (not just tool results), end it. **Expected:** exactly one
  `Stop hook error: Sidegraph: if this session produced a durable decision...` banner, the
  first time you finish — never again in that same session.
- **Disabled:** set `SIDEGRAPH_CAPTURE_NUDGE=off` (alongside `SIDEGRAPH_DIR`/
  `SIDEGRAPH_GRAPH` in the hook command — see
  [claude-code-setup.md](../getting-started/claude-code-setup.md)) and repeat the substantial
  session. **Expected:** no banner at all, regardless of how substantial the session was.

**✅ Success:** 1-prompt session → 0 nudges; ≥2-prompt session → exactly 1 nudge, on its
first `Stop`, never on later ones; `SIDEGRAPH_CAPTURE_NUDGE=off` → 0 nudges unconditionally.

## Case 6 — Refactor survival

**Proves:** an anchored decision follows code that moves, degrades honestly when it can't,
and never silently re-anchors to the wrong thing.

**Setup:** the gotcha you anchored in Case 4 (or anchor one now to a real function).

**Steps.** Move that function to a different file, **keeping its name**:

```bash
# e.g. move `check_fees` from src/fee_gate.py to src/risk/checks.py
graphify update .
uvx --from git+https://github.com/SantyagoSeaman/sidegraph.git@main sidegraph-sync
```

(The `sidegraph-sync` half also works without the terminal: after the rebuild, call the
`sync_anchors` MCP tool from inside the session — same report, as data — or invoke the
`sidegraph:heal-anchors` skill, which leads with it.)

**Expected observable result:** a report line naming the move, e.g.:

```
moved: check_fees() (src/fee_gate.py -> src/risk/checks.py)
```

Ask the agent about the new location — the decision should still surface exactly as before.

Now revert the move, and instead **rename the symbol outright** to something with no match
anywhere else in the repo:

```bash
graphify update .
uvx --from git+https://github.com/SantyagoSeaman/sidegraph.git@main sidegraph-sync
```

**Expected observable result:** a report line like `orphaned: check_fees() (no match)` plus a
`possibly stale decisions (all anchors gone — verify):` block naming the affected decision —
never a silent guess at what the symbol became.

**✅ Success:** a same-name move across files reports `moved:` and retrieval follows the code
to its new location automatically; a genuine rename/deletion reports `orphaned:` +
"possibly stale," never a silent (and possibly wrong) re-anchor.

## Case 7 — Git-native

**Proves:** the store really is small, human-readable JSON files that live and travel with
your repo like code — no hidden database, no server, nothing sent anywhere. (Local usage
diagnostics do exist, in the gitignored index only; see the Trust & privacy note in the
README. They never leave the machine and `SIDEGRAPH_TELEMETRY=off` disables them.)

**Steps.**

```bash
git status
```

**Expected:** only small `.sidegraph/decisions/*.json` / `.sidegraph/domains/*.json` /
`.sidegraph/entities/*.json` / `.sidegraph/bindings/*.json` files show up as new or changed —
`.sidegraph/index.db` never appears (confirm it's covered:
`cat .sidegraph/.gitignore`).

```bash
git diff --stat HEAD~1     # after anything got ratified in this session
```

**Expected:** a handful of small text files, each a few KB — reviewable in a PR exactly like
a code change, never "binary file changed."

Once you've superseded or dropped at least one decision/domain (Case 3 or Case 6 will have
produced one), dry-run the maintenance command:

```bash
uvx --from git+https://github.com/SantyagoSeaman/sidegraph.git@main sidegraph-compact --dry-run
```

**Expected:** a list of the terminal-status (superseded/rejected/dropped) records that
**would** move into an immutable archive segment — `would compact N record(s) (...)` — with
nothing actually written (re-run without `--dry-run` when you're ready to do it for real; see
[`reference/cli.md`](../reference/cli.md#sidegraph-compact)).

**✅ Success:** `git status`/`git diff` show only small human-readable JSON files;
`.sidegraph/index.db` never appears in either; `sidegraph-compact --dry-run` lists real
candidates and writes nothing.

## Case 8 — Facts: evidence and cascade

**Proves:** a `Fact` is a first-class, falsifiable record that rides its decision's ratify
verdict when it's attached to one, and stands on its own in the queue and in retrieval when
it isn't — the evidence layer under decision memory (see
[`guides/capturing-decisions.md#facts-the-evidence-layer`](capturing-decisions.md#facts-the-evidence-layer)).

**Steps.** In a session, propose a decision with an attached fact, plus a separate standalone
fact anchored to the same place. The wording below is a worked example — anchor it to a real
function or file in your own repo, but keep the title/choice text as shown; it matters for the
literal output further down:

> Record a proposed decision for `<a real function or file>`: cache quotes for 5 seconds,
> because the exchange rate-limits quote calls. I also have a fact that informed it — a
> benchmark showed the exchange trips its rate limit after roughly 20 quote calls per minute,
> from a benchmark run on 2026-07-09. Separately, record a standalone fact anchored to the same
> place: the exchange API returns HTTP 429 with a Retry-After header when rate-limited, from
> the exchange API docs.

The agent calls `propose_decisions`:

```python
propose_decisions(drafts=[{
    "title": "Cache quotes for 5 seconds",
    "kind": "adr",
    "context": "the exchange rate-limits quote calls",
    "choice": "Cache quotes for 5 seconds",
    "anchors": [{"name": "<a real function or file>", "file_path": "<path>"}],
    "facts": [{
        "statement": "A benchmark showed the exchange trips its rate limit after roughly 20 "
                     "quote calls per minute.",
        "source": "benchmark run 2026-07-09"
    }]
}], facts=[{
    "statement": "The exchange API returns HTTP 429 with a Retry-After header when "
                 "rate-limited.",
    "source": "exchange API docs",
    "anchors": [{"name": "<same function or file>", "file_path": "<path>"}]
}])
```

The first fact is *attached* — it names no `anchors` of its own, so it inherits the decision's.
The second is *standalone* — it needs its own `anchors` or a `supports` id, or the pipeline
rejects it as unreachable. Both write `status=proposed`. Note `choice` here repeats `title`
verbatim — that's deliberate: when a decision's `choice` starts with its `title`, every render
below collapses the two into just the title (no `: <choice>` suffix) rather than showing it
twice; see [`guides/retrieval-in-sessions.md`](retrieval-in-sessions.md) for the general shape
of a decision line.

Ask what's pending:

> What's waiting on ratification?

**Expected observable result:** the agent calls `list_proposed()`. Under `Decisions:`, your
draft's block carries one indented line right under it — nested, not a separate entry:

```
  evidence: A benchmark showed the exchange trips its rate limit after roughly 20 quote calls per minute. [benchmark run 2026-07-09]  (<fact-id>)
```

The standalone fact gets its own entry further down, under a `Facts:` section.

Ratify the decision:

> Ratify that decision.

**Expected observable result:** the agent calls `ratify(accept=[<decision-id>])`. The returned
dict has an entry for the attached fact's id too, even though you never listed it — the
cascade:

```
"<attached-fact-id>": "accepted (evidence of <decision-id>)"
```

Now ask a task-scoped question seeded on that same anchor:

> I'm about to touch `<that function or file>`. What should I know first?

**Expected observable result:** the agent calls `get_task_context(...)`. `## Decisions` renders
your ADR with the now-accepted attached fact directly beneath it — `choice` collapsed into
`title` (see above), `context` appended as a suffix since this is the direct/detailed render
tier:

```
- [adr] Cache quotes for 5 seconds (context: the exchange rate-limits quote calls)
  evidence: A benchmark showed the exchange trips its rate limit after roughly 20 quote calls per minute. [benchmark run 2026-07-09]
```

and a separate `## Known facts` block — rendered right after `## Decisions`, ahead of the
structural map — carries the still-`proposed` standalone one, tagged:

```
## Known facts
- fact [unratified]: The exchange API returns HTTP 429 with a Retry-After header when rate-limited. [exchange API docs]
```

Finally, falsify the standalone fact:

> That standalone fact turned out to be wrong: the exchange actually returns a plain 503 under
> rate limiting, no Retry-After header, according to a follow-up support ticket.

The agent calls `supersede_fact(old_fact_id=<fact-id>, statement="The exchange returns a plain
503 under rate limiting, no Retry-After header.", source="support ticket #4821")` with no
`anchors` — the successor inherits the predecessor's bindings verbatim. Re-run the same
task-scoped question.

**Expected observable result:** `## Known facts` now shows only the corrected statement, and
with no `[unratified]` tag this time — `supersede_fact` always writes its replacement
`status=accepted`, regardless of the predecessor's status:

```
## Known facts
- fact: The exchange returns a plain 503 under rate limiting, no Retry-After header. [support ticket #4821]
```

The old statement is gone — `supersede_fact` closes the predecessor (`valid_to` set,
`status=superseded`), so it drops out of retrieval exactly the way `supersede_decision` does
for a decision.

**✅ Success:** `list_proposed()` nests the attached fact under its decision as one indented
`  evidence: ...` line, never as a separate `Facts:` entry; ratifying the decision reports the
attached fact's own cascade line, `accepted (evidence of <decision-id>)`; `get_task_context`
shows the inline evidence line under the decision AND a `## Known facts` block for the
standalone one; `supersede_fact` retires the old statement from both, replaced cleanly by the
new one.

## If a case doesn't match

Start with [`reference/configuration.md`](../reference/configuration.md) (store/graph path
resolution is the most common cause of "nothing shows up") and
[`integrations/graphify.md`](../integrations/graphify.md#troubleshooting). If a hook seems
silent, remember all three degrade to doing nothing rather than crashing your session — see
[`reference/hooks.md`](../reference/hooks.md) for exactly what each one does and when it
no-ops. For the deeper mechanics behind any single case, the concept docs are the next stop:
[mind model](../concepts/mind-model.md), [retrieval](../concepts/retrieval.md),
[anchoring](../concepts/anchoring.md), [store format](../reference/store-format.md).
