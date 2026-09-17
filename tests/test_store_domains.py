"""Domain store invariants — these ARE the contract (see CLAUDE.md,
docs/concepts/mind-model.md#domain-lifecycle)."""

from __future__ import annotations

import json

import pytest

from sidegraph.schema import Descriptor, Domain, DomainStatus, EntityKind, Provenance
from sidegraph.store import Store


@pytest.fixture
def store(tmp_path) -> Store:
    with Store(tmp_path / "test.db") as s:
        yield s


def _domain(**overrides) -> Domain:
    base = dict(
        slug="payments",
        title="Payments",
        summary="Handles order settlement and refunds.",
        provenance=Provenance(source="manual"),
    )
    base.update(overrides)
    return Domain(**base)


# -- add / get / find / iter -------------------------------------------------


def test_add_and_get_domain(store: Store) -> None:
    d = store.add_domain(_domain())
    got = store.get_domain(d.domain_id)
    assert got is not None
    assert got.slug == "payments"
    assert got.status == DomainStatus.PROPOSED


def test_get_domain_missing_returns_none(store: Store) -> None:
    assert store.get_domain("nope") is None


def test_find_domain_by_slug(store: Store) -> None:
    store.add_domain(_domain())
    found = store.find_domain_by_slug("payments")
    assert found is not None and found.slug == "payments"
    assert store.find_domain_by_slug("no-such-slug") is None


def test_find_domain_by_slug_prefers_live_over_dropped(store: Store) -> None:
    """No ORDER BY previously left this to rowid/insertion order, so a drop-then-
    recreate at the same slug could resolve back to the older, dropped row."""
    p1 = store.add_domain(_domain(slug="payments", title="Payments v1"))
    store.ratify_domains(drop=[p1.domain_id])
    p2 = store.add_domain(_domain(slug="payments", title="Payments v2"))

    found = store.find_domain_by_slug("payments")
    assert found is not None
    assert found.domain_id == p2.domain_id


def test_find_domain_by_slug_prefers_accepted_over_proposed(store: Store) -> None:
    """Slug uniqueness among *live* domains means an accepted and a proposed row can
    never coexist for the same slug through the public API — seed the ambiguous state
    directly via the private writer (corrupt-data / defensive-ordering simulation) to
    exercise the ORDER BY preference in isolation. The proposed row is written SECOND
    (so it has the newer, lexicographically-greater ULID) to prove status precedence
    wins over recency, not the other way around."""
    accepted = _domain(slug="payments", title="Payments accepted", status=DomainStatus.ACCEPTED)
    proposed = _domain(slug="payments", title="Payments proposed")
    with store._lock:
        store._write_domain(accepted)
        store._write_domain(proposed)
        store._commit()

    found = store.find_domain_by_slug("payments")
    assert found is not None
    assert found.domain_id == accepted.domain_id


def test_add_domain_existing_id_rejected(store: Store) -> None:
    """append-only: add_domain must never silently rewrite an existing row via
    INSERT..ON CONFLICT DO UPDATE — that erases history through a method documented as
    append-only. Reversal goes through supersede_domain instead."""
    d = store.add_domain(_domain())
    with pytest.raises(ValueError, match="already exists"):
        store.add_domain(_domain(domain_id=d.domain_id, slug="other-slug", title="Rewritten"))
    # the original row must be untouched
    reloaded = store.get_domain(d.domain_id)
    assert reloaded.slug == "payments"
    assert reloaded.title == "Payments"


def test_iter_domains_all_and_by_status(store: Store) -> None:
    a = store.add_domain(_domain(slug="a", title="A"))
    store.add_domain(_domain(slug="b", title="B"))
    store.ratify_domains(accept=[a.domain_id])

    all_slugs = {d.slug for d in store.iter_domains()}
    assert all_slugs == {"a", "b"}

    accepted_slugs = {d.slug for d in store.iter_domains(status=DomainStatus.ACCEPTED)}
    assert accepted_slugs == {"a"}
    proposed_slugs = {d.slug for d in store.iter_domains(status=DomainStatus.PROPOSED)}
    assert proposed_slugs == {"b"}


