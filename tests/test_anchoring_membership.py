"""Capture's Tier-1 pick: the newest accepted domain holding the node (whatever its
community), or listing its community; the index is rebuilt first when a reload emptied it.

see design/superpowers/specs/2026-10-14-graphify-upgrade-design.md (D5.3)"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import UTC, datetime

import pytest

import sidegraph.server as server_module
from sidegraph import anchoring
from sidegraph.anchoring import resolve_and_bind
from sidegraph.engine.reader import GraphifyReader
from sidegraph.retrieval import build_toc, drill_down
from sidegraph.schema import (
    AnchorBinding,
    Decision,
    DecisionKind,
    DecisionStatus,
    Descriptor,
    DomainStatus,
    Provenance,
)
from sidegraph.server import (
    _add_anchors_impl,
    _add_decision_impl,
    _add_fact_impl,
    _supersede_fact_impl,
)
from sidegraph.store import Store
from sidegraph.store_layout import DOMAIN_SNAPSHOT_NAME
from sidegraph.sync import sync
from tests.graph_fixtures import write_graph
from tests.snapshot_support import (
    STUB,
    accept,
    call_tool,
    legacy_graph,
    nodes_of,
    seed_members,
    standard_domains,
    tier1_names,
    touch_a_record,
)


def _tier1(store, reader, name, path):
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
    resolve_and_bind(d.id, Descriptor(name=name, file_path=path), reader, store)
    return tier1_names(store, d.id)


def _setup(tmp_path, data=None):
    return Store(tmp_path / ".sidegraph"), GraphifyReader(
        write_graph(tmp_path, "g.json", data or legacy_graph())
    )


def test_tier1_binds_the_domain_that_holds_the_node(tmp_path):
    """Red against the community-only pick: no domain lists community 1."""
    store, reader = _setup(tmp_path)
    part = accept(store, "part")
    seed_members(store, reader, part, {"model_invoice"})
    assert _tier1(store, reader, "Invoice", "src/pay/model.py") == ["domain:part"]


def test_a_node_outside_every_membership_keeps_the_community_binding(tmp_path):
    """Guard: a domain holding only Invoice does not catch .charge() in the same community."""
    store, reader = _setup(tmp_path)
    part = accept(store, "part")
    seed_members(store, reader, part, {"model_invoice"})
    assert _tier1(store, reader, "charge", "src/pay/api.py") == ["community:1"]


def test_an_indexed_member_with_no_community_binds_its_domain(tmp_path):
    """Red against the community gate (anchoring.py:150): a resolved node without a
    community got no Tier-1 binding at all."""
    data = legacy_graph()
    data["nodes"] += nodes_of((("solo", "Solo", "code", "src/pay/solo.py"),), None)
    store, reader = _setup(tmp_path, data)
    part = accept(store, "part")
    seed_members(store, reader, part, {"solo"})
    assert _tier1(store, reader, "Solo", "src/pay/solo.py") == ["domain:part"]


def _mistakes(store, reader) -> dict[str, int]:
    return {d["slug"]: d["mistakes"] for d in build_toc(store, reader)["domains"]}


def test_a_collapsed_stub_binds_a_domain_only_when_it_holds_every_stub(tmp_path):
    """Red against probing only the representative (the smallest stub id, stub_basemodel):
    part holds it, so BaseModel would bind domain:part although other holds the second stub,
    and a decision anchored to BaseModel would show under part in drill-down and the TOC. The
    stubs sit in communities 1 and 2, so the collapsed answer has no community either."""
    data = legacy_graph()
    data["nodes"] += nodes_of((("stub_basemodel_b", "BaseModel", "code", ""),), 2)
    store, reader = _setup(tmp_path, data)
    part, other = accept(store, "part"), accept(store, "other")
    seed_members(store, reader, part, {STUB[0]})
    seed_members(store, reader, other, {"stub_basemodel_b"})
    gotcha = store.add_decision(
        Decision(
            title="BaseModel copies",
            kind=DecisionKind.GOTCHA,
            status=DecisionStatus.ACCEPTED,
            context="c",
            choice="ch",
            valid_from=datetime.now(UTC),
            provenance=Provenance(source="manual"),
        )
    )
    resolve_and_bind(gotcha.id, Descriptor(name="BaseModel"), reader, store)
    assert tier1_names(store, gotcha.id) == []
    assert gotcha.id not in drill_down("part", store, reader)["decision_ids"]
    assert _mistakes(store, reader)["part"] == 0
    seed_members(store, reader, part, {STUB[0], "stub_basemodel_b"})
    assert _tier1(store, reader, "BaseModel", None) == ["domain:part"]
    assert gotcha.id in drill_down("part", store, reader)["decision_ids"]
    assert _mistakes(store, reader)["part"] == 1


def test_a_collapsed_entity_s_old_community_does_not_bypass_the_stub_rule(tmp_path):
    """Red against a branch-(b) join whose community alternative skips the all-stub test: the
    entity kept community 1 from a graph where BaseModel had one stub, part lists community 1,
    and the decision would show under part although part holds one stub of two."""
    data = legacy_graph()
    data["nodes"] += nodes_of((("stub_basemodel_b", "BaseModel", "code", ""),), 2)
    store, reader = _setup(tmp_path, data)
    part = accept(store, "part")
    store.refresh_domain_communities(part.domain_id, ["1"])
    seed_members(store, reader, part, {STUB[0]})
    entity = store.get_or_create_entity(Descriptor(name="BaseModel"))
    entity.last_seen_node_id, entity.last_seen_community = STUB[0], "1"
    store.upsert_entity(entity)
    gotcha = store.add_decision(
        Decision(
            title="BaseModel copies",
            kind=DecisionKind.GOTCHA,
            status=DecisionStatus.ACCEPTED,
            context="c",
            choice="ch",
            valid_from=datetime.now(UTC),
            provenance=Provenance(source="manual"),
        )
    )
    store.add_binding(
        AnchorBinding(record_id=gotcha.id, entity_id=entity.entity_id, tier=2, status="live")
    )
    assert gotcha.id not in drill_down("part", store, reader)["decision_ids"]


def test_capture_never_picks_a_domain_whose_refresh_failed(tmp_path, monkeypatch):
    """Red against a pick from every row: billing's refresh fails for this state, its rows are
    the previous pass's, and as the newest domain holding Invoice it would win."""
    store, reader = _setup(tmp_path)
    standard_domains(store)
    accept(store, "billing", seeds=[("charge", "src/pay/api.py")])
    sync(store, reader)
    real = GraphifyReader.resolve

    def resolve(self, desc, **kwargs):
        if desc.name == "charge" and desc.file_path == "src/pay/api.py":
            raise RuntimeError("malformed descriptor")
        return real(self, desc, **kwargs)

    monkeypatch.setattr(GraphifyReader, "resolve", resolve)
    accept(store, "misc")  # a status change: the next capture recomputes, and billing fails
    assert _tier1(store, reader, "Invoice", "src/pay/model.py") == ["domain:payments"]


