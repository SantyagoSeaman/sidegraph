"""``_touch_digest`` refuses to certify a canonical file this index did not actually load
(digest-integrity design, rev 5, Task 3). The defect: ``_touch_digest`` used to hash the
FILESYSTEM -- every canonical file it could see, not the ones THIS process actually
indexed -- so one process could certify another process's work as indexed when it was not.
`stored_digest == compute_digest()` then held while the index was missing (or held a stale
copy of) a real record, permanently: unlike ordinary crash debris, a MATCHING digest means
the next open takes the fast path and never reloads, so nothing ever heals it.

Rev 4 (D3): a refusal CLEARS the stored digest (deletes the meta row) rather than merely
declining to update it. An implementation finding mid-Task-3 showed "merely withhold" is
not enough: a no-crash inversion of replace order against commit order (item 8 below) can
leave a DIFFERENT writer's already-valid stamp matching current disk while THIS index has
since drifted -- "unchanged" is then indistinguishable from "correct". Clearing makes the
next open reload unconditionally, regardless of what the freshly-computed digest happens to
equal.

Ledger (spec §4 item 9) -- report each test as what it is, never as evidence it is not:
- items 1, 3a and 8 are genuine reds against unfixed code;
- item 3 ("every canonical-writing operation still stamps") is a DECLARED EXCEPTION: it
  passes today too, since unfixed ``_touch_digest`` always stamps unconditionally. Its
  point is guarding against the fix over-reaching (a version that never stamps would pass
  items 1/3a perfectly but fail this one);
- items 4 and 5 (Task 4) are ALSO declared exceptions -- both pass before and after, and
  exist to pin D2's one-directional check against a naive two-directional equality check
  nobody proposed but the tests guard against anyway;
- item 7 (Task 4) is a declared exception whose red target is the DISCARDED id-based draft
  (D4), not unfixed code -- unfixed ``_touch_digest`` always stamps unconditionally, so
  nothing wedges there either;
- item 6 (Task 4) is a tripwire, not a benchmark, and not red-first by nature.

Note on item 8 and the stat-before-replace rule (§3): item 8's own test
(`test_no_crash_inverted_replace_order_vs_commit_order_stays_consistent`) pins the
clear-vs-withhold behaviour (D3) but, by construction, CANNOT tell a stat-before-replace
``_atomic_write_text`` apart from a stat-after-replace one -- it takes its stat straight
from `_atomic_write_json`'s return value inside one synchronous call, with no window for a
foreign replace to land in between. That gap was found by mutation testing (verified: the
whole suite passed 1486/1486 against a stat-after-replace mutation, before this note was
added) and is closed by the dedicated
`test_stat_before_replace_is_pinned_against_a_stat_after_replace_regression` below, which
injects the foreign replace via a monkeypatched `os.replace` instead.
"""

from __future__ import annotations

import json
import shutil
import time
from datetime import UTC, datetime

import pytest

import sidegraph.store as store_module
from sidegraph.schema import (
    AnchorBinding,
    Decision,
    DecisionKind,
    DecisionStatus,
    Domain,
    Entity,
    EntityKind,
    Fact,
    Initiative,
    Provenance,
)
from sidegraph.store import Store, _atomic_write_json


def _decision(title: str = "an adr", **overrides) -> Decision:
    base = dict(
        title=title,
        kind=DecisionKind.ADR,
        context="ctx",
        choice="choice",
        valid_from=datetime.now(UTC),
        provenance=Provenance(source="manual"),
    )
    base.update(overrides)
    return Decision(**base)


def _fact(statement: str = "a fact", **overrides) -> Fact:
    base = dict(
        statement=statement,
        source="observed",
        valid_from=datetime.now(UTC),
        provenance=Provenance(source="manual"),
    )
    base.update(overrides)
    return Fact(**base)


def _domain(slug: str = "a-domain", **overrides) -> Domain:
    base = dict(
        slug=slug,
        title=slug,
        summary="A domain used for digest-integrity testing.",
        provenance=Provenance(source="manual"),
    )
    base.update(overrides)
    return Domain(**base)


