from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from sidegraph.bootstrap.model import ProofResult
from sidegraph.bootstrap.proof import prove_task_context, select_default_proof
from sidegraph.engine.reader import GraphifyReader
from sidegraph.retrieval import Seed, TaskContext
from sidegraph.schema import (
    AnchorBinding,
    Decision,
    DecisionKind,
    DecisionStatus,
    Descriptor,
    Entity,
    EntityKind,
    Provenance,
)
from sidegraph.store import Store


def _add_entity(
    store: Store,
    name: str,
    file_path: str | None,
    *,
    kind: EntityKind = EntityKind.CONCRETE,
) -> Entity:
    return store.upsert_entity(
        Entity(
            canonical_name=name,
            kind=kind,
            descriptor=Descriptor(name=name, file_path=file_path),
        )
    )


def _add_decision(
    store: Store,
    entity: Entity,
    *,
    title: str,
    kind: DecisionKind = DecisionKind.ADR,
    status: DecisionStatus = DecisionStatus.ACCEPTED,
    valid_from: datetime,
    valid_to: datetime | None = None,
    rejected: str | None = None,
    binding_status: str = "live",
    tier: int = 2,
    decision_id: str | None = None,
) -> Decision:
    decision = store.add_decision(
        Decision(
            **({"id": decision_id} if decision_id is not None else {}),
            title=title,
            kind=kind,
            status=status,
            context="test context",
            choice="test choice",
            rejected=rejected,
            valid_from=valid_from,
            valid_to=valid_to,
            provenance=Provenance(source="manual", ref="docs/proof.md"),
        )
    )
    store.add_binding(
        AnchorBinding(
            record_id=decision.id,
            entity_id=entity.entity_id,
            tier=tier,
            status=binding_status,
        )
    )
    return decision


def proof_store_and_reader(tmp_path):
    graph_path = tmp_path / "graph.json"
    graph_path.write_text(
        json.dumps(
            {
                "built_at_commit": "proof",
                "nodes": [
                    {
                        "id": "ordinary",
                        "label": "Ordinary",
                        "norm_label": "ordinary",
                        "file_type": "code",
                        "source_file": "src/ordinary.py",
                        "source_location": "L1",
                        "community": "1",
                    },
                    {
                        "id": "older",
                        "label": "Older",
                        "norm_label": "older",
                        "file_type": "code",
                        "source_file": "src/older.py",
                        "source_location": "L1",
                        "community": "1",
                    },
                    {
                        "id": "alpha",
                        "label": "Alpha",
                        "norm_label": "alpha",
                        "file_type": "code",
                        "source_file": "src/alpha.py",
                        "source_location": "L1",
                        "community": "1",
                    },
                    {
                        "id": "zeta",
                        "label": "Zeta",
                        "norm_label": "zeta",
                        "file_type": "code",
                        "source_file": "src/zeta.py",
                        "source_location": "L1",
                        "community": "1",
                    },
                ],
                "links": [],
            }
        )
        + "\n"
    )
    store = Store(tmp_path / "store")
    ordinary = _add_entity(store, "Ordinary", "src/ordinary.py")
    older = _add_entity(store, "Older", "src/older.py")
    alpha = _add_entity(store, "Alpha", "src/alpha.py")
    zeta = _add_entity(store, "Zeta", "src/zeta.py")
    _add_decision(
        store,
        ordinary,
        title="Ordinary newer ADR",
        valid_from=datetime(2026, 1, 1, tzinfo=UTC),
    )
    _add_decision(
        store,
        older,
        title="Older gotcha",
        kind=DecisionKind.GOTCHA,
        valid_from=datetime(2024, 1, 1, tzinfo=UTC),
    )
    selected = _add_decision(
        store,
        alpha,
        title="Newest rejected lesson",
        kind=DecisionKind.LESSON,
        rejected="The earlier approach failed.",
        valid_from=datetime(2025, 1, 1, tzinfo=UTC),
    )
    store.add_binding(
        AnchorBinding(record_id=selected.id, entity_id=zeta.entity_id, tier=2, status="live")
    )
    _add_decision(
        store,
        alpha,
        title="Proposed alternative",
        kind=DecisionKind.GOTCHA,
        status=DecisionStatus.PROPOSED,
        valid_from=datetime(2027, 1, 1, tzinfo=UTC),
    )
    return store, GraphifyReader(graph_path)


@pytest.fixture
def proof_store(tmp_path):
    store, _reader = proof_store_and_reader(tmp_path)
    return store


def selected_id(store: Store) -> str:
    selection = select_default_proof(store, accepted_record_ids=accepted_ids(store))
    assert selection is not None
    return selection.decision.id


def accepted_ids(store: Store) -> tuple[str, ...]:
    return tuple(
        decision.id
        for decision in store.iter_decisions()
        if decision.status == DecisionStatus.ACCEPTED
    )


