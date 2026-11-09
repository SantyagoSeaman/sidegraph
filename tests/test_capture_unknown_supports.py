"""An unknown ``supports`` id is a fixable refusal, never "the record may be on disk".

# see design/superpowers/specs/2026-11-07-capture-unknown-supports-refusal-design.md
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from sidegraph import capture
from sidegraph.schema import Decision, DecisionKind, DecisionStatus, Fact, Provenance
from sidegraph.store import Store

MISSING = "01ZZZZZZZZZZZZZZZZZZZZZZZZ"
MISSING_B = "01YYYYYYYYYYYYYYYYYYYYYYYY"
ANCHOR = [dict(name="sample.py")]
MAY_BE_ON_DISK = "may be on disk"
TERMINAL_TEXT = "resolve only to a superseded"


def _decision(store: Store, *, supersedes: str | None = None) -> Decision:
    return store.add_decision(
        Decision(
            title="a decision",
            kind=DecisionKind.ADR,
            status=DecisionStatus.ACCEPTED,
            context="c",
            choice="ch",
            valid_from=datetime.now(UTC),
            supersedes=supersedes,
            provenance=Provenance(source="manual"),
        )
    )


def _live(store: Store) -> Decision:
    return _decision(store)


def _terminal(store: Store) -> Decision:
    old = _decision(store)
    _decision(store, supersedes=old.id)
    assert store.get_decision(old.id).status == DecisionStatus.SUPERSEDED
    return old


def _canonical(path):
    return {
        str(p.relative_to(path)): p.read_bytes()
        for p in sorted(path.rglob("*"))
        if p.is_file() and p.suffix in {".json", ".shard", ".jsonl"}
    }


def _fact_count(path) -> int:
    with Store(path) as reopened:
        return len(list(reopened.iter_facts()))


def _draft(supports, anchors=None, **updates):
    raw = dict(statement="observed", source="probe", supports=list(supports))
    if anchors is not None:
        raw["anchors"] = anchors
    raw.update(updates)
    return raw


def test_store_add_fact_raises_typed_refusal_naming_every_position(tmp_path):
    from sidegraph.store import UnknownSupportError

    path = tmp_path / "store"
    with Store(path) as store:
        live = _live(store)
        before = _canonical(path)
        fact = Fact(
            statement="s",
            source="probe",
            supports=[live.id, MISSING, MISSING_B],
            valid_from=datetime.now(UTC),
            provenance=Provenance(source="manual"),
        )
        with pytest.raises(UnknownSupportError) as caught:
            store.add_fact(fact)
        assert caught.value.positions == (1, 2)
        assert isinstance(caught.value, ValueError)
        assert MISSING not in str(caught.value) and MISSING_B not in str(caught.value)
        assert "1, 2" in str(caught.value)
        assert _canonical(path) == before
        assert list(store.iter_facts()) == []
    assert _fact_count(path) == 0


# (label, supports builder, anchored, expected) -- expected: "unknown" | "terminal" | "written"
MATRIX = [
    ("only missing", lambda s: [MISSING], True, "unknown"),
    ("only missing", lambda s: [MISSING], False, "unknown"),
    ("only terminal", lambda s: [_terminal(s).id], True, "written"),
    ("only terminal", lambda s: [_terminal(s).id], False, "terminal"),
    ("only live", lambda s: [_live(s).id], True, "written"),
    ("only live", lambda s: [_live(s).id], False, "written"),
    ("live + missing", lambda s: [_live(s).id, MISSING], True, "unknown"),
    ("live + missing", lambda s: [_live(s).id, MISSING], False, "unknown"),
    ("terminal + missing", lambda s: [_terminal(s).id, MISSING], True, "unknown"),
    ("terminal + missing", lambda s: [_terminal(s).id, MISSING], False, "unknown"),
]


@pytest.mark.parametrize(
    "label,build,anchored,expected",
    MATRIX,
    ids=[f"{m[0]}-{'anchor' if m[2] else 'no-anchor'}" for m in MATRIX],
)
def test_propose_facts_matrix(tmp_path, label, build, anchored, expected):
    path = tmp_path / "store"
    with Store(path) as store:
        supports = build(store)
        raw = _draft(supports, ANCHOR if anchored else None)
        before = _canonical(path)
        [result] = capture.propose_facts([raw], store, None)
        if expected == "written":
            assert result.status in {"written", "proposed"}
            return
        assert result.status == "rejected"
        assert MAY_BE_ON_DISK not in result.reason
        assert _canonical(path) == before
        assert list(store.iter_facts()) == []
        if expected == "terminal":
            assert TERMINAL_TEXT in result.reason
        else:
            position = next(i for i, sid in enumerate(supports) if sid == MISSING)
            assert result.reason.startswith(
                f"invalid draft: supports[{position}]: unknown_decision;"
            )
            assert "Nothing was written for this fact" in result.reason
            assert MISSING not in result.reason
        # nothing is on disk for the refused row: a reopened store shows 0 facts BEFORE any
        # retry, while this Store stays live for the corrected write
        assert _fact_count(path) == 0
        if expected == "unknown":
            fixed = [sid if sid != MISSING else _live(store).id for sid in supports]
            [retry] = capture.propose_facts(
                [_draft(fixed, ANCHOR if anchored else None)], store, None
            )
            assert retry.status in {"written", "proposed"}
    if expected == "unknown":
        assert _fact_count(path) == 1


def test_attached_fact_names_draft_position_and_the_written_parent(tmp_path):
    path = tmp_path / "store"
    with Store(path) as store:
        raw = dict(
            title="parent",
            kind="lesson",
            context="context",
            choice="choice",
            facts=[dict(statement="observed", source="probe", supports=[MISSING], anchors=ANCHOR)],
        )
        [result] = capture.propose([raw], store, None)
        assert result.status == "written"
        child = result.facts[0]
        assert child.status == "rejected"
        assert child.reason.startswith("invalid draft: supports[0]: unknown_decision;")
        assert result.decision_id in child.reason
        assert "was written" in child.reason
        assert "supports=[" + result.decision_id in child.reason
        assert MISSING not in child.reason
        assert MAY_BE_ON_DISK not in child.reason
    with Store(path) as reopened:
        assert len(list(reopened.iter_decisions())) == 1
        assert list(reopened.iter_facts()) == []


def test_sibling_batch_continues_after_an_unknown_support(tmp_path):
    path = tmp_path / "store"
    with Store(path) as store:
        bad = _draft([MISSING], ANCHOR)
        good = _draft([], ANCHOR, statement="another")
        results = capture.propose_facts([bad, good], store, None)
    assert [r.status for r in results][0] == "rejected"
    assert results[1].status in {"written", "proposed"}
    assert _fact_count(path) == 1


@pytest.mark.parametrize("anchored", [True, False])
def test_target_vanishing_between_preflight_and_write_is_still_the_refusal(
    tmp_path, monkeypatch, anchored
):
    """Simulates an external removal of the supported decision, not a Store API: while the
    original ``add_fact`` runs, the lookup answers ``None`` for the id; capture's own
    preflight (outside it) still sees the decision live."""
    path = tmp_path / "store"
    with Store(path) as store:
        target = _live(store)
        inside = {"on": False}
        original_add = store.add_fact
        original_get = store.get_decision
        writes: list[str] = []
        original_write = store._write_fact

        def add_fact(fact):
            inside["on"] = True
            try:
                return original_add(fact)
            finally:
                inside["on"] = False

        def get_decision(sid):
            if inside["on"] and sid == target.id:
                return None
            return original_get(sid)

        def write_fact(fact):
            writes.append(fact.id)
            return original_write(fact)

        monkeypatch.setattr(store, "add_fact", add_fact)
        monkeypatch.setattr(store, "get_decision", get_decision)
        monkeypatch.setattr(store, "_write_fact", write_fact)
        [result] = capture.propose_facts(
            [_draft([target.id], ANCHOR if anchored else None)], store, None
        )
        assert result.status == "rejected"
        assert result.reason.startswith("invalid draft: supports[0]: unknown_decision;")
        assert MAY_BE_ON_DISK not in result.reason
        assert writes == []
        # the lookup outside add_fact still sees the decision live (the anchorless gate
        # passed on it), so only the write-time check refused
        assert store.get_decision(target.id) is not None
    assert _fact_count(path) == 0


@pytest.mark.parametrize("write_first", [False, True])
def test_plain_value_error_still_reports_uncertain_write(tmp_path, monkeypatch, write_first):
    """Guard against catching every ``ValueError``: an index failure after a real canonical
    write must keep the uncertainty text. Calls ``_propose_fact_one`` directly so no outer
    batch guard can mask a handler that blows up."""
    path = tmp_path / "store"
    with Store(path) as store:
        original = store.add_fact

        def fail(record, *args, **kwargs):
            if write_first:
                original(record, *args, **kwargs)
            raise ValueError("opaque")

        monkeypatch.setattr(store, "add_fact", fail)
        result = capture._propose_fact_one(_draft([], ANCHOR), store, None, "probe", None, None)
        assert result.status == "rejected"
        assert MAY_BE_ON_DISK in result.reason
        assert "do not re-propose" in result.reason
        assert "opaque" not in result.reason
    assert _fact_count(path) == int(write_first)


def test_reason_helper_keeps_only_bounded_int_positions():
    from sidegraph.validation_errors import MAX_ERRORS, unknown_support_reason

    text = unknown_support_reason([0, -1, 65536, "2", True, 3])  # type: ignore[list-item]
    assert text.startswith("invalid draft: supports[0], supports[3]: unknown_decision;")
    assert "-1" not in text and "65536" not in text and "[2]" not in text
    assert "supports[True]" not in text
    none = unknown_support_reason([-1])
    assert none.startswith("invalid draft: supports: unknown_decision;")
    many = unknown_support_reason(list(range(MAX_ERRORS + 3)))
    assert many.count("supports[") == MAX_ERRORS
    assert "[truncated]" in many
    mcp = unknown_support_reason([1], surface="mcp")
    assert mcp.startswith("Write refused: supports[1]: unknown_decision;")
    assert "Nothing was written for this fact" in mcp
    assert "retry the write" in mcp
    attached = unknown_support_reason([0], attached_to=MISSING)
    assert f"Its decision {MISSING} was written" in attached
    assert "Its decision" not in unknown_support_reason([0], attached_to="not-a-ulid")
    assert "Its decision" not in unknown_support_reason([0], attached_to={"x": 1})  # type: ignore[arg-type]


@pytest.mark.parametrize("terminal", [False, True], ids=["unknown", "terminal"])
def test_anchorless_refusal_keeps_the_redaction_count(tmp_path, terminal):
    with Store(tmp_path / "store") as store:
        supports = [_terminal(store).id] if terminal else [MISSING]
        raw = _draft(supports, statement="leaked password=hunter2secret")
        [result] = capture.propose_facts([raw], store, None)
        assert result.status == "rejected"
        assert result.redactions == 1
