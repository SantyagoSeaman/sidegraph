"""A path-less descriptor adopts the same-named entity that already carries a path.

``get_or_create_entity`` looked up by canonical name AND ``file_path`` only. A descriptor
built from a bare token — ``doc_import`` mints ``Descriptor(name=token)`` for every
backticked mention, with no path it could know — therefore never matched the entity that
already existed for that symbol with a real path, and minted a path-less twin instead. The
twin resolves to nothing in the graph, so every decision anchored through it surfaces
nowhere: measured on the committed corpora, 38 twin names across four stores, and three
airflow ADRs unreachable from the code they describe.

Red targets, per test:

- ``adopts`` / ``bind_orphaned_adopts`` / ``resolve_and_bind_adopts`` — RED against unfixed
  code (two distinct ids). ``resolve_and_bind`` is the channel the airflow twins actually
  arrived through: ``_mention_anchors`` only keeps a token whose name RESOLVES, so the
  anchor reaches ``resolve_and_bind`` with status "resolved" and the binding is written
  ``live`` — the twin is not an orphan, which is why binding status never flagged it.
- ``ambiguous_name_is_not_adopted`` — RED against the discarded "adopt the first candidate"
  fix, not against unfixed code. Evidence about a rejected alternative.
- ``distinct_paths_stay_distinct`` — RED against an over-reaching fix that collapses by name
  regardless of path. Passes before AND after; declared a guard, not evidence.
- ``abstract_entity_is_never_adopted`` — guard, passes before and after. A concrete anchor
  reaching an abstract entity is pre-existing ``find_entity(name, None)`` behaviour and is
  not this change's subject; the test pins that the fix leaves it alone. (Measured over the
  four committed stores: zero abstract/concrete canonical-name collisions exist today.)
- ``pathless_lookup_is_idempotent`` — RED against a fix that mints unconditionally whenever
  ``file_path`` is None. Guard on the no-candidate branch.

Added after external review, which demonstrated that adoption on the WRITE path alone
re-opens the same read/write split one level up (finding 1):

- ``live_neighbors_sees_...`` / ``fact_dedup_sees_...`` — RED against the write-only fix
  (dedup returns None where it used to return the record's id). These are the regression,
  not a guard.
- ``abstract_entity_is_not_adopted_by_a_pathless_lookup`` — RED against dropping the
  ``e.descriptor is not None`` filter in ``_adopt_path_carrying_entity`` (finding 2: the
  pathed sibling test above never enters adoption, so that clause was pinned by nothing).
- ``adoption_ties_resolve_to_the_lowest_entity_id`` — RED against ``min`` -> ``max`` in
  ``_adopt_path_carrying_entity`` (finding 3). ``test_entity_duplicate_resolution.py`` pins
  D4 for ``find_entity``, not for adoption.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sidegraph.anchoring import resolve_and_bind
from sidegraph.capture import (
    AnchorDraft,
    DraftDecision,
    _bind_orphaned,
    _is_duplicate_fact,
    _live_neighbors,
)
from sidegraph.engine.reader import NodeRef, ResolveResult
from sidegraph.retrieval import Seed, resolve_seeds
from sidegraph.schema import (
    AnchorBinding,
    Decision,
    DecisionKind,
    Descriptor,
    Entity,
    Fact,
    Provenance,
)
from sidegraph.server import _add_anchors_impl
from sidegraph.store import Store


class _FakeReader:
    """Minimal stand-in (same shape as tests/test_anchoring_mapping_refresh.py's)."""

    def __init__(self, result: ResolveResult, graph_version: str) -> None:
        self._result = result
        self._graph_version = graph_version

    def resolve(self, desc):
        return self._result

    def get_node(self, node_id):
        return None  # unknown node -> the cross-suffix rail abstains (see _crosses_suffix)

    def graph_version(self):
        return self._graph_version


class _NodeReader:
    """Resolves any name to one path-less node — the shape `graphify` emits for an
    imported symbol it can see used but cannot place in a file."""

    def __init__(self, node: NodeRef, community: str | None = None) -> None:
        self._node = node
        self._community = community

    def resolve(self, desc):
        return ResolveResult(
            status="resolved", node_id=self._node.node_id, community=self._community
        )

    def get_node(self, node_id):
        return self._node if node_id == self._node.node_id else None

    def nodes_in_file(self, file_path):
        return []

    def graph_version(self):
        return "gv1"


def test_pathless_descriptor_adopts_the_path_carrying_entity(tmp_path):
    store = Store(tmp_path / "s")
    real = store.get_or_create_entity(
        Descriptor(name="ParseImportError", file_path="models/errors.py")
    )

    adopted = store.get_or_create_entity(Descriptor(name="ParseImportError"))

    assert adopted.entity_id == real.entity_id
    assert adopted.descriptor is not None and adopted.descriptor.file_path == "models/errors.py"


def test_ambiguous_name_is_not_adopted(tmp_path):
    """Two files own the name: adopting either would be a guess, so a path-less descriptor
    keeps its own identity."""
    store = Store(tmp_path / "s")
    a = store.get_or_create_entity(Descriptor(name="Config", file_path="core/config.py"))
    b = store.get_or_create_entity(Descriptor(name="Config", file_path="api/config.py"))

    pathless = store.get_or_create_entity(Descriptor(name="Config"))

    assert pathless.entity_id not in {a.entity_id, b.entity_id}
    assert pathless.descriptor is not None and pathless.descriptor.file_path is None


def test_distinct_paths_stay_distinct(tmp_path):
    store = Store(tmp_path / "s")
    a = store.get_or_create_entity(Descriptor(name="Config", file_path="core/config.py"))
    b = store.get_or_create_entity(Descriptor(name="Config", file_path="api/config.py"))

    assert a.entity_id != b.entity_id


def test_abstract_entity_is_never_adopted(tmp_path):
    """An abstract entity has no descriptor and no path — it must not become the answer to a
    concrete anchor just because the fix started scanning by name."""
    store = Store(tmp_path / "s")
    domain = store.get_or_create_abstract_entity("domain:retrieval")
    real = store.get_or_create_entity(Descriptor(name="domain:retrieval", file_path="r.py"))

    assert real.entity_id != domain.entity_id
    assert real.descriptor is not None and real.descriptor.file_path == "r.py"


def test_pathless_lookup_is_idempotent(tmp_path):
    """Nothing to adopt: the second path-less call must return the first one's entity rather
    than mint a second twin."""
    store = Store(tmp_path / "s")
    first = store.get_or_create_entity(Descriptor(name="SomeConcept"))
    second = store.get_or_create_entity(Descriptor(name="SomeConcept"))

    assert first.entity_id == second.entity_id


def test_bind_orphaned_adopts_the_path_carrying_entity(tmp_path):
    """The channel the defect actually arrived through: no engine available, so capture binds
    the anchor as an orphaned leaf — onto the real entity, not onto a fresh twin."""
    store = Store(tmp_path / "s")
    real = store.get_or_create_entity(
        Descriptor(name="DagImportError", file_path="importers/base.py")
    )
    decision = store.add_decision(
        Decision(
            title="Import errors are reported per file",
            kind=DecisionKind.ADR,
            context="Parse failures must not abort the whole run.",
            choice="Collect per-file errors and continue.",
            valid_from=datetime.now(UTC),
            provenance=Provenance(source="manual"),
        )
    )

    _bind_orphaned(decision.id, Descriptor(name="DagImportError"), store)

    bound = {b.entity_id for b in store.bindings_for_record(decision.id)}
    assert bound == {real.entity_id}


def test_resolve_and_bind_adopts_and_refreshes_the_path_carrying_entity(tmp_path):
    """The channel the airflow twins arrived through, engine available: a name-only ref that
    resolves must land on the entity that owns the path — and the engine-mapping refresh must
    follow it there, not onto a fresh twin."""
    store = Store(tmp_path / "s")
    real = store.get_or_create_entity(
        Descriptor(name="ParseImportError", file_path="models/errors.py")
    )
    decision = store.add_decision(
        Decision(
            title="Import errors carry a hint",
            kind=DecisionKind.ADR,
            context="c",
            choice="ch",
            valid_from=datetime.now(UTC),
            provenance=Provenance(source="manual"),
        )
    )
    reader = _FakeReader(
        ResolveResult(status="resolved", node_id="models_errors_parseimporterror", community="4"),
        graph_version="gv1",
    )

    resolve_and_bind(decision.id, Descriptor(name="ParseImportError"), reader, store)

    leaves = [b for b in store.bindings_for_record(decision.id) if b.tier == 2]
    assert [b.entity_id for b in leaves] == [real.entity_id]
    refreshed = store.get_entity(real.entity_id)
    assert refreshed is not None
    assert refreshed.last_seen_node_id == "models_errors_parseimporterror"
    assert refreshed.last_seen_graph_version == "gv1"
    assert refreshed.descriptor is not None and refreshed.descriptor.file_path == "models/errors.py"


def test_live_neighbors_sees_a_record_anchored_through_a_pathless_anchor(tmp_path):
    """Read path must resolve a descriptor exactly as the write path did. The write adopts
    the carrier, so a read that still keys on ``(name, None)`` finds nothing and dedup stops
    seeing an identical re-proposal — the same read/write split as the twin bug itself."""
    store = Store(tmp_path / "s")
    real = store.get_or_create_entity(
        Descriptor(name="ParseImportError", file_path="models/errors.py")
    )
    decision = store.add_decision(
        Decision(
            title="Import errors carry a hint",
            kind=DecisionKind.ADR,
            context="c",
            choice="ch",
            valid_from=datetime.now(UTC),
            provenance=Provenance(source="manual"),
        )
    )
    store.add_binding(AnchorBinding(record_id=decision.id, entity_id=real.entity_id, tier=2))
    draft = DraftDecision(
        title="Import errors carry a hint",
        kind=DecisionKind.ADR,
        context="c",
        choice="ch",
        anchors=[AnchorDraft(name="ParseImportError")],
    )

    assert [d.id for d in _live_neighbors(draft, store)] == [decision.id]


def test_fact_dedup_sees_a_pathless_anchor(tmp_path):
    """``_is_duplicate_fact`` walks bindings of the anchor's entity — it must reach the same
    entity the write path bound the fact to."""
    store = Store(tmp_path / "s")
    real = store.get_or_create_entity(Descriptor(name="DagImportError", file_path="base.py"))
    fact = store.add_fact(
        Fact(
            statement="Import errors are collected per file.",
            source="manual",
            valid_from=datetime.now(UTC),
            provenance=Provenance(source="manual"),
        )
    )
    store.add_binding(AnchorBinding(record_id=fact.id, entity_id=real.entity_id, tier=2))

    dup = _is_duplicate_fact(
        "Import errors are collected per file.", [AnchorDraft(name="DagImportError")], [], store
    )

    assert dup == fact.id


def test_abstract_entity_is_not_adopted_by_a_pathless_lookup(tmp_path):
    """The abstract-exclusion clause, exercised through the branch that actually runs it: a
    PATH-LESS lookup whose name collides with an abstract entity. (The pathed sibling test
    never enters adoption at all — reviewer finding 2.)"""
    store = Store(tmp_path / "s")
    store.get_or_create_abstract_entity("Retrieval")
    carrier = store.get_or_create_entity(Descriptor(name="Retrieval", file_path="retrieval.py"))

    got = store.get_or_create_entity(Descriptor(name="Retrieval"))

    assert got.entity_id == carrier.entity_id


def test_adoption_ties_resolve_to_the_lowest_entity_id(tmp_path):
    """Design D4 — the store's one duplicate rule — applied to adoption. Two entities share
    name AND path (legal: a git-merged store, see test_entity_duplicate_resolution.py); the
    lower id wins. Constructed as the INVERSION (higher id minted first, lower id upserted
    afterwards so its row lands later) or an un-ordered scan would pass by accident."""
    store = Store(tmp_path / "s")
    higher = store.get_or_create_entity(Descriptor(name="Config", file_path="core/config.py"))
    lower = Entity(
        entity_id="00000000000000000000000000",
        canonical_name="Config",
        descriptor=Descriptor(name="Config", file_path="core/config.py"),
    )
    store.upsert_entity(lower)
    assert higher.entity_id > lower.entity_id  # sanity: a real ULID sorts above the stand-in

    assert store.get_or_create_entity(Descriptor(name="Config")).entity_id == lower.entity_id


def test_mcp_anchor_summary_reports_the_entity_a_pathless_anchor_was_bound_to(tmp_path):
    """``_anchor_leaf_summary`` looks the entity up again AFTER the write. Keyed on
    ``(name, None)`` it reports nothing for every path-less anchor the write just adopted,
    so the MCP caller is told no leaf was bound when one was."""
    store = Store(tmp_path / "s")
    real = store.get_or_create_entity(Descriptor(name="DagImportError", file_path="base.py"))
    decision = store.add_decision(
        Decision(
            title="t",
            kind=DecisionKind.ADR,
            context="c",
            choice="ch",
            valid_from=datetime.now(UTC),
            provenance=Provenance(source="manual"),
        )
    )

    res = _add_anchors_impl(store, None, decision.id, [{"name": "DagImportError"}])

    assert [e["entity_id"] for e in res["orphaned"]] == [real.entity_id]


def test_retrieval_seeds_resolve_a_pathless_node_to_the_carrier(tmp_path):
    """Real graphs carry path-less nodes (imported symbols: 45 in this repo's own graph,
    6646 in airflow's). Seeding on one must reach the entity the write path bound its
    anchors to, or every decision anchored through a path-less mention becomes unreachable
    from the node that names it.

    ``file_path=""`` is deliberate, not sloppy: Graphify emits an empty STRING for every
    path-less node (6646/6646 in airflow, 45/45 here) and ``_to_node`` passes it through, so
    a NodeRef with ``file_path=None`` is a state production never reaches. Pinning ``None``
    here made this test pass while the wiring stayed inert against the real reader — caught
    by external review, round 2."""
    store = Store(tmp_path / "s")
    real = store.get_or_create_entity(Descriptor(name="BaseModel", file_path="schema.py"))
    node = NodeRef(
        node_id="basemodel",
        name="BaseModel",
        norm_name="basemodel",
        file_type="code",
        file_path="",  # the shape GraphifyReader actually emits
    )
    reader = _NodeReader(node)

    res = resolve_seeds([Seed(name="BaseModel")], reader, store)

    assert [e.entity_id for e in res.seed_entities] == [real.entity_id]


def test_pathless_fallback_matches_find_entity_exactly(tmp_path):
    """With no carrier to adopt, the single-scan path must return what ``find_entity(name,
    None)`` returns — including its D4 tie-break among path-less duplicates. Red against
    ``min`` -> ``max`` on the fallback line, and against a fallback that skips abstract
    entities (``find_entity`` matches them: no descriptor means no path)."""
    store = Store(tmp_path / "s")
    higher = store.get_or_create_entity(Descriptor(name="SomeConcept"))
    lower = Entity(
        entity_id="00000000000000000000000000",
        canonical_name="SomeConcept",
        descriptor=Descriptor(name="SomeConcept"),
    )
    store.upsert_entity(lower)
    assert higher.entity_id > lower.entity_id  # sanity: a real ULID sorts above the stand-in

    assert store.resolve_descriptor("SomeConcept", None) == store.find_entity("SomeConcept", None)
    assert store.resolve_descriptor("SomeConcept", None).entity_id == lower.entity_id

    store.get_or_create_abstract_entity("domain:only-abstract")
    assert store.resolve_descriptor("domain:only-abstract", None) == store.find_entity(
        "domain:only-abstract", None
    )


def test_empty_string_path_resolves_like_no_path(tmp_path):
    """The engine's own vocabulary for "no path" is ``""``, so the resolver must fold the
    two. Red against keying adoption on ``is None``, which left every reader-fed lookup on
    the pre-fix behaviour while the write path adopted."""
    store = Store(tmp_path / "s")
    real = store.get_or_create_entity(Descriptor(name="BaseModel", file_path="schema.py"))

    assert store.resolve_descriptor("BaseModel", "") == store.resolve_descriptor("BaseModel", None)
    assert store.resolve_descriptor("BaseModel", "").entity_id == real.entity_id


def test_a_cross_suffix_name_hit_does_not_clobber_the_engine_mapping(tmp_path):
    """A path-less anchor that adopts a code entity but resolves by NAME to a doc node must
    not overwrite that entity's engine mapping — sync refuses the same collision ("a unique
    cross-suffix hit is a collision, not a move") and never restores what capture wrote.
    Red against the adoption fix without the rail: pre-fix the damage landed on a throwaway
    twin, which is why nothing caught it."""
    store = Store(tmp_path / "s")
    real = store.get_or_create_entity(Descriptor(name="Foo", file_path="src/foo.py"))
    real.last_seen_node_id = "src_foo_node"
    real.last_seen_graph_version = "gv0"
    store.upsert_entity(real)
    decision = store.add_decision(
        Decision(
            title="t",
            kind=DecisionKind.ADR,
            context="c",
            choice="ch",
            valid_from=datetime.now(UTC),
            provenance=Provenance(source="manual"),
        )
    )
    doc_node = NodeRef(
        node_id="docs_foo", name="Foo", norm_name="foo", file_type="doc", file_path="docs/foo.md"
    )

    resolve_and_bind(decision.id, Descriptor(name="Foo"), _NodeReader(doc_node), store)

    kept = store.get_entity(real.entity_id)
    assert kept is not None
    assert kept.last_seen_node_id == "src_foo_node"  # not the .md node
    assert kept.last_seen_graph_version == "gv0"
    leaves = [b for b in store.bindings_for_record(decision.id) if b.tier == 2]
    assert [(b.entity_id, b.status) for b in leaves] == [(real.entity_id, "live")]


def test_a_same_suffix_name_hit_still_refreshes(tmp_path):
    """The rail must not swallow an ordinary move: a same-suffix hit IS a move, and sync
    heals the descriptor on its next pass. Red against a rail that withholds every refresh
    once adoption happened."""
    store = Store(tmp_path / "s")
    real = store.get_or_create_entity(Descriptor(name="Foo", file_path="src/foo.py"))
    decision = store.add_decision(
        Decision(
            title="t",
            kind=DecisionKind.ADR,
            context="c",
            choice="ch",
            valid_from=datetime.now(UTC),
            provenance=Provenance(source="manual"),
        )
    )
    moved = NodeRef(
        node_id="src_pkg_foo_node",
        name="Foo",
        norm_name="foo",
        file_type="code",
        file_path="src/pkg/foo.py",
    )

    resolve_and_bind(decision.id, Descriptor(name="Foo"), _NodeReader(moved), store)

    kept = store.get_entity(real.entity_id)
    assert kept is not None and kept.last_seen_node_id == "src_pkg_foo_node"


def test_an_empty_path_is_never_stored_as_a_path(tmp_path):
    """An agent writing ``"file_path": ""`` for "I don't know" must not create a descriptor
    that later counts as a path. Red against the schema without the fold: the ``""`` entity
    is minted verbatim, `_adopt_path_carrying_entity` then sees paths {"", "a.py"}, reads
    that as ambiguity, and re-mints the very twin this branch exists to kill."""
    store = Store(tmp_path / "s")
    blank = store.get_or_create_entity(Descriptor(name="X", file_path=""))
    assert blank.descriptor is not None and blank.descriptor.file_path is None

    again = store.get_or_create_entity(Descriptor(name="X", file_path="   "))
    assert again.entity_id == blank.entity_id  # whitespace is the same "no path"

    carrier = store.get_or_create_entity(Descriptor(name="X", file_path="a.py"))
    adopted = store.get_or_create_entity(Descriptor(name="X"))

    assert adopted.entity_id == carrier.entity_id
    assert len(store.find_entities_by_name("X")) == 2  # the blank one and the carrier, no third


def test_a_cross_suffix_hit_does_not_spend_the_rejected_nodes_community(tmp_path):
    """The rail rejects the doc node's identity, so Tier-1 must not be built from that same
    node's community. Nothing re-adjudicates a Tier-1 binding — when sync later orphans the
    leaf, `_repoint_off_path` sees the vanished file's community (None) and repoints nothing,
    so a docs-community binding would outlive the evidence for it. Red against taking Tier-1
    from `result.community` while the rail fires."""
    store = Store(tmp_path / "s")
    real = store.get_or_create_entity(Descriptor(name="Foo", file_path="src/foo.py"))
    real.last_seen_node_id = "src_foo_node"
    real.last_seen_community = "3"  # the code community, known-good evidence
    store.upsert_entity(real)
    decision = store.add_decision(
        Decision(
            title="t",
            kind=DecisionKind.ADR,
            context="c",
            choice="ch",
            valid_from=datetime.now(UTC),
            provenance=Provenance(source="manual"),
        )
    )
    doc_node = NodeRef(
        node_id="docs_foo",
        name="Foo",
        norm_name="foo",
        file_type="doc",
        file_path="docs/foo.md",
        community="7",  # the DOCS community — the evidence the rail just rejected
    )

    resolve_and_bind(
        decision.id, Descriptor(name="Foo"), _NodeReader(doc_node, community="7"), store
    )

    tier1 = [b for b in store.bindings_for_record(decision.id) if b.tier == 1]
    names = {store.get_entity(b.entity_id).canonical_name for b in tier1}
    assert names == {"community:3"}
