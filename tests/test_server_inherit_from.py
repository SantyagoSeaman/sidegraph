import asyncio
from collections.abc import Callable, Iterator
from functools import partial
from pathlib import Path
from typing import Any

import fastmcp
import pytest
from ulid import ULID

from sidegraph import server
from sidegraph.schema import AnchorBinding, DecisionStatus, Descriptor, Scope
from sidegraph.store import VOLATILE_STALE_KEY, Store, canonical_digest
from sidegraph.validation_errors import SafeWriteError
from tests.store_format2_support import (
    bindings_body,
    decision_body,
    fact_body,
    format1_store,
    put_shard_line,
)
from tests.store_record_support import _decision, _fact


def _bind(store: Store, record_id: str, name: str, *, orphaned: bool = False) -> AnchorBinding:
    entity = store.get_or_create_entity(Descriptor(name=name, file_path="symbols.py"))
    return store.add_binding(
        AnchorBinding(
            record_id=record_id,
            entity_id=entity.entity_id,
            tier=2,
            weight=0.4,
            relation="modifies",
            status="orphaned" if orphaned else "live",
        )
    )


def _add_decision(store: Store, **kwargs: Any) -> dict:
    return server._add_decision_impl(store, None, "new", "adr", "context", "choice", **kwargs)


def _supersede_decision(store: Store, predecessor_id: str, **kwargs: Any) -> dict:
    return server._supersede_decision_impl(
        store, None, predecessor_id, "new", "adr", "context", "choice", **kwargs
    )


def _add_fact(store: Store, **kwargs: Any) -> dict:
    return server._add_fact_impl(store, None, "new", "observed", **kwargs)


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    with Store(tmp_path / "store") as opened:
        yield opened


@pytest.fixture(params=["add_decision", "add_fact", "supersede_decision", "supersede_fact"])
def write(request: pytest.FixtureRequest, store: Store) -> Callable[..., dict]:
    if request.param == "add_decision":
        return partial(_add_decision, store)
    if request.param == "add_fact":
        decision = store.add_decision(_decision())
        return partial(_add_fact, store, supports=[decision.id])
    if request.param == "supersede_decision":
        predecessor = store.add_decision(_decision())
        return partial(_supersede_decision, store, predecessor.id, source="human")
    decision = store.add_decision(_decision())
    predecessor = store.add_fact(_fact(supports=[decision.id]))
    return partial(server._supersede_fact_impl, store, None, predecessor.id, "new", "observed")


@pytest.mark.parametrize(
    ("overrides", "expected_scope", "expected_layer"),
    [
        pytest.param(
            {"scope": Scope.GLOBAL, "layer": "business"}, Scope.GLOBAL, "business", id="both"
        ),
        pytest.param({"scope": Scope.GLOBAL, "layer": None}, Scope.GLOBAL, None, id="clear-layer"),
        pytest.param({"layer": "business"}, Scope.MODULE, "business", id="layer-only"),
        pytest.param({"scope": Scope.GLOBAL}, Scope.GLOBAL, "technical", id="scope-only"),
    ],
)
def test_supersede_explicit_layer_and_scope_win(
    store: Store,
    overrides: dict[str, str | None],
    expected_scope: Scope,
    expected_layer: str | None,
) -> None:
    old = store.add_decision(_decision(scope=Scope.MODULE, layer="technical"))
    out = _supersede_decision(store, old.id, source="human", **overrides)
    successor = store.get_decision(out["id"])
    assert successor is not None
    assert (successor.scope, successor.layer) == (expected_scope, expected_layer)


@pytest.mark.parametrize("field", ["layer", "scope"])
def test_mcp_supersede_inherits_predecessor_field(
    store: Store, monkeypatch: pytest.MonkeyPatch, field: str
) -> None:
    old = store.add_decision(_decision(scope=Scope.MODULE, layer="technical"))
    monkeypatch.setattr(server, "_get_store", lambda: store)
    monkeypatch.setattr(server, "_load_reader", lambda: None)

    async def supersede() -> str:
        async with fastmcp.Client(server.mcp) as client:
            result = await client.call_tool(
                "supersede_decision",
                {
                    "old_decision_id": old.id,
                    "title": "new",
                    "kind": "adr",
                    "context": "context",
                    "choice": "choice",
                },
            )
            return result.data["id"]

    successor = store.get_decision(asyncio.run(supersede()))
    assert successor is not None
    assert getattr(successor, field) == getattr(old, field)
    assert successor.status == DecisionStatus.PROPOSED
    assert store.get_decision(old.id) == old