# -- seed_anchors (§2a amendment: durable membership survives a fresh clone / rebuild) ----


def test_domain_seed_anchors_round_trips_through_canonical_file_and_index_reload(
    tmp_path,
) -> None:
    """``seed_anchors`` is COMMITTED authoring intent (like ``path_prefixes``), never the
    volatile ``communities`` mapping ``_domain_canonical_payload`` pops. Write, blow away
    the derived index (simulates a fresh clone), reopen: ``seed_anchors`` survives verbatim
    and ``communities`` resets to its cold default (``[]`` — the next sync re-derives it)."""
    store = Store(tmp_path / "s")
    anchor = Descriptor(name="OrderBook", file_path="trader/order_book.py")
    d = store.add_domain(_domain(seed_anchors=[anchor], communities=["40"]))
    store.close()

    canonical = json.loads((tmp_path / "s" / "domains" / f"{d.domain_id}.json").read_text())
    assert canonical["seed_anchors"] == [{"name": "OrderBook", "file_path": "trader/order_book.py"}]
    assert "communities" not in canonical  # volatile -- popped by _domain_canonical_payload

    (tmp_path / "s" / "index.db").unlink()
    reopened = Store(tmp_path / "s")
    try:
        reloaded = reopened.get_domain(d.domain_id)
        assert reloaded is not None
        assert reloaded.seed_anchors == [anchor]
        assert reloaded.communities == []  # volatile, cold default -- next sync re-derives it
    finally:
        reopened.close()


# -- refresh_domain_communities (sanctioned mutable-field update, like
# Entity.last_seen_* — see docs/concepts/mind-model.md) --------------------------------


def test_refresh_domain_communities_updates_only_that_field(store: Store) -> None:
    d = store.add_domain(_domain(communities=["1"]))
    updated = store.refresh_domain_communities(d.domain_id, ["7"])
    assert updated.communities == ["7"]
    assert updated.title == d.title
    assert updated.summary == d.summary
    assert updated.status == d.status

    reloaded = store.get_domain(d.domain_id)
    assert reloaded.communities == ["7"]


def test_refresh_domain_communities_noop_when_unchanged(store: Store) -> None:
    d = store.add_domain(_domain(communities=["1"]))
    same = store.refresh_domain_communities(d.domain_id, ["1"])
    assert same.communities == ["1"]
    assert same == d


def test_refresh_domain_communities_unknown_id_rejected(store: Store) -> None:
    with pytest.raises(ValueError, match="not found"):
        store.refresh_domain_communities("nope", ["1"])


# -- find_domain_by_community (M2: anchoring.resolve_and_bind's Tier-1 preference; M5
# review fold-in: pin the tie-break) ------------------------------------------------------


def test_find_domain_by_community_newest_accepted_first_tie_break(store: Store) -> None:
    """Two accepted domains both claiming the same community id (not prevented at write
    time, though sync should keep it from happening in practice) -- deterministic
    newest-first resolution via domain_id (ULID, sortable by creation time)."""
    older = store.add_domain(_domain(slug="payments-old", communities=["7"]))
    store.ratify_domains(accept=[older.domain_id])
    newer = store.add_domain(_domain(slug="payments-new", communities=["7"]))
    store.ratify_domains(accept=[newer.domain_id])

    found = store.find_domain_by_community("7")
    assert found is not None
    assert found.domain_id == newer.domain_id


def test_find_domain_by_community_ignores_proposed_and_dropped(store: Store) -> None:
    store.add_domain(_domain(slug="proposed-only", communities=["7"]))
    dropped = store.add_domain(_domain(slug="dropped-one", communities=["7"]))
    store.ratify_domains(drop=[dropped.domain_id])

    assert store.find_domain_by_community("7") is None


def test_find_domain_by_community_no_match_returns_none(store: Store) -> None:
    store.add_domain(_domain(slug="payments", communities=["7"]))
    store.ratify_domains(accept=[store.find_domain_by_slug("payments").domain_id])
    assert store.find_domain_by_community("999") is None


