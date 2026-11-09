"""One ``rank_decisions`` call reads the supporting facts once, and answers exactly as before.

The support map is built lazily on first use, dies with the call, and is never persisted
(a longer-lived copy could go stale under an older Sidegraph writing the same store).

# see design/superpowers/specs/2026-11-07-retrieval-support-map-design.md
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta

import pytest

from sidegraph.retrieval import RetrievalBudget, rank_decisions
from sidegraph.schema import (
    AnchorBinding,
    Decision,
    DecisionKind,
    DecisionStatus,
    Entity,
    Fact,
    Provenance,
)
from sidegraph.store import Store

SUPPORT_QUERY = "SELECT data FROM facts WHERE status IN"
BUDGET = RetrievalBudget(memory_chars=40000, structure_chars=0)
_NOW = datetime.now(UTC)


def _entity(store: Store, name: str) -> Entity:
    return store.upsert_entity(
        Entity.model_validate(
            dict(
                entity_id=f"E-{name}",
                canonical_name=name,
                kind="concrete",
                descriptor=dict(name=name, file_path=f"src/{name}.py"),
            )
        )
    )


def _decision(
    store: Store,
    entity: Entity | None,
    title: str,
    *,
    kind: DecisionKind = DecisionKind.ADR,
    status: DecisionStatus = DecisionStatus.ACCEPTED,
    age: int = 3,
    binding_status: str = "live",
) -> Decision:
    d = Decision(
        title=title,
        kind=kind,
        status=status,
        context="context of " + title,
        choice="choice of " + title,
        valid_from=_NOW - timedelta(days=age),
        provenance=Provenance(source="manual"),
    )
    store.add_decision(d)
    if entity is not None:
        store.add_binding(
            AnchorBinding(record_id=d.id, entity_id=entity.entity_id, tier=2, status=binding_status)
        )
    return d


def _fact(
    store: Store,
    supports: list[str],
    statement: str,
    *,
    fid: str | None = None,
    status: DecisionStatus = DecisionStatus.ACCEPTED,
    age: int = 3,
    valid_to: datetime | None = None,
    when: datetime | None = None,
) -> Fact:
    raw: dict = dict(
        statement=statement,
        source="probe",
        supports=supports,
        status=status,
        valid_from=when or (_NOW - timedelta(days=age)),
        valid_to=valid_to,
        provenance=Provenance(source="manual"),
    )
    if fid is not None:
        raw["id"] = fid
    f = Fact(**raw)
    store.add_fact(f)
    return f


def _sized_store(tmp_path, n_adr: int, n_facts: int):
    store = Store(tmp_path / "store")
    entity = _entity(store, "e0")
    adrs = [_decision(store, entity, f"D{i}") for i in range(n_adr)]
    for i in range(n_facts):
        _fact(store, [adrs[i].id] if i < n_adr else [], f"observed {i}", fid=f"F{i:04d}")
    store.set_meta("volatile_stale", "0")
    return store, entity


class _Counters:
    def __init__(self, store: Store, monkeypatch) -> None:
        self.queries: list[str] = []
        self.validations = 0
        store._conn.set_trace_callback(self.queries.append)
        original = Fact.model_validate_json

        def counting(*args, **kwargs):
            self.validations += 1
            return original(*args, **kwargs)

        monkeypatch.setattr(Fact, "model_validate_json", counting)

    @property
    def support_queries(self) -> int:
        return sum(1 for q in self.queries if q.lstrip().startswith(SUPPORT_QUERY))

    def reset(self) -> None:
        self.queries.clear()
        self.validations = 0


def test_one_support_query_and_one_validation_per_fact(tmp_path, monkeypatch):
    store, entity = _sized_store(tmp_path, 4, 40)
    with store:
        rank_decisions([entity], [], [], store, BUDGET)  # warm
        counters = _Counters(store, monkeypatch)
        ctx = rank_decisions([entity], [], [], store, BUDGET)
        assert ctx.shown_ids
        assert counters.support_queries == 1
        assert counters.validations == 40


def test_the_map_does_not_outlive_the_call(tmp_path, monkeypatch):
    store, entity = _sized_store(tmp_path, 20, 400)
    with store:
        rank_decisions([entity], [], [], store, BUDGET)
        counters = _Counters(store, monkeypatch)
        rank_decisions([entity], [], [], store, BUDGET)
        assert counters.support_queries == 1
        assert counters.validations == 400
        counters.reset()
        rank_decisions([entity], [], [], store, BUDGET)
        assert counters.support_queries == 1


def test_no_decisions_means_no_support_query(tmp_path, monkeypatch):
    with Store(tmp_path / "store") as store:
        empty = _entity(store, "empty")
        store.set_meta("volatile_stale", "0")
        rank_decisions([empty], [], [], store, BUDGET)
        counters = _Counters(store, monkeypatch)
        rank_decisions([empty], [], [], store, BUDGET)
        assert counters.support_queries == 0


# -- parity: today's per-call behaviour, fresh scan and fresh objects each time --------------


def _legacy(store: Store):
    return type(
        "Legacy", (), {"get": lambda self, key, default=(): store.facts_for_decision(key)}
    )()


def _fixture_many_support(store):
    e = _entity(store, "e")
    ds = [_decision(store, e, f"many {i}", age=5 - i) for i in range(3)]
    _fact(store, [d.id for d in ds], "one fact, three decisions")
    return dict(seed_entities=[e])


def _status_mix(store):
    e = _entity(store, "e")
    d = _decision(store, e, "mixed")
    _fact(store, [d.id], "accepted", fid="F1")
    _fact(store, [d.id], "proposed inside window", fid="F2", status=DecisionStatus.PROPOSED, age=2)
    _fact(store, [d.id], "proposed aged out", fid="F3", status=DecisionStatus.PROPOSED, age=90)
    _fact(
        store,
        [d.id],
        "superseded",
        fid="F4",
        status=DecisionStatus.SUPERSEDED,
        valid_to=_NOW - timedelta(days=1),
    )
    _fact(store, [d.id], "rejected", fid="F5", status=DecisionStatus.REJECTED)
    return dict(seed_entities=[e])


def _unratified_off(store, monkeypatch):
    monkeypatch.setenv("SIDEGRAPH_UNRATIFIED", "off")
    return _status_mix(store)


def _future_valid_to(store):
    e = _entity(store, "e")
    d = _decision(store, e, "closing")
    _fact(store, [d.id], "closed in the future", valid_to=_NOW + timedelta(days=30))
    _fact(store, [d.id], "open")
    return dict(seed_entities=[e])


def _proposed_decision(store):
    e = _entity(store, "e")
    d = _decision(store, e, "proposal", status=DecisionStatus.PROPOSED, age=2)
    _fact(store, [d.id], "accepted evidence", fid="F1")
    _fact(store, [d.id], "proposed evidence", fid="F2", status=DecisionStatus.PROPOSED, age=1)
    return dict(seed_entities=[e])


def _mistakes_with_evidence(store):
    e = _entity(store, "e")
    for i in range(4):
        d = _decision(store, e, f"mistake {i}", kind=DecisionKind.GOTCHA, age=i + 1)
        for j in range(3):
            _fact(store, [d.id], f"evidence {i}.{j} " + "x" * 120, fid=f"F{i}{j}")
    return dict(seed_entities=[e], budget=RetrievalBudget(memory_chars=900, structure_chars=0))


def _related_overflow(store):
    peri = _entity(store, "peri")
    related = [_decision(store, peri, f"related {i}", age=i + 1) for i in range(8)]
    # Descending explicit ids with one shared valid_from, written without a reopen: the index
    # row order differs from id order, so a map that sorts by id changes the order of the
    # proposed bucket.
    when = _NOW - timedelta(days=2)
    for decision in related[5:]:
        for tag in ("9", "8", "7"):
            _fact(
                store,
                [decision.id],
                f"proposal {decision.title} {tag}",
                fid=f"FP-{decision.title[-1]}-{tag}",
                status=DecisionStatus.PROPOSED,
                when=when,
            )
    return dict(seed_entities=[], peripheral_entities=[peri])


def _file_bound_and_ordinary(store):
    e = _entity(store, "e")
    ordinary = _decision(store, e, "ordinary")
    orphan = _decision(store, e, "file only", binding_status="orphaned")
    _fact(store, [ordinary.id], "ordinary evidence", fid="F1")
    _fact(store, [orphan.id], "file-bound evidence", fid="F2")
    _fact(store, [orphan.id], "file-bound proposal", fid="F3", status=DecisionStatus.PROPOSED)
    ids = frozenset({e.entity_id})
    return dict(seed_entities=[e], file_bound_entity_ids=ids, ordinary_seed_entity_ids=ids)


def _file_bound_only(store):
    e = _entity(store, "e")
    orphan = _decision(store, e, "file only", binding_status="orphaned")
    _fact(store, [orphan.id], "file-bound evidence", fid="F2")
    return dict(seed_entities=[e], file_bound_entity_ids=frozenset({e.entity_id}))


def _trailing(store):
    seed = _entity(store, "seed")
    trail = _entity(store, "trail")
    _decision(store, seed, "seed decision")
    accepted = _decision(store, trail, "trailing accepted")
    proposed = _decision(store, trail, "trailing proposed", status=DecisionStatus.PROPOSED, age=2)
    _fact(store, [accepted.id], "trailing accepted fact", fid="F1")
    _fact(store, [accepted.id], "trailing proposal", fid="F2", status=DecisionStatus.PROPOSED)
    _fact(store, [proposed.id], "its accepted fact", fid="F3")
    _fact(store, [proposed.id], "its proposed fact", fid="F4", status=DecisionStatus.PROPOSED)
    return dict(seed_entities=[seed], trailing_entities=[trail])


def _none_next_to_many(store):
    e = _entity(store, "e")
    bare = _decision(store, e, "bare", age=1)
    busy = _decision(store, e, "busy", age=2)
    for i in range(6):
        _fact(store, [busy.id], f"busy evidence {i}", fid=f"F{i}")
    assert bare.id != busy.id
    return dict(seed_entities=[e])


PARITY = [
    _fixture_many_support,
    _status_mix,
    _unratified_off,
    _future_valid_to,
    _proposed_decision,
    _mistakes_with_evidence,
    _related_overflow,
    _file_bound_and_ordinary,
    _file_bound_only,
    _trailing,
    _none_next_to_many,
]


@pytest.mark.parametrize("build", PARITY, ids=[b.__name__.lstrip("_") for b in PARITY])
def test_answer_is_identical_to_the_per_call_scan(tmp_path, monkeypatch, build):
    with Store(tmp_path / "store") as store:
        kwargs = build(store, monkeypatch) if build is _unratified_off else build(store)
        store.set_meta("volatile_stale", "0")
        budget = kwargs.pop("budget", BUDGET)
        seeds = kwargs.pop("seed_entities")
        peripheral = kwargs.pop("peripheral_entities", [])

        def run():
            return dataclasses.asdict(
                rank_decisions(seeds, peripheral, [], store, budget, **kwargs)
            )

        with_map = run()
        monkeypatch.setattr(store, "facts_by_supported_decision", lambda: _legacy(store))
        legacy = run()
        assert with_map == legacy
        assert with_map["shown_ids"] or with_map["related_overflow"]


def test_facts_by_supported_decision_groups_each_fact_under_every_distinct_id(tmp_path):
    with Store(tmp_path / "store") as store:
        e = _entity(store, "e")
        d1 = _decision(store, e, "one")
        d2 = _decision(store, e, "two")
        shared = _fact(store, [d1.id, d2.id, d1.id], "shared", fid="F-shared")
        _fact(store, [d1.id], "only one", fid="F-one")
        _fact(store, [d1.id], "rejected", fid="F-rej", status=DecisionStatus.REJECTED)
        _fact(store, [d1.id], "deprecated", fid="F-dep", status=DecisionStatus.DEPRECATED)
        _fact(store, [d2.id], "superseded", fid="F-sup", status=DecisionStatus.SUPERSEDED)
        grouped = store.facts_by_supported_decision()
        assert [f.id for f in grouped[d1.id]].count("F-shared") == 1
        assert [f.id for f in grouped[d2.id]].count("F-shared") == 1
        by_one = next(f for f in grouped[d1.id] if f.id == "F-shared")
        by_two = next(f for f in grouped[d2.id] if f.id == "F-shared")
        assert by_one is by_two
        assert shared.id == by_one.id
        for decision_id in (d1.id, d2.id):
            assert [f.id for f in grouped[decision_id]] == [
                f.id for f in store.facts_for_decision(decision_id)
            ]
        listed = {f.id for facts in grouped.values() for f in facts}
        assert listed == {"F-shared", "F-one"}
