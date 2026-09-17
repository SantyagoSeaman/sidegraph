"""``sidegraph-doctor``'s D5 informational lines: ``auto share`` and ``auto supersede
rate`` (spec ledger row T8), plus the ``time-to-ratify`` auto-stamp exclusion that ships
in the same change.

Spec: design/superpowers/specs/2026-09-11-auto-ratification-policy-design.md D5, T8. Both
lines read hot files PLUS ``archive/*.jsonl`` through one shared read-only reader
(``doctor._hot_plus_archive_records``) — never a ``Finding``, never affects the exit
code, absent from ``--json`` (key set pinned, ``tests/test_cli_doctor.py:118-127``).

Red target at HEAD: both lines absent from doctor's human output, and ``time-to-ratify``
still counts ``auto:*`` stamps.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sidegraph.capture import RatifyPolicy, _auto_ratify
from sidegraph.cli import doctor_main
from sidegraph.doctor import UNRATIFIED_ACCEPT, curate
from sidegraph.schema import (
    AnchorBinding,
    Decision,
    DecisionKind,
    DecisionStatus,
    Descriptor,
    Domain,
    Entity,
    Provenance,
)
from sidegraph.store import Store


def _fake_ratifier_identity(actor: str | None = None) -> str:
    """Stand-in for ``sidegraph.store._ratifier_identity`` (never calls out to git):
    an explicit ``actor`` (the auto-ratify stamp) still wins outright, matching the real
    function's contract -- only the git-lookup fallback for a human ratify is faked."""
    return actor if actor else "a human"


def _bind(store: Store, record_id: str, name: str) -> None:
    """The ``tests/test_cli_doctor.py:24-43`` pattern: a fresh entity plus a tier-2
    binding, so an otherwise-open decision doesn't trip ``dangling-record``."""
    e = store.upsert_entity(
        Entity(canonical_name=name, descriptor=Descriptor(name=name, file_path=f"{name}.py"))
    )
    store.add_binding(AnchorBinding(record_id=record_id, entity_id=e.entity_id, tier=2))


def _decision_draft(
    title: str,
    valid_from: datetime,
    *,
    source: str = "agent",
    status: DecisionStatus = DecisionStatus.PROPOSED,
) -> Decision:
    return Decision(
        title=title,
        kind=DecisionKind.GOTCHA,
        status=status,
        context="c",
        choice="ch",
        valid_from=valid_from,
        provenance=Provenance(source=source),
    )


def _t8_fixture(store: Store, monkeypatch) -> dict[str, Decision]:
    """5-decision-row T8 fixture (spec T8; pre-flight review Q6, dispatch context
    amendment 7): H1/H2 ratified via the real ``store.ratify`` transition (human), A1/A2
    auto-ratified via ``_auto_ratify`` (the production stamp origin, ``capture.py:317``),
    A2 later superseded by a human successor S. Every open row (H1, H2, A1, S) is bound
    to its own entity so the store stays advisory-clean.

    H1/H2 use DISTINCT latencies (3d/5d), never equal ones — Q8's red-target trap: equal
    human latencies make the ``time-to-ratify`` median assert vacuous (``[0,0,3,3]``
    medians to 3 whether or not auto stamps are excluded).

    Timing trap (Q6): S's ``valid_from`` is captured AFTER every ratify call above. A
    ``valid_from`` captured before them would misclassify S as legacy-human instead of
    excluded and shift every pinned number.
    """
    monkeypatch.setattr("sidegraph.store._ratifier_identity", _fake_ratifier_identity)
    now = datetime.now(UTC)
    h1 = store.add_decision(_decision_draft("h1 three days", now - timedelta(days=3)))
    h2 = store.add_decision(_decision_draft("h2 five days", now - timedelta(days=5)))
    a1 = store.add_decision(_decision_draft("a1 eligible", now))
    a2 = store.add_decision(_decision_draft("a2 superseded later", now))
    for d, name in ((h1, "h1-anchor"), (h2, "h2-anchor"), (a1, "a1-anchor"), (a2, "a2-anchor")):
        _bind(store, d.id, name)

    store.ratify(h1.id)
    store.ratify(h2.id)
    _auto_ratify(store, a1.id, "gotcha", RatifyPolicy.AUTO_ALL)
    _auto_ratify(store, a2.id, "gotcha", RatifyPolicy.AUTO_ALL)

    s = store.add_decision(
        Decision(
            title="s supersedes a2",
            kind=DecisionKind.GOTCHA,
            status=DecisionStatus.ACCEPTED,
            context="c",
            choice="ch",
            supersedes=a2.id,
            valid_from=datetime.now(UTC),  # after every ratify call above -- the Q6 trap
            provenance=Provenance(source="human"),
        )
    )
    _bind(store, s.id, "s-anchor")
    return {"h1": h1, "h2": h2, "a1": a1, "a2": a2, "s": s}


