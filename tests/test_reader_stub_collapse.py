"""Identical path-less stubs (one external name Graphify emits per importing file) collapse to one
candidate for anchoring; a class and a module of the same name, and repository code, never do.
Reading surfaces ask for the uncollapsed answer and keep the ambiguous one they always had.
# see design/superpowers/specs/2026-10-14-graphify-upgrade-design.md (D4)"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path

import pytest

from sidegraph.anchoring import resolve_and_bind
from sidegraph.bootstrap.planner import _anchor_plan
from sidegraph.capture import _domain_anchored
from sidegraph.doc_import import (
    DocWriteRequest,
    ParsedDoc,
    _select_mention_anchors,
    apply_doc_candidate,
)
from sidegraph.domains import _label_names_other_community
from sidegraph.importer import _resolved
from sidegraph.retrieval import Seed, resolve_seeds
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
from sidegraph.sync import _recompute_domain_communities, rebind_entity, sync
from tests.graph_fixtures import node, reader_of, write_graph


def stub(node_id, label, *, file_type="code", community=1):
    return node(node_id, label, "", file_type=file_type, location=None, community=community)


def test_stubs_in_different_communities_resolve_to_the_smallest_id_with_no_community(tmp_path):
    # red against unfixed code: ambiguous
    r = reader_of(
        tmp_path,
        stub("b1", "BaseModel", community=1),
        stub("b2", "BaseModel", community=2),
        stub("b0", "BaseModel", community=3),
    ).resolve(Descriptor(name="BaseModel"))
    assert r.status == "resolved"
    assert (r.node_id, r.community, r.collapsed) == ("b0", None, True)
    assert r.candidates == ["b1", "b2", "b0"]  # graph order, as the ambiguous answer listed them


def test_stubs_in_one_community_resolve_with_that_community(tmp_path):
    # Unfixed code already returns community "7", as ambiguous: the status is the red assertion.
    r = reader_of(
        tmp_path, stub("b1", "BaseModel", community=7), stub("b2", "BaseModel", community=7)
    ).resolve(Descriptor(name="BaseModel"))
    assert (r.status, r.node_id, r.community) == ("resolved", "b1", "7")


def test_a_class_and_a_module_of_one_name_stay_ambiguous(tmp_path):
    # red against the unrefined (canonical-key) rule, a discarded design, declared
    r = reader_of(
        tmp_path,
        stub("f1", "FastMCP"),
        stub("f2", "FastMCP"),
        stub("m", "fastmcp", file_type="concept"),
    ).resolve(Descriptor(name="fastmcp"))
    assert r.status == "ambiguous" and sorted(r.candidates) == ["f1", "f2", "m"]


def test_labels_that_differ_only_in_case_stay_ambiguous(tmp_path):
    # red against the canonical-key rule (discarded, declared)
    r = reader_of(tmp_path, stub("e1", "Enum"), stub("e2", "enum")).resolve(Descriptor(name="enum"))
    assert r.status == "ambiguous"


def test_repository_code_with_a_path_is_never_merged_by_name(tmp_path):
    # red against nothing: the over-collapse guard
    r = reader_of(
        tmp_path,
        stub("s1", "BaseModel"),
        stub("s2", "BaseModel"),
        node("own", "BaseModel", "src/models.py"),
    ).resolve(Descriptor(name="BaseModel"))
    assert r.status == "ambiguous" and not r.collapsed


def test_a_stub_without_a_community_keeps_the_answer_community_free(tmp_path):
    # red against unfixed code: ambiguous
    b = {**stub("b", "Path"), "community": None}
    r = reader_of(tmp_path, stub("a", "Path", community=1), b).resolve(Descriptor(name="Path"))
    assert (r.status, r.community) == ("resolved", None)


def _stub_graph(tmp_path):
    return reader_of(
        tmp_path,
        stub("p1", "BaseModel", community=2),
        stub("p2", "BaseModel", community=3),
        node("w", "WidgetStore", "src/w.py"),
    )


def test_uncollapsed_answer_is_the_ambiguous_one_the_parent_gave(tmp_path):
    r = _stub_graph(tmp_path).resolve(Descriptor(name="BaseModel"), collapse=False)
    assert (r.status, r.candidates, r.community, r.collapsed) == (
        "ambiguous",
        ["p1", "p2"],
        None,
        False,
    )


def test_reading_surfaces_keep_the_ambiguous_answer(tmp_path):
    reader = _stub_graph(tmp_path)
    anchors, skipped = _select_mention_anchors("Uses `BaseModel` and `WidgetStore`.", reader)
    assert anchors == [Descriptor(name="WidgetStore")]
    assert skipped == [{"name": "BaseModel", "reason": "ambiguous", "candidates": ["p1", "p2"]}]
    plan = _anchor_plan(Descriptor(name="BaseModel"), reader)
    assert (plan.status, plan.candidates, plan.tier) == ("ambiguous", ("p1", "p2"), None)
    assert _label_names_other_community("BaseModel", "1", reader) is True
    seeds = resolve_seeds([Seed(name="BaseModel")], reader, Store(tmp_path / ".sidegraph"))
    assert seeds.seed_node_ids == ["p1", "p2"]
    assert _domain_anchored(reader, [], [Descriptor(name="BaseModel")], []) is False
    assert _resolved(reader, Descriptor(name="BaseModel")) is False


def test_candidates_keep_graph_order_for_every_reader_of_the_answer(tmp_path):
    reader = reader_of(
        tmp_path,
        stub("z_basemodel", "BaseModel", community=1),
        stub("a_basemodel", "BaseModel", community=2),
        node("n_z", "Zed", "src/z.py", community=1),
        node("n_a", "Ay", "src/a.py", community=2),
        links=[
            {"source": "z_basemodel", "target": "n_z", "relation": "uses"},
            {"source": "a_basemodel", "target": "n_a", "relation": "uses"},
        ],
    )
    desc = Descriptor(name="BaseModel")
    assert reader.resolve(desc, collapse=False).candidates == ["z_basemodel", "a_basemodel"]
    collapsed = reader.resolve(desc)
    assert collapsed.candidates == ["z_basemodel", "a_basemodel"]
    assert collapsed.node_id == "a_basemodel"
    seeds = resolve_seeds([Seed(name="BaseModel")], reader, Store(tmp_path / ".sidegraph"))
    assert seeds.seed_node_ids == ["z_basemodel", "a_basemodel"]


def test_anchor_plan_of_a_split_stub_is_ambiguous_with_tier_1(tmp_path):
    reader = reader_of(
        tmp_path,
        stub("s1", "BaseModel", community=7),
        {**stub("s2", "BaseModel"), "community": None},
    )
    plan = _anchor_plan(Descriptor(name="BaseModel"), reader)
    assert (plan.status, plan.candidates, plan.tier) == ("ambiguous", ("s1", "s2"), 1)


def test_one_raw_label_of_two_types_stays_ambiguous(tmp_path):
    r = reader_of(
        tmp_path,
        stub("d1", "datetime"),
        stub("d2", "datetime"),
        stub("d3", "datetime", file_type="concept"),
    ).resolve(Descriptor(name="datetime"))
    assert r.status == "ambiguous" and not r.collapsed


def _decision(store: Store) -> Decision:
    return store.add_decision(
        Decision(
            title="t",
            kind=DecisionKind.LESSON,
            context="c",
            choice="ch",
            valid_from=datetime.now(UTC),
            provenance=Provenance(source="manual"),
        )
    )


def _rows(store: Store, record_id: str) -> list[tuple]:
    out = []
    for b in store.bindings_for_record(record_id):
        e = store.get_entity(b.entity_id)
        out.append((b.tier, b.status, e.canonical_name if e else b.entity_id))
    return sorted(out)


def test_bootstrap_apply_binds_a_leaf_only_for_the_mention_its_plan_calls_resolved(tmp_path):
    from sidegraph.bootstrap.planner import _mention_descriptors

    reader = reader_of(
        tmp_path,
        node("fn_submit", "submit_order", "exec.py"),
        stub("exec_py_path", "Path", community=1),
        stub("other_py_path", "Path", community=2),
    )
    choice = "Call `submit_order` and pass a `Path`."
    intents = _mention_descriptors(choice)
    plans = {p.descriptor.name: p.status for p in (_anchor_plan(d, reader) for d in intents)}
    assert plans == {"submit_order": "resolved", "Path": "ambiguous"}
    store = Store(tmp_path / ".sidegraph")
    request = DocWriteRequest(
        parsed=ParsedDoc(title="Submit path", context="ctx\n\nimported", choice=choice),
        rel_path="docs/a.md",
        source_hash=hashlib.sha256(b"x").hexdigest(),
        status=DecisionStatus.ACCEPTED,
        anchors=intents,
        graph_version="x",
    )
    res = apply_doc_candidate(store, reader, request)
    leaves = [row for row in _rows(store, res.decision_id) if row[0] == 2]
    assert [name for _, _, name in leaves] == ["submit_order"]


def test_resolve_and_bind_collapses_stubs_into_one_leaf_and_keeps_the_entity(tmp_path):
    store = Store(tmp_path / ".sidegraph")
    d = _decision(store)
    g1 = reader_of(
        tmp_path,
        stub("m_b", "BaseModel", community=1),
        stub("m_c", "BaseModel", community=2),
        node("w", "Widget", "src/w.py"),
    )
    res = resolve_and_bind(d.id, Descriptor(name="BaseModel"), g1, store)
    assert (res.status, res.candidates) == ("resolved", [])
    rows = _rows(store, d.id)
    assert [r for r in rows if r[0] == 2] == [(2, "live", "BaseModel")]
    assert [r for r in rows if r[0] == 1] == []
    entity = store.find_entity("BaseModel", None)
    assert entity is not None and entity.last_seen_node_id == "m_b"
    # a rebuild in which a new stub sorts first: same entity, new smallest id, leaf still live
    g2 = reader_of(
        tmp_path,
        stub("m_a", "BaseModel", community=4),
        stub("m_b", "BaseModel", community=1),
        stub("m_c", "BaseModel", community=2),
    )
    rebind_entity(entity, store, g2)
    again = store.find_entity("BaseModel", None)
    assert again is not None and again.entity_id == entity.entity_id
    assert again.last_seen_node_id == "m_a"
    assert [r for r in _rows(store, d.id) if r[0] == 2] == [(2, "live", "BaseModel")]


def _sync_graph(tmp_path, commit, widget_community, c1, c2, *, extra_nodes=()):
    nodes = [
        node("w", "Widget", "src/w.py", location="L3", community=widget_community),
        node("other", "Other", "src/w.py", location="L3", community=widget_community),
        stub("a_basemodel", "BaseModel", community=c1),
        stub("b_basemodel", "BaseModel", community=c2),
    ]
    nodes.extend(extra_nodes)
    gdir = tmp_path / "graphify-out"
    gdir.mkdir(exist_ok=True)
    data = {"built_at_commit": commit * 40, "nodes": nodes, "links": []}
    from sidegraph.engine.reader import GraphifyReader

    return GraphifyReader(write_graph(gdir, "graph.json", data))


@pytest.mark.parametrize("sibling_holds_old", [False, True])
def test_a_collapsed_leaf_vacates_its_shared_community_when_the_stubs_split(
    tmp_path, sibling_holds_old
):
    import subprocess

    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "src").mkdir()
    (tmp_path / "src/w.py").write_text("class Widget: pass\n")
    store = Store(tmp_path / ".sidegraph")
    widget_community = 7 if sibling_holds_old else 4
    r1 = _sync_graph(tmp_path, "a", widget_community, 7, 7)
    d = _decision(store)
    answer = resolve_and_bind(d.id, Descriptor(name="BaseModel"), r1, store)
    assert (answer.status, answer.candidates) == ("resolved", [])
    assert _rows(store, d.id) == [(1, "live", "community:7"), (2, "live", "BaseModel")]
    resolve_and_bind(d.id, Descriptor(name="Widget", file_path="src/w.py"), r1, store)
    sync(store, r1)
    canonical = {p.relative_to(store.path): p.read_bytes() for p in store.path.rglob("*.json")}
    r2 = _sync_graph(tmp_path, "b", widget_community, 1, 2)
    sync(store, r2)
    entity = store.find_entity("BaseModel", None)
    assert entity is not None and entity.last_seen_community is None
    rows = _rows(store, d.id)
    assert (2, "live", "BaseModel") in rows
    expected = "live" if sibling_holds_old else "orphaned"
    assert (1, expected, "community:7") in rows
    assert canonical == {
        p.relative_to(store.path): p.read_bytes() for p in store.path.rglob("*.json")
    }


def test_reconcile_ignores_a_collapsed_leaf_when_communities_renumber(tmp_path):
    import subprocess

    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "src").mkdir()
    (tmp_path / "src/w.py").write_text("class Widget: pass\n")
    store = Store(tmp_path / ".sidegraph")
    r1 = _sync_graph(tmp_path, "a", 4, 1, 2)
    d = _decision(store)
    resolve_and_bind(d.id, Descriptor(name="Widget", file_path="src/w.py"), r1, store)
    resolve_and_bind(d.id, Descriptor(name="BaseModel"), r1, store)
    sync(store, r1)
    r2 = _sync_graph(tmp_path, "b", 8, 5, 6)
    sync(store, r2)
    rows = _rows(store, d.id)
    assert (1, "orphaned", "community:4") in rows
    assert (1, "live", "community:8") in rows


@pytest.mark.parametrize("resolve_raises, leaf_status", [(False, "degraded"), (True, "live")])
def test_reconcile_abstains_when_a_collapsed_leaf_is_no_longer_resolved(
    tmp_path, monkeypatch, resolve_raises, leaf_status
):
    import subprocess

    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "src").mkdir()
    (tmp_path / "src/w.py").write_text("class Widget: pass\n")
    store = Store(tmp_path / ".sidegraph")
    r1 = _sync_graph(tmp_path, "a", 4, 1, 2)
    d = _decision(store)
    resolve_and_bind(d.id, Descriptor(name="Widget", file_path="src/w.py"), r1, store)
    resolve_and_bind(d.id, Descriptor(name="BaseModel"), r1, store)
    sync(store, r1)
    r2 = _sync_graph(
        tmp_path,
        "b",
        8,
        5,
        6,
        extra_nodes=(node("real_basemodel", "BaseModel", "src/models.py", community=9),),
    )
    if resolve_raises:
        original = r2.resolve

        def fail_basemodel(desc, *, collapse=True):
            if desc.name == "BaseModel":
                raise RuntimeError("injected resolver failure")
            return original(desc, collapse=collapse)

        monkeypatch.setattr(r2, "resolve", fail_basemodel)
    report = sync(store, r2)
    if resolve_raises:
        assert any(
            outcome.canonical_name == "BaseModel" and outcome.status == "error"
            for outcome in report.outcomes
        )
    rows = _rows(store, d.id)
    assert (2, leaf_status, "BaseModel") in rows
    assert (1, "live", "community:4") in rows
    assert (1, "live", "community:8") in rows


def test_reconcile_keeps_a_degraded_leaf_unknown_after_another_record_refreshes_its_mapping(
    tmp_path,
):
    import subprocess

    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "src").mkdir()
    (tmp_path / "src/w.py").write_text("class Widget: pass\n")
    store = Store(tmp_path / ".sidegraph")
    r1 = _sync_graph(tmp_path, "a", 4, 1, 2)
    d = _decision(store)
    resolve_and_bind(d.id, Descriptor(name="Widget", file_path="src/w.py"), r1, store)
    resolve_and_bind(d.id, Descriptor(name="BaseModel"), r1, store)
    sync(store, r1)
    r2 = _sync_graph(
        tmp_path,
        "b",
        4,
        5,
        6,
        extra_nodes=(node("real_basemodel", "BaseModel", "src/models.py", community=9),),
    )
    sync(store, r2)
    assert (2, "degraded", "BaseModel") in _rows(store, d.id)
    r3 = _sync_graph(tmp_path, "c", 8, 5, 6)
    other = _decision(store)
    resolve_and_bind(other.id, Descriptor(name="BaseModel"), r3, store)
    widget = store.find_entity("Widget", "src/w.py")
    assert widget is not None
    rebind_entity(widget, store, r3)
    rows = _rows(store, d.id)
    assert (2, "degraded", "BaseModel") in rows
    assert (1, "live", "community:4") in rows
    assert (1, "live", "community:8") in rows


def test_moved_rung_orphans_a_path_carrier_when_only_identical_stubs_match(tmp_path):
    store = Store(tmp_path / ".sidegraph")
    reader = reader_of(
        tmp_path, stub("m_b", "BaseModel", community=1), stub("m_c", "BaseModel", community=2)
    )
    e = store.upsert_entity(
        Entity(
            canonical_name="BaseModel",
            descriptor=Descriptor(name="BaseModel", file_path="src/old.py"),
            last_seen_node_id="old",
            last_seen_graph_version="v0",
            last_seen_community="7",
        )
    )
    d = _decision(store)
    store.add_binding(AnchorBinding(record_id=d.id, entity_id=e.entity_id, tier=2, status="live"))
    outcome = rebind_entity(e, store, reader, repo_root=Path(tmp_path))
    assert outcome.status == "orphaned"
    assert [r for r in _rows(store, d.id) if r[0] == 2] == [(2, "orphaned", "BaseModel")]


def test_domain_seed_anchor_naming_stubs_of_one_community_contributes_it(tmp_path):
    reader = reader_of(
        tmp_path,
        stub("m_b", "BaseModel", community=7),
        stub("m_c", "BaseModel", community=7),
        node("w", "Widget", "src/w.py", community=1),
    )
    domain = Domain(
        slug="models",
        title="Models",
        summary="s",
        seed_anchors=[Descriptor(name="BaseModel")],
        path_prefixes=[],
        communities=[],
        provenance=Provenance(source="manual"),
    )
    nodes = reader.list_nodes()
    got = _recompute_domain_communities(
        domain, nodes, {n.community for n in nodes if n.community}, reader
    )
    assert got[0] == ["7"]


def test_seed_ladder_reports_stubs_ambiguous_and_asks_for_tolerance(tmp_path):
    from sidegraph.seed_ladder import needs_tolerance, tolerate

    reader = _stub_graph(tmp_path)
    assert needs_tolerance([Seed(name="BaseModel")], reader) is True
    bare = tolerate([Seed(name="BaseModel")], reader, None)
    assert bare.exact == []
    assert [(n.outcome, n.total) for n in bare.notes] == [("unresolved-name", 2)]