def test_selector_uses_documented_order_not_insertion_order(proof_store):
    """Fails if selector follows Store iteration rather than its stated priority."""
    selected = select_default_proof(proof_store, accepted_record_ids=accepted_ids(proof_store))

    assert selected is not None
    assert selected.decision.title == "Newest rejected lesson"
    assert selected.file_path == "src/alpha.py"
    assert selected.rule == (
        "accepted -> valid -> live tier-2 -> gotcha/lesson-or-rejected "
        "-> newest valid_from -> stable id -> lexicographically first non-empty concrete "
        "entity file_path"
    )


def test_selector_rejects_proposed_or_nonlive_candidates(tmp_path):
    """Fails if proposals or orphaned tier-2 bindings become default-eligible."""
    store = Store(tmp_path / "store")
    proposed_entity = _add_entity(store, "Proposed", "src/proposed.py")
    orphaned_entity = _add_entity(store, "Orphaned", "src/orphaned.py")
    _add_decision(
        store,
        proposed_entity,
        title="Proposed live decision",
        status=DecisionStatus.PROPOSED,
        valid_from=datetime(2026, 1, 1, tzinfo=UTC),
    )
    _add_decision(
        store,
        orphaned_entity,
        title="Accepted orphaned decision",
        valid_from=datetime(2026, 1, 1, tzinfo=UTC),
        binding_status="orphaned",
    )
    expired_entity = _add_entity(store, "Expired", "src/expired.py")
    _add_decision(
        store,
        expired_entity,
        title="Expired accepted decision",
        valid_from=datetime(2020, 1, 1, tzinfo=UTC),
        valid_to=datetime(2021, 1, 1, tzinfo=UTC),
    )

    assert select_default_proof(store, accepted_record_ids=accepted_ids(store)) is None


def test_selector_excludes_future_dated_accepted_record(tmp_path):
    """Fails if a record that has not started yet can outrank a current record."""
    store = Store(tmp_path / "store")
    current = _add_entity(store, "Current", "src/current.py")
    future = _add_entity(store, "Future", "src/future.py")
    _add_decision(
        store,
        current,
        title="Current ADR",
        valid_from=datetime(2025, 1, 1, tzinfo=UTC),
    )
    _add_decision(
        store,
        future,
        title="Future gotcha",
        kind=DecisionKind.GOTCHA,
        valid_from=datetime(2999, 1, 1, tzinfo=UTC),
    )

    selected = select_default_proof(store, accepted_record_ids=accepted_ids(store))

    assert selected is not None
    assert selected.decision.title == "Current ADR"


def test_selector_breaks_equal_priority_and_time_ties_by_stable_id(tmp_path):
    """Fails if equal-priority records retain insertion-dependent ordering."""
    store = Store(tmp_path / "store")
    entity = _add_entity(store, "Tied", "src/tied.py")
    timestamp = datetime(2025, 1, 1, tzinfo=UTC)
    _add_decision(
        store,
        entity,
        title="Later ID",
        kind=DecisionKind.GOTCHA,
        valid_from=timestamp,
        decision_id="z-record",
    )
    _add_decision(
        store,
        entity,
        title="Earlier ID",
        kind=DecisionKind.GOTCHA,
        valid_from=timestamp,
        decision_id="a-record",
    )

    selected = select_default_proof(store, accepted_record_ids=accepted_ids(store))

    assert selected is not None
    assert selected.decision.id == "a-record"


def test_selector_excludes_wrong_tier_nonconcrete_and_pathless_bindings(tmp_path):
    """Fails if an unusable binding is mistaken for a concrete live leaf anchor."""
    store = Store(tmp_path / "store")
    wrong_tier = _add_entity(store, "Tier one", "src/tier-one.py")
    nonconcrete = _add_entity(
        store,
        "Abstract",
        "src/abstract.py",
        kind=EntityKind.ABSTRACT,
    )
    pathless = _add_entity(store, "Pathless", None)
    for title, entity, tier in (
        ("Tier one decision", wrong_tier, 1),
        ("Abstract decision", nonconcrete, 2),
        ("Pathless decision", pathless, 2),
    ):
        _add_decision(
            store,
            entity,
            title=title,
            kind=DecisionKind.GOTCHA,
            valid_from=datetime(2025, 1, 1, tzinfo=UTC),
            tier=tier,
        )

    assert select_default_proof(store, accepted_record_ids=accepted_ids(store)) is None


def test_selector_restricts_default_proof_to_current_run_ids(tmp_path):
    """Allowing an older global record can falsely complete a failed current run."""
    store, _reader = proof_store_and_reader(tmp_path)
    current_run = next(
        decision for decision in store.iter_decisions() if decision.title == "Ordinary newer ADR"
    )

    selected = select_default_proof(store, accepted_record_ids=(current_run.id,))

    assert selected is not None
    assert selected.decision.id == current_run.id
    assert selected.file_path == "src/ordinary.py"


