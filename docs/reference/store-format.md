# Store format

The decision store is a public contract, not an implementation detail — it is meant to be
**committed to the repo it documents**, merged by ordinary `git merge`, and read by tooling
other than Sidegraph itself over time. This page specifies the on-disk layout and the rules
that govern writing to it. Implemented in
[`src/sidegraph/store.py`](../../src/sidegraph/store.py); the record types it persists are in
[data model](../concepts/data-model.md).

## Layout: file-per-record JSON, plus a derived local index

```
.sidegraph/
├── format                  # committed marker: "sidegraph-store 0.6.0"
├── stamping_live_since      # committed, ONLY on a genuinely new store: an aware UTC ISO-8601 timestamp
├── .gitignore               # committed, written by the store: ignores index.db* and *.tmp
├── decisions/<ulid>.json    # one file per Decision — full row, status/valid_to included
├── facts/<ulid>.json        # one file per Fact — full row, status/valid_to included (non-derivable knowledge)
├── domains/<ulid>.json      # one file per Domain — full row minus the volatile `communities` field
├── entities/<ulid>.json     # one file per Entity — identity only (no last_seen_* fields)
├── bindings/<record_ulid>.json  # one file per record (decision or fact): its durable anchor set (community bindings excluded), no status
├── initiatives/<ulid>.json  # one file per Initiative — full row (nothing volatile on this model)
├── archive/                 # optional — only exists once `sidegraph-compact` has run
│   └── <date>-<seq>-<hash12>.jsonl  # immutable segments of terminal (closed) records
└── index.db                 # DERIVED, gitignored: fast queries + every volatile field
```

Every directory except `archive/` and `index.db` is created empty the first time a `Store`
opens a fresh path — a brand-new `.sidegraph/` has all six subdirectories present even before
the first decision is written.