# -- find_domains_by_community (Gate-5 finding 2: the retrieval bucket-C union — ALL
# covering domains, not just the newest) --------------------------------------------------


def test_find_domains_by_community_returns_all_accepted_newest_first(store: Store) -> None:
    older = store.add_domain(_domain(slug="payments-old", communities=["7"]))
    store.ratify_domains(accept=[older.domain_id])
    newer = store.add_domain(_domain(slug="payments-new", communities=["7"]))
    store.ratify_domains(accept=[newer.domain_id])

    found = store.find_domains_by_community("7")
    assert [d.domain_id for d in found] == [newer.domain_id, older.domain_id]


def test_find_domains_by_community_excludes_proposed_and_dropped(store: Store) -> None:
    store.add_domain(_domain(slug="proposed-only", communities=["7"]))
    dropped = store.add_domain(_domain(slug="dropped-one", communities=["7"]))
    store.ratify_domains(drop=[dropped.domain_id])

    assert store.find_domains_by_community("7") == []


def test_find_domains_by_community_no_match_returns_empty_list(store: Store) -> None:
    d = store.add_domain(_domain(slug="payments", communities=["7"]))
    store.ratify_domains(accept=[d.domain_id])
    assert store.find_domains_by_community("999") == []


def test_find_domain_by_community_singular_is_newest_of_the_plural_set(store: Store) -> None:
    """The singular lookup (anchoring's Tier-1 write-time pick) must stay consistent with
    the plural one (retrieval's read-time union): same newest-first ordering, just [0]."""
    older = store.add_domain(_domain(slug="payments-old", communities=["7"]))
    store.ratify_domains(accept=[older.domain_id])
    newer = store.add_domain(_domain(slug="payments-new", communities=["7"]))
    store.ratify_domains(accept=[newer.domain_id])

    singular = store.find_domain_by_community("7")
    plural = store.find_domains_by_community("7")
    assert singular.domain_id == plural[0].domain_id


# -- slug uniqueness ----------------------------------------------------------


def test_slug_collision_among_live_domains_rejected(store: Store) -> None:
    store.add_domain(_domain(slug="payments", title="Payments"))
    with pytest.raises(ValueError, match="slug"):
        store.add_domain(_domain(slug="payments", title="Payments Again"))


def test_slug_reusable_once_predecessor_superseded(store: Store) -> None:
    old = store.add_domain(_domain(slug="payments", title="Payments v1"))
    new = store.supersede_domain(
        old.domain_id,
        _domain(slug="payments", title="Payments v2", supersedes=old.domain_id),
    )
    assert new.slug == "payments"
    assert store.get_domain(old.domain_id).status == DomainStatus.SUPERSEDED


def test_slug_reusable_once_dropped(store: Store) -> None:
    d = store.add_domain(_domain(slug="payments"))
    store.ratify_domains(drop=[d.domain_id])
    # dropped frees the slug back up for a brand new proposal
    again = store.add_domain(_domain(slug="payments", title="Payments retry"))
    assert again.slug == "payments"


# -- slug uniqueness against MULTIPLE live holders (review round 3, N5-adjacent fix) --------
#
# A cross-branch merge can legitimately leave TWO live domains sharing a slug (design §6;
# see domain_slug_conflicts() / test_store_slug_conflicts.py) -- something the public write
# API alone can never produce (add_domain's own uniqueness check would reject the second
# one), so these seed the shape directly via the private writer, same technique
# test_find_domain_by_slug_prefers_accepted_over_proposed uses above.


def test_add_domain_against_duplicate_live_holders_names_both_blocking_ids(
    store: Store,
) -> None:
    d1 = _domain(slug="payments", title="Payments A", status=DomainStatus.ACCEPTED)
    d2 = _domain(slug="payments", title="Payments B", status=DomainStatus.ACCEPTED)
    with store._lock:
        store._write_domain(d1)
        store._write_domain(d2)
        store._commit()

    with pytest.raises(ValueError, match="slug") as exc_info:
        store.add_domain(_domain(slug="payments", title="Payments C"))
    msg = str(exc_info.value)
    assert d1.domain_id in msg
    assert d2.domain_id in msg


