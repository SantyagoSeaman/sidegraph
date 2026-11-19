"""The live binding digest reads the indexed fields that drive sync events (D11)."""

from hashlib import sha256
from pathlib import Path

from sidegraph.schema import AnchorBinding, Entity
from sidegraph.store import Store
from sidegraph.store_format import encode_body
from tests.store_record_support import _decision


def test_binding_fingerprint_is_sorted_and_tracks_volatile_fields(tmp_path: Path) -> None:
    with Store(tmp_path / ".sidegraph") as store:
        first = store.add_decision(_decision())
        second = store.add_decision(_decision())
        a = store.upsert_entity(Entity(canonical_name="a", last_seen_node_id="a0"))
        b = store.upsert_entity(Entity(canonical_name="b"))
        early = AnchorBinding(record_id=first.id, entity_id=a.entity_id, tier=0)
        late = AnchorBinding(record_id=second.id, entity_id=b.entity_id, tier=0)
        store.add_binding(late)
        store.add_binding(early)
        canonical = store.canonical_digest()

        rows = sorted(
            [(first.id, a.entity_id, "live", "a0"), (second.id, b.entity_id, "live", None)]
        )
        fingerprint = sha256(encode_body(rows).encode()).hexdigest()
        assert store.binding_fingerprint() == fingerprint

        store.add_binding(early.model_copy(update={"status": "orphaned"}))
        status_changed = store.binding_fingerprint()
        assert status_changed != fingerprint

        store.upsert_entity(a.model_copy(update={"last_seen_node_id": "a1"}))
        assert store.binding_fingerprint() != status_changed
        assert store.canonical_digest() == canonical


def test_binding_fingerprint_keeps_a_binding_whose_entity_was_skipped(tmp_path: Path) -> None:
    with Store(tmp_path / ".sidegraph") as store:
        decision = store.add_decision(_decision())
        entity = store.upsert_entity(Entity(canonical_name="unreadable entity"))
        binding = AnchorBinding(record_id=decision.id, entity_id=entity.entity_id, tier=0)
        store.add_binding(binding)
        (store.path / "entities" / f"{entity.entity_id}.json").write_text("{malformed\n")
        store.refresh_if_stale()

        assert store.get_entity(entity.entity_id) is None
        assert store.bindings_for_record(decision.id) == [binding]
        rows = [(decision.id, entity.entity_id, "live", None)]
        assert store.binding_fingerprint() == sha256(encode_body(rows).encode()).hexdigest()
