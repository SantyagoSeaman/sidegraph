from datetime import UTC, datetime
from pathlib import Path

from sidegraph.anchoring import resolve_and_bind
from sidegraph.engine.reader import GraphifyReader
from sidegraph.retrieval import Seed, build_toc, drill_down, get_task_context, top_tier_map
from sidegraph.schema import (
    AnchorBinding,
    Decision,
    DecisionKind,
    DecisionStatus,
    Descriptor,
    Domain,
    Provenance,
    Scope,
)
from sidegraph.store import Store

SLICE = Path(__file__).parent / "fixtures" / "bitfinex_slice.json"


def test_decision_anchored_to_real_entity_surfaces_by_file_seed(tmp_path):
    reader = GraphifyReader(SLICE)
    store = Store(tmp_path / "e.db")
    # capture a gotcha about BitfinexAdapter, anchored via the real reader (Stage 3 path)
    d = Decision(
        title="rate-limit retries",
        kind=DecisionKind.GOTCHA,
        status=DecisionStatus.ACCEPTED,
        context="429s during bursts",
        choice="retry with backoff in the adapter",
        valid_from=datetime.now(UTC),
        provenance=Provenance(source="manual"),
    )
    store._write_decision(d)
    store._conn.commit()
    resolve_and_bind(
        d.id, Descriptor(name="BitfinexAdapter", file_path="adapters/bitfinex.py"), reader, store
    )

    # now retrieve by seeding the file the agent is working in
    ctx = get_task_context([Seed(file_path="adapters/bitfinex.py")], store, reader)
    rendered = ctx.render()
    assert "rate-limit retries" in rendered  # the decision surfaced
    assert any("rate-limit retries" in m for m in ctx.mistakes)  # mistakes-first
    assert ctx.structure  # structural map present


def _global_decision(store, title, status, kind=DecisionKind.ADR):
    return store.add_decision(
        Decision(
            title=title,
            kind=kind,
            status=status,
            context="c",
            choice="ch",
            scope=Scope.GLOBAL,
            valid_from=datetime.now(UTC),
            provenance=Provenance(source="manual"),
        )
    )


def test_top_tier_map_preserves_all_ten_accepted_slots(tmp_path):
    store = Store(tmp_path / "top.db")
    accepted = [
        _global_decision(store, f"Accepted {n}", DecisionStatus.ACCEPTED) for n in range(10)
    ]
    proposed = _global_decision(
        store,
        "Newest proposal",
        DecisionStatus.PROPOSED,
        kind=DecisionKind.GOTCHA,
    )

    rendered = top_tier_map(store, reader=None)

    assert all(item.title in rendered for item in accepted)
    assert rendered.index(accepted[-1].title) < rendered.index("## Unratified proposals")
    assert proposed.title in rendered


def test_toc_and_drill_down_quarantine_proposed_mistakes(tmp_path):
    store = Store(tmp_path / "domain.db")
    domain = store.add_domain(
        Domain(
            slug="payments",
            title="Payments",
            summary="Handles settlement.",
            provenance=Provenance(source="manual"),
        )
    )
    store.ratify_domains(accept=[domain.domain_id])
    domain = store.get_domain(domain.domain_id)
    entity = store.find_abstract_entity(f"domain:{domain.slug}")
    accepted = _global_decision(store, "Accepted", DecisionStatus.ACCEPTED)
    proposed = _global_decision(
        store,
        "Proposed gotcha",
        DecisionStatus.PROPOSED,
        kind=DecisionKind.GOTCHA,
    )
    for decision in (accepted, proposed):
        store.add_binding(
            AnchorBinding(
                record_id=decision.id,
                entity_id=entity.entity_id,
                tier=1,
                status="live",
            )
        )

    toc = build_toc(store)
    detail = drill_down(domain.slug, store)

    assert toc["domains"][0]["mistakes"] == 0
    assert proposed.title not in "\n".join(toc["global_mistakes"])
    assert proposed.title in "\n".join(toc["unratified"])
    assert detail["decision_ids"] == [accepted.id, proposed.id]
    assert detail["decisions"][-1].startswith("- [gotcha] [unratified]")