@pytest.mark.parametrize("noun", ["decision", "fact"])
def test_inherit_from_replaces_predecessor_bindings(store: Store, noun: str) -> None:
    if noun == "decision":
        old = store.add_decision(_decision())
        draft = store.add_decision(_decision(status=DecisionStatus.PROPOSED))
        replace = partial(_supersede_decision, store, old.id, source="human")
    else:
        old = store.add_fact(_fact())
        draft = store.add_fact(_fact(status=DecisionStatus.PROPOSED))
        replace = partial(server._supersede_fact_impl, store, None, old.id, "new", "observed")

    _bind(store, old.id, "OldSym")
    draft_bindings = [
        _bind(store, draft.id, "NewSym"),
        _bind(store, draft.id, "MissingSym", orphaned=True),
    ]
    record_id = str(ULID())
    out = replace(record_id=record_id, inherit_from=draft.id)
    assert out["id"] == record_id
    assert {b.entity_id: b for b in store.bindings_for_record(record_id)} == {
        b.entity_id: b.model_copy(update={"record_id": record_id}) for b in draft_bindings
    }
    current = store.get_decision(record_id) if noun == "decision" else store.get_fact(record_id)
    assert current is not None and current.supersedes == old.id


def test_add_decision_inherit_from_and_record_id(store: Store) -> None:
    draft = store.add_decision(_decision(status=DecisionStatus.PROPOSED))
    binding = _bind(store, draft.id, "NewSym")
    record_id = str(ULID())
    out = _add_decision(store, record_id=record_id, scope="global", inherit_from=draft.id)
    assert out["id"] == record_id
    saved = store.get_decision(record_id)
    assert saved is not None
    assert saved.scope == Scope.GLOBAL
    assert saved.status == DecisionStatus.ACCEPTED
    assert store.bindings_for_record(record_id) == [
        binding.model_copy(update={"record_id": record_id})
    ]


def test_add_fact_inherit_from_satisfies_reachability(store: Store) -> None:
    draft = store.add_fact(_fact(status=DecisionStatus.PROPOSED))
    binding = _bind(store, draft.id, "MissingSym", orphaned=True)
    record_id = str(ULID())
    out = _add_fact(store, supports=[], record_id=record_id, inherit_from=draft.id)
    assert out["id"] == record_id
    saved = store.get_fact(record_id)
    assert saved is not None
    assert saved.status == DecisionStatus.ACCEPTED
    assert saved.supports == []
    assert store.bindings_for_record(record_id) == [
        binding.model_copy(update={"record_id": record_id})
    ]


def test_add_fact_inherit_from_source_without_bindings_needs_supports(store: Store) -> None:
    draft = store.add_fact(_fact(status=DecisionStatus.PROPOSED))
    before = canonical_digest(store.path)
    with pytest.raises(SafeWriteError, match="no live supporting decision"):
        _add_fact(store, supports=[], inherit_from=draft.id)
    assert canonical_digest(store.path) == before


@pytest.mark.parametrize("anchors", [[{"name": "NewSym"}], []])
def test_inherit_from_and_anchors_are_exclusive(
    store: Store, write: Callable[..., dict], anchors: list[dict]
) -> None:
    draft = store.add_decision(_decision(status=DecisionStatus.PROPOSED))
    before = canonical_digest(store.path)
    with pytest.raises(ValueError, match="mutually exclusive"):
        write(anchors=anchors, inherit_from=draft.id)
    assert canonical_digest(store.path) == before


@pytest.mark.parametrize("invalid", ["", "../outside", "8" + "0" * 25, "0" * 25 + "I"])
def test_record_id_must_be_ulid(store: Store, write: Callable[..., dict], invalid: str) -> None:
    before = canonical_digest(store.path)
    with pytest.raises(SafeWriteError, match="record_id"):
        write(record_id=invalid)
    assert canonical_digest(store.path) == before