# == item 1: the original reproduction =======================================================


def test_original_repro_heals_after_reopen(tmp_path):
    """Spec §4 item 1, the reproduction §1 opens with. Writer A publishes a canonical file
    and dies before its index write; already-open B (the load-bearing detail) performs a
    normal, UNRELATED write. Red against unfixed code with ``indexed_after_reopen: False``
    -- that exact output is the evidence (spec §1's own repro line).

    ``a.close()`` runs immediately after the simulated crash, before B's write -- not
    deferred to the end. A REAL crash (the process dying) releases the OS-level file lock
    on ``index.db`` at once (that is what SQLite's own crash-recovery model relies on); a
    live Python connection left open with an uncommitted transaction does not, and would
    make B's own write block/fail on ``database is locked`` for an unrelated reason (the
    canonical_stat INSERT Task 1 added to ``_write_decision_canonical`` is itself now a DML,
    unlike the pre-Task-1 filesystem-only version) -- closing here is what makes A's
    uncommitted stat row disappear exactly the way a crash would, while the canonical FILE
    (already ``os.replace``d) stays, which is the whole point of the repro."""
    path = tmp_path / "s"
    a = Store(path)
    b = Store(path)

    orphan = _decision("A's orphan")
    a._write_decision_canonical(orphan)  # canonical only -- simulated crash before A indexes
    a.close()  # the crash: releases A's lock and its own uncommitted stat row, file stays

    b.add_fact(_fact("B's normal write"))  # B's own, unrelated, otherwise-ordinary write

    b.close()

    c = Store(path)
    try:
        indexed_after_reopen = c.get_decision(orphan.id) is not None
        assert indexed_after_reopen, "indexed_after_reopen: False"
    finally:
        c.close()


# == item 2: the mechanism itself, pinned directly on meta.canonical_digest =================


def test_digest_stamp_is_cleared_while_a_foreign_file_is_unindexed(tmp_path):
    """Spec item 2: assert directly on ``meta.canonical_digest`` -- not just the outcome a
    reopen exposes -- so the MECHANISM is pinned, not only its symptom.

    Rev 4 (design D3): B's own touch must CLEAR the stamp, not merely leave it unchanged.
    An earlier draft asserted "unchanged", on the assumption that withholding a new stamp
    always leaves ``stored`` behind ``current`` for the next open. That assumption is
    false and needs no crash at all (spec item 8 / the no-crash inversion) -- a SEPARATE,
    already-valid stamp from another writer can still numerically match current disk even
    though this index has since drifted, so "unchanged" is not enough to guarantee the next
    open reloads. Clearing the row makes ``stored`` become ``None``, which
    ``_refresh_freshness`` treats as "reload" unconditionally."""
    path = tmp_path / "s"
    a = Store(path)
    b = Store(path)

    before = b.get_meta("canonical_digest")
    assert before is not None  # a fresh store already has a real stamp from __init__
    orphan = _decision("A's second orphan")
    a._write_decision_canonical(orphan)
    a.close()  # the crash -- see test_original_repro_heals_after_reopen for why this matters

    b.add_fact(_fact("B's second normal write"))
    after = b.get_meta("canonical_digest")

    assert after is None, (
        "the stamp must be CLEARED, not merely withheld, while A's file is unindexed"
    )
    b.close()


# == item 3a: the staleness variant ==========================================================


def test_staleness_variant_serves_the_rewrite_after_reopen(tmp_path):
    """Spec item 3a (design D4): writer A REWRITES an EXISTING record's canonical file and
    dies before its index write; already-open B performs a normal, unrelated write. Red
    against unfixed code AND against the discarded id-based draft (D4) alike -- an id-based
    check sees the id still present and happily stamps, certifying the ORIGINAL content
    while the file on disk already says something else. Assert the reopened index serves
    A's NEW content, not the stale original."""
    path = tmp_path / "s"
    setup = Store(path)
    victim = setup.add_decision(_decision("victim", context="ORIGINAL"))
    setup.close()

    a = Store(path)
    b = Store(path)

    mutated = victim.model_copy(update={"context": "REWRITTEN BY A"})
    a._write_decision_canonical(mutated)  # os.replace lands; A dies before its index write
    a.close()  # the crash -- see test_original_repro_heals_after_reopen for why this matters

    b.add_fact(_fact("B's third normal write"))

    b.close()

    c = Store(path)
    try:
        assert c.get_decision(victim.id).context == "REWRITTEN BY A"
    finally:
        c.close()


