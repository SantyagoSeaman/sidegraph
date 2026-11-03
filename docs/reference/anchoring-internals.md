# Anchor synchronization contract

> **Start here:** Use the section matching the command or payload you are checking. For everyday work, begin with the [documentation paths](../README.md). [Glossary](../concepts/glossary.md).

## The rebind ladder

Every tracked concrete `Entity` (one with a `descriptor`) is re-resolved through a
deterministic ladder, one rung at a time, and the first rung that fires wins:

| Status | What happened | What Sidegraph does |
|---|---|---|
| `unchanged` | Exact `name`+`file_path` match, same node id as last sync | Leaf binding stays `live`; still checks for community re-pointing (below). |
| `rebound` | Exact `name`+`file_path` match, but the node id changed | Leaf binding stays/returns to `live`, mapping refreshed to the new node id. |
| `moved` | No exact match, but a *unique* name-only match exists **with the same name** (decoration stripped, case kept) **in a file of the same suffix**, **the entity's old `file_path` is confirmed gone from disk**, that same move is **independently confirmed by committed git history** (old path absent, new path tracked — both at `HEAD`), AND git shows the new path **arriving with the old path's removal** | The entity's `descriptor.file_path` is updated to follow it; leaf binding heals to `live`. A same-name hit in a file of a *different* suffix (e.g. a vanished code symbol colliding with an unrelated doc heading) is treated as a collision, not a move, and falls through to orphaned instead — as does any hit whose old path is still on disk, a hit that is only a case-insensitive match (a deleted type `Priority` against an old member `.priority`), and a new path that was already there before the old one went (below). |
| `moved_uncommitted` | Same disk-level evidence as `moved` (unique same-suffix hit of the same name, old path gone from disk), but git's committed history at `HEAD` does not yet confirm it — an uncommitted delete, rename, or stash on this one working tree — and the new path is not already in `HEAD` (one that is was there before the old path went, so no commit can make it a move: that is `orphaned`) | Nothing is touched — the binding, descriptor, and node mapping are all left exactly as they were. Commit the move (or set `SIDEGRAPH_TRUST_DIRTY_TREE=on`, see below). Sidegraph remembers the entity, and the first sync after `HEAD` moves re-verifies it even if the graph was not rebuilt. If the old path is back on disk (the rename was reverted or stashed) the leaf is left untouched, and the entity stays watched until a later `HEAD` change or a rebuild settles it. A new file that takes the old path before the move is committed and synced hides the move: it surfaces as `orphaned` after the next graph rebuild. |
| `ambiguous` | More than one node now matches (identical path-less external stubs count as one match) | Leaf binding flips to `degraded` (not deleted). If every candidate shares one community, that community is still used for re-pointing. |
| `orphaned` | No match, or only a name match that cannot be followed as a move (including identical path-less external stubs for an entity whose file disappeared) | Leaf binding flips to `orphaned`. If the entity's file still exists and its nodes agree on a single community, that community is used for re-pointing (see below) — Sidegraph is not guessing which node the entity *became*, only where its code still lives. |

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

The disk check alone is not enough, though: it reflects whatever this one working tree
looks like *right now*, and an uncommitted `rm`/`git mv`/`git stash` makes the old path
disappear from disk just as convincingly as a real, shared move. Since `moved` rewrites the
entity's canonical, **repo-committed** `descriptor` — the same file every other clone reads —
trusting a purely local, unshared change there would let one person's dirty tree silently
corrupt the team's memory. So the rung requires a *third* signal before it adopts: the same
move confirmed by **committed** history, via `git cat-file -e HEAD:<path>` — the old path
absent from `HEAD`'s tree, and the new path present in it. When the disk-level evidence looks
like a move but git's `HEAD` doesn't yet back it up, the rung reports `moved_uncommitted` and
leaves everything untouched instead of guessing; committing the move (or, on a fresh
repository with no commits yet, making one) heals it the normal way, no different from any
other rung. The entity is remembered along with `HEAD`, so the first sync after `HEAD` moves
re-verifies it — lazily or explicitly, and even when the graph was not rebuilt. If the rename
was reverted or stashed instead, the old path is back on disk and the leaf is left as it was.
For the rare case of someone who has verified their own working tree and wants the old,
disk-only behavior back, `SIDEGRAPH_TRUST_DIRTY_TREE=on` is a documented, off-by-default
escape hatch scoped to exactly this one check.

