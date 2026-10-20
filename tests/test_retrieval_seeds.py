from pathlib import Path

import sidegraph.retrieval as retrieval
import sidegraph.server as server
from sidegraph.engine.reader import GraphifyReader, NodeRef, ResolveResult
from sidegraph.nearest_anchored import Plan
from sidegraph.retrieval import Seed, TaskContext, get_task_context, resolve_seeds
from sidegraph.schema import Descriptor, Entity, EntityKind
from sidegraph.server import _listing_entities
from sidegraph.store import DescriptorLookup, Store

FIXTURE = Path(__file__).parent / "fixtures" / "mini_graph.json"


def test_render_orders_sections_mistakes_first():
    ctx = TaskContext(
        mistakes=["- gotcha A"], decisions=["- adr B"], structure=["- node C"], related=["- rel D"]
    )
    out = ctx.render()
    assert out.index("gotcha A") < out.index("adr B") < out.index("node C") < out.index("rel D")


def test_render_empty_is_note():
    assert TaskContext().render() == "No context found."


def test_resolve_file_seed_to_nodes(tmp_path):
    r = GraphifyReader(FIXTURE)
    s = Store(tmp_path / "t.db")
    res = resolve_seeds([Seed(file_path="trader/exec.py")], r, s)
    assert set(res.seed_node_ids) == {"m_cls", "m_fn"}
    assert res.seed_communities == ["1"]
    assert res.seed_entities == []  # no stored entities yet


def test_resolve_entity_ref_seed(tmp_path):
    r = GraphifyReader(FIXTURE)
    s = Store(tmp_path / "t.db")
    e = s.upsert_entity(
        Entity(
            canonical_name="Trader",
            descriptor=Descriptor(name="Trader", file_path="trader/exec.py"),
        )
    )
    res = resolve_seeds([Seed(name="Trader", file_path="trader/exec.py")], r, s)
    assert res.seed_node_ids == ["m_cls"]
    assert [x.entity_id for x in res.seed_entities] == [e.entity_id]


def test_resolve_no_reader_is_empty(tmp_path):
    s = Store(tmp_path / "t.db")
    res = resolve_seeds([Seed(file_path="trader/exec.py")], None, s)
    assert res.seed_node_ids == [] and res.seed_entities == []


def _track_legacy_descriptor_scans(store, monkeypatch):
    calls = {
        name: 0
        for name in (
            "resolve_descriptor",
            "find_entity",
            "find_entities_by_name",
            "iter_concrete_entities",
        )
    }
    for name in calls:
        original = getattr(store, name)

        def counted(*args, _name=name, _original=original, **kwargs):
            calls[_name] += 1
            return _original(*args, **kwargs)

        monkeypatch.setattr(store, name, counted)
    return calls


def _track_lookup_materializations(monkeypatch):
    calls = 0
    original = DescriptorLookup._read_entities

    def counted(self):
        nonlocal calls
        calls += 1
        return original(self)

    monkeypatch.setattr(DescriptorLookup, "_read_entities", counted)
    return lambda: calls


class _ManyNodeReader:
    def __init__(self, count):
        self.nodes = {
            f"node-{i}": NodeRef(
                node_id=f"node-{i}",
                name=f"Symbol{i}",
                norm_name=f"symbol{i}",
                file_type="code",
                file_path=f"src/file{i}.py",
                community="1",
            )
            for i in range(count)
        }

    def resolve(self, descriptor, *, collapse=False):
        node_id = next(
            (node_id for node_id, node in self.nodes.items() if node.name == descriptor.name), None
        )
        return ResolveResult(status="resolved" if node_id else "unresolved", node_id=node_id)

    def get_node(self, node_id):
        return self.nodes.get(node_id)

    def nodes_in_file(self, _file_path):
        return ()

    def source_files(self):
        return frozenset()


