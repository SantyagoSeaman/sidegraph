"""A path-less anchor takes its node's file after a unique resolve, before any lookup, unless the
store already holds the identity: one same-named entity with a path (adoption), or a path-less
entity of that name while none sits at the node's file. Capture's dedup reads the identity the bind
will use.
# see design/superpowers/specs/2026-10-14-graphify-upgrade-design.md (D2)"""

from __future__ import annotations

from datetime import UTC, datetime

from sidegraph import capture
from sidegraph.anchoring import anchor_descriptor, resolve_and_bind
from sidegraph.retrieval import Seed, resolve_seeds
from sidegraph.schema import (
    AnchorBinding,
    Decision,
    DecisionKind,
    DecisionStatus,
    Descriptor,
    Provenance,
)
from sidegraph.server import _add_anchors_impl
from sidegraph.store import Store
from tests.graph_fixtures import CLI, node, reader_of, two_cli_graph

DRAFT = {"title": "Keep widgets small", "kind": "gotcha", "context": "c", "choice": "ch"}


def _decision(store, title="t", kind=DecisionKind.ADR):
    return store.add_decision(
        Decision(
            title=title,
            kind=kind,
            status=DecisionStatus.ACCEPTED,
            context="c",
            choice="ch",
            valid_from=datetime.now(UTC),
            provenance=Provenance(source="manual"),
        )
    )


def _widget_graph(tmp_path):
    """One `Widget`, in src/a.py; src/old.py, where an older one lived, is gone."""
    return reader_of(
        tmp_path, node("w", "Widget", "src/a.py", location="L3"), node("fa", "a.py", "src/a.py")
    )


def test_a_resolved_answer_carries_its_nodes_file(tmp_path):
    # red against unfixed code: ResolveResult has no file_path
    r = two_cli_graph(tmp_path)
    assert r.resolve(Descriptor(name="main")).file_path == CLI
    assert r.resolve(Descriptor(name="cli.py", file_path=CLI)).file_path == CLI
    assert r.resolve(Descriptor(name="cli.py")).file_path is None  # ambiguous
    assert r.resolve(Descriptor(name="nothing")).file_path is None


def test_anchor_descriptor_upgrades_only_when_the_store_does_not_hold_the_identity(tmp_path):
    # red against unfixed code: ImportError
    reader, store = _widget_graph(tmp_path), Store(tmp_path / ".sidegraph")
    pathless = Descriptor(name="Widget")
    assert anchor_descriptor(pathless, reader, store) == Descriptor(
        name="Widget", file_path="src/a.py"
    )
    for ref in (Descriptor(name="Widget", file_path="src/a.py"), Descriptor(name="Nothing")):
        assert anchor_descriptor(ref, reader, store) is ref
    assert anchor_descriptor(pathless, None, store) is pathless
    store.get_or_create_entity(pathless)  # a path-less entity, none at src/a.py: kept
    assert anchor_descriptor(pathless, reader, store) is pathless
    store.get_or_create_entity(
        Descriptor(name="Widget", file_path="src/a.py")
    )  # one carrier: adopted
    assert anchor_descriptor(pathless, reader, store) is pathless


def test_a_path_less_symbol_with_no_entity_mints_a_pathful_one(tmp_path):
    # red against unfixed code: the entity is Descriptor(name="Widget", file_path=None)
    reader, store = _widget_graph(tmp_path), Store(tmp_path / ".sidegraph")
    result = resolve_and_bind(_decision(store).id, Descriptor(name="Widget"), reader, store)
    (leaf,) = [b for b in result if b.tier == 2]
    assert store.get_entity(leaf.entity_id).descriptor == Descriptor(
        name="Widget", file_path="src/a.py"
    )


def test_a_path_less_suffix_anchor_binds_the_file_entity_and_mints_no_twin(tmp_path):
    # red against unfixed code: a path-less "sidegraph/cli.py" entity
    reader, store = two_cli_graph(tmp_path), Store(tmp_path / ".sidegraph")
    result = resolve_and_bind(
        _decision(store).id, Descriptor(name="sidegraph/cli.py"), reader, store
    )
    (leaf,) = [b for b in result if b.tier == 2]
    assert store.get_entity(leaf.entity_id).descriptor == Descriptor(name="cli.py", file_path=CLI)
    assert [e for e in store.iter_concrete_entities() if e.descriptor.file_path is None] == []


def test_a_single_carrier_still_wins_over_a_cross_suffix_answer(tmp_path):
    # Green on unfixed code; red against "upgrade before adoption" (a discarded design,
    # declared): the doc heading would get the binding instead of the code entity.
    reader = reader_of(
        tmp_path, node("h", "Store", "docs/guide.md", file_type="document", location="L5")
    )
    store = Store(tmp_path / ".sidegraph")
    carrier = store.get_or_create_entity(Descriptor(name="Store", file_path="src/store.py"))
    result = resolve_and_bind(_decision(store).id, Descriptor(name="Store"), reader, store)
    (leaf,) = [b for b in result if b.tier == 2]
    assert leaf.entity_id == carrier.entity_id
    assert store.get_entity(carrier.entity_id).last_seen_node_id is None  # rail: no refresh


