"""One identity rule for file anchors: a name that is a path suffix of its own file is that
file's basename, on both sides of every comparison.
# see design/superpowers/specs/2026-10-14-graphify-upgrade-design.md (D2)"""

from __future__ import annotations

from datetime import UTC, datetime

from sidegraph import capture
from sidegraph.schema import (
    AnchorBinding,
    Decision,
    DecisionKind,
    DecisionStatus,
    Descriptor,
    Entity,
    Fact,
    Provenance,
    file_anchor_name,
)
from sidegraph.server import _find_entity_impl, _get_entity_history_impl
from sidegraph.store import Store
from tests.graph_fixtures import node, reader_of

P = "src/sidegraph/cli.py"


def test_a_path_suffix_of_its_own_file_folds_to_the_basename():
    assert file_anchor_name("sidegraph/cli.py", P) == "cli.py"
    assert file_anchor_name(P, P) == "cli.py"
    assert file_anchor_name(f" {P} ", P) == "cli.py"


def test_anything_else_is_left_as_it_is():
    assert file_anchor_name("cli.py", P) == "cli.py"
    assert file_anchor_name("other/cli.py", P) == "other/cli.py"
    assert file_anchor_name("idegraph/cli.py", P) == "idegraph/cli.py"  # the boundary is a '/'
    assert file_anchor_name("sidegraph/cli.py", None) == "sidegraph/cli.py"
    assert file_anchor_name("Store.close", "src/sidegraph/store.py") == "Store.close"


OLDER = "00000000000000000000000000"
STORE = "src/sidegraph/store.py"


def _suffix_entity(store, name="sidegraph/cli.py"):
    return store.upsert_entity(
        Entity(canonical_name=name, descriptor=Descriptor(name=name, file_path=P))
    )


def test_a_stored_suffix_name_and_a_basename_descriptor_are_one_identity(tmp_path):
    # red against unfixed code: a second entity
    s = Store(tmp_path / ".sidegraph")
    stored = _suffix_entity(s)
    assert (
        s.get_or_create_entity(Descriptor(name="cli.py", file_path=P)).entity_id == stored.entity_id
    )


def test_a_stored_basename_and_a_path_named_descriptor_are_one_identity(tmp_path):
    # red against unfixed code: a second entity
    s = Store(tmp_path / ".sidegraph")
    stored = s.get_or_create_entity(Descriptor(name="cli.py", file_path=P))
    for name in (P, "sidegraph/cli.py"):
        assert (
            s.get_or_create_entity(Descriptor(name=name, file_path=P)).entity_id == stored.entity_id
        )


def test_a_new_entity_is_minted_under_the_basename(tmp_path):
    # red against unfixed code: canonical_name is the full path
    e = Store(tmp_path / ".sidegraph").get_or_create_entity(Descriptor(name=P, file_path=P))
    assert (e.canonical_name, e.descriptor) == ("cli.py", Descriptor(name="cli.py", file_path=P))


def test_lookups_prefer_the_basename_spelling_over_an_older_full_path_twin(tmp_path):
    # red against unfixed code (find_entity(P, P) returns the twin) and against lowest-id-wins
    # under the folded key (a discarded design, declared)
    s = Store(tmp_path / ".sidegraph")
    twin = s.upsert_entity(
        Entity(entity_id=OLDER, canonical_name=P, descriptor=Descriptor(name=P, file_path=P))
    )
    basename = s.upsert_entity(
        Entity(canonical_name="cli.py", descriptor=Descriptor(name="cli.py", file_path=P))
    )
    assert twin.entity_id < basename.entity_id
    assert s.find_entity(P, P).entity_id == basename.entity_id
    assert s.find_entity("cli.py", P).entity_id == basename.entity_id
    assert s.resolve_descriptor("sidegraph/cli.py", None).entity_id == basename.entity_id


def test_a_path_less_suffix_name_finds_only_the_entity_whose_file_it_names(tmp_path):
    # red against unfixed code: []
    s = Store(tmp_path / ".sidegraph")
    mine = s.get_or_create_entity(Descriptor(name="cli.py", file_path=P))
    s.get_or_create_entity(Descriptor(name="cli.py", file_path="src/sidegraph/bootstrap/cli.py"))
    assert [e.entity_id for e in s.find_entities_by_name("sidegraph/cli.py")] == [mine.entity_id]
    assert len(s.find_entities_by_name("cli.py")) == 2


def test_the_find_entity_tool_finds_a_suffix_named_entity_by_its_basename(tmp_path):
    # red against unfixed code: {"found": False}
    s = Store(tmp_path / ".sidegraph")
    stored = _suffix_entity(s)
    out = _find_entity_impl(s, "cli.py", P)
    assert out["found"] is True and out["entity_id"] == stored.entity_id