def test_many_node_resolution_has_no_legacy_entity_scans(tmp_path, monkeypatch):
    store = Store(tmp_path / "many.db")
    count = 10
    reader = _ManyNodeReader(count)
    expected = []
    for i in range(count):
        expected.append(
            store.upsert_entity(
                Entity(
                    entity_id=f"entity-{i:02}",
                    canonical_name=f"Symbol{i}",
                    descriptor=Descriptor(name=f"Symbol{i}", file_path=f"src/file{i}.py"),
                    last_seen_node_id=f"node-{i}",
                )
            )
        )
    off_graph = store.upsert_entity(
        Entity(
            entity_id="off-graph",
            canonical_name="OldFile",
            descriptor=Descriptor(name="OldFile", file_path="deleted.py"),
        )
    )
    calls = _track_legacy_descriptor_scans(store, monkeypatch)
    reads = _track_lookup_materializations(monkeypatch)

    result = resolve_seeds(
        [
            *(Seed(name=f"Symbol{i}", file_path=f"src/file{i}.py") for i in range(count)),
            Seed(file_path="deleted.py"),
        ],
        reader,
        store,
    )

    assert {e.entity_id for e in result.seed_entities} == {
        *(e.entity_id for e in expected),
        off_graph.entity_id,
    }
    assert result.file_bound_entity_ids == frozenset({off_graph.entity_id})
    assert calls == {
        "resolve_descriptor": 0,
        "find_entity": 0,
        "find_entities_by_name": 0,
        "iter_concrete_entities": 0,
    }
    assert reads() == 1


def test_many_entry_listing_has_no_legacy_entity_scans(tmp_path, monkeypatch):
    reader = GraphifyReader(FIXTURE)
    store = Store(tmp_path / "listing.db")
    entities = [
        store.upsert_entity(
            Entity(
                entity_id=f"listing-{node_id}",
                canonical_name=name,
                descriptor=Descriptor(name=name, file_path="trader/exec.py"),
                last_seen_node_id=node_id,
            )
        )
        for node_id, name in (("m_cls", "Trader"), ("m_fn", "execute"))
    ]
    calls = _track_legacy_descriptor_scans(store, monkeypatch)
    reads = _track_lookup_materializations(monkeypatch)

    found, unresolved = _listing_entities(store, reader, ["trader/exec.py"] * 6, borrowed_from=None)

    assert found == {e.entity_id for e in entities}
    assert unresolved == []
    assert calls == {
        "resolve_descriptor": 0,
        "find_entity": 0,
        "find_entities_by_name": 0,
        "iter_concrete_entities": 0,
    }
    assert reads() == 1