@pytest.mark.parametrize("tool", ["add_fact", "supersede_fact"])
def test_a_fact_binds_from_membership_certified_after_a_status_change(tmp_path, tool):
    """Red against a binding that reads rows a status change left stale: billing, accepted
    between the two writes and seeded on Invoice, is the newest domain holding it, and the
    second fact would still bind payments. The rebuild runs inside the fact's reservation."""
    store, reader = _setup(tmp_path)
    standard_domains(store)
    sync(store, reader)
    anchors = [{"name": "Invoice", "file_path": "src/pay/model.py"}]
    first = _add_fact_impl(store, reader, "Invoices are numbered.", "observed", anchors=anchors)
    assert tier1_names(store, first["id"]) == ["domain:payments"]
    accept(store, "billing", seeds=[("Invoice", "src/pay/model.py")])
    if tool == "add_fact":
        second = _add_fact_impl(
            store, reader, "Invoices never change.", "observed", anchors=anchors
        )
    else:
        second = _supersede_fact_impl(
            store, reader, first["id"], "Invoices never change.", "observed", anchors=anchors
        )
    assert tier1_names(store, second["id"]) == ["domain:billing"]


def _draft(title):
    return {
        "title": title,
        "kind": "gotcha",
        "context": "c",
        "choice": "ch",
        "anchors": [{"name": "Invoice", "file_path": "src/pay/model.py"}],
    }


def test_a_capture_right_after_a_reload_binds_the_domain(tmp_path, monkeypatch):
    """Red against a capture that reads the index a reload emptied (and the communities it
    reset): Invoice would bind community:1. No retrieval call runs in between."""
    root = tmp_path / "proj" / ".sidegraph"
    graph = write_graph(tmp_path, "legacy.json", legacy_graph())
    with Store(root) as store:
        standard_domains(store)
        sync(store, GraphifyReader(graph))
    monkeypatch.setenv("SIDEGRAPH_GRAPH", str(graph))
    monkeypatch.setenv("SIDEGRAPH_DIR", str(root))
    monkeypatch.setattr(server_module, "_store", None)
    first = call_tool("add_decision", _draft("first"))
    assert not first.is_error, first
    assert tier1_names(server_module._store, json.loads(first.content[0].text)["id"]) == [
        "domain:payments"
    ]
    touch_a_record(root)  # a pull: the next tool call reloads the index
    second = call_tool("add_decision", _draft("second"))
    assert not second.is_error, second
    assert tier1_names(server_module._store, json.loads(second.content[0].text)["id"]) == [
        "domain:payments"
    ]