def test_a_legacy_path_less_entity_keeps_its_records_and_dedup(tmp_path):
    # Green on unfixed code; red against upgrading regardless of a path-less entity (a discarded
    # design, declared): the draft is "written" and a second, pathful SyncReport is minted.
    reader = reader_of(tmp_path, node("s", "SyncReport", "src/sync.py", location="L40"))
    store = Store(tmp_path / ".sidegraph")
    legacy = store.get_or_create_entity(Descriptor(name="SyncReport"))
    old = _decision(store, "Report every orphan", DecisionKind.GOTCHA)
    store.add_binding(AnchorBinding(record_id=old.id, entity_id=legacy.entity_id, tier=2))
    draft = {
        "title": "Report every orphan",
        "kind": "gotcha",
        "context": "c",
        "choice": "ch",
        "anchors": [{"name": "SyncReport"}],
    }
    (again,) = capture.propose([draft], store, reader)
    assert again.status == "deduped"
    result = resolve_and_bind(_decision(store).id, Descriptor(name="SyncReport"), reader, store)
    (leaf,) = [b for b in result if b.tier == 2]
    assert leaf.entity_id == legacy.entity_id
    assert [e.entity_id for e in store.iter_concrete_entities()] == [legacy.entity_id]


def test_the_nodes_own_entity_wins_over_a_legacy_path_less_one(tmp_path):
    # red against unfixed code: two carriers block adoption, so the path-less one is reused
    reader, store = _widget_graph(tmp_path), Store(tmp_path / ".sidegraph")
    store.get_or_create_entity(Descriptor(name="Widget"))
    own = store.get_or_create_entity(Descriptor(name="Widget", file_path="src/a.py"))
    store.get_or_create_entity(Descriptor(name="Widget", file_path="src/old.py"))
    result = resolve_and_bind(_decision(store).id, Descriptor(name="Widget"), reader, store)
    (leaf,) = [b for b in result if b.tier == 2]
    assert leaf.entity_id == own.entity_id


def test_a_path_less_symbol_dedups_where_two_carriers_block_adoption(tmp_path):
    # red against unfixed code: "written" (two carriers, so no adoption and no match)
    reader, store = _widget_graph(tmp_path), Store(tmp_path / ".sidegraph")
    store.get_or_create_entity(Descriptor(name="Widget", file_path="src/old.py"))
    (first,) = capture.propose(
        [{**DRAFT, "anchors": [{"name": "Widget", "file_path": "src/a.py"}]}], store, reader
    )
    assert first.status == "written"
    (again,) = capture.propose([{**DRAFT, "anchors": [{"name": "Widget"}]}], store, reader)
    assert again.status == "deduped"


def test_a_path_less_suffix_anchor_dedups_against_the_record_on_that_file(tmp_path):
    # red against unfixed code: "written"
    reader, store = two_cli_graph(tmp_path), Store(tmp_path / ".sidegraph")
    store.get_or_create_entity(Descriptor(name="cli.py", file_path="lib/sidegraph/cli.py"))
    (first,) = capture.propose(
        [{**DRAFT, "anchors": [{"name": "cli.py", "file_path": CLI}]}], store, reader
    )
    assert first.status == "written"
    (again,) = capture.propose(
        [{**DRAFT, "anchors": [{"name": "sidegraph/cli.py"}]}], store, reader
    )
    assert again.status == "deduped"


def test_a_path_less_fact_anchor_dedups_too(tmp_path):
    # red against unfixed code: written twice
    reader, store = _widget_graph(tmp_path), Store(tmp_path / ".sidegraph")
    store.get_or_create_entity(Descriptor(name="Widget", file_path="src/old.py"))
    fact = {"statement": "Widgets render in one pass.", "source": "observed"}
    (first,) = capture.propose_facts(
        [{**fact, "anchors": [{"name": "Widget", "file_path": "src/a.py"}]}], store, reader
    )
    assert first.status == "written"
    (again,) = capture.propose_facts([{**fact, "anchors": [{"name": "Widget"}]}], store, reader)
    assert again.status == "deduped"


def test_add_anchors_reports_the_entity_the_upgrade_bound(tmp_path):
    # red against unfixed code: two carriers block adoption, a path-less twin is minted
    reader, store = _widget_graph(tmp_path), Store(tmp_path / ".sidegraph")
    store.get_or_create_entity(Descriptor(name="Widget", file_path="src/old.py"))
    target = store.get_or_create_entity(Descriptor(name="Widget", file_path="src/a.py"))
    out = _add_anchors_impl(store, reader, _decision(store).id, [{"name": "Widget"}])
    assert [b["entity_id"] for b in out["bound"]] == [target.entity_id]


def test_a_retrieval_seed_named_by_the_alias_reaches_the_file_entity(tmp_path):
    # red against unfixed code: the node is still named "sidegraph/cli.py", and no entity is found
    reader, store = two_cli_graph(tmp_path), Store(tmp_path / ".sidegraph")
    entity = store.get_or_create_entity(Descriptor(name="cli.py", file_path=CLI))
    resolved = resolve_seeds([Seed(name="sidegraph/cli.py")], reader, store)
    assert entity.entity_id in [e.entity_id for e in resolved.seed_entities]