# == item 8: two writers, no crash at all, replace order inverted against commit order ======


def test_no_crash_inverted_replace_order_vs_commit_order_stays_consistent(tmp_path):
    """Spec §4 item 8 / §3's stat-before-replace rule -- the sharpest test in the plan, and
    the one item-8's own scenario found the rev-3 gap that produced rev 4 (D3: clear, don't
    merely withhold).

    Two writers rewrite the SAME record. NEITHER crashes -- both complete and commit fully.
    S1 replaces the canonical file first; S2 replaces over it SECOND and fully completes
    (index write + its own correct stat row + a digest that validly matches current disk,
    since disk IS S2's content at that moment) and commits. S1 then finishes: its own index
    write (now stale -- S2's file has already superseded it) and its own canonical_stat row
    (S1's OWN pre-replace stat, correct per the stat-before-replace rule -- and it correctly
    no longer matches current disk, since S2 replaced over it). ``_touch_digest`` detects
    that mismatch and must not just leave S2's ALREADY-VALID stamp in place (it still
    matches disk, coincidentally, since nothing rewrote the file again after S2) -- it must
    CLEAR it, or the next open's fast path would serve S1's stale index forever, silently.

    Split into explicit steps (module-level ``_atomic_write_json`` for each writer's file
    portion, the real ``Store`` methods for the index/stat/digest portions) rather than
    calling the bundled ``_write_decision_canonical`` for both writers back to back: since
    Task 1, that method's own canonical_stat INSERT is itself a DML, so calling it for BOTH
    writers without S2 ever committing in between would just serialize them on SQLite's
    write lock and never reach the inverted interleaving at all. This mirrors review's own
    probe (`probe_rev2.py`'s P3) and the same technique this project's existing rollback
    tests use (deterministic construction standing in for a genuine cross-process race,
    exactly as ``test_store_mutation_guard.py`` injects failures instead of relying on
    actual concurrent threads).

    Red against a "withhold, don't clear" implementation of D3 (rev 3, before the
    implementation finding that produced rev 4): S1's touch DOES detect the mismatch, but
    merely leaving S2's already-matching stamp in place changes nothing -- reproduced
    independently, `stored==current: True` while `idx==disk: False`, before the clear was
    added.

    **What this test does NOT pin: the stat-before-replace rule itself.** This test takes
    ``st1`` straight from ``_atomic_write_json``'s own return value -- computed and returned
    from ONE synchronous call, with nothing able to interleave inside it. A stat-AFTER-
    replace implementation of ``_atomic_write_text`` would ALSO return an accurate stat of
    S1's own content here, because S2's replace has no chance to land between S1's own
    replace and S1's own stat call within this construction -- both happen back to back,
    synchronously, before S2 ever runs. Verified by mutating ``_atomic_write_text`` to stat
    ``path`` after ``os.replace`` instead of the tmp before it: the full suite, this test
    included, stayed green (1486/1486) -- an earlier version of this docstring claimed this
    test was red against that mutation, and it is not; that claim was false and has been
    removed. See ``test_stat_before_replace_is_pinned_against_a_stat_after_replace_regression``
    below for the test that actually exercises ``_atomic_write_text``'s own ordering and
    does catch that mutation.
    """
    path = tmp_path / "s"
    setup = Store(path)
    victim = setup.add_decision(_decision("no-crash-victim", context="ORIGINAL"))
    setup.close()

    s1 = Store(path)
    s2 = Store(path)

    v1 = victim.model_copy(update={"context": "S1-VERSION"})
    v2 = victim.model_copy(update={"context": "S2-VERSION-LONGER-CONTENT"})
    file_path = path / "decisions" / f"{victim.id}.json"

    # S1 replaces the file first (pure filesystem op -- no SQL touched yet):
    st1 = _atomic_write_json(file_path, v1.model_dump(mode="json"))
    # S2 replaces over it and FULLY completes: index write, its own (correct, matching)
    # stat row, a digest that validly matches current disk, and commits.
    st2 = _atomic_write_json(file_path, v2.model_dump(mode="json"))
    s2._index_write_decision(v2)
    s2._record_canonical_stat("decisions", victim.id, st2)
    s2._touch_digest()
    s2._commit()

    # S1 finishes: its own (now stale) index write, its own (correct per stat-before-
    # replace, but now stale relative to CURRENT disk) stat row, then its own touch --
    # which must detect the mismatch and CLEAR S2's already-valid stamp, not leave it.
    s1._index_write_decision(v1)
    s1._record_canonical_stat("decisions", victim.id, st1)
    s1._touch_digest()
    s1._commit()

    s1.close()
    s2.close()

    c = Store(path)
    try:
        idx = c.get_decision(victim.id).context
        disk = json.loads(file_path.read_text())["context"]
        assert idx == disk, f"digest-certified-lie: index={idx!r}, disk={disk!r}"
    finally:
        c.close()


