# Capturing decisions

Two ways a decision enters the store: the agent writes it **directly** mid-session
(`add_decision`), or the session gets **distilled and proposed** at the end and is ratified
later — by a human by default (`propose_decisions` → `sidegraph-ratify`), or at write time by
an opt-in [auto-ratification policy](#5-auto-ratification-policy-opt-in). Both paths write into the same
append-only store; see [`reference/mcp-tools.md`](../reference/mcp-tools.md) for exact tool
signatures. Three plugin skills carry this guide's discipline into the session itself:
[`sidegraph:record-decision`](../../plugin/sidegraph/skills/record-decision/SKILL.md) (the
decision authoring craft),
[`sidegraph:ratify-decisions`](../../plugin/sidegraph/skills/ratify-decisions/SKILL.md) (the
in-session ratification gate), and
[`sidegraph:record-fact`](../../plugin/sidegraph/skills/record-fact/SKILL.md) (the fact
authoring craft — see [Facts: the evidence layer](#facts-the-evidence-layer) below).

## 1. Recording a decision mid-session

Just ask the agent, in plain language, to record what was just decided. For example:

> Record a gotcha: ADR-001's "Vocabulary and Entity Relations" section is the normative
> vocabulary for all DQ terms; earlier drafts drifted (check vs rule vs control) and
> mis-mapped concepts. Anchor it to that heading AND to the ADR-001 file.

The agent calls `add_decision(title, kind, context, choice, rejected=None,
consequences=None, anchors=[...])`. This path writes with `status=accepted` immediately —
there is no ratification step for decisions you record directly; you were the human in the
loop when you asked for it.

### The multi-anchor rule: always pair the symbol or heading with its file

`anchors` is a list of `{"name": ..., "file_path": ...}` refs. Each is resolved against the
current Graphify graph and bound at up to three tiers (leaf, community, initiative) — see
[`concepts/anchoring.md`](../concepts/anchoring.md). For a documentation corpus, the anchor
`name` is a **heading text** or the **file name itself**; for code, a **function/class name**
or the file — all valid entity names in a Graphify graph.

**Always add the containing file as a second anchor — for code symbols and doc headings
alike.** A heading rename or symbol move orphans the precise anchor on the next sync; the
file anchor survives and keeps the decision retrievable (and its community fallback intact).
In the first full maintenance pass over this project's own store (2026-08-09), 7 of the 10
records that had gone completely invisible would have stayed reachable under this one rule:

```json
{
  "anchors": [
    {"name": "Vocabulary and Entity Relations", "file_path": "adr/ADR-001.md"},
    {"name": "ADR-001.md", "file_path": "adr/ADR-001.md"}
  ]
}
```

One anchor orphaning does not take the decision down with it — the other keeps it live. See
[`guides/surviving-refactors.md`](surviving-refactors.md) for what happens to each anchor
independently when the graph is rebuilt.

Two name shapes look valid and never resolve — write the pair instead:

- **A bare module-level constant** (`MEMORY_GUARD_LINE`, `_SECRET_PATTERNS`): the engine's
  AST pass indexes functions, classes and files, **not constants** — such an anchor is born
  orphaned. Anchor the containing file (or enclosing function) and name the constant in the
  record's prose.
- **A path-qualified or class-qualified name** (`src/sidegraph/store.py`,
  `Store._touch_digest`): the graph labels files by basename and methods bare. Write
  `{"name": "store.py", "file_path": "src/sidegraph/store.py"}` and
  `{"name": "_touch_digest", "file_path": "src/sidegraph/store.py"}` — the `file_path`
  field, not the name, carries the path.

Two more targets fail *later*, even when the name resolves today:

- **A file in another repository.** This store's graph will never contain it, so the
  anchor is permanently unresolvable here. Anchor the knowledge to its **nearest in-repo
  consumer** instead — the file a future session will actually be touching when the
  record matters (and name the foreign path in the record's prose).
- **An ephemeral process artifact** — a task brief, a run report, a scratch directory.
  When the wave that produced it is cleaned up, the record's anchors die with it. If the
  lesson outlives the wave, anchor it to what outlives the wave: the code or doc the
  lesson is *about*.

Either name-shape mistake is visible at write time: the result's `anchors_orphaned` names
it — fix it before leaving the record, don't ship a memory nothing will ever surface. The
two late-failing targets are not (they resolve fine today) — they are a judgment call this
guide is the checklist for.

## 2. The automated path: Stop hook → distill → propose → ratify

At the end of a session, the `Stop` hook fires a one-time nudge (see
[`reference/hooks.md`](../reference/hooks.md)) asking the agent to review the session for
anything durable. The agent — not a separate LLM call, the same in-session agent — distills
what happened into one or more **What / Why / Where / Learned** drafts and calls
`propose_decisions(drafts=[...])`.

Each draft maps directly onto the decision schema:

| Draft field | Decision field | Notes |
|---|---|---|
| What | `title`, `choice` | short title + what was decided |
| Why | `context` | including constraints that emerged |
| Where | `anchors` | `[{"name", "file_path"}, ...]` |
| Learned | `consequences` | trade-offs accepted |
| (rejected) | `rejected` | what was tried and abandoned |

The write pipeline (`src/sidegraph/capture.py`) is deterministic — no LLM key required:

1. **Redact** every text field first (AWS keys, GitHub/Slack tokens, bearer tokens, private
   key blocks, `key=value`/`key: value` secrets) — the scrubbed text is the only text that
   ever reaches the store.
2. **Dedup** conservatively: a draft is dropped as `deduped` only if a currently-valid
   decision with the same `kind` and the same canonicalized `title` already binds one of the
   same anchor entities. When unsure, it writes — the human drops it at ratification instead.
   (Under an auto-ratification policy nobody drops it: a near-duplicate with a different
   title can ratify itself — see [§5](#5-auto-ratification-policy-opt-in).)
3. **Write** as `status="proposed"`, with `provenance.source="agent"` and the current
   `graph_version` recorded.
4. **Anchor** best-effort via the same multi-anchor resolution as `add_decision` (orphaned
   leaf if no graph is present).

Proposed decisions are **not silently invisible**: they do surface in `get_task_context` and
`retrieve_decisions`, but every rendered line is tagged `[unratified]` so you always know a
draft hasn't been reviewed yet (see
[`guides/retrieval-in-sessions.md`](retrieval-in-sessions.md)). Ratification is what removes
that tag, not what makes the record appear.

## 3. Ratification

Ratify without leaving the session. `SessionStart` tells you when something is waiting
("Sidegraph: N record(s) awaiting ratification …"), and saying "ratify" — or invoking the
`sidegraph:ratify-decisions` skill — walks the queue conversationally. Under the hood that
is two MCP tools: `list_proposed()` (human-readable render of everything pending) and
`ratify(accept=[...], drop=[...])` — the unified tool that ratifies decisions *and* domains
through one gate (see [mind model](../concepts/mind-model.md#domain-lifecycle)).
`ratify_decisions(accept=[...], drop=[...])` still works but is a **deprecated alias**, kept
for one release for existing callers only — prefer `ratify` in new code.

- **Accept** flips `proposed → accepted`. The record now reads untagged everywhere.
- **Drop** is append-only, like everything else in the store: it sets `valid_to` and flips
  status to `rejected`. The record is never deleted — a dropped draft that turns out to be
  wrong for now can still be revisited later, or referenced as "considered and rejected."

Prefer the shell for scripted use or ratification-as-PR-review (see
[team workflow](team-workflow.md)). With no `--db` flag, `sidegraph-ratify` auto-prefers a
store: it uses `$SIDEGRAPH_DIR` if set, otherwise the deprecated `$SIDEGRAPH_DB`, otherwise
an existing `.sidegraph/` if present, otherwise the default `.sidegraph` — printing a stderr
warning if the resolved path doesn't exist yet (it's about to create a new, empty store). If
your store lives at the recommended `.sidegraph/` path, the bare invocations below just
work; otherwise pass `--db` or set `SIDEGRAPH_DIR` explicitly (full resolution order:
[`reference/cli.md`](../reference/cli.md)):

```bash
uv run sidegraph-ratify                          # list what's pending
uv run sidegraph-ratify --accept 01J...ULID       # accept one or more ids
uv run sidegraph-ratify --drop 01J...ULID         # drop one or more ids
uv run sidegraph-ratify --all                     # accept everything pending
```

Full exact flags and behavior: [`reference/cli.md`](../reference/cli.md).

## 4. Auto-accept (opt-in)

The proposed → ratify gate above is the store's **only noise filter** — every agent-drafted
decision and fact stops there until it is accepted — explicitly by a human, unless an
[auto-ratification policy](#5-auto-ratification-policy-opt-in) admits it. Some workflows (a solo
developer working alone, a personal/scratch corpus) would rather skip that stop entirely.
Set `SIDEGRAPH_AUTO_ACCEPT=on` in the environment the MCP server runs in, and every agent
capture through `propose_decisions` — each decision draft, its attached facts, and any
standalone facts passed via the top-level `facts` parameter — lands `status="accepted"`
directly instead of `"proposed"`, with no ratification step at all.

**It is detectable, and in a shared store it should be.** The switch lives in one
environment while the store it writes to is committed for everyone, so
`sidegraph-doctor` reports the signature it leaves — an accepted record with
`provenance.source="agent"` and no ratifier stamp — as an `unratified-accept`
finding. Use it in CI if your team's answer to "is the gate on?" needs to be a
check rather than a promise.

**State the trade-off plainly before turning this on:**

- **It removes the store's only noise filter.** Nothing stands between an agent's draft and
  durable team memory anymore — a bad title, an off-target anchor, or a decision that isn't
  actually decision-worthy lands as accepted memory just as readily as a good one.
- **Recommended for solo use — not for team stores.** On a shared, repo-committed store,
  every teammate (and every future reader) inherits whatever an agent decided was worth
  keeping, unreviewed. On a personal corpus where you're the only consumer, that trade is
  often worth the convenience.
- **Provenance never lies about authorship.** `provenance.source` still stamps `"agent"`
  under auto-accept, exactly as it does under the default gated path — the record honestly
  shows a human never reviewed it, even though its `status` reads `accepted`.
- **Domains always stay gated under this flag.** `propose_domains` never consults this
  variable — a domain draft always lands `status="proposed"`, auto-accept on or off (only
  `SIDEGRAPH_RATIFY_POLICY=auto-all`, §5, can ratify an eligible domain). Domains
  are few in number, expensive to get wrong (a bad name/scope sticks around and shapes the
  whole mind-model layer), and `drill_down`/retrieval hard-gate on `accepted` domains — so
  the naming decision stays a deliberate human act regardless of this setting.

There is no TTL or auto-promote path (a proposed record that ages out and silently becomes
accepted) — that was considered and rejected as legitimizing noise by default. Auto-accept is
all-or-nothing, opt-in, and off unless the environment variable is exactly `"on"`. See
[`reference/configuration.md`](../reference/configuration.md) for the exact env var contract
and [`reference/mcp-tools.md`](../reference/mcp-tools.md#propose_decisions) for
`propose_decisions`'s full auto-accept behavior.

## 5. Auto-ratification policy (opt-in)

`SIDEGRAPH_RATIFY_POLICY` is a second, independent opt-in: instead of skipping ratification
entirely (auto-accept, above), it lets a *deterministic, stamped* policy stand in for the
human at write time — useful for an unattended import pipeline where nobody is going to run
`sidegraph-ratify` at all. Set it in the environment the MCP server or
CLI command runs in; matching is exact after trimming surrounding whitespace, and any unknown
or empty value falls back to `manual`.

| Policy | Admits |
|---|---|
| `manual` (default) | Nothing — every write keeps today's behavior exactly, including the three CLI batch summaries and every rendered surface. |
| `auto-low-risk` | An eligible `gotcha`/`lesson` decision or standalone fact. |
| `auto-all` | Everything `auto-low-risk` admits, plus `adr`/`constraint` decisions and domains. |

Eligibility is deterministic and conjunctive, never an LLM judgment call: the policy admits
the record's shape (under `auto-low-risk` a draft that `supersedes` another is never
eligible); the record has at least one live Tier-1/Tier-2 anchor binding (a domain instead
needs a graph reader, a clean `path_prefixes` lint, and a resolving seed anchor or non-empty
prefixes); the write was clean and not a dry run; provenance is present. An attached fact
never ratifies itself — it rides its decision's cascade, and one ineligible attached fact
keeps the whole decision proposed. The policy is read once per call — by the MCP tool shell
or the CLI command — and passed down from there; one batch never sees two different values,
even if the environment changes mid-run.

**How to tell a record was auto-ratified:** its `ratified_by` reads `auto:<policy>` (e.g.
`auto:auto-low-risk`) instead of a human identity; its result `status` keeps its
write-action value (`written` for a decision or fact, `proposed` for a domain) —
auto-ratification is a *consequence* of a successful write, not a different result shape; and
once any record anywhere carries an `auto:` stamp, `sidegraph-doctor`'s human output gains an
`auto share` and an `auto supersede rate` line (see
[`reference/cli.md`](../reference/cli.md#sidegraph-doctor)).

**Residual risk (read this before turning it on):**

- **No human drop for near-duplicates.** The dedup check above still runs, but a
  near-duplicate with a different title is not a duplicate to it — under a human gate that
  draft would sit `[unratified]` until someone recognized it and dropped it; under an
  auto-ratification policy it can ratify itself instead.
- **`auto-low-risk` admits gotchas and lessons, which retrieval ranks first.** A wrong
  auto-ratified gotcha renders first on every retrieval surface, and — because it is no
  longer `[unratified]` — untagged, indistinguishable from a human-reviewed one except by
  its `ratified_by` stamp.
- **The supersede rate only moves when something is actually superseded — by a human, by
  an agent calling `supersede_decision` directly, or under `auto-all` by an agent's own
  superseding proposal.** A bad auto-ratified record that nobody ever revisits looks
  identical, in that metric, to a good one.
- **A record's own anchor check is check-then-act, not atomic.** Bindings that go stale in
  the instant between the eligibility check and the transition are still accepted;
  `sidegraph-sync`/`heal-anchors` will surface the drift afterward, the same as for any other
  record.
- **v1 sets no auto-share threshold and takes no automatic response** to any of the above —
  the two doctor lines are informational only, for now.

Legacy `SIDEGRAPH_AUTO_ACCEPT=on` still wins when both knobs are set — see
[`reference/configuration.md`](../reference/configuration.md).

## Facts: the evidence layer

Not everything worth keeping is a decision. Sometimes what you learned is a plain,
non-derivable **fact** that *informed* one (or several) — a benchmark result, an external
limit, something a docs page or a failed attempt taught you. Sidegraph has a dedicated
`Fact` record for exactly that, scoped by a razor:

> Only facts the code graph cannot derive belong here: empirics (benchmarks, observed
> behavior), external constraints (API limits, library capabilities), trial-learned
> knowledge — never 'the code does X'.

If Graphify could answer it by reading the code, it isn't a `Fact` — that's the engine's
job, and it goes stale with every commit; a `Fact` is for what the engine *can't* answer.

### Attached to a decision

Extend the What/Why/Where/Learned draft from the automated path above with a `facts` list
— each entry supports the decision it's attached to and, unless it names its own `anchors`,
inherits the decision's:

| Draft field | Value |
|---|---|
| What | Wrap httpx with an explicit retry policy for the exchange client |
| Why | The exchange API returns transient 5xx under load; a request must not silently fail once |
| Where | `[{"name": "ExchangeClient", "file_path": "src/exchange/client.py"}]` |
| (rejected) | httpx's own retry support — it doesn't ship one |
| Facts | one attached `DraftFact`: the statement below, sourced to the httpx docs |

```python
propose_decisions(drafts=[{
    "title": "Wrap httpx with an explicit retry policy for the exchange client",
    "kind": "adr",
    "context": "The exchange API returns transient 5xx under load; a request must not "
               "silently fail once.",
    "choice": "Wrap httpx.Client with a tenacity-based retry policy scoped to 5xx/timeout.",
    "rejected": "httpx's own retry support — it doesn't ship one.",
    "anchors": [{"name": "ExchangeClient", "file_path": "src/exchange/client.py"}],
    "facts": [{
        "statement": "httpx has no built-in retry — a transient 5xx is not retried "
                     "automatically.",
        "source": "httpx docs, 'Timeouts and retries' section"
    }]
}])
```

The fact writes as its own `status="proposed"` record, `supports=[<the decision's id>]`,
bound to `ExchangeClient` (inherited, since it named no `anchors` of its own) — and rides
the decision's ratify verdict: accept the decision and the fact accepts with it (see
[Ratification](#3-ratification) above and the `sidegraph:ratify-decisions` skill for the
full cascade — including what happens to a fact when its decision is dropped instead).

### Standalone

A fact that doesn't attach to any decision in the same call goes in `propose_decisions`'s
top-level `facts` parameter instead — it needs at least one `anchors` entry or one
`supports` id of its own, or it's unreachable and the pipeline rejects it with a reason.
Human-asked facts skip the queue entirely, same rule as `add_decision`: `add_fact(statement,
source, supports=[...], anchors=[...])` lands `status="accepted"` immediately — the asking
human was the gate.

### Falsifying a fact

Never edit a fact or delete it — `supersede_fact(old_fact_id, statement, source, ...)`
closes the predecessor (`status=superseded`, `valid_to` set) and writes a replacement in the
same transaction, exactly like `supersede_decision`. Omit `anchors` to inherit the
predecessor's bindings verbatim (including any `orphaned` ones, carried as-is); `supports`
defaults to the predecessor's own `supports` when omitted.

Full signatures and return shapes:
[`docs/reference/mcp-tools.md`](../reference/mcp-tools.md#add_fact). For the full
fact-authoring craft — the scope razor as an entry gate, telling a fact apart from a
decision, field craft, and falsification — see
[`sidegraph:record-fact`](../../plugin/sidegraph/skills/record-fact/SKILL.md).

## Already have ADRs? Import them

Everything above is about capturing decisions going forward. If the team already has a folder
of ADRs, design specs, or runbooks, don't re-type them — `sidegraph-import --docs <path>`
parses decision-shaped markdown directly (the parser is deterministic and LLM-free — no key
needed) and writes one anchored `Decision` per document, so the store starts seeded with the
team's existing history instead of empty. The
[`sidegraph:import-adrs`](../../plugin/sidegraph/skills/import-adrs/SKILL.md) skill drives the
whole import (dry-run first, report triage, gated real run) from inside a session. Import still requires a current, readable graph,
though: every decision it writes must anchor, and a document with nothing anchorable at all is
skipped rather than imported. **Run `graphify update .` right before importing** so the docs
you're about to seed can actually resolve:

```bash
graphify update .
uv run sidegraph-import --docs docs/adr --dry-run
```

See [`guides/semantic-docs.md`](semantic-docs.md#the-other-importer---docs)
for the walkthrough and [`reference/cli.md`](../reference/cli.md#importing-decision-shaped-markdown---docs)
for the full flag reference.

## What makes a GOOD decision record

- **Fill `rejected`.** This is the highest-value field — what was tried, considered, or hit before
  landing on `choice` — and it is the reason Sidegraph exists at all. Gotchas and lessons rank
  first in every retrieval precisely because `rejected` is what saves the next person (or
  agent) from re-discovering the same rake. A decision with an empty `rejected` is a plain
  fact; one with a filled `rejected` is durable team memory.
- **Anchor to what the decision is actually about**, not everything touched incidentally.
  Weak or off-target anchors show up as noise in someone else's task context later.
- **Pick the right `kind`.** `gotcha`, `lesson`, and `constraint` all ride the mistakes-first
  block, ahead of plain `adr`s — reserve `gotcha`/`lesson` for things that actually cost time
  or caused a mistake, not routine architectural choices.
- **Keep `context`/`choice`/`consequences` short and concrete.** Retrieval is budget-bounded
  in characters (see [`reference/configuration.md`](../reference/configuration.md)); a long
  decision crowds out others in the same task context.
- **Use `supersede_decision` (or `supersedes` in a draft), not a fresh unrelated record**,
  when a past decision was reversed — the chain of "tried before, abandoned because…" is the
  point (see [`concepts/decision-memory.md`](../concepts/decision-memory.md)).