Committed history that shows the old path gone and the new path present is still not a move:
a file that was there all along says the same. A type deleted along with its file, whose name
matches (ignoring case) an old member in some unrelated file, looks exactly like that. So the
rung asks two more questions before it adopts. First, is the hit the same symbol? The name
must be equal once the decoration both sides carry (`()`, a leading `.`) is stripped, with
**case kept**: `Priority` is not `.priority`. Second, did the new path arrive with the old
one's removal? Sidegraph finds the commit on the main line of history (`git log
--first-parent -m`, so a branch merged with `--no-ff` counts as one change) that deleted the
old path, and requires its first parent to lack the new path and the commit itself to hold
it. Any other answer refuses: a git call that fails, a history with no deletion in it (the
old path was never committed, say a gitignored directory), a shallow clone whose boundary
hides the answer, or a new path added long after the deletion. Moves out of a path git never
held are therefore reported `orphaned`, and `heal-anchors` re-anchors them. With
`SIDEGRAPH_TRUST_DIRTY_TREE=on`, where the old path is still in `HEAD`, the new path must be
absent from `HEAD` instead: new in the working tree. A refused move falls through to
`orphaned`. `moved_uncommitted` is kept for a move that can still come true: the old path
is gone from the working tree and the new path is not in `HEAD` yet.

Git's history alone does not show some real moves as one change, so these are refused too.
Each ends `orphaned`, and `heal-anchors` re-anchors it:

- **The add and the delete in two commits on the first-parent line.** The new file lands in
  one commit and the old one goes in the next, whether both are on the main branch, or a branch
  is merged fast-forward or rebase-merged (a `--no-ff` merge is judged as one change and is
  adopted).
- **A second move before any sync ran.** The file went `A` to `B` to `C` while the descriptor
  still names `A`: the commit that deleted `A` did not add `C`.
- **An old path that was re-added and then deleted again.** The latest deletion is the one
  that is judged, and it need not be the move.

If an earlier release adopted a false move, it rewrote an entity's `descriptor.file_path`.
List those rewrites with
`git log -p --diff-filter=M -- .sidegraph/entities/ .sidegraph/shards/ | grep -E '^(commit |[-+].*"file_path")'`
and check each one against the rule above. The second path covers a format-2 store, where
maintenance has packed entities into one-line records inside shard files.

## Community re-pointing

Leiden community ids are **snapshot labels, not identities** — a rebuild can renumber every
community (observed on a real repo: `149 → 19 → 256` across three successive rebuilds).
So on every sync, for every entity with an *observed* community that differs from its last-seen
baseline, Sidegraph:

1. Creates (or reuses) a Tier-1 abstract entity for the new `community:<id>`, and adds a
   `live` binding to it for every decision leaf-bound to that entity.
2. Reconciles the old `community:<id>` per record, once the whole pass has run: its
   binding flips to `orphaned` (never deleted) only when no other live or degraded leaf of that same
   record still sits in that community. While a sibling leaf does, the old row stays `live`.

An unknown community baseline on a live or degraded sibling makes reconcile leave the old row
live. A live path-less leaf successfully mapped on the current graph with no community is known
to hold none, so it does not block reconciliation. A degraded leaf or a mapping left from an
earlier graph provides no such evidence. When stubs that once shared a community split, the
resolved leaf vacates that group; its row is retired unless a sibling still holds it or an
unknown baseline makes reconcile abstain.

Both of those are **index-only**: the `community:<id>` entity and the Tier-1 binding pointing
at it are derived state, not committed to a canonical file — old or new. Repointing a hundred
decisions across a Leiden renumbering never produces a git diff; it's the exact same
sync-clean guarantee that covers `Entity.last_seen_*` and `AnchorBinding.status` (see [store
format](store-format.md#community-bindings-are-derived-not-committed)). A
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
the very first graph rebuild (`graphify update .` with the environment variable `GRAPHIFY_NO_AUTO_REFRESH=1` set) followed by `sidegraph-sync` after the clone self-heals it. With no
baseline, every *tracked* entity's community comparison starts from `None`, so the ordinary
rebind ladder above re-resolves each Tier-2 leaf against the live graph (typically landing on
`rebound`, since the cold reload also resets `last_seen_node_id`) and re-derives that leaf's
Tier-1 `community:<id>` binding from scratch, purely in the index — no new code path, just the
same repointing machinery running with an empty baseline instead of a stale one. Nothing about
a fresh clone requires the old store to have committed a "community fallback" anywhere; the
committed anchor set a clone actually inherits (Tier-2 leaves, Tier-0, `domain:*` Tier-1) is
exactly what regenerating community state needs to work from.

For portable applicability, resolve ambiguous names before capture and pair each precise
target with its containing file. Inspect `anchors_skipped` and `anchors_orphaned`, then
verify retrieval from that file after sync. Durable leaves give sync a target from which to
regenerate community context.

## Facts anchor the same way

A `Fact`'s bindings are ordinary `AnchorBinding` rows too — same table, keyed on the same
`record_id` column a decision's bindings use (see
[`reference/store-format.md`](store-format.md)). The rebind ladder and community
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
merely looks plausible. `moved` only fires on a **unique** name-only hit **that is the same
name, case kept,** within the same file type **whose old path is confirmed gone from disk AND
confirmed by committed git history, with the new path arriving in the change that removed
the old one** (see [why `moved` checks the disk](#why-moved-checks-the-disk)); anything with more than one
candidate is `ambiguous`, not resolved; anything unverifiable stays `orphaned`; and disk-level
evidence git's `HEAD` doesn't yet back up reports `moved_uncommitted` rather than adopting on
a dirty tree. An `ambiguous` or
`orphaned` entity's `last_seen_node_id` is **never** touched by the community re-pointing
path — only community/Tier-1 bindings move, never the leaf's node mapping. If Sidegraph can't
say for certain "this is the same thing," it says so (`ambiguous`/`orphaned`) instead of
guessing — a false rebind is worse than an honestly degraded one, because it would surface a
decision against the wrong code with no way to tell.

## Descriptors: name + file

An anchor reference is a `Descriptor`: a `name` (symbol name or, for docs, a heading text or
the file name itself) and an optional `file_path`. This is deliberately minimal — current
Graphify releases (verified on 0.9.6 and 0.9.90) emit no signatures, qualified names, or
enclosing-module info, so `name` + `file_path` is all identity has to work with.
`canonicalize()` normalizes both sides of a match
(lowercase, strip a leading `.`, drop call-decoration like `()`), so `.foo()` in a decision's
anchor matches the graph node named `foo`.

**`Type.member` names.** Graphify labels a method `.foo()` and links it to its type with a
`method` edge (an enum case with `case_of`), so a name written `Type.foo` has no exact match.
When none is found and the descriptor has a `file_path`, `resolve()` falls back to the member.
It is for code identifiers only: the owner (its last segment, so `Outer.Inner.foo` reads as
`Inner`) and the member must both be identifiers, and the candidate must be a `code` node, so
prose with a dot in it, a numbered heading or `verify-release.sh` never reads as a member.
The name is split at its last `.`, `::` or `#`, the node in that file whose label is the member
is found (case kept), a candidate the graph ties to a different type is rejected, and, of
several, those tied to the named type are kept. Anything that does not narrow to exactly one
node is `unresolved`, the orphaned, healable leaf a bare miss has always been, never
`ambiguous`. So is a name with a case-only twin in the file (a struct `Message` beside a
method `.message()`): the store dedups an anchor by its lowercased name, so the two spellings
are one entity, and binding either node would put the other's record on it. Without a file the
member fallback never runs. A path-qualified name (`src/pkg/mod.py`) has its own rung after
it: with the anchor's own `file_path` it resolves to that file's node, and it is stored under
the basename. On newer Graphify, where a file with a shared basename is labelled by a path
suffix (`pkg/mod.py`), that engine label also resolves without a file.