def _run_doctor(db: Path, capsys, *extra_args: str) -> tuple[int, str]:
    capsys.readouterr()  # discard whatever an earlier call in this test already printed
    code = doctor_main(["--db", str(db), *extra_args])
    return code, capsys.readouterr().out


def _find_d5_lines(out: str) -> tuple[str | None, str | None]:
    share = next((line for line in out.splitlines() if line.startswith("auto share:")), None)
    rate = next(
        (line for line in out.splitlines() if line.startswith("auto supersede rate:")), None
    )
    return share, rate


def _snapshot(root: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()
    }


# -- T8: pinned strings, string-equal before and after compact -----------------------------


def test_t8_share_and_rate_lines_pinned_before_and_after_compact(tmp_path, capsys, monkeypatch):
    """Spec T8 (design D5, Ruling Z's exact strings): the 5-row fixture reports auto
    share 50% (ever-auto-ratified over all classified, rev 10) and auto-supersede 50% vs
    human 0%, string-equal before and after ``store.compact()`` — a hot-only reader would
    shift the numbers on compact (amendment 9 proves that red target directly). Red
    target at HEAD: both D5 lines absent from doctor's human output.

    m4: the ``CompactReport`` is asserted to have actually archived A2, so the
    before/after equality is not vacuously true (e.g. because a future default made
    ``compact()`` a no-op for this fixture)."""
    db = tmp_path / "store"
    store = Store(db)
    _t8_fixture(store, monkeypatch)

    _, out_before = _run_doctor(db, capsys)
    share_before, rate_before = _find_d5_lines(out_before)
    assert share_before == (
        "auto share: decisions 2/4 (50%), facts n/a, domains n/a "
        "(1 stamp-less record(s) after the first stamp excluded)"
    )
    assert rate_before == (
        "auto supersede rate: decisions+facts superseded 1/2 (50%) vs human 0/2 (0%); "
        "domains retired n/a vs human n/a "
        "(1 stamp-less record(s) after the first stamp excluded)"
    )

    report = store.compact()
    assert report.decisions_compacted == 1  # non-vacuous: A2 was actually archived

    _, out_after = _run_doctor(db, capsys)
    share_after, rate_after = _find_d5_lines(out_after)
    assert share_after == share_before
    assert rate_after == rate_before


def test_t8_time_to_ratify_excludes_auto_stamps(tmp_path, capsys, monkeypatch):
    """In the same change (D5): the existing ``time-to-ratify`` median excludes
    ``auto:*`` stamps — otherwise auto's ``ratified_at ~= valid_from`` collapses it
    toward 0 and it stops measuring human queue latency. Red target at HEAD (unfixed,
    measured): ``median 3 days, max 5 days (4 stamped record(s))`` — A1/A2's near-zero
    auto latencies pull two more samples into the set. Fixed: only H1(3d)/H2(5d) remain,
    so ``latencies[len // 2]`` on a 2-element sorted list lands on the larger one."""
    db = tmp_path / "store"
    store = Store(db)
    _t8_fixture(store, monkeypatch)

    _, out = _run_doctor(db, capsys)
    assert "time-to-ratify: median 5 days, max 5 days (2 stamped record(s))" in out


# -- amendment 9: the hot-only reader is a declared red target, proved by mutation in the --
# checkout (Ruling C discipline; see task-6-report.md for the transcript, restored before --
# commit). No permanent test here — the mutation is transient and never lands.


# -- T5 doctor half + Q7 control -------------------------------------------------------------