def test_supersede_domain_against_duplicate_live_holder_deterministically_rejected_both_orders(
    store: Store,
) -> None:
    """CRITICAL regression: the old single-arbitrary-row check (_live_domain_by_slug, no
    ORDER BY) could non-deterministically PASS a supersede on one duplicate depending on
    which of the two live rows sqlite happened to return -- spuriously leaving the OTHER
    duplicate's slug collision completely unvalidated. The fixed all-holders check must
    reject BOTH orders, every time, naming the specific still-live id that blocks it."""
    d1 = _domain(slug="payments", title="Payments A", status=DomainStatus.ACCEPTED)
    d2 = _domain(slug="payments", title="Payments B", status=DomainStatus.ACCEPTED)
    with store._lock:
        store._write_domain(d1)
        store._write_domain(d2)
        store._commit()

    with pytest.raises(ValueError, match="slug") as exc_info_1:
        store.supersede_domain(
            d1.domain_id,
            _domain(slug="payments", title="Payments A v2", supersedes=d1.domain_id),
        )
    assert d2.domain_id in str(exc_info_1.value)

    with pytest.raises(ValueError, match="slug") as exc_info_2:
        store.supersede_domain(
            d2.domain_id,
            _domain(slug="payments", title="Payments B v2", supersedes=d2.domain_id),
        )
    assert d1.domain_id in str(exc_info_2.value)

    # neither duplicate was touched by either rejected attempt
    assert store.get_domain(d1.domain_id).status == DomainStatus.ACCEPTED
    assert store.get_domain(d2.domain_id).status == DomainStatus.ACCEPTED


def test_supersede_domain_single_holder_still_unaffected_by_the_all_holders_check(
    store: Store,
) -> None:
    """The fix must not regress the ordinary, single-holder case: exclude_id still excuses
    the predecessor being superseded, same slug, no other live holder in the way."""
    old = store.add_domain(_domain(slug="payments", title="Payments v1"))
    new = store.supersede_domain(
        old.domain_id,
        _domain(slug="payments", title="Payments v2", supersedes=old.domain_id),
    )
    assert new.slug == "payments"
    assert store.get_domain(old.domain_id).status == DomainStatus.SUPERSEDED


# -- retiring an ACCEPTED domain via ratify_domains(drop=...) (review round 3 fix 3) --------
#
# The actual resolution path for an accepted-vs-accepted slug conflict: supersede_domain
# cannot resolve it (the successor would just collide with the OTHER still-live duplicate --
# see the tests above), so ratify_domains' drop guard is extended from proposed-only to
# proposed-or-accepted for DOMAINS specifically. Decisions keep their strict, proposal-only
# drop vocabulary untouched -- see test_store_ratification.py's pinned regression.


def test_ratify_domains_drop_accepted_domain_flips_to_dropped(store: Store) -> None:
    d = store.add_domain(_domain(slug="payments"))
    store.ratify_domains(accept=[d.domain_id])
    assert store.get_domain(d.domain_id).status == DomainStatus.ACCEPTED

    result = store.ratify_domains(drop=[d.domain_id])
    assert result == {d.domain_id: "dropped"}
    assert store.get_domain(d.domain_id).status == DomainStatus.DROPPED


def test_ratify_domains_drop_accepted_domain_frees_its_slug(store: Store) -> None:
    d = store.add_domain(_domain(slug="payments", title="Payments v1"))
    store.ratify_domains(accept=[d.domain_id])
    store.ratify_domains(drop=[d.domain_id])

    again = store.add_domain(_domain(slug="payments", title="Payments v2"))
    assert again.slug == "payments"


