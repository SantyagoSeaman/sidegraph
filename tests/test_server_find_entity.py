"""find_entity — the missing entity_id discovery tool (see CLAUDE.md gap notes)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from sidegraph import server
from sidegraph.engine.reader import GraphifyReader
from sidegraph.schema import Descriptor, Entity
from sidegraph.server import (
    _FIND_ENTITY_NO_GRAPH,
    _FIND_ENTITY_NO_RECORD,
    _FIND_ENTITY_OTHER_FILE,
    _FIND_ENTITY_UNKNOWN_NAME,
    _find_entity_impl,
    _find_entity_listing,
)
from sidegraph.store import Store
from tests.test_bounded_listings import call

_GRAPH = Path(__file__).parent / "fixtures" / "member_names_python_graph.json"
_FILE = "src/sidegraph/engine/reader.py"
_QUERY = "GraphifyReader.graph_version"
_NODE = "src_sidegraph_engine_reader_graphifyreader_graph_version"


@pytest.fixture
def reader() -> GraphifyReader:
    return GraphifyReader(_GRAPH)


@pytest.fixture
def member_store(tmp_path):
    with Store(tmp_path / "t.db") as store:
        entity = store.upsert_entity(
            Entity(
                canonical_name=".graph_version()",
                descriptor=Descriptor(name=".graph_version()", file_path=_FILE),
                last_seen_node_id=_NODE,
            )
        )
        yield store, entity


def test_find_entity_by_name_and_file_path(tmp_path):
    store = Store(tmp_path / "t.db")
    e = store.upsert_entity(
        Entity(
            canonical_name="Trader",
            descriptor=Descriptor(name="Trader", file_path="trader/exec.py"),
        )
    )

    load_reader = Mock(return_value=None)
    out = _find_entity_impl(store, "Trader", "trader/exec.py", load_reader=load_reader)

    load_reader.assert_not_called()
    assert out["found"] is True
    assert out["entity_id"] == e.entity_id
    assert out["canonical_name"] == "Trader"
    assert out["descriptor"] == {"name": "Trader", "file_path": "trader/exec.py"}
    assert out["bindings"] == []


def test_find_entity_name_only_unique_match(tmp_path):
    store = Store(tmp_path / "t.db")
    e = store.upsert_entity(
        Entity(
            canonical_name="Trader",
            descriptor=Descriptor(name="Trader", file_path="trader/exec.py"),
        )
    )

    # No file_path given, and the exact descriptor lookup (file_path=None) misses —
    # falls back to the name-only scan, which finds exactly one match.
    load_reader = Mock(return_value=None)
    out = _find_entity_impl(store, "Trader", load_reader=load_reader)

    load_reader.assert_not_called()
    assert out["found"] is True
    assert out["entity_id"] == e.entity_id


def test_find_entity_name_only_ambiguous_returns_candidates_never_guesses(tmp_path):
    store = Store(tmp_path / "t.db")
    a = store.upsert_entity(
        Entity(
            canonical_name="Trader",
            descriptor=Descriptor(name="Trader", file_path="trader/exec.py"),
        )
    )
    b = store.upsert_entity(
        Entity(
            canonical_name="Trader",
            descriptor=Descriptor(name="Trader", file_path="other/trader.py"),
        )
    )

    load_reader = Mock(return_value=None)
    out = _find_entity_impl(store, "Trader", load_reader=load_reader)

    load_reader.assert_not_called()
    assert out["found"] is False
    assert "candidates" in out
    ids = {c["entity_id"] for c in out["candidates"]}
    assert ids == {a.entity_id, b.entity_id}
    files = {c["file_path"] for c in out["candidates"]}
    assert files == {"trader/exec.py", "other/trader.py"}


def test_find_entity_not_found(tmp_path):
    store = Store(tmp_path / "t.db")

    out = _find_entity_impl(store, "NoSuchEntity")

    assert out == {"found": False, "hint": _FIND_ENTITY_NO_GRAPH}


def test_find_entity_reaches_graph_owned_member(member_store, reader):
    store, entity = member_store
    out = _find_entity_impl(store, _QUERY, _FILE, load_reader=lambda: reader)
    assert out["found"] is True
    assert out["entity_id"] == entity.entity_id


def test_find_entity_graph_symbol_without_memory(tmp_path, reader):
    with Store(tmp_path / "t.db") as store:
        out = _find_entity_impl(store, "NodeRef", _FILE, load_reader=lambda: reader)
    assert out == {"found": False, "hint": _FIND_ENTITY_NO_RECORD}


@pytest.mark.parametrize("name", ["no such thing", _QUERY])
def test_find_entity_unknown_name_with_graph(member_store, reader, name):
    store, _ = member_store
    out = _find_entity_impl(store, name, load_reader=lambda: reader)
    assert out == {"found": False, "hint": _FIND_ENTITY_UNKNOWN_NAME}


@pytest.mark.parametrize("stored_file", ["trader/exec.py", None])
def test_find_entity_mismatch_hint_requires_a_stored_file(tmp_path, stored_file):
    with Store(tmp_path / "t.db") as store:
        entity = store.upsert_entity(
            Entity(
                canonical_name="Trader",
                descriptor=Descriptor(name="Trader", file_path=stored_file),
            )
        )
        out = _find_entity_impl(store, "Trader", "other/x.py")
    assert out["found"] is True
    assert out["entity_id"] == entity.entity_id
    if stored_file:
        assert out["hint"] == _FIND_ENTITY_OTHER_FILE
    else:
        assert "hint" not in out


def test_find_entity_returns_both_identities_on_one_graph_node(member_store, reader):
    store, bare = member_store
    other = store.upsert_entity(
        Entity(
            canonical_name="Reader.graph_version",
            descriptor=Descriptor(name="Reader.graph_version", file_path=_FILE),
            last_seen_node_id=_NODE,
        )
    )

    out = _find_entity_impl(store, _QUERY, _FILE, load_reader=lambda: reader)

    assert out["found"] is False
    assert out["candidates"] == sorted(
        [
            {
                "entity_id": entity.entity_id,
                "canonical_name": entity.canonical_name,
                "file_path": _FILE,
            }
            for entity in (bare, other)
        ],
        key=lambda candidate: candidate["entity_id"],
    )


@pytest.mark.parametrize("fail_at", ["load", "resolve"])
def test_find_entity_uses_no_graph_hint_on_reader_error(tmp_path, fail_at):
    class BrokenReader:
        def resolve(self, *args, **kwargs):
            raise RuntimeError("test resolver failure")

    def load_reader():
        if fail_at == "load":
            raise RuntimeError("test loader failure")
        return BrokenReader()

    with Store(tmp_path / "t.db") as store:
        out = _find_entity_impl(store, "NodeRef", _FILE, load_reader=load_reader)
    assert out == {"found": False, "hint": _FIND_ENTITY_NO_GRAPH}


def test_find_entity_listing_keeps_mismatch_and_clipped_hints(tmp_path):
    name = "a" * 1000
    with Store(tmp_path / "t.db") as store:
        store.upsert_entity(
            Entity(canonical_name=name, descriptor=Descriptor(name=name, file_path="a.py"))
        )
        out = json.loads(_find_entity_listing(store, name, "b.py"))
    assert out["hint"].startswith(_FIND_ENTITY_OTHER_FILE)
    assert "Bounded: the entity canonical_name was clipped." in out["hint"]


def test_find_entity_mcp_uses_graph_on_miss(monkeypatch, member_store):
    store, entity = member_store
    monkeypatch.setattr(server, "_store", store)
    monkeypatch.setenv("SIDEGRAPH_GRAPH", str(_GRAPH))

    result = call("find_entity", {"name": _QUERY, "file_path": _FILE})

    assert not result.is_error, result.content
    out = json.loads(result.content[0].text)
    assert out["found"] is True
    assert out["entity_id"] == entity.entity_id


def test_find_entity_offers_entities_on_ambiguous_graph_nodes(tmp_path):
    graph = tmp_path / "graph.json"
    graph.write_text(
        json.dumps(
            {
                "nodes": [
                    {
                        "id": suffix,
                        "label": "NodeRef",
                        "source_file": f"{suffix}.py",
                        "file_type": "code",
                    }
                    for suffix in ("a", "b")
                ],
                "links": [],
            }
        )
    )
    with Store(tmp_path / "t.db") as store:
        entities = [
            store.upsert_entity(
                Entity(
                    canonical_name=f"noderef_{suffix}",
                    descriptor=Descriptor(name=f"noderef_{suffix}", file_path=f"{suffix}.py"),
                    last_seen_node_id=suffix,
                )
            )
            for suffix in ("a", "b")
        ]
        graph_reader = GraphifyReader(graph)
        out = _find_entity_impl(store, "NodeRef", load_reader=lambda: graph_reader)
    assert out["found"] is False
    assert [candidate["entity_id"] for candidate in out["candidates"]] == sorted(
        entity.entity_id for entity in entities
    )


def test_find_entity_miss_allows_an_unmapped_spelling(tmp_path, reader):
    with Store(tmp_path / "t.db") as store:
        name = "GraphifyReader::graph_version"
        store.upsert_entity(
            Entity(canonical_name=name, descriptor=Descriptor(name=name, file_path=_FILE))
        )
        out = _find_entity_impl(store, _QUERY, _FILE, load_reader=lambda: reader)
    assert out == {
        "found": False,
        "hint": (
            "The code graph holds this name, but this lookup found no memory entity for it. "
            "An entity stored under another spelling of the name is not ruled out."
        ),
    }


@pytest.mark.parametrize("name", ["", "  "])
def test_find_entity_blank_name_does_not_load_graph(member_store, reader, name):
    store, _ = member_store
    for other in ("NodeRef", "Community"):
        store.upsert_entity(
            Entity(canonical_name=other, descriptor=Descriptor(name=other, file_path=_FILE))
        )
    load_reader = Mock(return_value=reader)

    out = _find_entity_impl(store, name, _FILE, load_reader=load_reader)

    assert out == {"found": False, "hint": _FIND_ENTITY_UNKNOWN_NAME}
    load_reader.assert_not_called()


def test_find_entity_includes_bindings_summary(tmp_path):
    from datetime import UTC, datetime

    from sidegraph.schema import AnchorBinding, Decision, DecisionKind, Provenance

    store = Store(tmp_path / "t.db")
    e = store.upsert_entity(
        Entity(
            canonical_name="Trader",
            descriptor=Descriptor(name="Trader", file_path="trader/exec.py"),
        )
    )
    d = store.add_decision(
        Decision(
            title="t",
            kind=DecisionKind.ADR,
            context="c",
            choice="ch",
            valid_from=datetime.now(UTC),
            provenance=Provenance(source="manual"),
        )
    )
    store.add_binding(AnchorBinding(record_id=d.id, entity_id=e.entity_id, tier=2))

    out = _find_entity_impl(store, "Trader", "trader/exec.py")

    assert out["bindings"] == [
        {"record_id": d.id, "record_type": "decision", "tier": 2, "status": "live"}
    ]


def test_find_entity_bindings_report_record_type_for_facts(tmp_path):
    """A binding to a Fact (not a Decision) is reported with record_type "fact" —
    see CLAUDE.md facts layer / store.add_binding widening."""
    from datetime import UTC, datetime

    from sidegraph.schema import AnchorBinding, Fact, Provenance

    store = Store(tmp_path / "t.db")
    e = store.upsert_entity(
        Entity(
            canonical_name="Trader",
            descriptor=Descriptor(name="Trader", file_path="trader/exec.py"),
        )
    )

    f = store.add_fact(
        Fact(
            statement="httpx has no built-in retry",
            source="httpx docs",
            valid_from=datetime.now(UTC),
            provenance=Provenance(source="test"),
        )
    )
    store.add_binding(AnchorBinding(record_id=f.id, entity_id=e.entity_id, tier=2))

    out = _find_entity_impl(store, "Trader", "trader/exec.py")

    assert out["bindings"] == [
        {"record_id": f.id, "record_type": "fact", "tier": 2, "status": "live"}
    ]