def test_stat_before_replace_is_pinned_against_a_stat_after_replace_regression(
    tmp_path, monkeypatch
):
    """§3's stat-before-replace rule itself, pinned directly against ``_atomic_write_text``
    -- the test the previous one's docstring wrongly claimed to be. That test takes its stat
    straight from ``_atomic_write_json``'s return value, computed inside ONE synchronous
    call with nothing able to interleave -- so it cannot tell a stat-before-replace
    implementation from a stat-after-replace one; both return an accurate stat of the
    caller's OWN content when nothing else touches the file in between. The vulnerability
    needs a FOREIGN replace landing strictly between THIS writer's own ``os.replace`` and
    THIS writer's own ``stat()`` call -- a window that exists only *inside*
    ``_atomic_write_text``, between its own two statements, and only a stat-AFTER-replace
    implementation ever has that window open at all (stat-before-replace closes it by
    construction, since the stat already happened before the replace).

    Reached here by monkeypatching ``os.replace`` (the name ``store.py`` calls, patched at
    module level) to run S2's ENTIRE write+commit immediately after S1's own replace
    returns, standing in for the OS pausing S1's thread in exactly that gap -- deterministic,
    not a real race, same technique as the neighbouring test's split-step construction and
    this project's existing rollback-injection tests.

    Against TODAY's stat-before-replace ``_atomic_write_text``, S1's stat is already
    captured (from the tmp, before its own replace) by the time this hook fires, so the
    patch changes nothing about what S1 records -- S1's touch correctly detects the
    resulting mismatch (S1's own accurate-but-now-stale row vs. current disk) and clears
    (D3), and the next open reloads and serves S2's content, consistent with disk.

    Red against a stat-AFTER-replace mutation of ``_atomic_write_text`` (move the ``st =
    ...stat()`` line to after ``os.replace``): there, S1's stat call fires only AFTER this
    hook has already let S2 fully replace and commit, so S1 stats CURRENT disk (S2's
    content) and records it as its own -- numerically accurate at that instant, so S1's
    touch finds no mismatch and stamps over the lie. Verified directly: mutating
    ``_atomic_write_text`` this way and rerunning the full suite passes 1486/1486 with the
    OLD (pre-fix) version of this test file, which is exactly the gap this test closes.

    Also red against the withhold-vs-clear mutation (D3, rev 3 -> rev 4) -- unsurprising,
    since this reaches the SAME final assertion (``idx == disk`` after reopen) as the
    neighbouring test, through a different construction. The two tests are not redundant:
    only THIS one can tell stat-before-replace apart from stat-after-replace at all (see
    above); both happen to also be sensitive to whether a detected mismatch clears the
    stamp or merely withholds it, which is expected, not a coincidence to chase down.
    """
    path = tmp_path / "s"
    setup = Store(path)
    victim = setup.add_decision(_decision("stat-order-victim", context="ORIGINAL"))
    setup.close()

    s1 = Store(path)
    s2 = Store(path)

    v1 = victim.model_copy(update={"context": "S1-VERSION"})
    v2 = victim.model_copy(update={"context": "S2-VERSION-LONGER-CONTENT"})
    file_path = path / "decisions" / f"{victim.id}.json"

    real_os_replace = store_module.os.replace
    state = {"fired": False}

    def patched_replace(src, dst):
        result = real_os_replace(src, dst)
        # Fire exactly once, only for S1's own replace of the victim's file -- S2's own
        # replace (triggered from inside this same hook) must go through untouched, and
        # nothing else in this test replaces this path a second time.
        if not state["fired"] and dst == file_path:
            state["fired"] = True
            s2._write_decision(v2)  # index write + stat row + touch -- NOT self-committing
            s2._commit()  # `_write_decision` is a private helper; `_mutation()` normally
            # owns the commit for every public caller (add_decision/ratify/...) -- called
            # directly here, so this test must own it instead, or S2 never actually commits
            # and its own transaction sits open, blocking S1's very next statement.
        return result

    monkeypatch.setattr(store_module.os, "replace", patched_replace)

    s1._write_decision(v1)  # internally: tmp write, os.replace (hook fires here), then stat
    s1._commit()  # same reason as s2._commit() above

    monkeypatch.undo()
    s1.close()
    s2.close()

    c = Store(path)
    try:
        idx = c.get_decision(victim.id).context
        disk = json.loads(file_path.read_text())["context"]
        assert idx == disk, f"digest-certified-lie: index={idx!r}, disk={disk!r}"
    finally:
        c.close()