def test_proof_returns_exact_no_eligible_result_without_retrieval(tmp_path, monkeypatch):
    """Fails if no-eligible proof attempts retrieval or returns an ambiguous result."""
    graph_path = tmp_path / "graph.json"
    graph_path.write_text('{"nodes": [], "links": []}\n')
    store = Store(tmp_path / "store")
    calls = []
    monkeypatch.setattr(
        "sidegraph.bootstrap.proof.get_task_context",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    result = prove_task_context(
        store,
        GraphifyReader(graph_path),
        accepted_record_ids=(),
    )

    assert result == ProofResult(complete=False, reason="no eligible accepted decision")
    assert calls == []


def test_proof_calls_production_get_task_context(tmp_path, monkeypatch):
    """Fails if proof bypasses production retrieval for an onboarding-only renderer."""
    store, reader = proof_store_and_reader(tmp_path)
    calls = []

    def fake_get_task_context(seeds, actual_store, actual_reader, budget=None):
        calls.append((seeds, actual_store, actual_reader))
        record_id = selected_id(store)
        return TaskContext(
            decisions=[f"- production line (id: {record_id})"], shown_ids=[record_id]
        )

    monkeypatch.setattr("sidegraph.bootstrap.proof.get_task_context", fake_get_task_context)
    result = prove_task_context(store, reader, accepted_record_ids=accepted_ids(store))

    assert calls == [([Seed(file_path="src/alpha.py")], store, reader)]
    assert result.primary_line == f"- production line (id: {selected_id(store)})"


def test_proof_is_incomplete_when_selector_record_does_not_surface(tmp_path, monkeypatch):
    """Fails if proof claims success merely because a selectable record exists."""
    store, reader = proof_store_and_reader(tmp_path)
    monkeypatch.setattr(
        "sidegraph.bootstrap.proof.get_task_context", lambda *args, **kwargs: TaskContext()
    )

    result = prove_task_context(store, reader, accepted_record_ids=accepted_ids(store))

    assert result.complete is False
    assert result.reason == "selected decision did not surface"


def test_user_path_uses_production_api_without_changing_default_selection(tmp_path, monkeypatch):
    """Fails if a requested seed alters selector priority or uses another retrieval path."""
    store, reader = proof_store_and_reader(tmp_path)
    record_id = selected_id(store)
    actual = prove_task_context(
        store,
        reader,
        accepted_record_ids=accepted_ids(store),
        file_path="src/zeta.py",
    )

    assert actual.complete is True
    assert actual.file_path == "src/zeta.py"
    assert record_id in (actual.primary_line or "")

    calls = []

    def fake_get_task_context(seeds, actual_store, actual_reader, budget=None):
        calls.append((seeds, actual_store, actual_reader))
        return TaskContext(decisions=[f"- selected path (id: {record_id})"], shown_ids=[record_id])

    monkeypatch.setattr("sidegraph.bootstrap.proof.get_task_context", fake_get_task_context)
    result = prove_task_context(
        store,
        reader,
        accepted_record_ids=accepted_ids(store),
        file_path="src/zeta.py",
    )

    selection = select_default_proof(store, accepted_record_ids=accepted_ids(store))
    assert selection is not None
    assert selection.file_path == "src/alpha.py"
    assert calls == [([Seed(file_path="src/zeta.py")], store, reader)]
    assert result.file_path == "src/zeta.py"
    assert result.primary_line == f"- selected path (id: {record_id})"


def test_proof_uses_real_retrieval_without_writing_or_promoting_proposals(tmp_path):
    """Fails if proof mutates durable data or presents a proposal as an accepted result."""
    store, reader = proof_store_and_reader(tmp_path)
    graph_before = reader.path.read_bytes()
    store_before = sorted(
        (path.relative_to(store.path), path.read_bytes())
        for path in store.path.rglob("*")
        if path.is_file()
    )

    result = prove_task_context(store, reader, accepted_record_ids=accepted_ids(store))

    assert result.complete is True
    assert result.primary_line is not None
    assert selected_id(store) in result.primary_line
    assert result.source == "docs/proof.md"
    assert result.selection_rule == (
        "accepted -> valid -> live tier-2 -> gotcha/lesson-or-rejected "
        "-> newest valid_from -> stable id -> lexicographically first non-empty concrete "
        "entity file_path"
    )
    assert result.copyable_prompt == (
        f"Call get_task_context for src/alpha.py and explain why record {selected_id(store)} "
        "applies before editing."
    )
    assert "## Unratified proposals" in (result.full_context or "")
    assert "[unratified] Proposed alternative" in (result.full_context or "")
    assert reader.path.read_bytes() == graph_before
    assert (
        sorted(
            (path.relative_to(store.path), path.read_bytes())
            for path in store.path.rglob("*")
            if path.is_file()
        )
        == store_before
    )
