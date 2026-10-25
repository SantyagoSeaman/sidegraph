"""Writes on a format-2 store: an edit of a packed record creates a per-record file and never
touches a shard (T-W1); a quarantined or skipped identity is refused per ``(type, id)``, through
the store's writers and through the MCP callers that ask ``is_skipped`` first (T-W3); a new
record still writes.
# see design/superpowers/specs/2026-10-20-packed-store-format-design.md (§5.4; T-W1, T-W3)
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from ulid import ULID

from sidegraph.schema import (
    AnchorBinding,
    Decision,
    DecisionKind,
    Descriptor,
    Entity,
    Provenance,
)
from sidegraph.server import (
    _add_anchors_impl,
    _record_drift_check_impl,
    _supersede_decision_impl,
    _supersede_fact_impl,
)
from sidegraph.store import Store
from sidegraph.validation_errors import SafeWriteError
from tests.store_format2_support import (
    bindings_body,
    canonical_bytes,
    commit_all,
    decision_body,
    domain_body,
    effective_snapshot,
    entity_body,
    fact_body,
    git_repo,  # noqa: F401  (pytest fixture)
    make_store,
    per_record_file,
    put_file,
    put_shard_line,
    shard_bytes,
)

CONFLICT = '<<<<<<< HEAD\n{"id": "x"}\n=======\n{"id": "y"}\n>>>>>>> branch\n'


def _id() -> str:
    return str(ULID())


def _decision(**over) -> Decision:
    base: dict = dict(
        title="a new adr",
        kind=DecisionKind.ADR,
        context="c",
        choice="ch",
        valid_from=datetime.now(UTC),
        provenance=Provenance(source="manual"),
    )
    base.update(over)
    return Decision(**base)


class _World:
    """A format-2 store whose every record lives only in shards."""

    def __init__(self, tmp_path: Path) -> None:
        self.store = make_store(tmp_path)
        self.ent1, self.ent2 = _id(), _id()
        self.dec_ratify, self.dec_drop, self.dec_old = _id(), _id(), _id()
        self.fact, self.domain = _id(), _id()
        put_shard_line(
            self.store,
            "entity",
            self.ent1,
            entity_body(self.ent1, descriptor={"name": "f", "file_path": "a.py"}),
        )
        put_shard_line(self.store, "entity", self.ent2, entity_body(self.ent2))
        put_shard_line(self.store, "decision", self.dec_ratify, decision_body(self.dec_ratify))
        put_shard_line(self.store, "decision", self.dec_drop, decision_body(self.dec_drop))
        put_shard_line(
            self.store, "decision", self.dec_old, decision_body(self.dec_old, status="accepted")
        )
        put_shard_line(
            self.store, "fact", self.fact, fact_body(self.fact, supports=[self.dec_ratify])
        )
        put_shard_line(self.store, "domain", self.domain, domain_body(self.domain))
        put_shard_line(self.store, "bindings", self.dec_ratify, bindings_body(self.ent1))


@pytest.mark.parametrize(
    "op",
    ["ratify", "supersede", "drop", "cascade", "domain_status", "entity_moved", "add_anchors"],
)
def test_write_to_packed_record_creates_per_record_file(tmp_path, op):
    world = _World(tmp_path)
    store = world.store
    before = shard_bytes(store)
    assert before, "the fixture packs its records"
    snapshot_before = effective_snapshot(store)
    assert all(body is not None for body in snapshot_before.values())
    with Store(store) as s:
        if op in ("ratify", "cascade"):
            s.ratify(world.dec_ratify)
            written = ("decision", world.dec_ratify) if op == "ratify" else ("fact", world.fact)
        elif op == "drop":
            s.drop(world.dec_drop)
            written = ("decision", world.dec_drop)
        elif op == "supersede":
            s.add_decision(_decision(supersedes=world.dec_old))
            written = ("decision", world.dec_old)
        elif op == "domain_status":
            s.ratify_domains(accept=[world.domain])
            written = ("domain", world.domain)
        elif op == "entity_moved":
            s.upsert_entity(
                Entity(
                    entity_id=world.ent1,
                    canonical_name=f"entity {world.ent1}",
                    descriptor=Descriptor(name="f", file_path="b.py"),
                )
            )
            written = ("entity", world.ent1)
        else:
            s.add_binding(AnchorBinding(record_id=world.dec_ratify, entity_id=world.ent2, tier=2))
            written = ("bindings", world.dec_ratify)
    assert shard_bytes(store) == before
    assert per_record_file(store, *written).exists()
    if op == "add_anchors":
        # a bindings file is the whole list: the shard line's entry and the new one
        import json  # noqa: PLC0415

        entries = json.loads(per_record_file(store, *written).read_text(encoding="utf-8"))
        assert sorted(e["entity_id"] for e in entries) == sorted([world.ent1, world.ent2])
    # every record the op did not touch is still readable and byte-for-byte the same body
    after = effective_snapshot(store)
    touched = {
        "ratify": {("decision", world.dec_ratify), ("fact", world.fact)},
        "cascade": {("decision", world.dec_ratify), ("fact", world.fact)},
        "drop": {("decision", world.dec_drop)},
        "supersede": {("decision", world.dec_old)},
        "domain_status": {("domain", world.domain)},
        "entity_moved": {("entity", world.ent1)},
        "add_anchors": {("bindings", world.dec_ratify)},
    }[op]
    for key, body in snapshot_before.items():
        if key not in touched:
            assert after[key] is not None and after[key] == body, key
    # a cold rebuild (no index) serves the persisted result of the write
    for stale in store.glob("index.db*"):
        stale.unlink()
    with Store(store) as s:
        if op in ("ratify", "cascade"):
            decision = s.get_decision(world.dec_ratify)
            assert decision is not None and decision.status.value == "accepted"
            assert decision.ratified_at is not None
            fact = s.get_fact(world.fact)
            assert fact is not None and fact.status.value == "accepted"
        elif op == "drop":
            dropped = s.get_decision(world.dec_drop)
            assert dropped is not None and dropped.status.value == "rejected"
            assert dropped.valid_to is not None
        elif op == "supersede":
            old = s.get_decision(world.dec_old)
            assert old is not None and old.status.value == "superseded"
            assert old.valid_to is not None
        elif op == "domain_status":
            domain = s.get_domain(world.domain)
            assert domain is not None and domain.status.value == "accepted"
        elif op == "entity_moved":
            moved = s.get_entity(world.ent1)
            assert moved is not None and moved.descriptor is not None
            assert moved.descriptor.file_path == "b.py"
        else:
            kept = sorted(b.entity_id for b in s.bindings_for_record(world.dec_ratify))
            assert kept == sorted([world.ent1, world.ent2])


# -- T-W3: refusal is per identity ---------------------------------------------------------------


def _settled_bytes(store: Path) -> dict[str, bytes]:
    """The store's files after one open has created its layout files (``.gitignore``), so a later
    comparison sees only what a refused write would change."""
    with Store(store):
        pass
    return canonical_bytes(store)


def _quarantine(store: Path, state: str, dec: str) -> None:
    if state == "skip_list":
        put_file(store, "decision", decision_body(dec)).write_text(CONFLICT, encoding="utf-8")
    elif state == "quarantined_unreadable":
        put_shard_line(store, "decision", dec, decision_body(dec, status="bogus"))
    elif state == "quarantined_conflict":
        put_shard_line(store, "decision", dec, decision_body(dec))
        shard = next((store / "shards").glob("*.shard"))
        shard.write_bytes(
            b"<<<<<<< HEAD\n"
            + shard.read_bytes()
            + b"=======\n"
            + shard.read_bytes()
            + b">>>>>>> b\n"
        )
    else:
        assert state == "duplicate_packed"
        put_shard_line(store, "decision", dec, decision_body(dec, title="one"))
        put_shard_line(store, "decision", dec, decision_body(dec, title="two"))


@pytest.mark.parametrize(
    "state", ["skip_list", "quarantined_unreadable", "quarantined_conflict", "duplicate_packed"]
)
def test_writes_to_skipped_or_quarantined_identity_refuse(tmp_path, state):
    store = make_store(tmp_path)
    dec = _id()
    _quarantine(store, state, dec)
    before = _settled_bytes(store)
    with Store(store) as s:
        assert s.is_skipped("decisions", dec)
        assert s.get_decision(dec) is None
        with pytest.raises(ValueError):
            s.ratify(dec)
        with pytest.raises(ValueError, match="refusing to overwrite"):
            s.add_decision(_decision(id=dec))
        assert canonical_bytes(store) == before
    with Store(store) as s:  # a reopen heals nothing and writes nothing
        assert s.is_skipped("decisions", dec)
    assert canonical_bytes(store) == before


def test_new_record_still_writes(tmp_path):
    store = make_store(tmp_path)
    bad = _id()
    put_shard_line(store, "decision", bad, decision_body(bad, status="bogus"))
    with Store(store) as s:
        d = s.add_decision(_decision())
        assert s.get_decision(d.id) is not None
        assert not s.is_skipped("decisions", d.id)
    assert per_record_file(store, "decision", d.id).exists()


# -- T-W3 through the MCP callers ----------------------------------------------------------------


def _quarantined_bindings_world(tmp_path: Path):
    """A readable accepted decision and fact whose bindings line cannot be read."""
    store = make_store(tmp_path)
    dec, fct = _id(), _id()
    put_shard_line(store, "decision", dec, decision_body(dec, status="accepted"))
    put_shard_line(store, "fact", fct, fact_body(fct, supports=[dec]))
    put_shard_line(store, "bindings", dec, {"not": "a list"})
    put_shard_line(store, "bindings", fct, {"not": "a list"})
    return store, dec, fct


def test_mcp_callers_refuse_quarantined_identity_supersede_decision(tmp_path):
    store, dec, _ = _quarantined_bindings_world(tmp_path)
    before = _settled_bytes(store)
    with Store(store) as s:
        assert s.get_decision(dec) is not None
        with pytest.raises(ValueError, match=rf"bindings {dec}.*shards/"):
            _supersede_decision_impl(s, None, dec, title="t", kind="adr", context="c", choice="x")
    assert canonical_bytes(store) == before


def test_mcp_callers_refuse_quarantined_identity_supersede_fact(tmp_path):
    store, _, fct = _quarantined_bindings_world(tmp_path)
    before = _settled_bytes(store)
    with Store(store) as s:
        assert s.get_fact(fct) is not None
        with pytest.raises(ValueError, match=rf"bindings {fct}.*shards/"):
            _supersede_fact_impl(s, None, fct, statement="s", source="measured")
    assert canonical_bytes(store) == before


def test_mcp_callers_refuse_quarantined_identity_add_anchors(tmp_path):
    store, dec, _ = _quarantined_bindings_world(tmp_path)
    before = _settled_bytes(store)
    with Store(store) as s:
        assert s.get_decision(dec) is not None
        with pytest.raises(ValueError, match=rf"bindings {dec}.*shards/"):
            _add_anchors_impl(
                s, None, dec, anchors=[{"name": "sync.py", "file_path": "src/sync.py"}]
            )
    assert canonical_bytes(store) == before
    assert not list((store / "entities").glob("*.json"))


def test_mcp_callers_refuse_quarantined_identity_record_drift_check(git_repo):  # noqa: F811
    store, dec, _ = _quarantined_bindings_world(git_repo)
    (git_repo / "a.py").write_text("x = 1\n", encoding="utf-8")
    head = commit_all(git_repo, "base", date="2026-01-01T00:00:00Z")
    with Store(store) as s:
        assert s.get_decision(dec) is not None
        with pytest.raises(SafeWriteError) as raised:
            _record_drift_check_impl(s, dec, head, ["a.py"], "still true")
    assert raised.value.field == "decision_id"