def test_t8_fixture_raises_no_unratified_accept_finding(tmp_path, monkeypatch):
    """T5 doctor half, on the T8 builder: humans ratified via the real ``store.ratify*``
    transitions (stamps present — the check is ARMED, ``doctor.py:944-945`` returns
    ``[]`` only on a stamp-less store), auto records carry the ``auto:auto-all`` stamp.
    Green at HEAD (``doctor.py:954`` skips any record with ``ratified_at`` set) — a
    declared guard, not a red target."""
    db = tmp_path / "store"
    store = Store(db)
    _t8_fixture(store, monkeypatch)
    hits = [f for f in curate(db).findings if f.code == UNRATIFIED_ACCEPT]
    assert hits == []


def test_t8_fixture_control_stamp_less_record_is_flagged(tmp_path, monkeypatch):
    """Q7 control: an identical stamp-less, agent-sourced, ACCEPTED record added AFTER
    every ratify call in the T8 fixture DOES raise ``unratified-accept`` — proving the
    "no finding on auto" result above is not vacuous. Its ``valid_from`` is captured
    AFTER the fixture's ratify calls, so it is provably armed; a control captured before
    them would be silent (``doctor.py:957``) — the vacuous form this test avoids. Kept in
    its own test (not the T8 fixture test above) so the D5 numbers there stay clean of
    this extra excluded record."""
    db = tmp_path / "store"
    store = Store(db)
    _t8_fixture(store, monkeypatch)
    ctrl = store.add_decision(
        Decision(
            title="auto-shaped, no stamp",
            kind=DecisionKind.GOTCHA,
            status=DecisionStatus.ACCEPTED,
            context="c",
            choice="ch",
            valid_from=datetime.now(UTC),  # after every ratify call above -- ARMED
            provenance=Provenance(source="agent"),
        )
    )
    hits = [f for f in curate(db).findings if f.code == UNRATIFIED_ACCEPT]
    assert len(hits) == 1
    assert ctrl.id in hits[0].path


# -- manual store: neither line prints --------------------------------------------------------


def test_manual_store_with_no_auto_stamp_prints_neither_d5_line(tmp_path, capsys, monkeypatch):
    """A store with human stamps and zero ``auto:`` stamps prints neither D5 line — the
    print gate keeps a manual deployment's doctor output byte-identical (D4: "manual
    deployments see zero render diff")."""
    db = tmp_path / "store"
    store = Store(db)
    monkeypatch.setattr("sidegraph.store._ratifier_identity", _fake_ratifier_identity)
    d = store.add_decision(_decision_draft("human ratified", datetime.now(UTC)))
    _bind(store, d.id, "human-anchor")
    store.ratify(d.id)

    _, out = _run_doctor(db, capsys)
    assert "auto share" not in out
    assert "auto supersede rate" not in out


# -- m1: the print gate must use the classifier's own "auto" predicate ----------------------


def test_print_gate_requires_the_classifiers_auto_predicate_not_ratified_by_alone(
    tmp_path, capsys, monkeypatch
):
    """m1: the print gate must require the SAME predicate the classifier uses -- a
    parseable ``ratified_at`` AND an ``auto:``-prefixed ``ratified_by`` -- not
    ``ratified_by`` alone. Red shape (today; chosen because an otherwise stamp-less
    store can't discriminate -- both the buggy and the fixed gate print nothing for
    that): a store with one genuine human stamp (so ``min()`` over ``ratified_at``
    succeeds) PLUS a hand-edited canonical decision carrying
    ``ratified_by="auto:auto-all"`` and NO ``ratified_at`` (a shape the real ratify
    transitions never produce -- the two fields are always set together, e.g.
    ``store.py:2568-2569``). Before the fix, the old ``any(rb.startswith("auto:"))``
    gate opens on the hand-edited record alone and the lines print
    ``decisions 0/1 (0%)``, describing a store the classifier itself considers
    auto-free (the hand-edited record is PROPOSED, so the classifier ignores it
    entirely -- neither auto nor human). Fixed: neither line prints, and
    ``doctor_main`` still exits 0."""
    db = tmp_path / "store"
    store = Store(db)
    monkeypatch.setattr("sidegraph.store._ratifier_identity", _fake_ratifier_identity)
    d = store.add_decision(_decision_draft("human ratified", datetime.now(UTC)))
    _bind(store, d.id, "human-anchor")
    store.ratify(d.id)

    corrupt = store.add_decision(_decision_draft("hand-edited, never ratified", datetime.now(UTC)))
    path = db / "decisions" / f"{corrupt.id}.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["ratified_at"] is None  # sanity: real transitions never leave this gap
    payload["ratified_by"] = "auto:auto-all"
    path.write_text(json.dumps(payload), encoding="utf-8")

    _, out = _run_doctor(db, capsys)
    assert "auto share" not in out
    assert "auto supersede rate" not in out
    assert doctor_main(["--db", str(db)]) == 0