@pytest.mark.parametrize("noun", ["decision", "fact"])
def test_record_id_collision_refused(store: Store, write: Callable[..., dict], noun: str) -> None:
    existing = store.add_decision(_decision()) if noun == "decision" else store.add_fact(_fact())
    before = canonical_digest(store.path)
    with pytest.raises(ValueError, match="already exists"):
        write(record_id=existing.id)
    assert canonical_digest(store.path) == before


def test_supersede_refuses_record_id_collision_before_missing_predecessor(store: Store) -> None:
    existing = store.add_decision(_decision())
    with pytest.raises(ValueError, match="already exists"):
        _supersede_decision(store, str(ULID()), record_id=existing.id)


def test_record_id_accepts_lowercase_ulid(store: Store, write: Callable[..., dict]) -> None:
    record_id = "01arz3ndektsv4rrffq69g5fav"
    out = write(record_id=record_id)
    assert out["id"] == record_id.upper()
    assert store.record_types([out["id"]])


@pytest.mark.parametrize("noun", ["decision", "fact"])
def test_record_id_collision_with_filename_alias(
    store: Store, write: Callable[..., dict], noun: str
) -> None:
    legacy_id = "01arz3ndektsv4rrffq69g5fav"
    if noun == "decision":
        store.add_decision(_decision(id=legacy_id))
    else:
        store.add_fact(_fact(id=legacy_id))
    record_id = legacy_id.upper()
    alias = store.path / f"{noun}s" / f"{record_id}.json"
    if not alias.is_file():
        pytest.skip("requires a case-insensitive file system")
    before = canonical_digest(store.path)
    with pytest.raises(ValueError, match="already exists"):
        write(record_id=record_id)
    assert canonical_digest(store.path) == before


@pytest.mark.parametrize("noun", ["decision", "fact"])
@pytest.mark.parametrize("record_id", ["01ARZ3NDEKTSV4RRFFQ69G5FAV", "01aRz3NDEKTSV4rrFFQ69G5FAV"])
def test_record_id_collision_with_quarantined_shard(
    store: Store, write: Callable[..., dict], noun: str, record_id: str
) -> None:
    body = decision_body if noun == "decision" else fact_body
    put_shard_line(store.path, noun, record_id, body(record_id, status="invalid"))
    store.refresh_if_stale()
    assert store.is_skipped(f"{noun}s", record_id)
    assert not store.record_types([record_id])
    before = canonical_digest(store.path)
    with pytest.raises(ValueError, match="already exists"):
        write(record_id=record_id)
    assert canonical_digest(store.path) == before


@pytest.mark.parametrize("noun", ["decision", "fact"])
def test_record_id_collision_with_packed_alias(
    store: Store, write: Callable[..., dict], noun: str
) -> None:
    legacy_id = "01arz3ndektsv4rrffq69g5fav"
    body = decision_body if noun == "decision" else fact_body
    put_shard_line(store.path, noun, legacy_id, body(legacy_id, status="accepted"))
    store.refresh_if_stale()
    assert store.record_types([legacy_id]) == {legacy_id: noun}
    source = store.add_decision(_decision())
    _bind(store, source.id, "NewSym")
    before = canonical_digest(store.path)
    with pytest.raises(ValueError, match="already exists"):
        write(record_id=legacy_id, inherit_from=source.id)
    assert canonical_digest(store.path) == before


@pytest.mark.parametrize("owner_id", ["01ARZ3NDEKTSV4RRFFQ69G5FAV", "01aRz3NDEKTSV4rrFFQ69G5FAV"])
def test_record_id_collision_with_unowned_packed_bindings(
    store: Store, write: Callable[..., dict], owner_id: str
) -> None:
    other = store.get_or_create_entity(Descriptor(name="Other", file_path="symbols.py"))
    put_shard_line(store.path, "bindings", owner_id, bindings_body(other.entity_id))
    store.refresh_if_stale()
    assert not store.record_types([owner_id])
    assert store.bindings_for_record(owner_id)
    source = store.add_decision(_decision())
    _bind(store, source.id, "Expected")
    before = canonical_digest(store.path)

    with pytest.raises(ValueError, match="already exists"):
        write(record_id=owner_id, inherit_from=source.id)

    assert canonical_digest(store.path) == before