def test_descriptor_lookup_matches_scan_oracle_coverage(tmp_path):
    """Coverage against the existing scan oracle; these cases intentionally pass pre-change."""
    store = Store(tmp_path / "parity.db")
    rows = [
        # Insertion inversion: lowest id arrives second and remains the deterministic winner.
        Entity(
            entity_id="z-high",
            canonical_name="Module.py",
            descriptor=Descriptor(name="Module.py", file_path="src/module.py"),
        ),
        Entity(
            entity_id="a-low",
            canonical_name="Module.py",
            descriptor=Descriptor(name="Module.py", file_path="src/module.py"),
        ),
        # Higher-id basename spelling beats the lower-id historical spelling despite insertion
        # order being the reverse of id order.
        Entity(
            entity_id="z-basename",
            canonical_name="file.py",
            descriptor=Descriptor(name="file.py", file_path="pkg/file.py"),
        ),
        Entity(
            entity_id="a-historical",
            canonical_name="pkg/file.py",
            descriptor=Descriptor(name="pkg/file.py", file_path="pkg/file.py"),
        ),
        # This identity has only a historical path spelling and remains independently reachable.
        Entity(
            entity_id="historical-only",
            canonical_name="legacy/tree/solo.py",
            descriptor=Descriptor(name="legacy/tree/solo.py", file_path="legacy/tree/solo.py"),
        ),
        # Case-mismatched descriptor/file suffix: identity must not receive another suffix fold.
        Entity(
            entity_id="case-edge",
            canonical_name="SRC/CaseThing.py",
            descriptor=Descriptor(name="SRC/CaseThing.py", file_path="src/casething.py"),
        ),
        # Descriptor-bearing abstract carrier and a descriptorless concrete fallback.
        Entity(
            entity_id="carrier-abstract",
            canonical_name="Shared",
            kind=EntityKind.ABSTRACT,
            descriptor=Descriptor(name="Shared", file_path="one/shared.py"),
        ),
        Entity(entity_id="shared-fallback", canonical_name="Shared"),
        # Multiple carrier paths preserve the existing pathless fallback.
        Entity(
            entity_id="ambig-carrier-a",
            canonical_name="Ambiguous",
            descriptor=Descriptor(name="Ambiguous", file_path="a.py"),
        ),
        Entity(
            entity_id="ambig-carrier-b",
            canonical_name="Ambiguous",
            descriptor=Descriptor(name="Ambiguous", file_path="b.py"),
        ),
        Entity(entity_id="ambig-fallback", canonical_name="Ambiguous", kind=EntityKind.ABSTRACT),
        Entity(
            entity_id="abstract-full", canonical_name="old/tree/Only.py", kind=EntityKind.ABSTRACT
        ),
    ]
    for entity in rows:
        store.upsert_entity(entity)

    cases = [
        ("Module.py", "src/module.py"),
        ("Module.py", None),
        ("Module.py", ""),
        ("Module.py", "   "),
        ("file.py", None),
        ("pkg/file.py", None),
        ("file.py", "pkg/file.py"),
        ("pkg/file.py", "pkg/file.py"),
        ("foreign/file.py", None),
        ("pkg/FILE.py", None),
        ("Module.py()", None),
        (".Shared", None),
        ("  pkg/file.py", None),
        ("pkg/file.py ", None),
        ("Module.py", "SRC/module.py"),
        ("Module.py", "x/src/module.py"),
        ("Module.py", "module.py"),
        ("solo.py", None),
        ("legacy/tree/solo.py", None),
        ("solo.py", "legacy/tree/solo.py"),
        ("legacy/tree/solo.py", "legacy/tree/solo.py"),
        ("SRC/CaseThing.py", None),
        ("casething.py", None),
        ("Shared", None),
        ("Shared", "one/shared.py"),
        ("Ambiguous", None),
        ("old/tree/Only.py", None),
        ("Only.py", None),
        ("missing", None),
    ]
    lookup = store.descriptor_lookup()
    for name, file_path in cases:
        expected = store.resolve_descriptor(name, file_path)
        actual = lookup.resolve_descriptor(name, file_path)
        assert (actual.model_dump(mode="json") if actual else None) == (
            expected.model_dump(mode="json") if expected else None
        ), (name, file_path)

    basename_winner = lookup.resolve_descriptor("pkg/file.py", None)
    assert basename_winner is not None
    assert basename_winner.entity_id == "z-basename"
    assert lookup.resolve_descriptor("foreign/file.py", None) is None
    assert lookup.resolve_descriptor("pkg/FILE.py", None) is None


def test_descriptor_lookup_empty_holder_reads_once(tmp_path, monkeypatch):
    store = Store(tmp_path / "empty.db")
    reads = _track_lookup_materializations(monkeypatch)
    lookup = store.descriptor_lookup()

    assert lookup.concrete_entities == ()
    assert lookup.concrete_entities == ()
    assert lookup.resolve_descriptor("missing", None) is None
    assert lookup.resolve_descriptor("missing", "") is None
    assert reads() == 1