# -- domain group: two retirement transitions, both count (Ruling Z, amendments 16/16b) -----


def test_domain_group_drop_counts_toward_domains_retired(tmp_path, capsys):
    """Amendment 16: an auto-accepted domain retired via ``ratify_domains(drop=...)``
    counts toward the domains group — ``domains 1/1 (100%)`` in share, ``domains retired
    1/1 (100%)`` in rate. Label is "domains retired" per Ruling Z (a domain retires via
    EITHER ``drop`` OR ``supersede_domain`` — both count; not the earlier draft's
    "domains dropped"). I1: the line is also asserted string-equal AFTER
    ``store.compact()`` archives the dropped domain — a hot-only reader would silently
    drop this domain from the count on compact (the domain half of amendment 9's mutant,
    which 32 tests missed before this clause existed)."""
    db = tmp_path / "store"
    store = Store(db)
    dom = store.add_domain(
        Domain(
            slug="trading",
            title="Trading",
            summary="Order execution path.",
            provenance=Provenance(source="agent"),
        )
    )
    store.ratify_domains(accept=[dom.domain_id], actor="auto:auto-all")
    store.ratify_domains(drop=[dom.domain_id])

    _, out_before = _run_doctor(db, capsys)
    share_before, rate_before = _find_d5_lines(out_before)
    assert share_before is not None and "domains 1/1 (100%)" in share_before
    assert rate_before is not None and "domains retired 1/1 (100%) vs human n/a" in rate_before

    report = store.compact()
    assert report.domains_compacted == 1  # non-vacuous: something was actually archived

    _, out_after = _run_doctor(db, capsys)
    share_after, rate_after = _find_d5_lines(out_after)
    assert share_after == share_before
    assert rate_after == rate_before


def test_domain_group_supersede_domain_counts_toward_domains_retired(tmp_path, capsys):
    """Amendment 16b: an auto-accepted domain retired through ``Store.supersede_domain``
    (D5's OTHER retirement transition, not just ``ratify_domains(drop=...)``) also counts
    — ``domains retired 1/1 (100%)``. The successor (proposed, unstamped) is ignored, not
    counted in either denominator. I1: the line is also asserted string-equal AFTER
    ``store.compact()`` archives the superseded domain (the unstamped successor stays
    proposed, so it is never compacted) — a hot-only reader would silently drop this
    domain from the count on compact."""
    db = tmp_path / "store"
    store = Store(db)
    dom = store.add_domain(
        Domain(
            slug="trading",
            title="Trading",
            summary="Order execution path.",
            provenance=Provenance(source="agent"),
        )
    )
    store.ratify_domains(accept=[dom.domain_id], actor="auto:auto-all")
    successor = Domain(
        slug="trading",
        title="Trading (rescoped)",
        summary="Order execution path, rescoped by a human.",
        supersedes=dom.domain_id,
        provenance=Provenance(source="human"),
    )
    store.supersede_domain(dom.domain_id, successor)

    _, out_before = _run_doctor(db, capsys)
    share_before, rate_before = _find_d5_lines(out_before)
    assert share_before is not None and "domains 1/1 (100%)" in share_before
    assert rate_before is not None and "domains retired 1/1 (100%) vs human n/a" in rate_before

    report = store.compact()
    assert report.domains_compacted == 1  # non-vacuous: something was actually archived

    _, out_after = _run_doctor(db, capsys)
    share_after, rate_after = _find_d5_lines(out_after)
    assert share_after == share_before
    assert rate_after == rate_before


