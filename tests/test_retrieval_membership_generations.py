"""Published node membership must survive community-id reuse across graph generations."""

from __future__ import annotations

from sidegraph import domain_snapshot
from sidegraph.engine.reader import GraphifyReader
from sidegraph.retrieval import (
    RetrievalBudget,
    _domain_decisions,
    _structure_fallback_lines,
    rank_decisions,
)
from sidegraph.server import _drill_down_impl
from sidegraph.store import Store
from sidegraph.sync import sync, take_snapshot
from tests.graph_fixtures import write_graph
from tests.snapshot_support import legacy_graph, standard_domains
from tests.test_retrieval_membership_index import _bind, _decision, _leaf


def _frozen(tmp_path):
    store = Store(tmp_path / ".sidegraph")
    reader = GraphifyReader(write_graph(tmp_path, "old.json", legacy_graph()))
    standard_domains(store)
    sync(store, reader)
    domain_snapshot.write_snapshot(store.path, take_snapshot(store, reader, refresh=False))
    return store


def test_lazy_membership_refresh_does_not_join_an_entity_by_its_old_community(tmp_path):
    with _frozen(tmp_path) as store:
        decision = _decision(store, "Core only")
        _bind(store, decision, _leaf(store, "helper", "src/core/util.py", "util_helper", "2"))
        graph = legacy_graph()
        for node in graph["nodes"]:
            node["community"] = {1: 2, 2: 1}.get(node["community"], node["community"])
        reader = GraphifyReader(write_graph(tmp_path, "swapped.json", graph))

        out = _drill_down_impl(store, reader, "payments")
        domain = store.find_domain_by_slug("payments")
        assert "util_helper" not in store.domain_member_ids(domain.domain_id)
        assert not any("Core only" in line for line in out["decisions"])
        assert decision.id not in {d.id for d in _domain_decisions(domain, store, reader)}


def test_failed_snapshot_refresh_does_not_expand_retained_communities(tmp_path):
    with _frozen(tmp_path) as store:
        decision = _decision(store, "Lonely only")
        _bind(store, decision, _leaf(store, "lonely", "src/misc/lonely.py", "lonely_fn", "4"))
        domain = store.find_domain_by_slug("docs")
        before = store.domain_member_ids(domain.domain_id)
        docs_decision = _decision(store, "Docs only")
        _bind(store, docs_decision, store.find_abstract_entity("domain:docs"), tier=1)
        graph = legacy_graph()
        for node in graph["nodes"]:
            node["community"] = {3: 7, 4: 3}.get(node["community"], node["community"])
        reader = GraphifyReader(write_graph(tmp_path, "reused.json", graph))
        domain_snapshot.snapshot_path(store.path).write_text("{bad json\n", encoding="utf-8")

        report = sync(store, reader)
        assert len(report.domain_failures) == 3
        domain = store.find_domain_by_slug("docs")
        assert store.domain_member_ids(domain.domain_id) == before
        assert "lonely_fn" not in before
        out = _drill_down_impl(store, reader, "docs")

        # members
        assert all("src/misc/lonely.py" not in line for line in out["members"])
        # decisions
        assert not any("Lonely only" in line for line in out["decisions"])
        assert decision.id not in {d.id for d in _domain_decisions(domain, store, reader)}
        # related
        context = rank_decisions(
            [], [], ["3"], store, RetrievalBudget(), seed_node_ids=["lonely_fn"]
        )
        assert not any("Docs only" in line for line in context.related)
        # structure
        lines = _structure_fallback_lines(["3"], store, 1000, ["lonely_fn"])
        assert not any("[domain] Docs" in line for line in lines)