def test_ratify_domains_drop_accepted_domain_resolves_a_live_slug_conflict(
    store: Store,
) -> None:
    """End-to-end: the actual fix for design §6's accepted-vs-accepted case."""
    d1 = _domain(slug="payments", title="Payments A", status=DomainStatus.ACCEPTED)
    d2 = _domain(slug="payments", title="Payments B", status=DomainStatus.ACCEPTED)
    with store._lock:
        store._write_domain(d1)
        store._write_domain(d2)
        store._commit()
    assert len(store.domain_slug_conflicts()) == 1

    store.ratify_domains(drop=[d2.domain_id])

    assert store.domain_slug_conflicts() == []  # computed live -- clears immediately
    assert store.get_domain(d1.domain_id).status == DomainStatus.ACCEPTED
    assert store.get_domain(d2.domain_id).status == DomainStatus.DROPPED


def test_ratify_domains_drop_still_rejects_an_already_dropped_or_superseded_domain(
    store: Store,
) -> None:
    """The extension is proposed-OR-accepted, not "anything" -- a domain already in a
    terminal status still can't be dropped again (append-only: it's already retired)."""
    d = store.add_domain(_domain(slug="payments"))
    store.ratify_domains(drop=[d.domain_id])
    result = store.ratify_domains(drop=[d.domain_id])
    assert result[d.domain_id].startswith("error:")
    assert store.get_domain(d.domain_id).status == DomainStatus.DROPPED  # unchanged


# -- parent existence + acyclicity -------------------------------------------


def test_parent_must_exist(store: Store) -> None:
    with pytest.raises(ValueError, match="unknown domain"):
        store.add_domain(_domain(parent_id="01JUNKULIDDOESNOTEXIST00"))


def test_parent_child_chain_allowed(store: Store) -> None:
    root = store.add_domain(_domain(slug="root", title="Root"))
    child = store.add_domain(_domain(slug="child", title="Child", parent_id=root.domain_id))
    assert child.parent_id == root.domain_id


def test_self_parent_rejected(store: Store) -> None:
    d = _domain()
    d.parent_id = d.domain_id  # a domain cannot be its own parent
    with pytest.raises(ValueError, match="cycle"):
        store.add_domain(d)


def test_two_node_parent_cycle_rejected(store: Store) -> None:
    # A -> no parent; B -> parent A. Both added normally through the public API — no
    # cycle yet.
    a = store.add_domain(
        Domain(
            domain_id="fixed-a",
            slug="a",
            title="A",
            summary="s",
            provenance=Provenance(source="manual"),
        )
    )
    b = store.add_domain(
        Domain(
            domain_id="fixed-b",
            slug="b",
            title="B",
            summary="s",
            parent_id=a.domain_id,
            provenance=Provenance(source="manual"),
        )
    )
    # add_domain now rejects a same-id re-add (that invariant is covered elsewhere), so
    # rewiring A's own parent to B can no longer go through the public API. Simulate the
    # corrupted state directly via the private writer instead (data corruption / a bug
    # elsewhere, never a legitimate write path) to reproduce A -> B -> A and still
    # exercise the acyclicity walk.
    corrupted_a = a.model_copy(update={"parent_id": b.domain_id})
    with store._lock:
        store._write_domain(corrupted_a)
        store._commit()

    # Any new domain chaining off the now-corrupted pair must have its acyclicity walk
    # detect the pre-existing loop.
    with pytest.raises(ValueError, match="cycle"):
        store.add_domain(
            Domain(
                slug="c",
                title="C",
                summary="s",
                parent_id=a.domain_id,
                provenance=Provenance(source="manual"),
            )
        )


# -- supersession -------------------------------------------------------------


def test_supersede_domain_closes_predecessor_and_keeps_history(store: Store) -> None:
    old = store.add_domain(_domain(summary="v1 summary"))
    new = store.supersede_domain(
        old.domain_id,
        _domain(summary="v2 summary", supersedes=old.domain_id),
    )
    reloaded_old = store.get_domain(old.domain_id)
    assert reloaded_old.status == DomainStatus.SUPERSEDED
    assert reloaded_old.summary == "v1 summary"  # history retrievable, unmodified
    assert store.get_domain(new.domain_id).supersedes == old.domain_id
    assert store.get_domain(new.domain_id).summary == "v2 summary"


def test_supersede_domain_unknown_old_id_rejected(store: Store) -> None:
    with pytest.raises(ValueError, match="unknown domain"):
        store.supersede_domain("nope", _domain(supersedes="nope"))