# -- Fix round 1 Minor 2: the human-stamped retired branch had no pin ------------------------


def test_human_retired_decision_counts_toward_supersede_rate(tmp_path, capsys, monkeypatch):
    """Wave-1 fix round 1, review's Minor 2: the ``_tally_kind`` refactor (item B) split
    one shared ``*_retired`` increment into an explicit auto/human branch; ``_t8_fixture``
    above never retires a HUMAN-stamped decision (H1/H2 are ratified but never superseded,
    "vs human 0/2" in every T8 assertion), so an in-memory mutant that swaps the human
    branch's retired increment for the auto one survived all 141 pre-existing tests in this
    file plus ``test_ratify_policy.py`` untouched. This is a minimal, standalone fixture
    (not an addition to ``_t8_fixture``, which would re-pin its six existing exact-string
    assertions) exercising exactly that branch: one auto-ratified decision only to open the
    D5 print gate (a store with zero ``auto:`` stamps prints neither line, see
    ``test_manual_store_with_no_auto_stamp_prints_neither_d5_line`` above), and one
    human-ratified decision later superseded by a human successor.

    RED before the fix — genuine evidence, not a guard: confirmed against an in-memory
    mutant of ``_tally_kind``'s human branch (the M2 mutant from the wave-1 review),
    swapped in via a pytest plugin against a scratchpad copy, never the checkout
    (``PYTHONDONTWRITEBYTECODE=1``) — the mutant makes this assert
    ``decisions+facts superseded 1/1 (100%) vs human 0/1 (0%)`` instead. See
    ``wave-1-report.md``'s "Fix round 1" section for the transcript.

    Timing trap (mirrors ``_t8_fixture``'s own Q6 note above): the successor's
    ``valid_from`` is captured AFTER the human ``ratify`` call, so it lands in "excluded"
    rather than being miscounted as a second legacy-human record."""
    db = tmp_path / "store"
    store = Store(db)
    monkeypatch.setattr("sidegraph.store._ratifier_identity", _fake_ratifier_identity)
    now = datetime.now(UTC)

    auto = store.add_decision(_decision_draft("auto eligible, only to open the gate", now))
    _bind(store, auto.id, "auto-anchor")
    _auto_ratify(store, auto.id, "gotcha", RatifyPolicy.AUTO_ALL)

    pred = store.add_decision(_decision_draft("human retired predecessor", now))
    _bind(store, pred.id, "human-pred-anchor")
    store.ratify(pred.id)

    successor = _decision_draft(
        "human successor",
        datetime.now(UTC),  # after the ratify call above -- the Q6 timing trap
        source="human",
        status=DecisionStatus.ACCEPTED,
    ).model_copy(update={"supersedes": pred.id})
    added_successor = store.add_decision(successor)
    _bind(store, added_successor.id, "human-successor-anchor")

    _, out = _run_doctor(db, capsys)
    _, rate = _find_d5_lines(out)
    assert rate is not None
    assert "decisions+facts superseded 0/1 (0%) vs human 1/1 (100%)" in rate


# -- legacy scope (Ruling B2 / amendment 17) --------------------------------------------------