# == item 3: the over-reach guard, parameterized over every canonical-writing operation =====
#
# DECLARED EXCEPTION (spec §4 item 9): this passes against UNFIXED code too, since unfixed
# _touch_digest always stamps unconditionally. Its job is catching the fix over-reaching --
# a version that never stamps (or that stamps correctly for only ONE call site) would pass
# items 1/3a/2 above perfectly while silently breaking every new entity/domain (the exact
# D4a regression an earlier draft shipped).


def _case_add_decision(store: Store):
    store.add_decision(_decision("still-stamps-decision"))


def _case_add_fact(store: Store):
    store.add_fact(_fact("still-stamps-fact"))


def _case_upsert_entity_new(store: Store):
    store.upsert_entity(Entity(canonical_name="still-stamps-entity"))


def _case_add_domain(store: Store):
    store.add_domain(_domain("still-stamps-domain"))


def _case_add_binding(store: Store):
    entity = store.upsert_entity(Entity(canonical_name="still-stamps-binding-entity"))
    decision = store.add_decision(_decision("still-stamps-binding-target"))
    store.add_binding(AnchorBinding(record_id=decision.id, entity_id=entity.entity_id, tier=2))


def _case_upsert_initiative(store: Store):
    store.upsert_initiative(Initiative(name="still-stamps-initiative"))


def _case_ratify_status_flip(store: Store):
    proposed = store.add_decision(_decision("still-stamps-ratify", status=DecisionStatus.PROPOSED))
    store.ratify(proposed.id)


_STILL_STAMPS_CASES = {
    "add_decision": _case_add_decision,
    "add_fact": _case_add_fact,
    "upsert_entity_new": _case_upsert_entity_new,
    "add_domain": _case_add_domain,
    "add_binding": _case_add_binding,
    "upsert_initiative": _case_upsert_initiative,
    "ratify_status_flip": _case_ratify_status_flip,
}


