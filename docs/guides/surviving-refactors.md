# Surviving refactors

Code and docs change; the store's job is to keep decisions anchored — or to say plainly that
it can't, never to guess. This is the sync/rebinding job (`src/sidegraph/sync.py`).

## Rebuild, then sync

Rebuilding the graph is the engine's job — `graphify update .` in the terminal (LLM-free,
incremental). The sync half is Sidegraph's, and you rarely need to run it by hand: both
`get_task_context` and `sidegraph-session-start` call `maybe_sync()` on the read path, so a
stale mapping heals itself on the next tool call or session start, best-effort — but that
lazy path is silent (a sync failure there just degrades to un-synced retrieval; nothing is
ever reported). When you want to **see** the rebind report immediately (e.g. right after a
rebuild), two equivalent paths:

- **MCP-first, from inside a session:** call the `sync_anchors` MCP tool —
  `sync_anchors(force=True)` re-runs even when the graph version hasn't changed; omit
  `force` to respect the version gate on a routine re-check. Returns the same report as
  data instead of stdout text (see
  [`reference/mcp-tools.md`](../reference/mcp-tools.md#sync_anchors)) — this is what the
  [`heal-anchors`](../../plugin/sidegraph/skills/heal-anchors/SKILL.md) skill leads with.
- **CLI fallback**, for scripted/CI use or outside a session:

  ```bash
  graphify update .            # rebuild graphify-out/graph.json (LLM-free, incremental)
  uv run sidegraph-sync        # re-resolve every tracked entity; prints the report
  ```

  `--force` for the same unconditional re-run. Like `sidegraph-ratify`, a bare
  `sidegraph-sync` (no `--db`) resolves the store as `$SIDEGRAPH_DIR` if set, else the
  deprecated `$SIDEGRAPH_DB` dispatch, else the default `.sidegraph` (existing or about to
  be created) — pass `--db`/set `SIDEGRAPH_DIR` explicitly if that's not the store you mean
  (see [`reference/cli.md`](../reference/cli.md)).

Sync is gated on `graph_version` — `<built_at_commit>:<content hash>` when the corpus is a
git repo, or a `content:<hash>` fallback for non-git corpora (see
[`reference/configuration.md`](../reference/configuration.md)): if the graph hasn't changed
since the last sync, both the CLI and the lazy read-path trigger are a no-op — **unless** a
canonical reload (`git pull`, merge, branch switch, even a bare `touch`) has reset the
store's volatile state (domain `communities`, entity `last_seen_*`, binding statuses) since
the last sync; that always reruns the pass once automatically, even though `graph_version`
itself hasn't moved. The content hash is folded into both forms because Graphify can rewrite
`graph.json` without bumping `built_at_commit` (e.g. rebuilding against a dirty working
tree) — a bare commit would miss that the content changed and let sync silently serve stale
anchors.

## The rebind ladder

Every tracked concrete `Entity` (one with a `descriptor`) is re-resolved through a
deterministic ladder, one rung at a time, and the first rung that fires wins:

| Status | What happened | What Sidegraph does |
|---|---|---|
| `unchanged` | Exact `name`+`file_path` match, same node id as last sync | Leaf binding stays `live`; still checks for community re-pointing (below). |
| `rebound` | Exact `name`+`file_path` match, but the node id changed | Leaf binding stays/returns to `live`, mapping refreshed to the new node id. |
| `moved` | No exact match, but a *unique* name-only match exists **in a file of the same suffix**, **and the entity's old `file_path` is confirmed gone from disk** | The entity's `descriptor.file_path` is updated to follow it; leaf binding heals to `live`. A same-name hit in a file of a *different* suffix (e.g. a vanished code symbol colliding with an unrelated doc heading) is treated as a collision, not a move, and falls through to orphaned instead — as does any hit whose old path is still on disk (below). |
| `ambiguous` | More than one node now matches | Leaf binding flips to `degraded` (not deleted). If every candidate shares one community, that community is still used for re-pointing. |
| `orphaned` | No match at all, exact or loose | Leaf binding flips to `orphaned`. If the entity's file still exists and its nodes agree on a single community, that community is used for re-pointing (see below) — Sidegraph is not guessing which node the entity *became*, only where its code still lives. |

None of this ever deletes a binding — only its `status` changes (`live` / `degraded` /
`orphaned`), which is why the ladder can run repeatedly without losing history.

### Why `moved` checks the disk

A unique same-name, same-suffix hit is not on its own evidence of a move: the entity's own
file may still be sitting right there, with only the *symbol* renamed inside it, while an
unrelated file elsewhere happens to define something with the old name. Adopting that hit
would silently re-anchor the decision onto code it was never about. So the rung requires a
second, independent signal — the old `descriptor.file_path` no longer exists in the checkout —
before it follows a name.

"Confirmed gone" is not the same as "unknown". Sidegraph locates the checkout from the
**graph's** own location (`git rev-parse --show-toplevel` next to `graph.json`, not next to
the store — a store directory is routinely copied elsewhere for inspection). When that lookup
can't answer — a corpus with no `.git` at all, or git unavailable — the rung **fails closed**
for the whole pass: nothing is adopted, and every candidate that would have moved lands
`orphaned` instead. A real move that degrades to `orphaned` is visible in `sidegraph-doctor`
and repairable with [`sidegraph:heal-anchors`](../../plugin/sidegraph/skills/heal-anchors/SKILL.md);
a wrong adoption is silent, and silence is the worse failure.

## Community re-pointing

Leiden community ids are **snapshot labels, not identities** — a rebuild can renumber every
community (observed on a real repo: `149 → 19 → 256` across three successive rebuilds).
So on every sync, for every entity whose *observed* community differs from its last-seen
baseline, Sidegraph:

1. Creates (or reuses) a Tier-1 abstract entity for the new `community:<id>`, and adds a
   `live` binding to it for every decision leaf-bound to that entity.
2. Flips the binding to the *old* `community:<id>` to `orphaned` (never deletes it).

Both of those are **index-only**: the `community:<id>` entity and the Tier-1 binding pointing
at it are derived state, not committed to a canonical file — old or new. Repointing a hundred
decisions across a Leiden renumbering never produces a git diff; it's the exact same
sync-clean guarantee that covers `Entity.last_seen_*` and `AnchorBinding.status` (see [store
format](../reference/store-format.md#community-bindings-are-derived-not-committed)). A
domain-covered Tier-1 binding is unaffected by any of this — it points at the domain's own
committed `domain:<slug>` entity instead, which sync never touches.

This runs regardless of which ladder rung the entity landed on — even an `orphaned` entity
gets re-pointed to its surviving file's community when one is unambiguous, and an
`ambiguous` entity gets re-pointed if its candidates share a community. A `sync` run with
re-pointed bindings prints `re-pointed N community binding(s)` — the count is per-anchor
bindings re-pointed, not distinct decisions (a decision with more than one leaf bound to the
same entity would count more than once).

## Fresh-clone recovery

A fresh `git clone` of a repo with a committed `.sidegraph/` has no community state at all —
`community:*` bindings and entities were never written to a canonical file to begin with (see
above), and `index.db` itself is gitignored, so it isn't cloned either; there is no
`last_seen_community` baseline anywhere on disk. This is by design, not a gap to work around:
the very first `graphify update . && sidegraph-sync` after the clone self-heals it. With no
baseline, every *tracked* entity's community comparison starts from `None`, so the ordinary
rebind ladder above re-resolves each Tier-2 leaf against the live graph (typically landing on
`rebound`, since the cold reload also resets `last_seen_node_id`) and re-derives that leaf's
Tier-1 `community:<id>` binding from scratch, purely in the index — no new code path, just the
same repointing machinery running with an empty baseline instead of a stale one. Nothing about
a fresh clone requires the old store to have committed a "community fallback" anywhere; the
committed anchor set a clone actually inherits (Tier-2 leaves, Tier-0, `domain:*` Tier-1) is
exactly what regenerating community state needs to work from.

**Residual: leafless Tier-1 bindings aren't covered.** Regeneration is driven by re-resolving
*tracked* (Tier-2) entities — an ambiguous-match anchor whose candidates all share one
community never gets a Tier-2 leaf in the first place (`resolve_and_bind` only creates a leaf
entity on `resolved`/`unresolved`, never on `ambiguous` — see
[anchoring](../concepts/anchoring.md#graceful-degradation-ladder-resolved---ambiguous---orphaned)),
so its bare Tier-1 community binding has nothing for sync to re-derive from and does not come
back after a clone. A decision anchored *only* that way effectively loses its anchor across a
fresh clone until a new capture touches the same reference again. This is a spec-inherited
residual, not something this wave attempted to close.

## Reading and healing `stale decisions` warnings

`sidegraph-sync` prints a `possibly stale decisions (all anchors gone — verify):` block for
any non-superseded, non-rejected decision whose Tier-2 (leaf) bindings all exist and are all
`orphaned`. Note the precision here: a decision with **no** leaf bindings at all (e.g. one
anchored only at Tier-0/Tier-1) is never flagged this way — surviving Tier-1/Tier-0 bindings
keep a decision retrievable via community/initiative fallback, but they don't vouch for the
underlying claim; the leaf-only check is what flags "the concrete thing this refers to is
gone, go look."

Two ways to heal a flagged decision (the
[`sidegraph:heal-anchors`](../../plugin/sidegraph/skills/heal-anchors/SKILL.md) skill walks an
agent through this triage, finding by finding):

- **Restore the name** (undo the rename, or re-add the removed symbol/heading) and re-run
  `graphify update . && sidegraph-sync` — the orphaned anchor heals back to `rebound` (or
  `unchanged`) once an exact match exists again.
- **Supersede the decision** (`supersede_decision`, or `supersedes` in a `propose_decisions`
  draft) if the code it described is genuinely gone for good — this appends a fresh record
  and closes the old one, keeping the "tried before, abandoned because…" history intact
  rather than leaving a dangling warning forever.

## Facts anchor the same way

A `Fact`'s bindings are ordinary `AnchorBinding` rows too — same table, keyed on the same
`record_id` column a decision's bindings use (see
[`reference/store-format.md`](../reference/store-format.md)). The rebind ladder and community
re-pointing above don't know or care whether `record_id` points at a `Decision` or a `Fact`: a
fact anchored to a renamed function degrades to `ambiguous`/`orphaned` and heals back to `live`
on the exact same rules — nothing fact-specific to run. The one thing to get right is
`supersede_fact`: omitting `anchors` inherits the predecessor's bindings **verbatim** — same
`entity_id`/`tier`/`weight`/`relation`/`status`, including an already-`orphaned` one, carried
over as-is (identical to `supersede_decision`'s own default). Don't omit `anchors` when the
point of the supersession is to re-anchor a fact whose predecessor had degraded — pass fresh
ones explicitly, or the successor inherits the same orphaned binding and starts out already
needing another sync pass.

## The never-guess promise

Nothing in the rebind ladder silently re-anchors a decision to a *different* entity that
merely looks plausible. `moved` only fires on a **unique** name-only hit within the same file
type **whose old path is confirmed gone from disk** (see [why `moved` checks the
disk](#why-moved-checks-the-disk)); anything with more than one candidate is `ambiguous`, not
resolved, and anything unverifiable stays `orphaned`. An `ambiguous` or
`orphaned` entity's `last_seen_node_id` is **never** touched by the community re-pointing
path — only community/Tier-1 bindings move, never the leaf's node mapping. If Sidegraph can't
say for certain "this is the same thing," it says so (`ambiguous`/`orphaned`) instead of
guessing — a false rebind is worse than an honestly degraded one, because it would surface a
decision against the wrong code with no way to tell.
