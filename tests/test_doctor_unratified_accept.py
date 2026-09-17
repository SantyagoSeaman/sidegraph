"""`unratified-accept`: an accepted record that never passed the human gate.

Practitioner re-review round 2 — three of four reviewers independently flagged
`SIDEGRAPH_AUTO_ACCEPT=on` as the hole in the ratification fence: it is per-environment
(one teammate's shell), it writes into the SHARED committed store, and nothing detects it
afterwards. The staff engineer named the detectable signature himself: `accepted` +
`provenance.source == "agent"` + no ratifier stamp. That makes the gate auditable at PR
time instead of trust-based.

Red target for the first test: unfixed code emits no such finding. The stamped-record and
human-sourced tests are declared over-reach guards — a check that fired on ordinary
ratified or human-authored records would be pure noise.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from sidegraph.doctor import UNRATIFIED_ACCEPT, curate
from sidegraph.schema import AnchorBinding, Decision, DecisionKind, DecisionStatus, Fact, Provenance
from sidegraph.store import _STAMPING_MARKER_NAME, Store


def _decision(
    store: Store,
    title: str,
    *,
    source: str,
    status=DecisionStatus.ACCEPTED,
    stamped: bool = False,
    supersedes: str | None = None,
    layer: str | None = None,
    ref: str | None = None,
) -> Decision:
    d = Decision(
        title=title,
        kind=DecisionKind.ADR,
        status=status,
        context="c",
        choice="ch",
        valid_from=datetime.now(UTC),
        supersedes=supersedes,
        layer=layer,
        provenance=Provenance(source=source, ref=ref),
    )
    if stamped:
        d.ratified_by = "a human"
        d.ratified_at = datetime.now(UTC)
    store._write_decision(d)
    store._conn.commit()
    return d


def _bind_tag(store: Store, record_id: str, slug: str) -> str:
    """Bind a tier-0 ``tag:<slug>`` entity to ``record_id`` — mirrors ``capture._propose_one``'s
    own tag-binding step (the ONLY place tag bindings are ever minted for a decision).
    Returns the tag entity's id, so a test can bind the SAME one onto a predecessor to
    prove an INHERITED tag is not mistaken for a NEW one."""
    tag_entity = store.get_or_create_abstract_entity(f"tag:{slug}")
    store.add_binding(AnchorBinding(record_id=record_id, entity_id=tag_entity.entity_id, tier=0))
    return tag_entity.entity_id


def _fact(
    store: Store,
    statement: str,
    *,
    source: str,
    status=DecisionStatus.ACCEPTED,
    stamped: bool = False,
    supersedes: str | None = None,
) -> Fact:
    f = Fact(
        statement=statement,
        source="test",
        status=status,
        valid_from=datetime.now(UTC),
        supersedes=supersedes,
        provenance=Provenance(source=source),
    )
    if stamped:
        f.ratified_by = "a human"
        f.ratified_at = datetime.now(UTC)
    store._write_fact(f)
    store._conn.commit()
    return f


def _findings(db: Path):
    return [f for f in curate(db).findings if f.code == UNRATIFIED_ACCEPT]


def test_agent_sourced_accepted_record_without_a_stamp_is_flagged(tmp_path):
    db = tmp_path / "store"
    store = Store(db)
    # One stamped record establishes that ratification stamping runs in this store; without
    # it the signature cannot discriminate and the check stays silent by design.
    _decision(store, "properly ratified earlier", source="agent", stamped=True)
    d = _decision(store, "auto-accepted by an env var", source="agent")
    hits = _findings(db)
    assert len(hits) == 1
    assert d.id in hits[0].path
    assert "SIDEGRAPH_AUTO_ACCEPT" in hits[0].detail


def test_stamped_agent_record_is_not_flagged(tmp_path):
    """Over-reach guard (declared): a proposal an agent wrote and a human ratified is the
    normal, wanted path."""
    db = tmp_path / "store"
    store = Store(db)
    _decision(store, "properly ratified", source="agent", stamped=True)
    assert _findings(db) == []


def test_human_sourced_record_is_not_flagged(tmp_path):
    """Over-reach guard (declared): `add_decision` asked by a human lands accepted by
    design — the asking human IS the gate."""
    db = tmp_path / "store"
    store = Store(db)
    _decision(store, "human wrote this directly", source="human")
    assert _findings(db) == []


def test_proposed_agent_record_is_not_flagged(tmp_path):
    db = tmp_path / "store"
    store = Store(db)
    _decision(store, "waiting in the queue", source="agent", status=DecisionStatus.PROPOSED)
    assert _findings(db) == []


def test_finding_wording_says_it_cannot_distinguish_legacy_records(tmp_path):
    """The signature is shared with records ratified before the stamp existed. The finding
    must say so rather than accuse — an advisory that overstates gets ignored."""
    db = tmp_path / "store"
    store = Store(db)
    _decision(store, "properly ratified earlier", source="agent", stamped=True)
    _decision(store, "ambiguous", source="agent")
    detail = _findings(db)[0].detail
    assert "before the ratifier stamp existed" in detail


def test_legacy_store_with_no_marker_and_no_stamps_produces_nothing(tmp_path):
    """The wall that matters most (doctor-blind-window fix, 2026-09-15): measured before
    shipping the original check — unscoped, it emitted 239 findings on this project's own
    store, every agent record predating the feature. A store that predates the store-side
    creation marker too (this test's setup) and has never ratified anything still produces
    zero findings, exactly like before that marker existed.

    Simulated the way a real pre-marker store looks to this code: the marker is deleted
    BEFORE any record is written, so by the time these stamp-less agent records land, the
    store looks exactly like one an older version created and populated — records
    present, marker absent, no stamps anywhere."""
    db = tmp_path / "store"
    store = Store(db)
    (db / _STAMPING_MARKER_NAME).unlink()
    for i in range(5):
        _decision(store, f"legacy {i}", source="agent")
    assert _findings(db) == []


def test_new_store_with_never_ratified_agent_accepts_is_flagged(tmp_path):
    """The bug this fix closes (doctor-blind-window, 2026-09-15): a store adopting an
    auto-ratification policy from scratch has never ratified anything, so the original
    stamp-only scoping rule left it invisible to this check FOREVER — even after a later
    ratification finally armed the check, every record already there predated that first
    stamp. The store's own creation marker (``stamping_live_since``, present here since
    this ``Store(db)`` is genuinely new) gives the check a scope-start that does not
    depend on any ratification ever having happened, so these stamp-less agent accepts —
    created after the store came into being, never ratified — are exactly what the check
    exists to catch."""
    db = tmp_path / "store"
    store = Store(db)
    for i in range(5):
        _decision(store, f"never ratified {i}", source="agent")
    hits = _findings(db)
    assert len(hits) == 5


def test_records_older_than_the_first_stamp_are_skipped(tmp_path):
    """Once stamping is observably in use, only records created after it are judgeable."""
    from datetime import timedelta

    db = tmp_path / "store"
    store = Store(db)
    old = _decision(store, "legacy agent record", source="agent")
    old.valid_from = datetime.now(UTC) - timedelta(days=60)
    store._write_decision(old)
    stamped = _decision(store, "properly ratified", source="agent", stamped=True)
    stamped.valid_from = datetime.now(UTC) - timedelta(days=30)
    store._write_decision(stamped)
    store._conn.commit()
    assert _findings(db) == []


def test_marker_extends_coverage_to_a_gap_before_the_first_ratification(tmp_path):
    """Scope start is the EARLIER of (marker, earliest stamp) when both exist (doctor-
    blind-window fix) — a store created new and only later ratified something must not
    lose coverage of its OWN first window: the gap between the store's creation and its
    first-ever ratification.

    Without folding the marker in (the pre-existing, stamp-only rule), scope would start
    at the first ratification and this gap record — created before any stamp exists, but
    still after the store itself came into being — would be silently skipped as
    "predating observable stamping", exactly the blind window this fix closes."""
    db = tmp_path / "store"
    store = Store(db)
    gap = _decision(store, "auto-accepted before the first ratification", source="agent")
    _decision(store, "the store's first-ever ratification", source="agent", stamped=True)
    hits = _findings(db)
    assert len(hits) == 1
    assert gap.id in hits[0].path


# -- Fix round 1 (2026-09-15): supersession successors are not SIDEGRAPH_AUTO_ACCEPT ------


def test_decision_supersession_successor_is_not_flagged(tmp_path):
    """RED pre-fix: measured live on this project's own store — both of the two findings
    `sidegraph-doctor` currently reports are `supersede_decision` successors, one written
    the same day this fix landed. A successor lands `accepted`, with
    `provenance.source` that CAN legitimately be `"agent"` (`supersede_decision`'s own
    docstring names "a future supersede-from-neighbors flow" as an agent-initiated
    caller — and every real successor in this project's own store today already IS
    `source="agent"`), and never goes through the ratify queue at all — supersession has
    no queue to bypass, so this is not `SIDEGRAPH_AUTO_ACCEPT=on`'s signature no matter how
    it looks. Before this fix, that reproduces the check's exact signature on every
    superseding record from a new store's first day onward."""
    db = tmp_path / "store"
    store = Store(db)
    predecessor = _decision(store, "predecessor", source="agent", stamped=True)
    _decision(store, "successor", source="agent", supersedes=predecessor.id)
    assert _findings(db) == []