def test_descriptor_lookup_is_fresh_after_entity_upsert(tmp_path, monkeypatch):
    store = Store(tmp_path / "fresh.db")
    reads = _track_lookup_materializations(monkeypatch)
    first = store.descriptor_lookup()

    assert first.resolve_descriptor("NewType", "new/type.py") is None
    created = store.upsert_entity(
        Entity(
            entity_id="created",
            canonical_name="NewType",
            descriptor=Descriptor(name="NewType", file_path="new/type.py"),
        )
    )
    second = store.descriptor_lookup()

    assert first.resolve_descriptor("NewType", "new/type.py") is None
    assert second.resolve_descriptor("NewType", "new/type.py") == created
    assert reads() == 2


def test_unused_retrieval_does_not_materialize_descriptor_lookup(tmp_path, monkeypatch):
    store = Store(tmp_path / "unused.db")
    reads = _track_lookup_materializations(monkeypatch)

    get_task_context([], store, None)

    assert reads() == 0


def test_guessed_seed_retrieval_reuses_one_lookup(tmp_path, monkeypatch):
    reader = GraphifyReader(FIXTURE)
    store = Store(tmp_path / "guessed.db")
    for entity_id, name, node_id in (("trader", "Trader", "m_cls"), ("execute", "execute", "m_fn")):
        store.upsert_entity(
            Entity(
                entity_id=entity_id,
                canonical_name=name,
                descriptor=Descriptor(name=name, file_path="trader/exec.py"),
                last_seen_node_id=node_id,
            )
        )
    reads = _track_lookup_materializations(monkeypatch)

    get_task_context(
        [Seed(name="Trader", file_path="trader/exec.py")],
        store,
        reader,
        guessed=[Seed(name="execute", file_path="trader/exec.py")],
    )

    assert reads() == 1


def test_unresolved_exact_plus_guessed_seed_uses_one_lookup(tmp_path, monkeypatch):
    reader = GraphifyReader(FIXTURE)
    store = Store(tmp_path / "combined-guessed.db")
    guessed_entity = store.upsert_entity(
        Entity(
            entity_id="guessed-trader",
            canonical_name="Trader",
            descriptor=Descriptor(name="Trader", file_path="trader/exec.py"),
            last_seen_node_id="m_cls",
        )
    )
    reads = _track_lookup_materializations(monkeypatch)
    calls = []
    original_resolve = retrieval.resolve_seeds

    def capture(seeds, reader, store, entities_by_node=None, *, descriptor_lookup=None):
        result = original_resolve(
            seeds, reader, store, entities_by_node, descriptor_lookup=descriptor_lookup
        )
        calls.append((seeds, result))
        return result

    monkeypatch.setattr(retrieval, "resolve_seeds", capture)

    get_task_context(
        [Seed(name="Nope", file_path="nope.py")],
        store,
        reader,
        guessed=[Seed(name="Trader", file_path="trader/exec.py")],
    )

    assert len(calls) == 2
    combined_seeds, combined_result = calls[-1]
    assert [seed.name for seed in combined_seeds] == ["Nope", "Trader"]
    result_ids = {entity.entity_id for entity in combined_result.seed_entities}
    assert guessed_entity.entity_id in result_ids
    assert reads() == 1


def test_server_main_and_nearest_retrieval_share_one_lookup(tmp_path, monkeypatch):
    reader = GraphifyReader(FIXTURE)
    store = Store(tmp_path / "server-fallback.db")
    reads = _track_lookup_materializations(monkeypatch)
    lookups = []

    def retrieve(_seeds, _store, _reader, _budget, *, guessed=(), descriptor_lookup=None):
        lookups.append(descriptor_lookup)
        assert descriptor_lookup.resolve_descriptor("absent", None) is None
        return TaskContext()

    monkeypatch.setattr(server, "_retrieve", retrieve)
    monkeypatch.setattr(
        server.nearest_anchored, "plan", lambda *args, **kwargs: Plan(files=["fallback.py"])
    )
    monkeypatch.setattr(server.nearest_anchored, "has_memory", lambda _ctx: False)

    server._get_task_context_impl(store, reader, ["trader/exec.py"], None, 4000, 4000)

    assert len(lookups) == 2
    assert lookups[0] is lookups[1]
    assert reads() == 1
