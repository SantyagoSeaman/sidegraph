# Naming your domains

The practical walkthrough for turning a graph's nameless community structure into the named
mental model the `SessionStart` TOC and `drill_down` serve from. See
[mind model](../concepts/mind-model.md) for why this layer exists; this guide is the "do this,
in this order" version.

## The fast path: ask your agent

Once a graph exists (see [step 1](#1-build-the-graph)), the recommended way to name domains
is conversational, not a CLI dry-run-then-hand-curate loop. In a Claude Code session started
in the repo:

> Name my domains

(or run `/sidegraph:name-domains` directly, or say "give this project a table of contents").
The skill calls the read-only `list_domain_candidates` MCP tool, studies the result —
reading the actual code behind an ambiguous candidate rather than trusting a label blindly.
With roughly 20 or fewer candidates it proposes one sensible set. For a larger result it
composes 2–3 alternatives at different granularities (typically Coarse, Medium, and Fine).

**On a big repo, the skill sees the top ~100 candidates by default**, not the full list —
`list_domain_candidates` caps at 100 significant communities (deterministic community-id
order) unless told otherwise, because an unbounded call on a monorepo-scale corpus is a
~276K-token dump (a real cross-project finding: Apache Airflow has 2,578 significant
communities). The tool flags this itself (`"truncated": true`, `"total_significant"` showing
the full count) and the skill can always ask for more — a narrower `min_members`/`paths`, an
explicit `limit=N`, or `limit=0` for everything — same convention as `sidegraph-domains
bootstrap --limit` below.

Each domain in each set carries
a human name, a one-line WHY-IT-EXISTS summary, and its membership as durable `seed_anchors`
(entity anchors, not raw community ids — those don't survive a fresh clone or graph rebuild).
The skill presents the sets as a compact table and **stops** — it waits for you to pick one,
ask for a merge/rename, or say "I'll choose myself" (which hands off to the
[`sidegraph:manage-domains`](../../plugin/sidegraph/skills/manage-domains/SKILL.md) skill for
a-la-carte add/rename/drop). Only after your explicit pick does it call `propose_domains` and
`ratify` — nothing is written before that.

This is what replaces hand-curating a 200+-line `sidegraph-domains bootstrap` listing: the
deterministic core still ranks and groups every candidate, but an agent drafts the named sets
so you just pick one instead of reading hundreds of raw community ids.

The rest of this guide covers the underlying CLI machinery the skill (and
`sidegraph-domains bootstrap`) both build on — useful for scripted/CI use, or when you want to
see exactly what's happening under the hood.

## How often do I run this? (mostly once)

Naming domains is **onboarding, not a routine task** — you do it once per project, and the
result is durable. It is *not* rebuilt from scratch each time, and you do not re-run it every
session.

- **Domains are committed records.** Once ratified they live in
  `.sidegraph/domains/<id>.json`, versioned with your repo and surviving a fresh clone. Naming
  them again does not regenerate them.
- **Membership stays fresh automatically.** As code moves, `sidegraph-sync` (which runs lazily
  on every `SessionStart` / `get_task_context`) re-resolves each domain's `seed_anchors` to its
  current communities. A function that moved or a file that was renamed is followed to its new
  home without you touching the domains — the same durable-anchor mechanism decisions use. No
  `name-domains` re-run needed for ordinary drift.
- **Re-running is incremental, not a redo.** A community claimed by any non-superseded domain is skipped
  (the tool reports it as `already_claimed`), so on an already-named project `name-domains`
  surfaces roughly zero candidates and the agent stops — it never re-offers the domains you
  already have. The one time it's worth re-running is when a genuinely **new area** appears (you
  added a subsystem with no domain yet); it then proposes only those new communities, which is
  quick.
- **Targeted edits don't need it either.** Renaming, splitting, or dropping a single domain is
  the [`sidegraph:manage-domains`](../../plugin/sidegraph/skills/manage-domains/SKILL.md) skill,
  not a re-onboarding pass.

The one-time cost is the agent's reasoning over the candidate list (~10–15k tokens); the
`list_domain_candidates` read itself is a fast deterministic query, and the graph build
(`graphify update .`) is a separate step you wire into your commit flow, not part of naming.

## 1. Build the graph

```bash
cd /path/to/your/repo
graphify update .
```

Domains are proposed from the graph's own Leiden communities, so a graph has to exist first
(same prerequisite as everything else in Sidegraph — see [quickstart](../getting-started/quickstart.md)).

## 2. Bootstrap, as a dry run first

**Scripted/CI alternative to [the skill above](#the-fast-path-ask-your-agent).** Use this path
when there's no agent session to ask — a CI job, a batch pass over many repos, or you just want
the raw candidate list yourself.

```bash
uv run --project /ABSOLUTE/PATH/TO/sidegraph sidegraph-domains bootstrap --dry-run
```

This lists every community at or above the member-count threshold (`--min-members`, default
`5`) as a proposed slug/title/summary, without writing anything. Read the list before writing
anything real — this is the step that decides how many domains you're about to have to review.

**Tune `--min-members` on big repos.** The default `5` is tuned for small-to-medium corpora. On
a large repo, a low threshold proposes a domain for every small utility cluster, and a
non-dry-run bootstrap that proposes **more than 50 domains in one run** prints a stderr hint
(`note: about to propose N domains — consider --min-members/--limit and ratify selectively`) for
exactly this reason. If the dry run lists an unreasonable number of candidates, raise `--min-members`
(and/or use `--paths`/`--limit` to scope the run to one area) and re-run the dry run before
writing anything:

**`--limit` also defaults to 100** (same scale-aware default `list_domain_candidates` applies)
— both a real run's write and a `--dry-run` listing cap at the top 100 significant communities
in deterministic community-id order, and print a stderr note (`note: M significant communities
found — showing only the top 100 …`) whenever that actually cuts something. Pass `--limit 0`
for the full, unbounded list when you genuinely want it, or a higher `--limit N` to widen
partway.

```bash
uv run --project /ABSOLUTE/PATH/TO/sidegraph sidegraph-domains bootstrap --dry-run --min-members 15
uv run --project /ABSOLUTE/PATH/TO/sidegraph sidegraph-domains bootstrap --dry-run --paths src/execution
```

### Guardrails against an over-broad path rule

Bootstrap derives a community's `path_prefixes` stabilizer only when >= 80% of its anchorable
members share one top-level directory — and even then, two guards can still veto that directory
back to `[]`, so it's the derivation, not the majority check, that has the final say:

- **Shared top-level directories never become auto-prefixes.** A fixed list of well-known
  shared/infra names (`test`, `tests`, `src`, `lib`, `docs`, `doc`, `examples`, `scripts`,
  `tools`, `utils`) is never derived as a stabilizer, no matter how large its share of one
  community's members is. A community that clusters with its own `tests/` (true of any
  god-node community that ships tests alongside its source) is the *normal* case, not evidence
  that `tests/` is that community's private home.
- **A directory shared by too many communities is skipped too.** Even outside that fixed
  list, a winning directory is discarded when the communities sharing it — the candidate's
  own community included — already add up to more than 20% of *all* communities in the
  graph (a single-community claim is never capped, no matter the graph's size) — evidence
  of a cross-cutting pattern (vendored deps, generated code, …), not this community's own
  home. This is deliberately the same ratio `sidegraph-sync`'s own refresh-time cap below
  computes, so a rule this guard lets through can never trip that cap against the graph it
  was derived from.

Both guards protect only *auto-derived* rules (`sidegraph-domains bootstrap`); a manually
authored `--path`/`path_prefixes` (`sidegraph-domains add`, `add_domain`) is never blocked at
write time — that's explicit human intent. But **every** accepted domain's `path_prefixes`,
regardless of who wrote it, is still capped at refresh time (a path candidate that resolves to
a single community is never capped, regardless of ratio — one community can't swallow anything
else): if a later `sidegraph-sync` finds that a path rule now resolves to more than 20% of all
current communities, that path contribution is **rejected and dropped** — but any
`seed_anchors` the domain carries are still resolved and applied, so a domain with both an
overbroad path rule and precise `seed_anchors` has its `communities` **replaced** by the
anchor-resolved set; only a domain whose overbroad path had no `seed_anchors` to rescue it
keeps its previous `communities` mapping, unchanged. Either way, `sidegraph-sync` prints a
dedicated warning block naming the domain and the matched/total counts (see
[`reference/cli.md`](../reference/cli.md#sidegraph-sync)). Narrow the `path_prefixes` or
re-scope the domain when you see that warning.

**A domain already swallowed by a pre-guard version is not auto-healed by this cap.** The
cap only stops a *new* over-broad claim from ever being written — it does not retroactively
shrink a `communities` list that an older, pre-fix `sidegraph-sync` already wrote broad
before this guard existed. If you're upgrading from a version without it, expect
`sidegraph-sync` to keep printing the overbroad warning on *every* pass for such a domain
(its current `path_prefixes` still resolves to more than 20% of communities, so the check
keeps tripping) until a human intervenes: narrow `path_prefixes` so it resolves to less, or
supersede the domain with a corrected `communities` list (see
[recovering from a mass-drop](#recovering-from-a-mass-drop) for the `supersede_domain`
mechanics). Sync itself will never do this for you.

**Review the membership rule at ratify time — by default this is the one human gate.** (Under
`SIDEGRAPH_RATIFY_POLICY=auto-all`, bootstrap ratifies eligible candidates at write time; run
it under the default policy when you want this review.) `sidegraph-ratify`'s
listing (and the MCP `list_proposed`) always shows a domain's `paths:` and `communities:` lines,
even when both are `(none)` — a rule that already looks like it covers more than a single
community's worth of code is worth a second look *before* you accept it, not after sync has
already expanded it.

## 3. Run it for real, then ratify selectively

```bash
uv run --project /ABSOLUTE/PATH/TO/sidegraph sidegraph-domains bootstrap --min-members 15
uv run --project /ABSOLUTE/PATH/TO/sidegraph sidegraph-ratify
```

The second command lists everything pending — decisions *and* domains, in separate sections —
human-readably. Accept by id:

```bash
uv run --project /ABSOLUTE/PATH/TO/sidegraph sidegraph-ratify --accept 01J... 01J... 01J...
```

**This is where the CLI path stops scaling.** On anything but a small, tightly-scoped
`--paths`/`--min-members` run, bootstrap can propose up to its default `--limit` of 100
domains — or well over a hundred if you've explicitly widened `--limit` — and nobody reads a
hundred-line list end to end and hand-picks the ones worth naming — that's exactly the gap
[the skill](#the-fast-path-ask-your-agent) closes: instead of a human reading and curating a
flat list, an agent studies the same candidates and offers either one sensible small-repo set
or 2–3 curated alternatives to pick from. Reach for `sidegraph-ratify --accept <ids>` here only when you're scripting a pipeline or
already know precisely which ids you want.

### Why selective, not `--all`

`sidegraph-ratify --all` accepts every pending decision *and* domain in one shot. That's fine
for decisions (a bad one is superseded later, cheaply). Domains are different: bootstrap is
**idempotent**, and the idempotency check claims a community for **any** non-superseded domain
— proposed, accepted, *or dropped* — not just accepted ones. Concretely: a community bootstrap
already proposed once (in any status other than superseded) is never re-proposed by a later
bootstrap run, even after a `--drop`.

**Mass-dropping a large batch therefore permanently claims those communities** against any
future bootstrap run — the community isn't "returned" for reconsideration just because you
dropped the domain that claimed it. If you `--all`-accept (or `--all`-drop) 80 auto-generated
domains and only 12 of them turn out to be meaningful, the other 68 communities are now stuck:
neither named usefully nor available for a future, better-tuned bootstrap pass to reconsider.

### Recovering from a mass-drop

The `supersede_domain` MCP tool is the direct fix now — no hand-written `Store` script
required. The underlying mechanism is unchanged: superseding a dropped (or badly-proposed)
domain flips it to `superseded`, which removes it from `_claimed_communities`'s scan (that
function explicitly excludes only `superseded` domains); giving the successor **no matching
`path_prefixes`/`seed_anchors`** leaves its `communities` at the model default, `[]`, so the
freed community stops being claimed — a subsequent `sidegraph-domains bootstrap` run will
reconsider it (if it still clears `--min-members`/`--paths`):

```python
supersede_domain(
    old_slug_or_id="<dropped-domain-slug-or-id>",
    new_slug="<placeholder-slug>",
    new_title="Freed for re-bootstrap",
    new_summary="Placeholder successor -- frees the community the old domain claimed.",
    # no path_prefixes / seed_anchors: the successor's `communities` stays [] on purpose
)
```

The successor lands `status=proposed` (the same one-gate rule every domain write follows) —
it never needs to be ratified for the freeing to take effect: `_claimed_communities` excludes
only `superseded` domains, and an empty `communities` list contributes nothing to that set
regardless of the successor's own status. This is the one place where being selective about
`--accept` up front is cheaper than fixing it after the fact — prefer listing candidates and
picking real ones over `--all` on a large auto-proposed batch.

## 4. Watch the TOC come alive

`ratify` recomputes the SessionStart TOC cache immediately whenever at least one domain was
accepted or dropped in that call — no `sidegraph-sync` needed first. Start a new Claude Code
session in the repo (`claude`) and the `SessionStart` context should now show a `## Domains`
section with the titles and summaries you just ratified, instead of the nameless
"top communities by member count" fallback.

## 5. After that: agent proposals, and re-running bootstrap

Once domains exist, the `Stop`-hook nudge also asks the agent, when it noticed a cluster of
code with no name yet during the session, to call `propose_domains` — the same ratification
gate applies (auto-ratified at write time only under `auto-all`). For ad hoc single-domain
adds/renames/drops after the initial naming pass, tell
the agent what you want (e.g. "add a domain for the risk gate", "this domain is wrong") — that's
the [`sidegraph:manage-domains`](../../plugin/sidegraph/skills/manage-domains/SKILL.md) skill.
Or add one manually at any time from the CLI:

```bash
uv run --project /ABSOLUTE/PATH/TO/sidegraph sidegraph-domains add \
  --slug risk-gate --title "Risk gate" \
  --summary "Pre-trade checks that block an order before it reaches the exchange." \
  --path src/risk
```

**Incremental bootstrap under-proposes relative to a fresh run.** Because idempotency claims a
community the moment *any* domain (proposed/accepted/dropped) has used it, re-running
`sidegraph-domains bootstrap` later on the same store will propose *fewer* domains than running
it fresh against the same graph would — every community already claimed, in any status, is
skipped outright. This is intentional (bootstrap never re-proposes or double-writes a community
it's already touched), but it means "run bootstrap again to pick up domains I dropped the first
time" doesn't do what it sounds like — see [recovering from a mass-drop](#recovering-from-a-mass-drop)
above for the actual fix.

## 6. Doc corpora: per-document domains, not per-community

Naming domains is about the graph's structure — *where things live*. If your ADR/SAD corpus
also needs its *content* seeded into the decision store (not just named as an area), that's a
separate step: `sidegraph-import --docs <path>` parses decision-shaped markdown directly (the
parser is deterministic and LLM-free), then anchors each decision against the current graph —
so run `graphify update .` right before importing, same as before bootstrapping domains — see
[`guides/capturing-decisions.md`](capturing-decisions.md#already-have-adrs-import-them) for why
a stale graph matters here. The two are complementary, not redundant: naming gives the corpus a
table of contents; import gives it memory.

On a **document graph** (ADR/SAD markdown, no code), Leiden's clustering tends to put nearly
every file node into one hub cluster — headings and files reference each other and the corpus's
own top-level docs so densely that the graph doesn't split into the kind of module-sized
communities a code repo produces. Bootstrapping domains per-community on a doc corpus therefore
proposes too few, too broad domains: one giant domain covering most of the corpus, which
`drill_down` can't usefully summarize and which won't collect heading-level decisions into
anything more specific than "the whole corpus."

**The working pattern is authoring one domain per document, with an explicit path rule**,
rather than relying on bootstrap's community-based proposal:

```bash
uv run --project /ABSOLUTE/PATH/TO/sidegraph sidegraph-domains add \
  --slug adr-003 --title "ADR-003: Metadata control plane" \
  --summary "Why the control plane owns metadata instead of each service owning its own." \
  --path ADR-003-metadata-control-plane.md
```

`--path` (repeatable) is the same `path_prefixes` stabilizer bootstrap derives automatically on
code graphs — here you set it by hand to exactly one file. Once ratified, `drill_down` on that
domain's slug shows the document's own headings as members (matched via `path_prefixes`, ranked
ahead of anything only reachable via the hub community — see
[retrieval](../concepts/retrieval.md#drill_downdomain_slug--the-axis-1-operation)), instead of a
member sample diluted by the rest of the corpus.

## See also

- [`sidegraph:name-domains`](../../plugin/sidegraph/skills/name-domains/SKILL.md) — the skill
  this guide leads with; the primary onboarding path.
- [`sidegraph:manage-domains`](../../plugin/sidegraph/skills/manage-domains/SKILL.md) — the
  escape hatch: add/rename/drop a domain by hand, after the initial naming pass.
- [Mind model](../concepts/mind-model.md) — the concept this guide is the walkthrough for.
- [Retrieval](../concepts/retrieval.md#sessionstart-toc) — what changes once domains exist.
- [`list_domain_candidates` reference](../reference/mcp-tools.md#list_domain_candidates) — the
  machine half of naming; what the skill calls.
- [`sidegraph-domains` reference](../reference/cli.md#sidegraph-domains) — full flag reference.
- [`sidegraph-ratify` reference](../reference/cli.md#sidegraph-ratify) — the gate both authoring
  paths pass through.