@pytest.mark.parametrize("tool", ["add_decision", "add_anchors"])
def test_a_drop_cannot_land_between_capture_s_pick_and_its_binding(tmp_path, monkeypatch, tool):
    """Red against a Tier-1 block whose reservation ends with the pick: another connection
    drops payments before the binding is written, and capture writes a live domain:payments
    binding for a domain already dropped. Decision capture and add_anchors hold no outer
    reservation, unlike the fact tools."""
    store, reader = _setup(tmp_path)
    payments = standard_domains(store)["payments"]
    sync(store, reader)
    anchors = [{"name": "Invoice", "file_path": "src/pay/model.py"}]
    record = store.add_decision(
        Decision(
            title="t",
            kind=DecisionKind.ADR,
            context="c",
            choice="ch",
            valid_from=datetime.now(UTC),
            provenance=Provenance(source="manual"),
        )
    )
    picked, release = threading.Event(), threading.Event()
    real_cover = anchoring._covering_domain

    def cover_then_pause(*a, **kw):
        found = real_cover(*a, **kw)
        picked.set()
        release.wait(5)
        return found

    monkeypatch.setattr(anchoring, "_covering_domain", cover_then_pause)
    status_at_write: list[DomainStatus] = []
    real_bind = store.add_binding

    def recording_bind(binding):
        if binding.tier == 1:
            status_at_write.append(store.get_domain(payments.domain_id).status)
        return real_bind(binding)

    monkeypatch.setattr(store, "add_binding", recording_bind)

    def capture():
        if tool == "add_decision":
            _add_decision_impl(store, reader, "t2", "adr", "c", "ch", anchors=anchors)
        else:
            _add_anchors_impl(store, reader, record.id, anchors)

    other = Store(store.path)
    worker = threading.Thread(target=capture)
    worker.start()
    assert picked.wait(5)
    drop = threading.Thread(target=other.ratify_domains, kwargs={"drop": [payments.domain_id]})
    drop.start()
    drop.join(0.5)
    waited = drop.is_alive()
    release.set()
    worker.join(10)
    drop.join(10)
    other.close()
    assert waited  # the drop waited for the capture's reservation
    assert status_at_write == [DomainStatus.ACCEPTED]


def test_a_pull_after_capture_s_pick_binds_no_domain(tmp_path, monkeypatch):
    """Red against a binding written from the pick whatever happened since: git takes no lock,
    and a pull that rewrote the snapshot after the pick would leave a live domain binding taken
    from membership the file on disk no longer gives."""
    store, reader = _setup(tmp_path)
    standard_domains(store)
    sync(store, reader)
    real_cover = anchoring._covering_domain

    def cover_then_pull(*a, **kw):
        found = real_cover(*a, **kw)
        (store.path / DOMAIN_SNAPSHOT_NAME).write_bytes(b"{}\n")  # the pull lands
        return found

    monkeypatch.setattr(anchoring, "_covering_domain", cover_then_pull)
    assert _tier1(store, reader, "Invoice", "src/pay/model.py") == ["community:1"]


def test_captures_fallback_preserves_its_callers_lost_transaction(tmp_path, monkeypatch):
    """A best-effort pick must not hide an aborted transaction and commit only later work."""
    store, reader = _setup(tmp_path)

    def lose_transaction(*args):
        store._conn.execute("ROLLBACK")
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(anchoring, "ensure_domain_membership", lose_transaction)
    with pytest.raises(sqlite3.OperationalError, match="disk I/O error"), store._mutation():
        store.set_meta("caller_before_loss", "1")
        anchoring._covering_domain(store, reader, ["model_invoice"], "1")
        store.set_meta("caller_after_loss", "1")
    assert store.get_meta("caller_before_loss") is None
    assert store.get_meta("caller_after_loss") is None


def test_a_failed_stub_resolve_does_not_break_reads_or_guess_a_domain(tmp_path, monkeypatch):
    """A failing re-resolve cannot establish that a representative covers every collapsed stub."""
    data = legacy_graph()
    data["nodes"] += nodes_of((("stub_basemodel_b", "BaseModel", "code", ""),), 2)
    store, reader = _setup(tmp_path, data)
    part = accept(store, "part")
    seed_members(store, reader, part, {STUB[0]})
    entity = store.get_or_create_entity(Descriptor(name="BaseModel"))
    entity.last_seen_node_id, entity.last_seen_community = STUB[0], "1"
    store.upsert_entity(entity)
    record = store.add_decision(
        Decision(
            title="BaseModel copies",
            kind=DecisionKind.GOTCHA,
            status=DecisionStatus.ACCEPTED,
            context="c",
            choice="ch",
            valid_from=datetime.now(UTC),
            provenance=Provenance(source="manual"),
        )
    )
    store.add_binding(
        AnchorBinding(record_id=record.id, entity_id=entity.entity_id, tier=2, status="live")
    )
    real = reader.resolve

    def broken(ref, **kwargs):
        if ref.name == "BaseModel":
            raise RuntimeError("injected resolver failure")
        return real(ref, **kwargs)

    monkeypatch.setattr(reader, "resolve", broken)
    assert record.id not in drill_down("part", store, reader)["decision_ids"]
    assert _mistakes(store, reader)["part"] == 0