**One file per record**, not one JSONL event log, is the whole point of the wave: different
records land in different files, so two branches that ratify different decisions merge with
zero conflict, a PR diff shows "added `decisions/01J....json` — one small file" instead of
"binary file changed," and the rare case where two branches genuinely disagree about the same
record (see [Merge semantics](#merge-semantics-git-resolves-it-not-the-store) below) surfaces as
an ordinary, human-readable git conflict in one small JSON file — not a corrupted database.

## Committed vs. derived

| Committed (git-visible, canonical) | Derived (`index.db`, gitignored, rebuilt from the committed files) |
|---|---|
| `Decision` — full row, incl. `status`/`valid_to` | `Entity.last_seen_node_id` / `last_seen_graph_version` / `last_seen_community` |
| `Fact` — full row, incl. `status`/`valid_to` (non-derivable knowledge; falsification is supersession, same as a `Decision`) | — |
| `Domain` — everything except `communities` | `AnchorBinding.status` (`live`/`degraded`/`orphaned`) |
| `Entity` — `entity_id`/`canonical_name`/`kind`/`descriptor`, for every entity EXCEPT a `community:*` abstract entity (index-only — see below) | `Domain.communities` |
| `AnchorBinding` set per decision — `entity_id`/`tier`/`relation`/`weight`, no `status`, EXCLUDING any binding whose entity is a `community:*` abstract entity | `toc_cache` (the `SessionStart` table-of-contents render) |
| `Initiative` — full row | the per-session capture ledger (`capture_sessions`, dedup markers) |
| `archive/*.jsonl` segments (once compaction has run) | `last_synced_graph_version`, `schema_version`, the canonical-digest freshness stamp |
| — | retrieval telemetry (`retrieval_shows`, `retrieval_seeds` — records that reached a render, areas that were asked about; see [Retrieval telemetry](#retrieval-telemetry) below) |
| — | `community:*` abstract `Entity` rows, and any Tier-1 `AnchorBinding` pointing at one — see [Community bindings are derived](#community-bindings-are-derived-not-committed) below |
| — | `canonical_stat` (`subdir`, `stem`, `size`, `mtime_ns` per canonical file this index actually loaded — see [Digest integrity](#digest-integrity) below) |

The rule of thumb: anything a human decided is committed; anything the engine can recompute
from `graph.json` is derived. The dividing line for a Tier-1 binding is the *entity*, not the
tier number: a `domain:<slug>` or `tag:<slug>` Tier-1/Tier-0 anchor is a durable, human-named
identity and stays canonical; a bare `community:<id>` Tier-1 anchor is an engine-recomputed
snapshot label and is derived, exactly like `Entity.last_seen_community` itself. `index.db` is
symmetric with the engine's own `graph.json` — both are disposable, rebuildable-from-source
local artifacts. Nothing in the derived column is ever written to a committed file — a
`sidegraph-sync` pass that only refreshes volatile state (the ordinary case: unchanged/rebound
leaves, status flips, community re-pointing) touches `index.db` alone, never a file under
`decisions/`, `domains/`, `entities/`, or `bindings/`, not even when it repoints a decision's
Tier-1 binding from an old `community:<id>` to a new one after a Leiden renumbering. The one
sanctioned exception is the rebind ladder's `moved` rung: when a leaf's `descriptor.file_path`
genuinely changes (the symbol's name survived but its file moved), that's a durable-identity
update, not derived state, and legitimately rewrites that leaf's own `entities/<id>.json` — the
same way any other entity-identity change would (see [the sync-clean
invariant](#the-sync-clean-invariant) below for the exact scope).

## Community bindings are derived, not committed

Tier-1 `community:*` bindings — and the abstract `community:*` `Entity` rows they point at —
are fully derived: index-only, never written to a canonical file, at capture time and at sync
time alike. The scope condition is the **entity**, not the tier number: a binding is derived
iff its entity's `canonical_name` starts with `community:` (`store._is_derived_entity`);
`domain:*`, `tag:*`, and initiative abstract entities are unaffected and stay canonical no
matter which tier they're bound at.

Two write-time guards enforce this by construction, not by caller discipline:

- `Store.upsert_entity` never writes an `entities/<id>.json` file for a `community:*` entity —
  the guard lives in `upsert_entity` itself, so `get_or_create_abstract_entity` and any other
  current or future caller that constructs a brand-new `community:*` `Entity` inherit it
  automatically.
- `Store._write_bindings_canonical_for_record` — the routine that recomputes a record's full
  committed anchor set whenever some binding's identity changes — filters out any binding whose
  entity is derived. A genuine identity change on a *non-community* binding on the same record
  (say, a leaf's tier or weight) still triggers a canonical rewrite, but the rewritten file only
  ever contains the non-derived bindings; a community binding never rides along.

The committed anchor set of a record is therefore exactly: Tier-2 leaves, Tier-0
(initiative/`tag:*`), and `domain:*` Tier-1 bindings — durable, human- or graph-topology-named
identities. Community ids are Leiden's own snapshot output, renumbered on every rebuild
(`149 → 19 → 256` was observed across three successive rebuilds on a real repo — see
[anchoring](../concepts/anchoring.md#domain-aware-tier-1)), and never belong in git.

**Fresh-clone recovery.** A clone has no committed community state at all (it was never
written) and no `last_seen_*` baseline either (`index.db` is gitignored, never cloned). The
first `sidegraph-sync` against the live graph self-heals: it re-resolves every *tracked*
Tier-2 leaf and re-derives that leaf's Tier-1 community binding from scratch, purely in the
index — the same adopted/orphan-path repointing machinery that runs on every ordinary sync,
not a special case. **Residual:** a Tier-1 community binding with no Tier-2 leaf at all — the
case for an ambiguous-match anchor whose candidates share one community (`resolve_and_bind`
only creates a leaf on `resolved`/`unresolved`, never on `ambiguous`) — has nothing for sync
to re-resolve *from*, so it is not regenerated post-clone; a decision anchored only that way
effectively loses its anchor until a fresh capture touches the same reference again. See
[surviving refactors](../guides/surviving-refactors.md#fresh-clone-recovery).

**Legacy committed binding entries decay lazily; legacy committed entity files do not decay at
all.** A store written by pre-0.6.0 code may still have `community:*` entries sitting in its
committed `bindings/<record_id>.json` files, each pointing at a `community:*`
`entities/<ulid>.json` file. Opening such a store tolerates both on reload: they load into the
index normally, no data loss, no rejection. From there the two diverge:

- The **binding** entry decays: `Store._write_bindings_canonical_for_record` recomputes a
  record's entire committed anchor set from the index every time it runs, filtering derived
  bindings out — so the legacy entry is not re-emitted the next time that record's
  `bindings/<record_id>.json` is legitimately rewritten for an unrelated (non-community)
  reason (see `tests/test_store_derived_community.py::test_reload_tolerates_and_decays_legacy_community_entries`).
- The **entity** file does not decay. Nothing ever rewrites or deletes a `community:*`
  entity's canonical file — the `_is_derived_entity` guard in `upsert_entity` blocks
  canonical rewrites for them, and hard deletes are forbidden outright — so a legacy
  `community:*` `entities/<ulid>.json` persists
  on disk indefinitely, orphaned from any live binding, once its last referencing binding has
  decayed away.

There is no proactive canonical sweep in this wave for either case, and a record that never
gets another leaf-identity change keeps its stale committed binding entry indefinitely too, same
as the untouched entity file. Revisit with a dedicated cleanup command if this residual turns
out to matter in practice.

## The format marker

`.sidegraph/format` is a single committed line, `sidegraph-store 0.6.0`, written the first time
a `Store` creates the layout and never rewritten after that. It exists **outside** the
gitignored `index.db` specifically so a teammate on an incompatible store format is rejected
the moment they open the repo's `.sidegraph/`, not just when their own local index happens to
disagree. An existing layout from before this marker existed gets one backfilled silently on
next open, never rejected for its absence. A stamped major version that doesn't match the
running code's major version is a hard `ValueError` on open — there is no migration path
beyond the one legacy step described below; the fix is a fresh store, not a coerced read.

## The creation marker

`.sidegraph/stamping_live_since` is a single committed line, an aware UTC ISO-8601
timestamp, written **only** the first time a `Store` finds the layout genuinely new — no
record of any kind anywhere yet, hot or archived. Unlike the format marker above, it is
**never backfilled**: a store that already held a record the first time a marker-writing
version opened it never gets this file, on any later open, however many records it
accumulates afterward. A store whose records were all moved into `archive/` by
`sidegraph-compact` still counts as existing, not new — archive segments are records too.

It exists so `sidegraph-doctor`'s `unratified-accept` check has a scope-start that
doesn't depend on any ratification ever having happened: that check normally scopes to
records created at/after the store's earliest ratifier stamp, which leaves a store that
has never ratified anything invisible to it forever (see [`cli.md`](cli.md#sidegraph-doctor)).
The creation marker closes that window by recording the instant stamping became live for
this store, independent of ratification.

Published keep-first (whichever opener's write reaches the filesystem first, wins,
forever — see `Store._ensure_stamping_marker`), unlike `format`/`.gitignore`'s
identical-content writes: two branches that each independently create a brand-new store
will each commit their own `stamping_live_since` with a different timestamp, a one-line
git conflict `format`'s byte-identical content never causes. Resolve it by keeping the
EARLIER timestamp — that is the correct answer regardless of which branch you're resolving
from, since an earlier creation instant is always the more accurate one.

## `.gitignore`

The store writes `.sidegraph/.gitignore` itself, containing:

```
index.db*
*.tmp
```

the moment any `Store` open touches the canonical layout — not just `sidegraph-init`, so a bare
`sidegraph-mcp`/hook invocation against a pre-existing store also leaves `git status` clean. It
covers `index.db` and its SQLite WAL/SHM sidecars (the `*` glob) and any crash-debris temp file
left mid-write. The store **never overwrites an existing `.gitignore`** — if you've hand-edited
it, your edits stand.

## Freshness: absorbing a `git pull`

`index.db` stores a canonical digest — a sha256 over the sorted `(relpath, size, mtime_ns)` of
every committed record file (plus the format marker and any archive segments). On every
`Store.__init__`:

- **Digest matches** the stamped value → fast path, index used as-is (after one guard: the
  index's own stamped `schema_version` must still agree with the running code, in case it was
  hand-corrupted).
- **Digest missing or stale** (a fresh store, an index that was deleted, or — the common case —
  a `git pull` that brought new/changed/removed record files) → a full reload: every committed
  file is re-read into the index, and every volatile field resets to its cold default
  (`Entity.last_seen_*` → `None`, `AnchorBinding.status` → `"live"`, `Domain.communities` →
  `[]`). The next `sidegraph-sync` (or the lazy `maybe_sync()` any read-path tool call
  triggers) re-derives that volatile state from the current graph.

This is what "a pull is absorbed automatically" means in practice: nothing has to notice a
teammate's new decision files landed — the next store open sees the digest disagree and
reloads. No command is needed beyond what already runs on every session start / tool call.

A reload marks the store `volatile_stale`, and the next `sidegraph-sync` — ordinary or
lazy, no `--force` needed — rebuilds every derived field from the graph and clears the
mark. Until that pass runs, domain membership is empty, entity engine-mappings are unset,
and every binding reads `live`; the committed files are untouched throughout, which is why
none of this produces a git diff.

## Digest integrity

The freshness digest above only protects "does the index match the CANONICAL FILES on
disk" — it says nothing about whether the index actually loaded a file it can currently
see. `canonical_stat(subdir, stem, size, mtime_ns)` (index-only, gitignored) closes that
gap: a row exists for a file only once THIS index has actually read (or written) it in its
current state.

Without it, a subtle defect is possible: writer A publishes a canonical file (`os.replace`
— durable and visible to every process immediately) and dies before its own index write.
An already-open writer B — unrelated, doing a normal write of its own — recomputes the
freshness digest by hashing the filesystem, which now includes A's orphaned file, and
stamps that digest as fresh. Every later open then sees `stored_digest == compute_digest()`
and takes the fast path, never reloading — A's record is durably on disk and permanently
invisible to every read. Unlike ordinary crash debris, a *matching* digest never self-heals.

**Every canonical writer records its own row, in the same transaction as its file write and
index write.** The stat is taken from the tmp file *before* `os.replace` (not the published
path afterward): `os.replace` preserves `(size, mtime_ns)`, so this is exactly the
published file's stat too, with no window in which a *later* writer's replace of the same
file could be picked up instead of this writer's own. The one writer with no `os.replace`
at all — the archive segment writer, which publishes via exclusive `os.link` with a retry
that can rename the target — records its row only after the link actually wins, using the
name that won.

**`_touch_digest` checks its own digest walk against this table before stamping**, and
refuses — clearing any existing stamp rather than merely leaving it in place — if any
walked file has no matching row or a different one: this index did not load that file in
its current state, so it must not certify that it did. The check is one-directional: a row
whose file is now gone (`sidegraph-compact` removed it, or the row belongs to a derived
`community:*` entity that never had a canonical file to begin with) is normal and never
blocks a stamp. Refusing costs one extra reload on the next open — the same tested heal
path freshness already uses, just entered unconditionally on a genuine mismatch instead of
relying on a value comparison that a rare, no-crash race (two writers rewriting the same
record with `os.replace` order inverted against commit order) can defeat.

An id-based check (does the record's id appear in the index, rather than does its exact
on-disk stat match) was tried first and rejected: it only catches an *absent* record, not a
*rewritten* one — a ratify status flip, a supersede, or an entity rename can each die
pre-commit after mutating an EXISTING canonical file, and an id-based check would happily
certify the stale original since the id was never missing. A cross-process writer lock was
also considered and rejected: it would still need this same check, since a crash while
holding the lock reintroduces the identical defect, while this check needs no lock at all.

## Retrieval telemetry

Two more index-only tables, alongside `capture_sessions`: `retrieval_shows` (a record id →
how many renders included it, and when it last did) and `retrieval_seeds` (a file path or
other seed key → how many retrieval calls asked about it, and when last). Written by the
retrieval path (`get_task_context`, `query_decisions`, `drill_down`) after a successful
render, read by `sidegraph-doctor`'s `never-surfaced` check (see
[`docs/reference/cli.md`](cli.md)) to tell a decision nobody has needed yet from one that's
anchored where people keep working and still never surfaces. `query_structure` records
nothing at all — it returns no decision memory, so it never offers an opportunity for one to
surface, and counting its seeds would inflate the never-surfaced denominator with areas no
decision could possibly have fired in.

Like `capture_sessions`, both tables are **excluded from the reload's `DROP` list** (see
above) and so survive a `git pull`-triggered reload untouched, even though every record
table gets rebuilt from scratch: losing the history on every pull would make the counts
meaningless for exactly the long-lived question they exist to answer. Set
`SIDEGRAPH_TELEMETRY=off` to disable recording entirely — see `cli.md`'s environment
variable list.

### `retrieval_events` (derived, index-only)

An ordered, session-scoped journal of what memory surfaced and which files were touched
afterwards. Three kinds: `seed` (a path a retrieval was asked about), `show_anchor` (a file
anchored by a record that reached a render — `detail` holds the record id), and `touch` (a
file the agent opened, edited or grepped — `detail` holds the tool name). `key` is always a
repo-relative file path, which is the join axis.

Like `retrieval_shows`/`retrieval_seeds` and `capture_sessions`, it lives only in the
gitignored `index.db` and survives `_reload_index_from_canonical`, so a `git pull` does not
erase it. Pruned to 30 days at each `SessionStart`, **unconditionally** — `SIDEGRAPH_TELEMETRY=off`
stops new events being recorded but never freezes expiry of what is already there. Until
2026-08-04 the same `telemetry_enabled()` check gated both, so opting out made an
engineer's existing behavioural journal live forever while everyone else's aged out; a
practitioner review caught that inversion. Opting out now strictly reduces retention.

## Archive segments (`sidegraph-compact`)

Terminal-status records — decisions `superseded`/`rejected`/`deprecated`, domains
`superseded`/`dropped` — can never change again under the append-only rules, so once a record
reaches one of those statuses it no longer needs individual-file mergeability or PR-diff
review. `sidegraph-compact` (an explicit, human-run maintenance command — never invoked by
sync/retrieval/ratify) packs every eligible terminal record into a new segment under
`archive/` and removes its now-redundant hot file in the same operation. `proposed`/`accepted`
records of either kind are never touched — a decision has to actually close before it's
eligible.

**Facts are not compacted in this wave (deferred).** `facts/` stays entirely hot regardless of
status — a `superseded`/`rejected` `Fact` is never packed into `archive/` the way a terminal
`Decision`/`Domain` is; `Store.compact` only ever walks `decisions/` and `domains/`.

**Filename shape:** `archive/<date>-<seq>-<hash12>.jsonl`, e.g.
`archive/2026-07-08-1-a1b2c3d4e5f6.jsonl` — the date and a same-day sequence number, plus the
first 12 hex characters of a sha256 over the segment's own content. The hash suffix exists
specifically so two branches that both run `sidegraph-compact` on the same day, archiving
*different* records, can never collide on a filename: different content always produces a
different filename (both segments survive a merge as separate files, no conflict — the loader
dedups any overlapping ULID between them), and identical content always produces the identical
filename *and* bytes (also no conflict — trivially the same file arriving twice). A segment is
published via exclusive-create and, once written, is **never rewritten** — publishing retries
under the next sequence number in the rare case the exact name is already taken, rather than
overwriting anything.

The loader reads hot files **and** every archive segment on every load, deduping by ULID (a
record can legitimately appear in more than one segment — e.g. compaction run independently on
two branches before they merged — but terminal records are immutable, so any duplicate is
byte-identical and it doesn't matter which copy wins). If a hot file and an archived copy of
the same id ever disagree, the hot file wins and a warning prints — that shape means
corruption or a hand-edit, and the store never silently prefers the archive over live disk
state. The freshness digest (above) covers `archive/*.jsonl` too, so a segment absorbed from a
teammate's branch via `git pull` is picked up on the next open exactly like any other new
committed file.

`sidegraph-compact --older-than N` additionally requires a record to have been terminal for at
least `N` days; domains are always excluded from that filter (nothing on `Domain` records when
it became terminal, so age can't be evaluated — see
[`reference/cli.md`](cli.md#sidegraph-compact) for the exact reporting shape). Everything
compaction moves stays retrievable afterward — the append-only invariant holds because records
*move*, not disappear.

Recommended on the default branch, as periodic team hygiene, not on every commit.

## Migration to 0.4.0 (from 0.2.x and 0.3.x)

Opening a legacy single-file SQLite store (`decisions.db`, or any store stamped
`schema_version` `0.2.0`/`0.3.0`) triggers a one-time, fail-closed export into the canonical
layout:

1. Every row in the legacy file is read **and Pydantic-validated in full** before a single byte
   is written anywhere. A garbled row raises immediately, naming the offending table and id,
   and the legacy file is untouched — safe to retry later, never partially migrated.
2. Only once every row is known-good is the export written, into a private staging area first
   (so a crash mid-export never corrupts either the legacy file or a real store directory).
3. The legacy file is renamed to `<name>.migrated-backup` (e.g.
   `decisions.db.migrated-backup`) — **never deleted**, matching the store's own append-only
   ethos even for its own retired format. This rename is the point of no return; the staged
   export then lands at the canonical path.
4. `index.db` is rebuilt from the freshly written canonical files, `SCHEMA_VERSION` is stamped
   at the running code's current version, and one line prints to stderr naming the source,
   destination, and backup path.

Volatile fields are **not** carried over from the legacy `index.db` — only canonical
(identity/content) data migrates; the next sync re-derives engine mappings from the live graph,
same as any other cold reload. A store already in the canonical (file-per-record) layout never
triggers this legacy-file migration step, as always — see the next section for what DOES
happen on open when such a store's stamp is merely older than the running code's.

## `SCHEMA_VERSION` and exact-match policy

`SCHEMA_VERSION` (in `schema.py`) is currently `0.6.0`. The format-marker check (above) gates
on the *major* component only; the index's own stamped `schema_version` inside `meta` is
checked for an **exact** match on the fast (digest-matches) freshness path — a mismatch there
normally raises immediately. The one exception is `_RELOADABLE_SCHEMA_VERSIONS` (currently
`{0.4.0, 0.5.0}`): both a 0.4.0 and a 0.5.0 store's canonical files are fully forward-compatible
with 0.6.0 — the 0.4.0 → 0.5.0 step only added a record type and renamed `AnchorBinding`'s
foreign key, and the 0.5.0 → 0.6.0 step ([derived community bindings](#community-bindings-are-derived-not-committed))
changed no on-disk *shape* either, only where `community:*` entities/bindings get written from
now on — so opening either triggers a full index reload (which re-stamps `schema_version`)
instead of a hard failure. A 0.5.0 store's committed files may still carry old `community:*`
entity/binding entries from before this wave; the reload tolerates them (loads them into the
index, no data loss) but they are not re-emitted on that record's next canonical rewrite — see
[Legacy committed entries decay lazily](#community-bindings-are-derived-not-committed) above.
Anything else is still a fresh store, not a coerced read. `set_meta("schema_version", ...)` is
blocked in code — it raises unconditionally, so nothing can silently reset the stamp from
within a running process.

**Additive optional fields do not bump `SCHEMA_VERSION`.** A field that is optional, defaulted,
and never required to read an existing record leaves every stored file loadable by old and new
code alike, so bumping would force a reload for nothing. Fields added this way so far:
`provenance.commit` (capture-time git SHA), `Domain.seed_anchors`, and — since 2026-08-04 —
`ratified_by` / `ratified_at` on `Decision`, `Fact` and `Domain` (stamped by the ratify
transitions; `null` on every record ratified before the field existed, and **never
backfilled** — a guessed ratifier would defeat the point of recording one). A field that
changes an existing field's meaning, or that a reader must have, is a version bump instead.

## The sync-clean invariant

**A graph rebuild must never produce a git diff from regenerable, engine-owned state —
enforced by construction, not by discipline.** `sidegraph-sync` (including the lazy
`maybe_sync()` any read-path tool call triggers) mutates two different kinds of thing.
Almost everything it does is purely derived, index-only state: entity engine-mappings
(`last_seen_node_id`/`last_seen_graph_version`/`last_seen_community`), binding status flips,
domain `communities`, the TOC cache, and any Tier-1 `community:*` re-pointing (community
bindings and the abstract entities they point at are themselves derived — see [Community
bindings are derived](#community-bindings-are-derived-not-committed) above). None of that ever
lands in a committed file: running sync, even repeatedly, on a graph whose Leiden communities
renumber on every rebuild, leaves `git status` empty in the canonical directories for that
state — not merely as an operational habit, but because the entity-keyed guard in `store.py`
(`_is_derived_entity`: any abstract entity whose `canonical_name` starts with `community:`)
makes it structurally impossible for a community label to reach a committed file, whether the
write attempt comes from `sync.py`'s repointing path or from ordinary capture-time binding —
that sub-claim holds with zero exceptions.

The one thing sync *does* legitimately write to a committed file is the rebind ladder's
`moved` rung (see [surviving refactors](../guides/surviving-refactors.md#the-rebind-ladder)):
a leaf whose name survived but whose file moved gets its `descriptor.file_path` updated and
its own `entities/<id>.json` rewritten to match — verified live, REF-1. That's a
durable-identity update, the same kind `upsert_entity` would apply for any other rename, not
regenerable engine-coupling state — so it's outside this invariant's scope, not a violation of
it: the sync report names it (`moved: <name>() (<old path> -> <new path>)`), and the new file
content is exactly as canonical as the old content was.

This is invariant #1 extended (see the project's `CLAUDE.md`): the store was always
append-only for human-authored content; this wave closes the gap where regenerable
engine-coupling state — specifically, community labels — could still touch git.

## Append-only write rules

- **No hard deletes, anywhere, ever.** No code path in `store.py` issues a file delete for a
  live record (only compaction removes a hot file, and only after the same content is durably
  archived — see above).
- **Supersede = close + link, atomically.** `add_decision(decision)`, when `decision.supersedes`
  is set, first loads the predecessor, sets its `valid_to` (to the max of its own `valid_from`
  and the new decision's `valid_from`, if not already closed) and flips its `status` to
  `superseded`, writes it, *then* writes the new decision — both committed files land in the
  same call, so a `superseded` decision is never observed on disk without its successor already
  present.
- **Status transitions, not rewrites.** `ratify(id)` flips `proposed -> accepted`. `drop(id)`
  flips `proposed -> rejected` and closes `valid_to`. Both raise if the decision isn't
  currently `proposed`; decision *content* (`context`, `choice`, `rejected`, ...) is never
  mutated after write, only `status`/`valid_to` and successor linkage — and every such
  transition rewrites that one committed `decisions/<id>.json` file, it never touches any other
  record's file.
- **Fact status transitions mirror a decision's.** `ratify_fact(id)`/`drop_fact(id)` flip a
  fact's own `status` the same way, and both raise if it isn't currently `proposed`. A fact
  never has to be ratified by hand just because it rides a decision, though: `ratify(id)`/
  `drop(id)` on the DECISION also flips every still-`proposed` fact whose `supports` names it
  (accept cascades unconditionally; drop cascades a fact only once every decision it supports
  has been rejected) — one committed `facts/<id>.json` rewrite per cascaded fact, same
  content-immutability guarantee as a decision's own transition.
- **Binding status flips, never deletes, and stay index-only.** Sync and re-anchoring change an
  `AnchorBinding`'s `status` (`live` / `degraded` / `orphaned`) in `index.db`; the committed
  `bindings/<record_id>.json` file (identity + tier/relation/weight, no `status`) is
  untouched by that flip. An `Entity`'s `last_seen_*` fields are likewise index-only.
- **Domain status transitions, not rewrites.** `ratify_domains` flips `proposed -> accepted`
  (and mints the paired `domain:<slug>` entity) or `proposed -> dropped`. Editing a `Domain`'s
  description is a new committed file: `supersede_domain` closes the predecessor (status ->
  `superseded`) and writes a successor with `supersedes` set, both in the same call — mirroring
  `add_decision`'s supersede path. The only other sanctioned mutation is
  `refresh_domain_communities`, which `sidegraph-sync` uses to update ONLY the index-only
  `communities` field (REPLACE semantics) on every `accepted` domain; `title`, `summary`,
  `parent_id`, and `path_prefixes` live in the committed file and are never touched by sync.
- Every write path also enforces the schema-level invariants from
  [data model](../concepts/data-model.md): `valid_to >= valid_from`; every `AnchorBinding`
  must reference an existing `Entity` and `Decision` (`add_binding` raises otherwise);
  `Provenance` is always present on a `Decision`.

## What tools may and may not do to this store

**May:**
- Append new `decisions/`, `facts/`, `domains/`, `entities/`, `bindings/`, `initiatives/`
  files.
- Flip `Decision.status`/`Fact.status` (and, index-only, `AnchorBinding.status`) through the
  documented transitions — each a rewrite of that one record's own committed file. A fact's
  status transitions mirror a decision's: `ratify_fact`/`drop_fact` flip it directly
  (`proposed → accepted`/`rejected`), and accepting or dropping a decision it supports
  cascades the same flip automatically (see [`mcp-tools.md#ratify`](mcp-tools.md#ratify)).
- Flip `Domain.status` through `ratify_domains` (`proposed -> accepted`; `proposed` or
  `accepted` `-> dropped` — dropping an accepted domain is the sanctioned way to retire a
  slug-conflict duplicate) or `supersede_domain` (close + link, like a decision reversal).
- Refresh an `Entity`'s engine-mapping fields or a `Domain`'s `communities` field — index-only,
  never touches a committed file.
- Pack terminal records into a new `archive/` segment and remove their now-redundant hot files
  (`sidegraph-compact`) — append-only holds because the record *moves*, byte-for-byte, into the
  segment before its hot file is removed.

**May not:**
- Hard-delete any record, hot or archived.
- Rewrite a `Decision`'s substantive fields (`title`, `context`, `choice`, `rejected`,
  `consequences`, `kind`) after creation.
- Rewrite a `Fact`'s substantive fields (`statement`, `source`, `supports`) after creation —
  falsification is `supersede_fact` (close + link a successor), never an edit in place.
- Rewrite a `Domain`'s substantive fields (`title`, `summary`, `parent_id`, `path_prefixes`)
  after creation — only `communities` is ever refreshed, and only index-only, by sync.
- Overwrite `.sidegraph/format` or the index's `schema_version` once stamped.
- Write `.sidegraph/stamping_live_since` onto a store that already held a record (hot or
  archived) the first time a marker-writing version opened it.
- Rewrite an already-archived segment, for any reason.
- Write to the engine's `graph.json` under any circumstance — that file belongs entirely to
  the engine and is regenerated from its own cache on every commit; the committed store is
  Sidegraph's *only* write target.

## Merge semantics: git resolves it, not the store

Concurrent writes **within one process** are lock-serialized (an instance-level
`threading.RLock` around every store call) — ordinary mutual exclusion, nothing merge-related
to reason about there. Concurrent writes **across processes on the same branch** (the MCP
server, `sidegraph-sync`/`sidegraph-ratify`, and the Claude Code hooks may all open the same
`.sidegraph/` independently) rely on each writer's own atomic file replace
(tmp file + `os.replace`) — a reader never observes a half-written record. Every such write
uses a per-write-unique tmp name, not a fixed one, so two writers of the *same* target file
can never share an inode and truncate each other's not-yet-replaced bytes.

Two more cross-process windows, both distinct from an ordinary record write, are closed the
same "make it atomic" way rather than with a lock:

- **A full index reload** (see [Freshness](#freshness-absorbing-a-git-pull)) drops and
  recreates the six record tables inside one explicit transaction. A concurrent reader on a
  separate connection — a long-lived MCP server included, since it never re-opens to retry —
  sees the pre-reload rows for the entire rebuild and the new ones only once it commits, never
  a table gone mid-flight.
- **The open-time tmp-debris sweep** only removes a `*.tmp` file older than 60 seconds. That
  gate exists because the sweep has no way to tell real crash debris from another process's
  tmp file that is mid-write right now, and used to delete the live one — a buffer lives
  microseconds, so it is never old enough to match.

The interesting case is **across branches**, and it's resolved by git, by design, not by any
locking Sidegraph implements:

- Two branches add *different* decisions or domains → different files, no overlap at all — git
  merges silently, and the next open+sync on the merged branch absorbs both (ULIDs make a
  filename collision between unrelated records impossible).
- Two branches change the **same** record's status (e.g. one branch accepts a decision, another
  drops it — in the JSON that reads `"status": "accepted"` vs `"status": "rejected"`; a dropped
  *decision* is recorded as `rejected`, `dropped` is domain vocabulary) → a genuine textual
  conflict in that one small JSON file — routed to a human, resolved the same way any other
  merge conflict is: read both sides, decide, edit the file.
- Two branches independently create a domain with the **same slug** → both files are valid on
  disk after the merge (different `domain_id`s, no file conflict) — `find_domain_by_slug`
  resolves the collision deterministically in the meantime (accepted > proposed, newest first),
  so retrieval never breaks. Any slug held by more than one live (`proposed`/`accepted`) domain
  at once is also *detected*: `Store.domain_slug_conflicts()` recomputes this live (a single
  indexed query, cheap enough to run on every call — nothing is cached), so every
  `sidegraph-sync` pass reports it fresh (`slug conflict: 'payments' held by 2 live domains
  (...) — drop one`), and a full index reload (cold open, or any digest mismatch — see
  [Freshness](#freshness-absorbing-a-git-pull) above) also warns to stderr the moment it's
  detected. Nothing resolves it automatically — dropped/superseded domains free their slug back
  up and are never part of a conflict, but a live duplicate stays flagged until a human retires
  one: `sidegraph-ratify --drop <id>` works on either a `proposed` OR an already-`accepted`
  domain (unlike a decision, where drop is proposal-only) and is the normal fix; reach for
  `supersede_domain` only if the retiring domain's slug should survive under a *different* name
  — its successor can't keep the SAME slug while the other duplicate is still live.

In short: **a same-record dispute across branches is a real git conflict, resolved by editing
the file it landed in — not a database-level race the store arbitrates for you.**