def test_supersede_domain_requires_matching_supersedes(store: Store) -> None:
    old = store.add_domain(_domain())
    with pytest.raises(ValueError, match="supersedes"):
        store.supersede_domain(old.domain_id, _domain(slug="other", supersedes="mismatch"))


def test_supersede_domain_invalid_successor_does_not_leak_flip(store: Store) -> None:
    """CRITICAL regression: supersede_domain must validate the successor BEFORE writing
    the predecessor's status flip. The old (buggy) order flipped+wrote old -> SUPERSEDED
    first; if the successor then failed validation, that flip sat uncommitted in
    sqlite's implicit transaction and the NEXT unrelated write would durably commit a
    "superseded with zero successors" domain."""
    old = store.add_domain(_domain(slug="payments", title="Payments"))
    store.ratify_domains(accept=[old.domain_id])
    # a third, unrelated live domain that the successor collides with
    store.add_domain(_domain(slug="shipping", title="Shipping"))

    with pytest.raises(ValueError, match="slug"):
        store.supersede_domain(
            old.domain_id,
            _domain(slug="shipping", title="Payments v2", supersedes=old.domain_id),
        )

    # same connection: the flip must not have leaked despite validation failing
    assert store.get_domain(old.domain_id).status == DomainStatus.ACCEPTED

    # an unrelated write on the same connection must not accidentally commit a leaked flip
    store.add_domain(_domain(slug="unrelated", title="Unrelated"))

    # and after a reopen at the same path, the predecessor must still be accepted
    path = store.path
    store.close()
    reopened = Store(path)
    try:
        assert reopened.get_domain(old.domain_id).status == DomainStatus.ACCEPTED
    finally:
        reopened.close()


def test_supersede_domain_reusing_predecessor_id_rejected(store: Store) -> None:
    """append-only: a successor reusing its OWN predecessor's domain_id must be rejected
    — without the guard, ON CONFLICT DO UPDATE in _write_domain would rewrite the
    predecessor's row in place, leaving no superseded row behind (history erased)."""
    old = store.add_domain(_domain(slug="payments", title="Payments v1"))

    with pytest.raises(ValueError, match="already exists"):
        store.supersede_domain(
            old.domain_id,
            _domain(
                domain_id=old.domain_id,
                slug="payments",
                title="Payments v2",
                supersedes=old.domain_id,
            ),
        )

    # the original row must be untouched after reopen
    path = store.path
    store.close()
    reopened = Store(path)
    try:
        reloaded = reopened.get_domain(old.domain_id)
        assert reloaded is not None
        assert reloaded.status == DomainStatus.PROPOSED
        assert reloaded.title == "Payments v1"
    finally:
        reopened.close()


def test_supersede_domain_successor_write_failure_does_not_leave_predecessor_flipped(
    store: Store, monkeypatch
) -> None:
    """CRITICAL-adjacent regression: supersede_domain must write the SUCCESSOR canonically
    before flipping the predecessor's status -- the old (buggy) order flipped+wrote the
    predecessor to SUPERSEDED to its canonical file FIRST; the try/except's
    ``self._conn.rollback()`` only undoes the in-memory INDEX transaction, not a canonical
    file already swapped onto disk via os.replace. If the successor's write then failed,
    that flip was already durable; the next Store to open this path would digest-reload a
    "superseded with zero successors" domain whose slug is unresolvable. Mirrors
    add_decision/ratify's write order."""
    old = store.add_domain(_domain(slug="payments", title="Payments v1"))
    store.ratify_domains(accept=[old.domain_id])

    new_domain = _domain(slug="payments", title="Payments v2", supersedes=old.domain_id)

    import sidegraph.store as store_module

    real_write_domain_canonical = store_module.Store._write_domain_canonical

    def _boom(self, domain):
        if domain.domain_id == new_domain.domain_id:
            raise OSError("simulated failure writing successor")
        return real_write_domain_canonical(self, domain)

    monkeypatch.setattr(store_module.Store, "_write_domain_canonical", _boom)
    with pytest.raises(OSError, match="simulated failure writing successor"):
        store.supersede_domain(old.domain_id, new_domain)
    monkeypatch.setattr(store_module.Store, "_write_domain_canonical", real_write_domain_canonical)

    path = store.path
    store.close()
    reopened = Store(path)
    try:
        reloaded_old = reopened.get_domain(old.domain_id)
        assert reloaded_old.status == DomainStatus.ACCEPTED  # unchanged, never flipped
        assert reopened.get_domain(new_domain.domain_id) is None  # successor never landed
    finally:
        reopened.close()