@pytest.mark.parametrize("case_name", sorted(_STILL_STAMPS_CASES))
def test_every_canonical_writing_operation_still_stamps_and_reopen_is_fast_path(
    tmp_path, case_name, monkeypatch
):
    """An earlier draft tested this with ``add_decision`` alone, which would have passed
    while stamping was broken for every new entity and domain (the D4a regression, caught
    only because ``upsert_entity``/``_write_domain`` touch the digest BEFORE their index
    write -- the opposite order from ``_write_decision``/``_write_fact``). One call site
    does not guard this; all seven do."""
    path = tmp_path / "s"
    store = Store(path)
    before = store.get_meta("canonical_digest")

    _STILL_STAMPS_CASES[case_name](store)

    after = store.get_meta("canonical_digest")
    assert after != before, f"{case_name}: digest did not advance -- the write was not stamped"
    store.close()

    reload_calls = {"n": 0}
    original_reload = Store._reload_index_from_canonical

    def _counting_reload(self, digest):
        reload_calls["n"] += 1
        return original_reload(self, digest)

    monkeypatch.setattr(Store, "_reload_index_from_canonical", _counting_reload)
    reopened = Store(path)
    reopened.close()
    assert reload_calls["n"] == 0, f"{case_name}: reopen took the reload path, not the fast path"


# == Task 4: the remaining guards ============================================================
#
# Items 4 and 5 are declared exceptions (spec §4 item 9): both pass before AND after this
# whole wave -- D2's one-directional check never refused on an index-richer-than-disk shape
# to begin with. Their value is pinning that a future "simplification" to a naive
# two-directional equality check (table has a row the walk didn't see -> also refuse) would
# break them, not proving a bug that exists today.


def test_compact_still_stamps_despite_index_richer_than_disk(tmp_path):
    """Spec item 4 (D2's reverse direction). After compacting, the index still holds the
    compacted decision's id even though its hot FILE is gone (moved into the archive
    segment) -- the digest must still be written. Red against a naive two-directional
    equality check (table/index has an entry the disk walk didn't see -> refuse), which
    this design deliberately does not implement (D2)."""
    store = Store(tmp_path / "s")
    old = store.add_decision(_decision("compact-stamps-old"))
    store.add_decision(_decision("compact-stamps-new", supersedes=old.id))
    before = store.get_meta("canonical_digest")

    store.compact()

    after = store.get_meta("canonical_digest")
    assert after is not None
    assert after != before
    assert not (store.path / "decisions" / f"{old.id}.json").is_file()  # hot file gone
    assert store.get_decision(old.id) is not None  # still indexed -- index richer than disk


def test_derived_community_entities_do_not_block_stamping(tmp_path):
    """Spec item 5 -- same direction as item 4, different cause, and worth its own case
    because it would break every sync pass (not just a compacted store) if it didn't.
    A derived ``community:*`` abstract entity gets an index row and NEVER a canonical file
    at all (``upsert_entity``'s own ``_is_derived_entity`` guard) -- so it never appears in
    the digest walk and never gets (or needs) a ``canonical_stat`` row. A SUBSEQUENT,
    ordinary write must still stamp normally."""
    store = Store(tmp_path / "s")
    entity = store.get_or_create_abstract_entity("community:42")
    assert entity.kind == EntityKind.ABSTRACT
    assert not (store.path / "entities" / f"{entity.entity_id}.json").is_file()

    before = store.get_meta("canonical_digest")
    store.add_decision(_decision("after-community-entity"))
    after = store.get_meta("canonical_digest")
    assert after is not None
    assert after != before


def test_empty_bindings_file_does_not_wedge_stamping(tmp_path):
    """Spec item 7 (§3's debris shape). An empty ``bindings/<id>.json`` (``[]`` -- a hand
    edit or merge artifact, never written by any Store writer) produces no index ROWS at
    reload, but under the stat-table design there is no branch left to wedge on it: the
    check reads ``canonical_stat``, not the record tables, and Task 2's reload loop gives
    the file its row unconditionally, regardless of how many binding items it contains.

    DECLARED EXCEPTION: its red target is the DISCARDED id-based draft (D4), not unfixed
    code -- unfixed ``_touch_digest`` always stamps unconditionally, so nothing wedges
    there either. Stated so this is never reported as red-first evidence."""
    path = tmp_path / "s"
    store = Store(path)
    store.add_decision(_decision("anchor me"))
    store.close()

    # hand-edit / merge artifact -- never written by any Store writer:
    (path / "bindings").mkdir(exist_ok=True)
    (path / "bindings" / "01FAKEULID00000000000000ZZ.json").write_text("[]\n")

    store = Store(path)  # digest busted by the new file -- full reload
    before = store.get_meta("canonical_digest")
    assert before is not None  # the reload itself stamps unconditionally

    store.add_decision(_decision("post-debris write"))
    after = store.get_meta("canonical_digest")
    assert after is not None
    assert after != before
    store.close()