## Lazy `Entity` creation

An `Entity` is created the first time a decision references it — via
`store.find_entity(name, file_path)` (dedup lookup) falling back to a fresh `Entity` — never
ahead of time. This keeps the store proportional to what's actually been decided about,
not a mirror of the whole graph. Abstract entities (communities, initiatives) are
get-or-created the same way, keyed by `canonical_name` (e.g. `community:19`,
`initiative:feature-aaa`) and reused across decisions. A `community:*` abstract entity never
gets a canonical file, though: Leiden renumbers communities on every rebuild, so community
identity is index-only, derived state — a snapshot label, not a durable one (see [store
format](store-format.md#community-bindings-are-derived-not-committed)).
`initiative:*`/`domain:*`/`tag:*` abstract entities are unaffected and get a committed file
the same way any other entity does.

## Multi-anchor at capture

`resolve_and_bind(record_id, ref, reader, store, relation=None)` resolves one
`Descriptor` against the current graph and can create up to two bindings for it in one call:

- **Tier-2 (leaf)** — the concrete entity itself, when the resolve either succeeded or failed
  outright (not when ambiguous).
- **Tier-1 (group)** — the entity's Leiden community, whenever one is known — see
  [domain-aware Tier-1](#domain-aware-tier-1) below for what actually gets bound once a domain
  has claimed that community.

`relation` (optional; `creates` \| `modifies` \| `affects` \| `deprecates` \| `considered`,
default `"affects"`) overrides the default on the Tier-2 and Tier-1 bindings created for *this*
anchor. See [data model](../concepts/data-model.md#anchorbinding--tiered-link).

The **Tier-0 initiative** binding is not made per anchor. When the decision names an owning
initiative (or the propose path derives one from the current branch of the project's
repository: the directory that holds the store, or its symlink), the write path binds it once,
`live`, whatever the reader and the anchors: the name is redacted first, and a blank or
all-secret name binds nothing.

A decision typically ends up multi-anchored: a leaf plus its community (or domain), optionally
plus an initiative — so a single rebuild that only shifts the leaf still leaves the group anchor
live.

## Domain-aware Tier-1

Once a [`Domain`](../concepts/mind-model.md) has been ratified and covers an anchor's current
community, the Tier-1 binding goes to the domain's paired `domain:<slug>` entity **instead of**
the bare `community:<id>` entity — `store.find_domain_by_community(result.community)` is
checked first, and only falls through to the legacy `community:<id>` entity when no accepted
domain claims that community. A domain-covered Tier-1 binding is always `status="live"`: once a
domain has claimed the community, Tier-1 confidence comes from the domain's curation, not from
whether this particular leaf resolved cleanly (an ambiguous-but-shared-community leaf still
gets a `live` domain binding, where the legacy path would have marked it `degraded`).

A store without any ratified domains behaves *exactly* as before this existed — the
domain check is a no-op fallthrough until at least one domain is accepted and covers the
community in question. New memory automatically lands on durable, named abstractions once
domains are named; decisions anchored before that point keep their `community:<id>` binding
(not migrated), and retrieval still finds them via the [community
fallback](../concepts/retrieval.md#bucket-c-domains-and-communities).

The two Tier-1 outcomes differ in durability, not just naming: a domain-covered binding points
at the domain's own `domain:<slug>` entity, which is a committed, human-named identity; the
bare `community:<id>` fallback points at a `community:*` entity that is index-only/derived
(snapshot labels, not identities — see [store
format](store-format.md#community-bindings-are-derived-not-committed)) and is
silently re-pointed, never written to git, whenever Leiden renumbers.

## What happens on rename or move

The sync job (triggered lazily on read — see [retrieval](../concepts/retrieval.md#lazy-sync-on-read))
re-resolves every concrete entity through a deterministic ladder (`rebind_entity` in
`sync.py`), gated on a graph-version check so the full pass is skipped when nothing changed (entities
remembered under `pending_uncommitted_moves` are still re-verified once `HEAD` has moved):

1. **Exact** — `resolve(name, file_path)` still resolves uniquely -> **rebound** (or
   **unchanged** if the node id didn't move); leaf bindings heal to `live`; the entity's
   community baseline is re-pointed if Leiden renumbered it (index-only — see [Community
   re-pointing](../guides/surviving-refactors.md#community-re-pointing)).
2. **Ambiguous** — leaf bindings flip to `degraded`; the node mapping is left untouched
   (never guess); community is re-pointed if a shared one is still resolvable.
3. **Moved** — exact fails but a name-only retry resolves uniquely, *and* the hit is the
   same name (decoration such as `()` or a leading `.` stripped from both sides, **case
   kept**), *and* the new file has the same suffix as the old `descriptor.file_path` (e.g.
   `.py` -> `.py`, `.md` -> `.md`), *and* the old `file_path` is confirmed gone from the
   checkout, *and* git's history shows the new file arriving in the same change that
   removed the old one -> `descriptor.file_path` is updated to follow the file, leaf
   bindings heal to `live`.
   **Suffix guard:** a unique name-only hit whose file suffix *differs* from the old one
   (e.g. a vanished code symbol whose name happens to collide with a doc heading) is a
   **collision, not a move** — it is never adopted, and falls through to orphaned instead.
   Cross-type unique hits are not trusted. **Disk guard:** so is a hit whose old file is
   still there — a symbol renamed *inside* a surviving file is not a move, however unique
   the name-only hit elsewhere looks, and "can't verify" counts as "still there" (see [why
   `moved` checks the disk](../guides/surviving-refactors.md#why-moved-checks-the-disk)).
   **Name guard:** the name-only lookup ignores case, so a deleted type `Priority` can find
   an old member `.priority` in an unrelated file. That is a different symbol, not a move,
   and is never adopted. **Arrival guard:** a new path that already existed before the old
   one was removed is a collision, not a move. So is an old path git cannot account for
   (never committed, or beyond the history a shallow clone holds): any git answer that is
   not a clear yes refuses. A refused move falls through to orphaned, which
   `sidegraph:heal-anchors` repairs.
   This rung resolves by name only, so a `Type.member` anchor cannot follow its file being
   moved; the bare member name can.
4. **Orphaned** — nothing resolves; leaf bindings flip to `orphaned`. Sync still tries to
   derive the entity's current community from its surviving file's nodes (useful when a
   symbol renamed but its file didn't move); that derivation only happens when the file's
   nodes agree on a single community — otherwise it abstains rather than guess.

All of these are status transitions on existing `AnchorBinding` rows or updates to `Entity`
mapping fields — nothing is ever deleted. A decision whose leaf anchors are *all* orphaned is
flagged "possibly stale" in the sync report, but stays retrievable through any surviving
Tier-1/Tier-0 binding.

## How domains relate to engine communities

A `Domain.communities` field is a list of Leiden community ids — a **snapshot mapping**, not
an identity. Leiden renumbers community ids on every rebuild (the dogfood corpus saw
`149 → 19 → 256` across three rebuilds), so nothing durable can be *keyed* on a community id; a
`Domain`'s durable identity is its `slug` and its paired `domain:<slug>` entity. What used to
require re-pointing on **every decision's** Tier-1 binding individually now only needs repair
**once per domain**, at sync time: `sync._recompute_domain_communities` reruns the domain's own
membership rule against the current graph and writes the result back into `communities`.

That refresh uses **REPLACE semantics, not a union**, and it is worth being explicit about why,
so nobody "fixes" it into a union later: when a domain's `path_prefixes` match at least one
anchorable node in the *current* graph, the communities those matches sit in are direct,
current evidence — they fully replace whatever `communities` recorded before, rather than being
added to it. A blind union would let a domain that was once associated with some community keep
silently absorbing that community's decisions forever, even after the graph reorganizes and a
*different, unrelated* domain gets assigned that same now-reused id — Leiden ids are recycled
across rebuilds, so a union has no way to tell "still the same area" from "coincidentally the
same number now." Only when `path_prefixes` matches nothing today does the refresh fall back to
"survivors" — the subset of the previously recorded ids that still exist somewhere in the
current graph — and even then it never adds a *new*, unevidenced id. A domain that recomputes
to no communities *and* whose `path_prefixes` matched nothing is flagged **empty** in the sync
report and routed to a human (re-scope or supersede) rather than silently degrading.

The "never guess" discipline extends to *scope*, not just existence: a `path_prefixes` rule
that would newly claim more than 20% of all current communities is never written either (a
candidate that resolves to a single community is never capped, regardless of ratio — one
community can't swallow anything else) — the overbroad path contribution is dropped and the
domain is flagged **overbroad** in the sync report. If the domain also carries `seed_anchors`,
those are precise per-entity authoring and are always applied, so the domain keeps its
anchor-resolved membership; a domain with only the overbroad path keeps its previous
`communities` mapping unchanged. This applies to every
accepted domain's `path_prefixes` regardless of how it was authored (bootstrap-derived or
hand-written), and bootstrap's own proposal step applies a matching guard before a rule is even
proposed — see the [naming guide's guardrails](../guides/naming-your-domains.md#guardrails-against-an-over-broad-path-rule)
for the full mechanics.

Once a domain is accepted, new anchoring at capture time (`resolve_and_bind`) prefers it over
the bare `community:<id>` entity: if an anchor's current community is covered by an accepted
domain, the Tier-1 binding goes to the domain's `domain:<slug>` entity instead. Decisions bound
to the community before the domain existed are not migrated, but retrieval still surfaces them
under the community fallback — see
[retrieval](../concepts/retrieval.md#bucket-c-domains-and-communities).

**Membership is computed once per sync and kept per node.** Each pass writes every accepted domain's
members into the local index, and everything that asks "is this in the domain" reads it: drill-down
members, the decisions a domain gathers, the Tier-1 domain a new decision binds to, retrieval's
related bucket. A domain's members are the nodes of its communities plus the nodes under its path
prefixes. With a [domain snapshot](store-format.md#the-domain-snapshot), a covered
domain's members come from the snapshot, and a change to its rules changes nothing for the files the snapshot lists until a refresh. A file the snapshot does not list (a new one) is still matched against the domain's path prefixes. Before all of
this, a community that holds code and documents is split in two: Graphify 0.9.62 and later link a
document to the code it mentions, which merges them.

## Sync triggers

Sync is gated on `graph_version` — `<built_at_commit>:<content hash>` when the corpus is a
git repo, or a `content:<hash>` fallback for non-git corpora (see
[`reference/configuration.md`](../reference/configuration.md)): if the graph hasn't changed
since the last sync, both the CLI and the lazy read-path trigger skip the full pass —
except that an entity remembered under `pending_uncommitted_moves` (a move the last pass
left as `moved_uncommitted`) is re-verified once `HEAD` has moved, in a narrow pass over just
those entities, and **unless** a canonical reload (`git pull`, merge, branch switch, even a bare `touch`) has reset the
store's volatile state (domain `communities`, entity `last_seen_*`, binding statuses) since
the last sync; that always reruns the pass once automatically, even though `graph_version`
itself hasn't moved. The content hash is folded into both forms because Graphify can rewrite
`graph.json` without bumping `built_at_commit` (e.g. rebuilding against a dirty working
tree) — a bare commit would miss that the content changed and let sync silently serve stale
anchors.
