"""Retrieval reads domain membership from the index sync writes (D5.3).

see design/superpowers/specs/2026-10-14-graphify-upgrade-design.md (D5)"""

from __future__ import annotations

from datetime import UTC, datetime

from sidegraph.engine.reader import GraphifyReader
from sidegraph.retrieval import (
    RetrievalBudget,
    Seed,
    _domain_decisions,
    _structure_fallback_lines,
    drill_down,
    get_task_context,
    rank_decisions,
)
from sidegraph.schema import (
    AnchorBinding,
    Decision,
    DecisionKind,
    DecisionStatus,
    Descriptor,
    Provenance,
)
from sidegraph.server import _drill_down_impl
from sidegraph.store import Store
from sidegraph.sync import sync
from tests.graph_fixtures import write_graph
from tests.snapshot_support import (
    accept,
    legacy_graph,
    seed_members,
    standard_domains,
    touch_a_record,
)
from tests.test_sync_membership import lib_graph


def _decision(store, title):
    return store.add_decision(
        Decision(
            title=title,
            kind=DecisionKind.ADR,
            status=DecisionStatus.ACCEPTED,
            context="c",
            choice="ch",
            valid_from=datetime.now(UTC),
            provenance=Provenance(source="manual"),
        )
    )


def _leaf(store, name, path, node_id, community):
    e = store.get_or_create_entity(Descriptor(name=name, file_path=path))
    e.last_seen_node_id, e.last_seen_community = node_id, community
    return store.upsert_entity(e)


def _bind(store, decision, entity, tier=2):
    store.add_binding(
        AnchorBinding(record_id=decision.id, entity_id=entity.entity_id, tier=tier, status="live")
    )


def _setup(tmp_path):
    return Store(tmp_path / ".sidegraph"), GraphifyReader(
        write_graph(tmp_path, "legacy.json", legacy_graph())
    )


def test_drill_down_lists_the_indexed_members_not_every_node_under_a_prefix(tmp_path):
    """Red against the read-time prefix rule: every node under src/ would be listed. A frozen
    domain's live prefix must not add a node its snapshot gave elsewhere."""
    store, reader = _setup(tmp_path)
    wide = accept(store, "wide", prefixes=["src"])
    seed_members(store, reader, wide, {"util_py", "util_helper"})
    got = drill_down("wide", store, reader)["members"]
    assert got and all("src/core/util.py" in m for m in got)


def test_drill_down_rebuilds_an_index_a_reload_emptied(tmp_path):
    """Red against a drill-down that reads the index as it finds it: no member after a pull."""
    root = tmp_path / ".sidegraph"
    reader = GraphifyReader(write_graph(tmp_path, "legacy.json", legacy_graph()))
    with Store(root) as store:
        standard_domains(store)
        sync(store, reader)
    touch_a_record(root)
    with Store(root) as store:
        out = _drill_down_impl(store, reader, "core")
    assert any("helper" in m for m in out["members"])


def test_a_decision_on_a_member_node_outside_the_listed_communities_reaches_the_domain(tmp_path):
    """Red against the community-only join: community 1 is in no domain's communities."""
    store, reader = _setup(tmp_path)
    part = accept(store, "part")
    seed_members(store, reader, part, {"model_invoice"})
    d = _decision(store, "invoice rule")
    _bind(store, d, _leaf(store, "Invoice", "src/pay/model.py", "model_invoice", "1"))
    assert d.id in {
        x.id for x in _domain_decisions(store.get_domain(part.domain_id), store, reader)
    }


def test_a_whole_document_decision_follows_an_indexed_heading(tmp_path):
    """Red against the read-time rule: no community and no prefix covered docs/pay.md."""
    store, reader = _setup(tmp_path)
    guide = accept(store, "guide")
    seed_members(store, reader, guide, {"pay_md_charging"})
    d = _decision(store, "doc rule")
    _bind(store, d, _leaf(store, "pay.md", "docs/pay.md", "pay_md", "3"))
    assert d.id in {
        x.id for x in _domain_decisions(store.get_domain(guide.domain_id), store, reader)
    }


def test_a_node_under_a_capped_path_rule_now_reaches_the_domain_s_decisions(tmp_path):
    """Intended change 2g (declared): before, such nodes showed as members but their decisions
    did not reach the domain, because only a claimed community joined them."""
    store = Store(tmp_path / ".sidegraph")
    reader = GraphifyReader(write_graph(tmp_path, "lib.json", lib_graph()))
    lib = accept(store, "lib", prefixes=["lib"])
    sync(store, reader)
    d = _decision(store, "lib rule")
    _bind(store, d, _leaf(store, "mod2.py", "lib/mod2.py", "lib2", "2"))
    assert d.id in {x.id for x in _domain_decisions(store.get_domain(lib.domain_id), store, reader)}


def test_the_related_bucket_reaches_a_domain_through_a_seed_node_it_holds(tmp_path):
    """Red against the community-only bucket C: no domain lists community 1."""
    store, reader = _setup(tmp_path)
    part = accept(store, "part")
    seed_members(store, reader, part, {"model_invoice"})
    d = _decision(store, "part-wide rule")
    _bind(store, d, store.find_abstract_entity("domain:part"), tier=1)
    ctx = rank_decisions([], [], ["1"], store, RetrievalBudget(), seed_node_ids=["model_invoice"])
    assert any("part-wide rule" in r for r in ctx.related)
    ctx = get_task_context([Seed(file_path="src/pay/model.py")], store, reader)
    assert any("part-wide rule" in r for r in ctx.related)  # red against a caller not passing them


def test_the_structure_fallback_names_a_domain_through_a_node_it_holds(tmp_path):
    store, reader = _setup(tmp_path)
    part = accept(store, "part")
    seed_members(store, reader, part, {"model_invoice"})
    assert any(
        "Part" in line for line in _structure_fallback_lines([], store, 1000, ["model_invoice"])
    )