def test_legacy_unstamped_records_count_as_human_only_before_first_stamp(
    tmp_path, capsys, monkeypatch
):
    """Ruling B2 / amendment 17: a stamp-less accepted/superseded decision created BEFORE
    the store's earliest stamp counts as legacy-human (a numerator hit when superseded).
    The identical shape created AFTER the first stamp lands in "excluded" instead (m3:
    both the accepted AND the superseded shape, not just the accepted one).

    m5: every superseded row is written the CANONICAL way -- a real successor via
    ``supersedes=``, never a hand-set ``status=SUPERSEDED`` with no successor (that shape
    is a strict violation, ``superseded-without-successor``, and `doctor_main` would
    exit 2 without this test ever checking). ``doctor_main([...]) == 0`` at the end pins
    the fixture's validity, not just its D5 numbers.
    """
    db = tmp_path / "store"
    store = Store(db)
    now = datetime.now(UTC)

    legacy_accepted = store.add_decision(
        _decision_draft(
            "legacy accepted", now - timedelta(days=100), status=DecisionStatus.ACCEPTED
        )
    )
    legacy_superseded_pred = store.add_decision(
        _decision_draft(
            "legacy superseded", now - timedelta(days=100), status=DecisionStatus.ACCEPTED
        )
    )
    legacy_successor = store.add_decision(
        _decision_draft(
            "legacy successor",
            now - timedelta(days=95),
            source="human",
            status=DecisionStatus.ACCEPTED,
        ).model_copy(update={"supersedes": legacy_superseded_pred.id})
    )

    eligible = store.add_decision(_decision_draft("auto eligible", now))
    _auto_ratify(store, eligible.id, "gotcha", RatifyPolicy.AUTO_ALL)  # the first stamp

    post_stamp_accepted = store.add_decision(
        _decision_draft("post-stamp stamp-less", datetime.now(UTC), status=DecisionStatus.ACCEPTED)
    )
    post_stamp_superseded_pred = store.add_decision(
        _decision_draft("post-stamp superseded", datetime.now(UTC), status=DecisionStatus.ACCEPTED)
    )
    post_stamp_successor = store.add_decision(
        _decision_draft(
            "post-stamp successor",
            datetime.now(UTC),
            source="human",
            status=DecisionStatus.ACCEPTED,
        ).model_copy(update={"supersedes": post_stamp_superseded_pred.id})
    )

    for d, name in (
        (legacy_accepted, "legacy-accepted-anchor"),
        (legacy_successor, "legacy-successor-anchor"),
        (eligible, "eligible-anchor"),
        (post_stamp_accepted, "post-stamp-accepted-anchor"),
        (post_stamp_successor, "post-stamp-successor-anchor"),
    ):
        _bind(store, d.id, name)

    _, out = _run_doctor(db, capsys)
    share, rate = _find_d5_lines(out)
    assert share is not None
    assert rate is not None
    # auto=1 (eligible); human=3 (legacy_accepted, legacy_superseded_pred [retired],
    # legacy_successor) -> share 1/4 (25%). excluded=3 (post_stamp_accepted,
    # post_stamp_superseded_pred, post_stamp_successor -- ALL created after the first
    # stamp, regardless of the superseded one's retired status: m3).
    assert "decisions 1/4 (25%)" in share
    assert "(3 stamp-less record(s) after the first stamp excluded)" in share
    # legacy_superseded_pred is the only retired human-baseline record -> human 1/3 (33%).
    assert "decisions+facts superseded 0/1 (0%) vs human 1/3 (33%)" in rate

    assert doctor_main(["--db", str(db)]) == 0  # m5: the fixture is a store verify accepts


# -- never writes -------------------------------------------------------------------------


def test_doctor_never_writes_on_the_auto_share_path(tmp_path, monkeypatch):
    """The new D5 read path is a pure helper: no ``set_meta``, no index write, no file
    write. Snapshots every file under the store (``archive/`` and ``index.db`` included)
    around a COMPACTED T8 fixture, exercising both the hot and the archive halves of the
    new reader."""
    db = tmp_path / "store"
    store = Store(db)
    _t8_fixture(store, monkeypatch)
    store.compact()

    before = _snapshot(db)
    doctor_main(["--db", str(db)])
    doctor_main(["--db", str(db), "--check", "--json"])
    assert _snapshot(db) == before


# -- --json / exit-code pins ----------------------------------------------------------------


def test_json_key_set_and_exit_code_unaffected_by_auto_stamps(tmp_path, capsys, monkeypatch):
    """``--json`` output key set stays exactly ``{clean, violations, findings, skipped}``
    and the exit code is unaffected by auto stamps being present. The T8 fixture is
    advisory-clean (every open decision bound to its own entity), so a healthy store
    carrying auto records still exits 0 and reports ``clean``."""
    db = tmp_path / "store"
    store = Store(db)
    _t8_fixture(store, monkeypatch)

    assert doctor_main(["--db", str(db)]) == 0
    assert doctor_main(["--db", str(db), "--check"]) == 0
    capsys.readouterr()
    assert doctor_main(["--db", str(db), "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)  # pure JSON: parse the WHOLE stdout
    assert set(doc) == {"clean", "violations", "findings", "skipped"}
    assert doc["clean"] is True