def test_fact_supersession_successor_is_not_flagged(tmp_path):
    """Same shape as the decision test above, for `supersede_fact` — the check reads
    decisions and facts through the identical loop, so the exclusion must hold for both."""
    db = tmp_path / "store"
    store = Store(db)
    predecessor = _fact(store, "predecessor fact", source="agent", stamped=True)
    _fact(store, "successor fact", source="agent", supersedes=predecessor.id)
    assert _findings(db) == []


def test_non_superseding_stamp_less_accept_still_flagged_beside_a_supersession(tmp_path):
    """Guard: the supersession exclusion must not blanket-suppress the check. An ordinary
    stamp-less agent accept sitting right beside a legitimate supersession successor is
    still exactly the signature this check exists to catch."""
    db = tmp_path / "store"
    store = Store(db)
    predecessor = _decision(store, "predecessor", source="agent", stamped=True)
    _decision(store, "legitimate successor", source="agent", supersedes=predecessor.id)
    culprit = _decision(store, "auto-accepted, not a supersession", source="agent")
    hits = _findings(db)
    assert len(hits) == 1
    assert culprit.id in hits[0].path


# -- Fix round 2 (2026-09-15, review Major 1): a supersession successor carrying a
# propose-only signal (layer / provenance.ref / a NEW tier-0 tag or initiative binding) is
# NOT a supersede_decision/supersede_fact successor -- neither tool can produce any of the
# three -- so it must still be flagged. RED against fix round 1's blanket `supersedes`
# exclusion, confirmed by running these against a scratchpad extract of commit 1964cd6
# (fix round 1's HEAD) before writing the round-2 fix.