def test_record_id_collision_with_path_only_quarantine(tmp_path: Path) -> None:
    path = format1_store(tmp_path / "store")
    record_id = str(ULID())
    with Store(path) as store:
        quarantined = path / "decisions" / f"{record_id}.json"
        quarantined.write_text("{\n", encoding="utf-8")
        store.refresh_if_stale()
        assert store.is_skipped("decisions", record_id)
        quarantined.unlink()
        before = canonical_digest(path)

        with pytest.raises(ValueError, match="already exists"):
            _add_decision(store, record_id=record_id)

        assert canonical_digest(path) == before


def test_record_id_collision_with_unindexed_bindings_file(store: Store) -> None:
    record_id = str(ULID())
    entity = store.get_or_create_entity(Descriptor(name="Stray", file_path="symbols.py"))
    path = store.path / "bindings" / f"{record_id}.json"
    path.write_text(f'[{{"entity_id": "{entity.entity_id}", "tier": 2}}]\n', encoding="utf-8")
    assert not store.bindings_for_record(record_id)
    before = canonical_digest(store.path)

    with pytest.raises(ValueError, match="already exists"):
        _add_decision(store, record_id=record_id)

    assert canonical_digest(store.path) == before


def test_inherit_from_refuses_unknown_source(store: Store, write: Callable[..., dict]) -> None:
    before = canonical_digest(store.path)
    with pytest.raises(ValueError, match="must name an existing decision or fact"):
        write(inherit_from=str(ULID()))
    assert canonical_digest(store.path) == before


def test_inherit_from_refuses_an_unreadable_entity(
    store: Store, write: Callable[..., dict]
) -> None:
    source = store.add_decision(_decision())
    binding = _bind(store, source.id, "MissingSym", orphaned=True)
    (store.path / "entities" / f"{binding.entity_id}.json").write_text("{\n", encoding="utf-8")
    store.refresh_if_stale()
    assert store.get_entity(binding.entity_id) is None
    assert store.bindings_for_record(source.id)
    before = canonical_digest(store.path)
    with pytest.raises(ValueError, match="binding references unknown entity"):
        write(inherit_from=source.id)
    assert canonical_digest(store.path) == before


def test_failed_binding_copy_leaves_no_indexed_record(
    store: Store, write: Callable[..., dict], monkeypatch: pytest.MonkeyPatch
) -> None:
    source = store.add_decision(_decision())
    _bind(store, source.id, "SourceSym")
    record_id = str(ULID())

    def fail_copy(self: Store, binding: AnchorBinding, **kwargs: Any) -> AnchorBinding:
        raise OSError("injected binding write failure")

    with Store(store.path) as observer:
        with monkeypatch.context() as patch:
            patch.setattr(Store, "add_binding", fail_copy)
            with pytest.raises(OSError, match="injected binding write failure"):
                write(record_id=record_id, inherit_from=source.id)
        for handle in (store, observer):
            assert handle.record_types([record_id]) == {}
            assert handle.bindings_for_record(record_id) == []

        published = canonical_digest(store.path)
        with pytest.raises(ValueError, match="already exists"):
            write(record_id=record_id, inherit_from=source.id)
        assert canonical_digest(store.path) == published
        store.refresh_if_stale()
        assert record_id in store.record_types([record_id])
        assert store.bindings_for_record(record_id) == []


def test_copy_bindings_idempotent(store: Store) -> None:
    source = store.add_decision(_decision())
    target = store.add_decision(_decision())
    binding = _bind(store, source.id, "NewSym", orphaned=True)
    expected = binding.model_copy(update={"record_id": target.id})
    assert server._copy_bindings(store, source.id, target.id) == 1
    assert store.bindings_for_record(target.id) == [expected]
    changed = store.add_binding(expected.model_copy(update={"weight": 0.8}))
    assert server._copy_bindings(store, source.id, target.id) == 0
    assert store.bindings_for_record(target.id) == [changed]


