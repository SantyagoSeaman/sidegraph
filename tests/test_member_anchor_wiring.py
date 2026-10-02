"""The member-name fix reaches the write path, the sync stamp and the read path.

# see design/superpowers/specs/2026-10-01-member-anchor-names-design.md (D1, D2, D4;
# tests T11-T14)

Names follow ``test_reader_member_names.py``: the spec's ledger is written against a private
corpus, so its playback type is ``AudioPlayback`` here, in ``Sources/App/AudioPlayback.swift``;
the test numbers T11-T14 are the ledger's.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from sidegraph.anchoring import resolve_and_bind
from sidegraph.capture import propose
from sidegraph.engine.reader import GraphifyReader, ResolveResult
from sidegraph.retrieval import Seed, get_task_context, resolve_seeds
from sidegraph.schema import (
    AnchorBinding,
    Decision,
    DecisionKind,
    DecisionStatus,
    Descriptor,
    Provenance,
)
from sidegraph.store import Store
from sidegraph.sync import LAST_SYNCED_KEY, VOLATILE_STALE_KEY, sync

FIXTURE = Path(__file__).parent / "fixtures" / "member_names_swift_graph.json"
SWIFT_FILE = "Sources/App/AudioPlayback.swift"
PLAY_CLIP_NODE = "sources_app_audioplayback_audioplayback_playclip"
MEMBER = "AudioPlayback.playClip"


def _reader() -> GraphifyReader:
    return GraphifyReader(FIXTURE)


def _gotcha(
    store: Store,
    reader: GraphifyReader,
    name: str = MEMBER,
    title: str = "playClip must stop the ticker first",
) -> Decision:
    """An accepted gotcha anchored to ``name`` in the Swift file, through the real write path."""
    d = store.add_decision(
        Decision(
            title=title,
            kind=DecisionKind.GOTCHA,
            status=DecisionStatus.ACCEPTED,
            context="the ticker races the clip",
            choice="stop the ticker before the clip starts",
            valid_from=datetime.now(UTC),
            provenance=Provenance(source="manual"),
        )
    )
    resolve_and_bind(d.id, Descriptor(name=name, file_path=SWIFT_FILE), reader, store)
    return d


def _leaf_statuses(store: Store, entity_id: str) -> list[str]:
    return [b.status for b in store.bindings_for_entity(entity_id) if b.tier == 2]


# -- T11: the write path --------------------------------------------------------------


def test_t11_propose_anchored_to_type_dot_member_binds_a_live_leaf(tmp_path):
    # red against unfixed code: the leaf is born orphaned (reason name-not-in-file)
    store = Store(tmp_path / "t.db")
    reader = _reader()
    draft = dict(
        title="playClip must stop the ticker first",
        kind="gotcha",
        context="the ticker races the clip",
        choice="stop the ticker first",
        anchors=[{"name": MEMBER, "file_path": SWIFT_FILE}],
    )
    [res] = propose([draft], store, reader, session_id="s1")
    assert res.status == "written"
    assert res.anchors_orphaned == []
    assert res.anchors_skipped == []
    entity = store.resolve_descriptor(MEMBER, SWIFT_FILE)
    assert entity is not None
    assert entity.last_seen_node_id == PLAY_CLIP_NODE
    assert _leaf_statuses(store, entity.entity_id) == ["live"]
    assert {b.tier for b in store.bindings_for_record(res.decision_id)} == {2, 1}


# -- T12: an ordinary sync heals what the old resolver left orphaned ---------------------


def _orphaned_member_leaf(store: Store, reader: GraphifyReader):
    """What the pre-fix write path left behind: an orphaned tier-2 leaf on the member entity."""

    class _OldResolver:
        def resolve(self, desc):
            return ResolveResult(status="unresolved")

        def graph_version(self):
            return reader.graph_version()

    d = store.add_decision(
        Decision(
            title="t",
            kind=DecisionKind.LESSON,
            context="c",
            choice="ch",
            valid_from=datetime.now(UTC),
            provenance=Provenance(source="manual"),
        )
    )
    resolve_and_bind(
        d.id,
        Descriptor(name=MEMBER, file_path=SWIFT_FILE),
        _OldResolver(),  # type: ignore[arg-type]
        store,
    )
    entity = store.resolve_descriptor(MEMBER, SWIFT_FILE)
    assert entity is not None
    assert _leaf_statuses(store, entity.entity_id) == ["orphaned"]
    return d, entity


def test_t12_an_ordinary_sync_after_the_revision_bump_heals_the_orphan(tmp_path):
    # red against unfixed code, and against mutation M7 (the revision not in the stamp): a
    # store stamped by the previous release carries the bare graph version, which then equals
    # what sync compares, so the pass is skipped and the leaf stays orphaned.
    store = Store(tmp_path / "t.db")
    reader = _reader()
    _d, entity = _orphaned_member_leaf(store, reader)
    store.set_meta(LAST_SYNCED_KEY, reader.graph_version())  # the pre-upgrade stamp
    store.set_meta(VOLATILE_STALE_KEY, "0")  # a warm store: only the stamp can ask for a rerun

    report = sync(store, reader)  # no force

    assert report.skipped is False
    assert _leaf_statuses(store, entity.entity_id) == ["live"]
    healed = store.get_entity(entity.entity_id)
    assert healed is not None and healed.last_seen_node_id == PLAY_CLIP_NODE
    assert sync(store, reader).skipped is True  # and the next one is a no-op again


def test_the_sync_stamp_carries_the_resolver_revision_and_provenance_does_not():
    reader = _reader()
    assert reader.sync_stamp().startswith(reader.graph_version())
    assert reader.sync_stamp() != reader.graph_version()
    assert ":r" not in reader.graph_version()


def test_sync_stamps_the_sync_stamp(tmp_path):
    store = Store(tmp_path / "t.db")
    reader = _reader()
    sync(store, reader)
    assert store.get_meta(LAST_SYNCED_KEY) == reader.sync_stamp()


# -- T13, T14: the read path ------------------------------------------------------------


def test_t13_a_named_member_seed_makes_its_entity_a_seed_and_its_gotcha_a_mistake(tmp_path):
    # red against D1 alone (mutation M8: drop D4): the resolved node's label `.playClip()`
    # never matches the stored entity's `AudioPlayback.playClip`, so the gotcha reaches the
    # task only through the community, never as a mistake.
    store = Store(tmp_path / "t.db")
    reader = _reader()
    d = _gotcha(store, reader)
    entity = store.resolve_descriptor(MEMBER, SWIFT_FILE)
    assert entity is not None

    res = resolve_seeds([Seed(name=MEMBER, file_path=SWIFT_FILE)], reader, store)
    assert res.seed_node_ids == [PLAY_CLIP_NODE]
    assert [e.entity_id for e in res.seed_entities] == [entity.entity_id]

    ctx = get_task_context([Seed(name=MEMBER, file_path=SWIFT_FILE)], store, reader)
    assert any(d.title in line for line in ctx.mistakes)
    assert "[unratified]" not in "".join(ctx.mistakes)


def test_a_seed_naming_a_stored_descriptor_is_a_seed_entity_whatever_the_node_label(tmp_path):
    # `AudioPlayback.state` anchors a property the graph has no node for: the leaf is orphaned
    # and the seed resolves to no node, yet the entity the name denotes is still a seed.
    store = Store(tmp_path / "t.db")
    reader = _reader()
    _gotcha(store, reader, name="AudioPlayback.state")
    entity = store.resolve_descriptor("AudioPlayback.state", SWIFT_FILE)
    assert entity is not None

    res = resolve_seeds([Seed(name="AudioPlayback.state", file_path=SWIFT_FILE)], reader, store)

    assert res.seed_node_ids == []
    assert [e.entity_id for e in res.seed_entities] == [entity.entity_id]


def test_t14_a_file_seed_makes_the_member_entities_anchored_in_it_seeds(tmp_path):
    # red against D1 alone: the file seed resolves to every node in the file, and none of
    # their labels equals a stored `Type.member` name.
    store = Store(tmp_path / "t.db")
    reader = _reader()
    _gotcha(store, reader)
    entity = store.resolve_descriptor(MEMBER, SWIFT_FILE)
    assert entity is not None

    res = resolve_seeds([Seed(file_path=SWIFT_FILE)], reader, store)

    assert PLAY_CLIP_NODE in res.seed_node_ids
    assert [e.entity_id for e in res.seed_entities] == [entity.entity_id]


def test_an_unrelated_file_seed_has_no_seed_entities(tmp_path):
    store = Store(tmp_path / "t.db")
    reader = _reader()
    _gotcha(store, reader)
    res = resolve_seeds([Seed(file_path="Sources/App/Other.swift")], reader, store)
    assert res.seed_node_ids == [] and res.seed_entities == []


def test_guard_binding_import_is_used():
    # keeps the AnchorBinding import honest for readers extending these tests
    assert AnchorBinding.model_fields["tier"] is not None


# -- a case-only twin shares one entity, and must not share a live leaf ------------------


def test_proposals_on_a_case_only_twin_pair_leave_both_leaves_orphaned(tmp_path):
    # `AudioPlayback.message` (the method) and `AudioPlayback.Message` (the nested struct) are
    # one store entity, because the store dedups an anchor by its lowercased name. Red against
    # the first cut of this branch: both leaves went live on the struct node.
    store = Store(tmp_path / "t.db")
    reader = _reader()
    drafts = [
        dict(
            title=title,
            kind="gotcha",
            context="c",
            choice="ch",
            anchors=[{"name": name, "file_path": SWIFT_FILE}],
        )
        for name, title in [
            ("AudioPlayback.message", "the method must run on the main thread"),
            ("AudioPlayback.Message", "the struct is immutable once it is sent"),
        ]
    ]
    results = propose(drafts, store, reader, session_id="s1")
    assert [r.status for r in results] == ["written", "written"]
    for res in results:
        leaves = [b for b in store.bindings_for_record(res.decision_id) if b.tier == 2]
        assert [b.status for b in leaves] == ["orphaned"]
    entity = store.resolve_descriptor("AudioPlayback.message", SWIFT_FILE)
    assert entity is not None and entity.last_seen_node_id is None


# -- the peripheral lookup, and several entities on one node ------------------------------


def _two_communities(tmp_path: Path) -> GraphifyReader:
    """The Swift fixture with ``.playClip()`` moved to its own community."""
    g = json.loads(FIXTURE.read_text())
    for n in g["nodes"]:
        if n["id"] == PLAY_CLIP_NODE:
            n["community"] = 2
    p = tmp_path / "two_communities.json"
    p.write_text(json.dumps(g))
    return GraphifyReader(p)


def test_a_member_next_to_the_seed_reaches_the_task_through_the_peripheral_walk(tmp_path):
    # `.stop()` is the seed; `.playClip()` calls it, lives in another community, and is only a
    # neighbour. Its gotcha reaches Related through the peripheral lookup, which used to match
    # node labels only. Red against the first cut: anchored `AudioPlayback.playClip` it never
    # appeared, while the same gotcha anchored bare `playClip` did.
    store = Store(tmp_path / "t.db")
    reader = _two_communities(tmp_path)
    _gotcha(store, reader, name=MEMBER)
    ctx = get_task_context([Seed(name="AudioPlayback.stop", file_path=SWIFT_FILE)], store, reader)
    assert any("playClip must stop" in line for line in ctx.related)


def test_two_entities_mapped_to_one_node_are_both_seed_entities_and_both_rank(tmp_path):
    # `AudioPlayback.playClip` and `AudioPlayback::playClip` are two entities (the store dedups
    # by the lowercased name, which differs) that resolve to the same node.
    store = Store(tmp_path / "t.db")
    reader = _reader()
    _gotcha(store, reader, name="AudioPlayback.playClip", title="first spelling of playClip")
    _gotcha(store, reader, name="AudioPlayback::playClip", title="second spelling of playClip")

    res = resolve_seeds([Seed(name="AudioPlayback.playClip", file_path=SWIFT_FILE)], reader, store)
    assert len(res.seed_entities) == 2
    ctx = get_task_context(
        [Seed(name="AudioPlayback.playClip", file_path=SWIFT_FILE)], store, reader
    )
    text = "\n".join(ctx.mistakes)
    assert "first spelling" in text and "second spelling" in text
    # a file seed reaches both through the node map as well
    res = resolve_seeds([Seed(file_path=SWIFT_FILE)], reader, store)
    assert len(res.seed_entities) == 2


# -- reports show the plain graph version on both sides ------------------------------------


def test_sync_reports_the_plain_graph_version_on_both_sides(tmp_path):
    # The stamp carries a resolver revision; the report must not. Red against the first cut:
    # a skipped pass reported `<version>:r2 -> <version>`.
    store = Store(tmp_path / "t.db")
    reader = _reader()
    first = sync(store, reader)
    assert first.from_version is None and first.to_version == reader.graph_version()
    forced = sync(store, reader, force=True)
    assert forced.skipped is False
    assert forced.from_version == forced.to_version == reader.graph_version()
    skipped = sync(store, reader)
    assert skipped.skipped is True
    assert skipped.from_version == skipped.to_version == reader.graph_version()
    assert ":r" not in str(forced.from_version) + str(skipped.from_version)
    assert store.get_meta(LAST_SYNCED_KEY) == reader.sync_stamp()  # the gate still compares it