def test_superseding_decision_with_layer_is_still_flagged(tmp_path):
    """`supersede_decision` has no `layer` parameter at all -- `layer` set on a
    `supersedes`-bearing record can only have come from the propose/auto_accept path."""
    db = tmp_path / "store"
    store = Store(db)
    predecessor = _decision(store, "predecessor", source="agent", stamped=True)
    successor = _decision(
        store,
        "auto-accepted superseding draft with a layer",
        source="agent",
        supersedes=predecessor.id,
        layer="technical",
    )
    hits = _findings(db)
    assert len(hits) == 1
    assert successor.id in hits[0].path


def test_superseding_decision_with_ref_is_still_flagged(tmp_path):
    """`supersede_decision` never sets `provenance.ref` (no parameter for it) -- present on
    a `supersedes`-bearing record, it can only have come from the propose path."""
    db = tmp_path / "store"
    store = Store(db)
    predecessor = _decision(store, "predecessor", source="agent", stamped=True)
    successor = _decision(
        store,
        "auto-accepted superseding draft with a ref",
        source="agent",
        supersedes=predecessor.id,
        ref="design/some-doc.md",
    )
    hits = _findings(db)
    assert len(hits) == 1
    assert successor.id in hits[0].path


def test_superseding_decision_with_a_new_tag_is_still_flagged(tmp_path):
    """`supersede_decision` either inherits the predecessor's bindings verbatim (anchors
    omitted) or resolves explicit anchors with no `tags`/`initiative` parameter to mint a
    fresh tier-0 binding from. A tier-0 `tag:` binding on the successor that the
    predecessor does NOT have can only have come from the propose path's own tag-binding
    step (`capture._propose_one`)."""
    db = tmp_path / "store"
    store = Store(db)
    predecessor = _decision(store, "predecessor", source="agent", stamped=True)
    successor = _decision(
        store,
        "auto-accepted superseding draft with a new tag",
        source="agent",
        supersedes=predecessor.id,
    )
    _bind_tag(store, successor.id, "some-new-tag")
    hits = _findings(db)
    assert len(hits) == 1
    assert successor.id in hits[0].path


def test_superseding_decision_with_an_inherited_tag_is_not_flagged(tmp_path):
    """Guard: the propose-only-signal check must compare against the PREDECESSOR, not fire
    on any tag at all. `supersede_decision`'s anchor-inheritance path copies the
    predecessor's bindings verbatim, including any tag it already carried -- that is not a
    NEW binding, so it must not be mistaken for the propose path's signature."""
    db = tmp_path / "store"
    store = Store(db)
    predecessor = _decision(store, "predecessor", source="agent", stamped=True)
    _bind_tag(store, predecessor.id, "inherited-tag")
    successor = _decision(
        store,
        "legitimate successor inheriting the predecessor's tag",
        source="agent",
        supersedes=predecessor.id,
    )
    _bind_tag(store, successor.id, "inherited-tag")  # same slug -- inherited, not new
    assert _findings(db) == []