def test_copy_bindings_refuses_unknown_source(store: Store) -> None:
    target = store.add_decision(_decision())
    before = canonical_digest(store.path)

    with pytest.raises(ValueError, match="source_id must name an existing decision or fact"):
        server._copy_bindings(store, str(ULID()), target.id)

    assert not store.bindings_for_record(target.id)
    assert canonical_digest(store.path) == before


def test_copy_bindings_refuses_unreadable_source(store: Store) -> None:
    source = store.add_decision(_decision())
    target = store.add_decision(_decision())
    put_shard_line(store.path, "bindings", source.id, {"not": "a list"})
    store.refresh_if_stale()
    assert store.is_skipped("bindings", source.id)
    with pytest.raises(ValueError, match="refusing to overwrite bindings"):
        server._copy_bindings(store, source.id, target.id)
    assert not store.bindings_for_record(target.id)


def test_copy_bindings_refuses_an_unreadable_entity(store: Store) -> None:
    source = store.add_decision(_decision())
    target = store.add_decision(_decision())
    bindings = [_bind(store, source.id, "First"), _bind(store, source.id, "Second")]
    unreadable = max(bindings, key=lambda binding: binding.entity_id)
    (store.path / "entities" / f"{unreadable.entity_id}.json").write_text("{\n", encoding="utf-8")
    store.refresh_if_stale()
    assert store.get_entity(unreadable.entity_id) is None
    before = canonical_digest(store.path)

    with pytest.raises(ValueError, match="binding references unknown entity"):
        server._copy_bindings(store, source.id, target.id)

    assert not store.bindings_for_record(target.id)
    assert canonical_digest(store.path) == before


def test_copy_bindings_keeps_an_orphaned_verdict_after_refresh(store: Store) -> None:
    source = store.add_decision(_decision())
    target = store.add_decision(_decision())
    _bind(store, source.id, "MissingSym", orphaned=True)
    store.set_meta(VOLATILE_STALE_KEY, "0")
    added = str(ULID())
    put_shard_line(store.path, "decision", added, decision_body(added))
    store.refresh_if_stale()
    assert store.get_meta(VOLATILE_STALE_KEY) == "1"
    assert store.bindings_for_record(source.id)[0].status == "live"
    assert source.id not in store.anchor_names()

    assert server._copy_bindings(store, source.id, target.id) == 1
    assert target.id not in store.anchor_names()


@pytest.mark.parametrize("noun", ["decision", "fact"])
def test_supersede_copies_bindings_on_index_without_kept_status(tmp_path: Path, noun: str) -> None:
    path = tmp_path / "store"
    with Store(path) as original:
        old = (
            original.add_decision(_decision()) if noun == "decision" else original.add_fact(_fact())
        )
        binding = _bind(original, old.id, "Original")
        original._conn.execute("ALTER TABLE anchor_bindings DROP COLUMN kept_status")

    with Store(path) as reopened:
        if noun == "decision":
            result = _supersede_decision(reopened, old.id, source="human")
            predecessor = reopened.get_decision(old.id)
        else:
            result = server._supersede_fact_impl(reopened, None, old.id, "new", "observed")
            predecessor = reopened.get_fact(old.id)
        assert reopened.bindings_for_record(result["id"]) == [
            binding.model_copy(update={"record_id": result["id"]})
        ]
        assert predecessor is not None and predecessor.status == DecisionStatus.SUPERSEDED


@pytest.mark.parametrize("supersede", [False, True])
def test_fact_inheritance_with_only_community_binding_needs_support(
    store: Store, supersede: bool
) -> None:
    source = store.add_fact(_fact())
    community = store.get_or_create_abstract_entity("community:5")
    store.add_binding(AnchorBinding(record_id=source.id, entity_id=community.entity_id, tier=1))
    if supersede:
        predecessor = store.add_fact(_fact())
        write = partial(server._supersede_fact_impl, store, None, predecessor.id, "new", "observed")
    else:
        write = partial(_add_fact, store)
    before = canonical_digest(store.path)

    with pytest.raises(SafeWriteError, match="no live supporting decision"):
        write(inherit_from=source.id, supports=[])

    assert canonical_digest(store.path) == before