def test_write_cost_stays_in_single_digit_milliseconds(tmp_path):
    """Spec item 6 -- a tripwire, not a benchmark (design D5: the per-write digest walk is
    pre-existing and unchanged, ~5.1ms measured at 763 canonical files; canonical_stat adds
    one table read of the same cardinality, roughly a 10% addition). Guards against an
    order-of-magnitude regression, not a precise number -- not red-first by nature."""
    store = Store(tmp_path / "s")
    for i in range(300):  # comparable scale to this repo's own store (~700-1000 files)
        store.add_decision(_decision(f"bulk-{i}"))

    start = time.perf_counter()
    store.add_decision(_decision("timed-write"))
    elapsed_ms = (time.perf_counter() - start) * 1000

    assert elapsed_ms < 50, f"write took {elapsed_ms:.2f}ms -- order-of-magnitude regression?"


# == the reload must CLEAR canonical_stat, not merely upsert over it =========================


def test_a_reload_clears_stat_rows_for_files_it_did_not_load(tmp_path):
    """The reload rebuilds ``canonical_stat`` -- and rebuilding means CLEARING first, not
    upserting over whatever was there. Branch review found this: the reload dropped the six
    record tables but not ``canonical_stat``, so a row for a file that was ABSENT during the
    reload survived it. If that file then came back with an identical ``(size, mtime_ns)``,
    the next ``_touch_digest`` matched the stale row and stamped -- recreating the exact
    poisoned state this whole design exists to kill, using this design's own mechanism.

    Spec §3 claimed the table "describes what this index loaded, by construction". Upsert
    without clear does not deliver that construction; the spec said "rebuilds/repopulates"
    and never said "clear first", so the gap was mine.

    Trigger honesty: it needs mtime-PRESERVING restoration across an intervening reload
    (``mv`` out and back, ``rsync -a``, ``tar -p``, a backup tool). Nanosecond mtime equality
    never happens by accident, and git writes fresh mtimes, so ordinary pull/checkout flows
    were never exposed. Reproduced deterministically here because "rare" is not "absent" and
    the damage is silent and permanent.
    """
    store_dir = tmp_path / "s"
    held = tmp_path / "held"
    held.mkdir()

    store = Store(store_dir)
    victim = store.add_decision(_decision("reload-clear-victim", context="ORIGINAL"))
    store.close()
    victim_file = store_dir / "decisions" / f"{victim.id}.json"

    # The file leaves, mtime preserved, and a reload happens without it.
    shutil.move(str(victim_file), str(held / victim_file.name))
    store = Store(store_dir)
    assert store.get_decision(victim.id) is None, "precondition: the reload dropped the record"
    surviving = store._conn.execute(
        "SELECT count(*) FROM canonical_stat WHERE stem = ?", (victim.id,)
    ).fetchone()[0]
    assert surviving == 0, "the reload left a stat row for a file it never loaded"

    # It returns with an identical stat, and an ordinary write must NOT certify it.
    shutil.move(str(held / victim_file.name), str(victim_file))
    store.add_fact(
        Fact(
            statement="an unrelated write",
            source="test",
            status=DecisionStatus.ACCEPTED,
            valid_from=datetime.now(UTC),
            provenance=Provenance(source="manual"),
        )
    )
    store.close()

    reopened = Store(store_dir)
    try:
        assert reopened.get_decision(victim.id) is not None, (
            "canonical file present, index still missing it after a reopen — "
            "the digest certified a file this index never loaded"
        )
    finally:
        reopened.close()