def test_supersede_domain_reusing_unrelated_third_id_rejected(store: Store) -> None:
    """append-only: a successor reusing a THIRD, unrelated domain's id must be rejected
    — without the guard, ON CONFLICT DO UPDATE in _write_domain would silently rewrite
    that unrelated row instead of inserting a new one."""
    old = store.add_domain(_domain(slug="payments", title="Payments v1"))
    third = store.add_domain(_domain(slug="shipping", title="Shipping"))

    with pytest.raises(ValueError, match="already exists"):
        store.supersede_domain(
            old.domain_id,
            _domain(
                domain_id=third.domain_id,
                slug="payments",
                title="Payments v2",
                supersedes=old.domain_id,
            ),
        )

    # the unrelated third row must be untouched after reopen
    path = store.path
    store.close()
    reopened = Store(path)
    try:
        reloaded_third = reopened.get_domain(third.domain_id)
        assert reloaded_third is not None
        assert reloaded_third.status == DomainStatus.PROPOSED
        assert reloaded_third.slug == "shipping"
        assert reloaded_third.title == "Shipping"
        # the predecessor must also be untouched (still live, not superseded)
        reloaded_old = reopened.get_domain(old.domain_id)
        assert reloaded_old is not None
        assert reloaded_old.status == DomainStatus.PROPOSED
    finally:
        reopened.close()


# -- ratification -------------------------------------------------------------


def test_ratify_domains_accept_mints_paired_entity(store: Store) -> None:
    d = store.add_domain(_domain(slug="payments"))
    result = store.ratify_domains(accept=[d.domain_id])
    assert result == {d.domain_id: "accepted"}
    assert store.get_domain(d.domain_id).status == DomainStatus.ACCEPTED

    entity = store.find_abstract_entity("domain:payments")
    assert entity is not None
    assert entity.kind == EntityKind.ABSTRACT


def test_ratify_domains_accept_mints_entity_exactly_once(store: Store) -> None:
    d = store.add_domain(_domain(slug="payments"))
    e1 = store.get_or_create_abstract_entity("domain:payments")
    store.ratify_domains(accept=[d.domain_id])
    e2 = store.find_abstract_entity("domain:payments")

    with store._lock:
        rows = store._conn.execute(
            "SELECT data FROM entities WHERE canonical_name = ?", ("domain:payments",)
        ).fetchall()
    assert len(rows) == 1  # idempotent: still exactly one abstract entity
    assert e1.entity_id == e2.entity_id


def test_ratify_domains_drop_flips_and_mints_no_entity(store: Store) -> None:
    d = store.add_domain(_domain(slug="payments"))
    result = store.ratify_domains(drop=[d.domain_id])
    assert result == {d.domain_id: "dropped"}
    assert store.get_domain(d.domain_id).status == DomainStatus.DROPPED
    assert store.find_abstract_entity("domain:payments") is None


def test_ratify_domains_unknown_id_reports_error_in_result(store: Store) -> None:
    result = store.ratify_domains(accept=["missing-id"])
    assert result["missing-id"].startswith("error:")


def test_ratify_domains_mixed_batch_partial_success(store: Store) -> None:
    good = store.add_domain(_domain(slug="payments"))
    result = store.ratify_domains(accept=[good.domain_id, "missing-id"])
    assert result[good.domain_id] == "accepted"
    assert result["missing-id"].startswith("error:")
    assert store.get_domain(good.domain_id).status == DomainStatus.ACCEPTED