def test_the_find_entity_tool_reads_spelling_twins_as_one_identity(tmp_path):
    # Red against unfixed code: the full-path query returns the twin. Red against the store's
    # folding without the per-identity collapse: "store.py" then matches both twins and comes
    # back ambiguous (a discarded design, declared).
    s = Store(tmp_path / ".sidegraph")
    s.upsert_entity(
        Entity(
            entity_id=OLDER,
            canonical_name=STORE,
            descriptor=Descriptor(name=STORE, file_path=STORE),
        )
    )
    basename = s.upsert_entity(
        Entity(canonical_name="store.py", descriptor=Descriptor(name="store.py", file_path=STORE))
    )
    for query in ("store.py", STORE):
        out = _find_entity_impl(s, query)
        assert (out["found"], out["entity_id"]) == (True, basename.entity_id), query
    s.upsert_entity(
        Entity(
            canonical_name="store.py",
            descriptor=Descriptor(name="store.py", file_path="lib/store.py"),
        )
    )
    out = _find_entity_impl(s, "store.py")
    assert out["found"] is False and len(out["candidates"]) == 2  # two files stay ambiguous


def _twin_store(tmp_path):
    """A full-path twin (older id) and the basename entity of one file, each with its own record."""
    s = Store(tmp_path / ".sidegraph")
    twin = s.upsert_entity(
        Entity(
            entity_id=OLDER,
            canonical_name=STORE,
            descriptor=Descriptor(name=STORE, file_path=STORE),
        )
    )
    base = s.upsert_entity(
        Entity(canonical_name="store.py", descriptor=Descriptor(name="store.py", file_path=STORE))
    )

    def record(title, entity):
        d = s.add_decision(
            Decision(
                title=title,
                kind=DecisionKind.GOTCHA,
                status=DecisionStatus.ACCEPTED,
                context="c",
                choice="ch",
                valid_from=datetime.now(UTC),
                provenance=Provenance(source="manual"),
            )
        )
        s.add_binding(AnchorBinding(record_id=d.id, entity_id=entity.entity_id, tier=2))

    record("Rebuild the index atomically", twin)
    record("Keep the store append-only", base)
    return s, twin, base


def test_identity_members_are_the_spelling_twins_of_one_file(tmp_path):
    # red against unfixed code: no such method
    s, twin, base = _twin_store(tmp_path)
    s.upsert_entity(
        Entity(
            canonical_name="store.py",
            descriptor=Descriptor(name="store.py", file_path="lib/store.py"),
        )
    )
    assert sorted(e.entity_id for e in s.identity_members(base)) == sorted(
        [twin.entity_id, base.entity_id]
    )


def test_the_history_of_a_file_includes_its_twins_records(tmp_path):
    # red against unfixed code: only the basename entity's record
    s, _twin, _base = _twin_store(tmp_path)
    for query in (STORE, "store.py"):
        out = _find_entity_impl(s, query, STORE)
        titles = sorted(h["title"] for h in _get_entity_history_impl(s, out["entity_id"]))
        assert titles == ["Keep the store append-only", "Rebuild the index atomically"], query


def test_a_twins_record_proposed_again_by_its_full_path_is_deduped(tmp_path):
    # red against unfixed code: "written"
    s, _twin, _base = _twin_store(tmp_path)
    r = reader_of(tmp_path, node("f", "store.py", STORE))
    draft = {
        "title": "Rebuild the index atomically",
        "kind": "gotcha",
        "context": "c",
        "choice": "ch",
        "anchors": [{"name": STORE, "file_path": STORE}],
    }
    (res,) = capture.propose([draft], s, r)
    assert res.status == "deduped"


def test_a_twins_fact_proposed_again_is_deduped(tmp_path):
    # red against unfixed code: "written"
    s, twin, _base = _twin_store(tmp_path)
    r = reader_of(tmp_path, node("f", "store.py", STORE))
    fact = {"statement": "The index rebuilds in one pass.", "source": "observed"}
    s_fact = s.add_fact(
        Fact(
            statement=fact["statement"],
            source="observed",
            valid_from=datetime.now(UTC),
            provenance=Provenance(source="manual"),
        )
    )
    s.add_binding(AnchorBinding(record_id=s_fact.id, entity_id=twin.entity_id, tier=2))
    (res,) = capture.propose_facts(
        [{**fact, "anchors": [{"name": STORE, "file_path": STORE}]}], s, r
    )
    assert res.status == "deduped"
