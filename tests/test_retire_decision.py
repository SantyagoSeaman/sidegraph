"""Retiring an accepted decision: ``accepted -> deprecated`` with a recorded reason.

A duplicate, or a decision whose subject is gone, can neither be dropped (only a proposal can)
nor honestly superseded (there is nothing new to say). ``Store.retire`` closes it in place:
status ``deprecated``, ``valid_to`` now, and ``retired = {reason, duplicate_of}``. Readers then
treat it as closed; history views still show it, with the reason.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

import fastmcp
import pytest

import sidegraph
from sidegraph import hot_index, integrity, server
from sidegraph.schema import (
    AnchorBinding,
    Decision,
    DecisionKind,
    DecisionStatus,
    Entity,
    EntityKind,
    Provenance,
    Retirement,
    Scope,
)
from sidegraph.server import (
    _decision_listing,
    _entity_history_listing,
    _retire_decision_impl,
    _retrieve_decisions_impl,
)
from sidegraph.store import RetireRefusedError, Store
from sidegraph.store_format import FORMAT2_MIN_SIDEGRAPH, marker_text
from sidegraph.store_layout import RECORD_FIELDS_REQUIREMENT, store_requirement
from sidegraph.store_rules import ILLEGAL_FIELD_CHANGE, ILLEGAL_STATUS_JUMP, classify_transition
from sidegraph.sync import _stale_decisions
from sidegraph.verify import PARSE_ERROR, verify_snapshot


def _decision(**overrides) -> Decision:
    base = dict(
        title="Use file-per-record JSON",
        kind=DecisionKind.ADR,
        status=DecisionStatus.ACCEPTED,
        context="A single committed SQLite file can't be merged by git.",
        choice="One JSON file per record, plus a derived, gitignored local index.",
        valid_from=datetime(2026, 1, 10, tzinfo=UTC),
        provenance=Provenance(source="manual"),
    )
    base.update(overrides)
    return Decision(**base)


@pytest.fixture(autouse=True)
def release_version(monkeypatch):
    """Run as the release that added ``retired``: a store it creates requires that release, so
    ``retire`` may write the field. A development build older than it can only refuse."""
    monkeypatch.setattr(sidegraph, "__version__", RECORD_FIELDS_REQUIREMENT)


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / ".sidegraph") as s:
        yield s


def _anchored(store: Store, decision: Decision) -> Entity:
    entity = store.upsert_entity(Entity(canonical_name="pkg.mod", kind=EntityKind.CONCRETE))
    store.add_binding(AnchorBinding(record_id=decision.id, entity_id=entity.entity_id, tier=2))
    return entity


def _on_disk(store: Store, decision_id: str) -> dict:
    return json.loads((store.path / "decisions" / f"{decision_id}.json").read_text())


# -- the store write path ---------------------------------------------------------------------


def test_retire_moves_accepted_to_deprecated_with_the_reason(store: Store):
    keep = store.add_decision(_decision(title="keep"))
    dup = store.add_decision(_decision(title="dup"))

    retired, _ = store.retire(dup.id, "Same rule as the kept record.", duplicate_of=keep.id)

    assert retired.status == DecisionStatus.DEPRECATED
    assert retired.valid_to is not None
    assert retired.retired == Retirement(
        reason="Same rule as the kept record.", duplicate_of=keep.id
    )
    body = _on_disk(store, dup.id)
    assert body["status"] == "deprecated"
    assert body["retired"] == {"reason": "Same rule as the kept record.", "duplicate_of": keep.id}
    # the index re-reads the same record
    assert store.get_decision(dup.id) == retired


def test_retire_without_duplicate_of_is_the_subject_gone_case(store: Store):
    d = store.add_decision(_decision())

    retired, _ = store.retire(d.id, "The module it governed was deleted.")

    assert retired.retired == Retirement(reason="The module it governed was deleted.")


@pytest.mark.parametrize("status", ["proposed", "superseded", "rejected", "deprecated"])
def test_retire_refuses_anything_not_accepted(store: Store, status: str):
    d = _decision(status=DecisionStatus(status))
    if status in ("superseded", "rejected", "deprecated"):
        d = d.model_copy(update={"valid_to": datetime(2026, 2, 1, tzinfo=UTC)})
    if status == "deprecated":
        d = d.model_copy(update={"retired": Retirement(reason="earlier")})
    # write the raw record directly: the API never lands these shapes on its own
    with store._mutation():
        store._write_decision(d)

    with pytest.raises(RetireRefusedError, match=f"is {status}, not accepted"):
        store.retire(d.id, "reason")
    assert store.get_decision(d.id) == d


def test_retire_refuses_an_unknown_id(store: Store):
    with pytest.raises(RetireRefusedError, match="no decision") as refused:
        store.retire("01NOPE", "reason")
    assert "01NOPE" not in str(refused.value)


def test_retire_refuses_a_blank_reason(store: Store):
    d = store.add_decision(_decision())
    with pytest.raises(RetireRefusedError, match="reason"):
        store.retire(d.id, "   ")
    assert store.get_decision(d.id).status == DecisionStatus.ACCEPTED


def test_retire_refuses_a_duplicate_of_that_is_not_an_open_accepted_decision(store: Store):
    d = store.add_decision(_decision())
    proposal = store.add_decision(_decision(status=DecisionStatus.PROPOSED))

    with pytest.raises(RetireRefusedError, match="itself"):
        store.retire(d.id, "dup", duplicate_of=d.id)
    with pytest.raises(RetireRefusedError, match="duplicate_of names no decision") as refused:
        store.retire(d.id, "dup", duplicate_of="01NOPE")
    assert "01NOPE" not in str(refused.value)
    with pytest.raises(RetireRefusedError, match="not an accepted"):
        store.retire(d.id, "dup", duplicate_of=proposal.id)
    assert store.get_decision(d.id).status == DecisionStatus.ACCEPTED


def test_retire_refuses_while_a_successor_is_pending(store: Store):
    d = store.add_decision(_decision())
    pending = store.add_decision(_decision(status=DecisionStatus.PROPOSED, supersedes=d.id))

    with pytest.raises(RetireRefusedError, match=pending.id):
        store.retire(d.id, "gone")


def test_a_retired_record_survives_a_reload(tmp_path):
    with Store(tmp_path / ".sidegraph") as s:
        d = s.add_decision(_decision())
        s.retire(d.id, "gone")
    (tmp_path / ".sidegraph" / "index.db").unlink()
    with Store(tmp_path / ".sidegraph") as s:
        again = s.get_decision(d.id)
    assert again.status == DecisionStatus.DEPRECATED
    assert again.retired == Retirement(reason="gone")


# -- the schema and verify ---------------------------------------------------------------------


def test_retired_on_a_record_that_is_not_deprecated_is_invalid():
    with pytest.raises(ValueError, match="retired"):
        _decision(retired=Retirement(reason="x"))


def test_verify_snapshot_flags_retired_on_an_accepted_record(store: Store):
    d = store.add_decision(_decision())
    path = store.path / "decisions" / f"{d.id}.json"
    body = json.loads(path.read_text())
    body["retired"] = {"reason": "hand edit", "duplicate_of": None}
    path.write_text(json.dumps(body))

    assert PARSE_ERROR in [v.code for v in verify_snapshot(store.path)]


def test_verify_snapshot_accepts_a_store_retire(store: Store):
    d = store.add_decision(_decision())
    store.retire(d.id, "gone")
    assert verify_snapshot(store.path) == []


def _pair(store: Store) -> tuple[dict, dict]:
    d = store.add_decision(_decision())
    old = _on_disk(store, d.id)
    store.retire(d.id, "gone")
    return old, _on_disk(store, d.id)


def test_transition_allows_retired_only_with_accepted_to_deprecated(store: Store):
    old, new = _pair(store)
    assert classify_transition("decision", old, new) == []
    # an old file written before the field existed has no key at all
    legacy = {k: v for k, v in old.items() if k != "retired"}
    assert classify_transition("decision", legacy, new) == []


def test_transition_flags_any_other_change_to_retired(store: Store):
    old, new = _pair(store)
    edited = {**new, "retired": {"reason": "rewritten", "duplicate_of": None}}
    unset = {**new, "retired": None}
    on_accepted = {**old, "retired": {"reason": "x", "duplicate_of": None}}

    for before, after in ((new, edited), (new, unset), (old, on_accepted)):
        codes = [v.code for v in classify_transition("decision", before, after)]
        assert ILLEGAL_FIELD_CHANGE in codes, (before, after)


def test_transition_flags_retired_landing_on_a_proposed_record_dropped():
    old = {"id": "A", "status": "proposed", "valid_to": None, "retired": None}
    new = {"id": "A", "status": "rejected", "valid_to": "2026-02-01T00:00:00Z"}
    new["retired"] = {"reason": "x", "duplicate_of": None}
    assert ILLEGAL_FIELD_CHANGE in [v.code for v in classify_transition("decision", old, new)]


def test_transition_allows_ratify_then_retire_inside_one_diffed_range():
    old = {"id": "A", "status": "proposed", "valid_to": None, "ratified_at": None}
    new = {
        "id": "A",
        "status": "deprecated",
        "valid_to": "2026-02-01T00:00:00Z",
        "ratified_at": "2026-01-20T00:00:00Z",
        "retired": {"reason": "x", "duplicate_of": None},
    }
    assert classify_transition("decision", old, new) == []


# -- readers treat deprecated as closed --------------------------------------------------------


def test_readers_stop_serving_a_retired_decision(store: Store):
    keep = store.add_decision(_decision(title="keep", scope=Scope.GLOBAL))
    gone = store.add_decision(_decision(title="gone", scope=Scope.GLOBAL))
    entity = _anchored(store, gone)
    store.add_binding(AnchorBinding(record_id=keep.id, entity_id=entity.entity_id, tier=2))
    store.retire(gone.id, "duplicate", duplicate_of=keep.id)
    # even a far-past ``as_of`` must not bring it back as live
    early = datetime(2026, 1, 11, tzinfo=UTC)

    assert [d.id for d in store.valid_decisions_for_entity(entity.entity_id, early)] == [keep.id]
    assert [d.id for d in store.decisions_by_scope(Scope.GLOBAL, early)] == [keep.id]
    assert [d["id"] for d in _retrieve_decisions_impl(store)] == [keep.id]
    listing = json.loads(_decision_listing(store, None, kind="adr"))
    assert [d["id"] for d in listing["decisions"]] == [keep.id]


def test_history_views_still_show_a_retired_decision_with_its_reason(store: Store):
    gone = store.add_decision(_decision(title="gone"))
    entity = _anchored(store, gone)
    store.retire(gone.id, "The module it governed was deleted.")

    history = json.loads(_entity_history_listing(store, entity.entity_id))
    (record,) = history["records"]
    assert record["status"] == "deprecated"
    assert record["retired"]["reason"] == "The module it governed was deleted."
    assert [d["id"] for d in _retrieve_decisions_impl(store, include_superseded=True)] == [gone.id]
    listing = json.loads(_decision_listing(store, None, status="deprecated"))
    assert [d["id"] for d in listing["decisions"]] == [gone.id]


def test_hot_path_treats_deprecated_as_closed():
    record = {"status": "deprecated", "valid_to": None}
    assert not hot_index._is_live(record, datetime(2026, 1, 1, tzinfo=UTC))


def test_sync_does_not_report_a_retired_decision_as_stale(store: Store):
    d = store.add_decision(_decision())
    entity = store.upsert_entity(Entity(canonical_name="gone", kind=EntityKind.CONCRETE))
    store.add_binding(
        AnchorBinding(record_id=d.id, entity_id=entity.entity_id, tier=2, status="orphaned")
    )
    assert [s["id"] for s in _stale_decisions(store)] == [d.id]

    store.retire(d.id, "The code it was about is gone.")

    assert _stale_decisions(store) == []


# -- the MCP tool --------------------------------------------------------------------------------


def test_tool_retires_and_redacts_the_reason(store: Store):
    keep = store.add_decision(_decision(title="keep"))
    dup = store.add_decision(_decision(title="dup"))
    secret = "AKIAIOSFODNN7EXAMPLE"

    result = _retire_decision_impl(store, dup.id, f"duplicate; key {secret}", duplicate_of=keep.id)

    assert result["id"] == dup.id
    assert result["status"] == "deprecated"
    assert result["duplicate_of"] == keep.id
    assert result["redactions"] >= 1
    assert secret not in json.dumps(_on_disk(store, dup.id))


def test_tool_refusal_reaches_the_caller_verbatim(store: Store, monkeypatch):
    proposal = store.add_decision(_decision(status=DecisionStatus.PROPOSED))
    monkeypatch.setattr(server, "_store", store)

    async def run():
        async with fastmcp.Client(server.mcp) as client:
            return await client.call_tool(
                "retire_decision", {"id": proposal.id, "reason": "r"}, raise_on_error=False
            )

    result = asyncio.run(run())
    assert result.is_error
    assert "is proposed, not accepted" in result.content[0].text


def test_proposed_to_deprecated_without_a_ratify_in_range_is_a_status_jump():
    old = {"id": "A", "status": "proposed", "valid_to": None, "ratified_at": None}
    new = {**old, "status": "deprecated", "valid_to": "2026-02-01T00:00:00Z"}
    assert ILLEGAL_STATUS_JUMP in [v.code for v in classify_transition("decision", old, new)]


# -- review round 1 ----------------------------------------------------------------------------


def test_retire_refuses_a_duplicate_of_that_is_accepted_but_closed(store: Store):
    d = store.add_decision(_decision())
    closed = store.add_decision(_decision(valid_to=datetime(2026, 2, 1, tzinfo=UTC)))

    with pytest.raises(RetireRefusedError, match="not an accepted, open"):
        store.retire(d.id, "dup", duplicate_of=closed.id)
    assert store.get_decision(d.id).status == DecisionStatus.ACCEPTED


def test_retire_refuses_while_a_proposed_fact_supports_the_decision(store: Store):
    from sidegraph.schema import Fact

    d = store.add_decision(_decision())
    fact = store.add_fact(
        Fact(
            statement="observed",
            source="bench",
            supports=[d.id],
            valid_from=datetime(2026, 1, 11, tzinfo=UTC),
            provenance=Provenance(source="agent"),
        )
    )

    with pytest.raises(RetireRefusedError, match=f"{fact.id}.*ratify or drop it first"):
        store.retire(d.id, "gone")
    assert store.get_decision(d.id).status == DecisionStatus.ACCEPTED


def test_retire_refuses_below_the_record_fields_requirement(store: Store):
    d = store.add_decision(_decision())
    (store.path / "format").write_text(marker_text(2, FORMAT2_MIN_SIDEGRAPH, 64), encoding="utf-8")

    with pytest.raises(RetireRefusedError) as refused:
        store.retire(d.id, "gone")
    assert str(refused.value) == (
        f"this store requires Sidegraph >= {FORMAT2_MIN_SIDEGRAPH}; run `sidegraph-require "
        f"--sidegraph {RECORD_FIELDS_REQUIREMENT}` and commit `.sidegraph/format` before retiring"
    )
    assert store.get_decision(d.id).status == DecisionStatus.ACCEPTED


def test_a_new_store_requires_the_record_fields_release_only_once_it_runs(tmp_path, monkeypatch):
    with Store(tmp_path / "release" / ".sidegraph") as s:
        assert store_requirement(s.path) == RECORD_FIELDS_REQUIREMENT
    monkeypatch.setattr(sidegraph, "__version__", "0.23.0")  # a development build
    with Store(tmp_path / "dev" / ".sidegraph") as s:
        assert store_requirement(s.path) == FORMAT2_MIN_SIDEGRAPH


def test_migrate_stamps_the_same_floor(monkeypatch):
    from sidegraph.migrate import _requirement_for_marker

    assert _requirement_for_marker(None) == RECORD_FIELDS_REQUIREMENT
    assert _requirement_for_marker("0.99.0") == "0.99.0"
    monkeypatch.setattr(sidegraph, "__version__", "0.23.0")
    assert _requirement_for_marker(None) == FORMAT2_MIN_SIDEGRAPH


def test_a_populated_new_store_is_healthy_at_the_release_version(tmp_path):
    with Store(tmp_path / ".sidegraph") as s:
        s.add_decision(_decision())
    result = integrity.run(integrity.Inputs(s.path, datetime.now(UTC)), "doctor")
    assert "record-fields-requirement" not in [p.check for p in result.problems]


def _file_entity(store: Store, name: str, path: str) -> Entity:
    from sidegraph.schema import Descriptor

    return store.upsert_entity(
        Entity(
            canonical_name=name,
            kind=EntityKind.CONCRETE,
            descriptor=Descriptor(name=name, file_path=path),
        )
    )


def test_retiring_a_duplicate_carries_its_reach_to_the_kept_record(store: Store):
    keep = store.add_decision(_decision(title="keep"))
    dup = store.add_decision(_decision(title="dup"))
    here = _file_entity(store, "here", "src/x.py")
    there = _file_entity(store, "there", "lib/y.py")
    community = store.upsert_entity(Entity(canonical_name="community:7", kind=EntityKind.ABSTRACT))
    gone = _file_entity(store, "gone", "lib/z.py")
    store.add_binding(AnchorBinding(record_id=keep.id, entity_id=here.entity_id, tier=2))
    store.add_binding(AnchorBinding(record_id=dup.id, entity_id=there.entity_id, tier=2))
    store.add_binding(
        AnchorBinding(record_id=dup.id, entity_id=community.entity_id, tier=1, status="degraded")
    )
    store.add_binding(
        AnchorBinding(record_id=dup.id, entity_id=gone.entity_id, tier=2, status="orphaned")
    )

    result = _retire_decision_impl(store, dup.id, "same rule", duplicate_of=keep.id)

    assert [d.id for d in store.valid_decisions_for_entity(there.entity_id)] == [keep.id]
    assert [d.id for d in store.valid_decisions_for_entity(community.entity_id)] == [keep.id]
    assert store.valid_decisions_for_entity(gone.entity_id) == []  # an orphaned anchor stays
    assert {a["entity_id"] for a in result["anchors_added"]} == {
        there.entity_id,
        community.entity_id,
    }
    assert {b.entity_id for b in store.bindings_for_record(dup.id)} == {
        there.entity_id,
        community.entity_id,
        gone.entity_id,
    }


def test_retiring_a_duplicate_restores_a_weaker_kept_binding_to_the_same_entity(store: Store):
    """Both records bound to one entity: an orphaned or degraded kept binding would leave
    nothing surfacing there once the duplicate's live one closes. A stronger kept binding
    stays as it is."""
    keep = store.add_decision(_decision(title="keep"))
    dup = store.add_decision(_decision(title="dup"))
    orphaned = _file_entity(store, "orphaned", "src/a.py")
    degraded = _file_entity(store, "degraded", "src/b.py")
    stronger = _file_entity(store, "stronger", "src/c.py")
    for entity, kept_status, dup_status in (
        (orphaned, "orphaned", "live"),
        (degraded, "degraded", "live"),
        (stronger, "live", "degraded"),
    ):
        store.add_binding(
            AnchorBinding(record_id=keep.id, entity_id=entity.entity_id, tier=2, status=kept_status)
        )
        store.add_binding(
            AnchorBinding(
                record_id=dup.id,
                entity_id=entity.entity_id,
                tier=1,
                weight=0.5,
                status=dup_status,
                relation="modifies",
            )
        )

    result = _retire_decision_impl(store, dup.id, "same rule", duplicate_of=keep.id)

    for entity in (orphaned, degraded, stronger):
        assert [d.id for d in store.valid_decisions_for_entity(entity.entity_id)] == [keep.id]
    assert {a["entity_id"] for a in result["anchors_added"]} == {
        orphaned.entity_id,
        degraded.entity_id,
    }
    kept = {b.entity_id: b for b in store.bindings_for_record(keep.id)}
    for entity in (orphaned, degraded):
        b = kept[entity.entity_id]
        assert (b.status, b.tier, b.weight, b.relation) == ("live", 1, 0.5, "modifies")
    assert (kept[stronger.entity_id].status, kept[stronger.entity_id].tier) == ("live", 2)


def test_retiring_without_duplicate_of_adds_no_anchors(store: Store):
    d = store.add_decision(_decision())
    _anchored(store, d)
    assert _retire_decision_impl(store, d.id, "gone")["anchors_added"] == []


def test_ratify_drop_of_an_accepted_decision_points_at_retire(store: Store):
    from sidegraph.server import _ratify_impl

    d = store.add_decision(_decision())
    assert "retire_decision" in _ratify_impl(store, drop=[d.id])[d.id]


def test_okf_export_shows_why_a_decision_was_retired(store: Store):
    from sidegraph.okf import build_bundle

    keep = store.add_decision(_decision(title="keep"))
    dup = store.add_decision(_decision(title="dup"))
    store.retire(dup.id, "Same rule as the kept one.", duplicate_of=keep.id)

    page = next(text for text in build_bundle(store).values() if dup.id.lower() in text)
    assert "Same rule as the kept one." in page
    assert "Duplicate of [keep]" in page


def test_adr_export_shows_why_a_decision_was_retired(store: Store, tmp_path):
    from sidegraph.adr import read_snapshot, render_record
    from sidegraph.profiles import get_profile

    keep = store.add_decision(_decision(title="keep"))
    dup = store.add_decision(_decision(title="dup"))
    store.retire(dup.id, "Same rule as the kept one.", duplicate_of=keep.id)

    snapshot = read_snapshot(store.path)
    paths = {keep.id: "keep.md", dup.id: "dup.md"}
    text = render_record(
        snapshot, dup.id, paths, get_profile("generic-adr").adr_export, tmp_path / "adr"
    )
    assert "Retired: Same rule as the kept one." in text
    assert "Duplicate of: [" in text and "keep.md" in text


def test_session_start_cache_does_not_serve_a_retired_global_mistake(tmp_path, monkeypatch, capsys):
    """No graph: SessionStart renders the cached domain map, whose global mistakes were built
    before the decision was retired."""
    import io
    import os
    import sys

    from sidegraph.host import hooks
    from sidegraph.retrieval import TOC_CACHE_KEY, build_toc
    from sidegraph.schema import Domain

    for key in list(os.environ):
        if key.startswith("SIDEGRAPH_"):
            monkeypatch.delenv(key)
    monkeypatch.setattr(sys, "argv", ["hook"])
    db = tmp_path / "store"
    with Store(db) as s:
        domain = s.add_domain(
            Domain(slug="test", title="Test", summary="Test", provenance={"source": "manual"})
        )
        s.ratify_domains(accept=[domain.domain_id])
        d = s.add_decision(
            _decision(kind=DecisionKind.GOTCHA, scope=Scope.GLOBAL, choice="RETIRED_SENTINEL")
        )
        s.set_meta(TOC_CACHE_KEY, json.dumps(build_toc(s)))
        _retire_decision_impl(s, d.id, "subject gone")
    monkeypatch.setenv("SIDEGRAPH_DIR", str(db))
    monkeypatch.setenv("SIDEGRAPH_GRAPH", str(tmp_path / "missing.json"))
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))

    hooks.session_start()

    out = json.loads(capsys.readouterr().out)
    assert "RETIRED_SENTINEL" not in out["hookSpecificOutput"]["additionalContext"]


def test_grok_instructions_do_not_serve_a_retired_global_mistake(tmp_path, monkeypatch):
    from sidegraph.host import mcp_entry
    from sidegraph.retrieval import TOC_CACHE_KEY, build_toc
    from sidegraph.schema import Domain

    with Store(tmp_path / "store") as s:
        domain = s.add_domain(
            Domain(slug="test", title="Test", summary="Test", provenance={"source": "manual"})
        )
        s.ratify_domains(accept=[domain.domain_id])
        d = s.add_decision(
            _decision(kind=DecisionKind.GOTCHA, scope=Scope.GLOBAL, choice="RETIRED_SENTINEL")
        )
        s.set_meta(TOC_CACHE_KEY, json.dumps(build_toc(s)))
        _retire_decision_impl(s, d.id, "subject gone")
        monkeypatch.setattr(server, "_store", s)

        text = mcp_entry._grok_session_map()

    assert text is not None and "RETIRED_SENTINEL" not in text


def test_cold_grok_instructions_read_global_mistakes_live(tmp_path, monkeypatch):
    """Before any tool call has opened the store (``server._store is None``) the map comes from
    the read-only index: the global sections are the current decision rows, not the cache's,
    so a retired mistake is gone, a live one stays and one added after the cache shows."""
    from sidegraph.host import mcp_entry
    from sidegraph.retrieval import TOC_CACHE_KEY, build_toc
    from sidegraph.schema import Domain

    db = tmp_path / "store"
    with Store(db) as s:
        domain = s.add_domain(
            Domain(slug="test", title="Test", summary="Test", provenance={"source": "manual"})
        )
        s.ratify_domains(accept=[domain.domain_id])
        retired = s.add_decision(
            _decision(kind=DecisionKind.GOTCHA, scope=Scope.GLOBAL, choice="RETIRED_SENTINEL")
        )
        s.add_decision(
            _decision(kind=DecisionKind.GOTCHA, scope=Scope.GLOBAL, choice="KEPT_SENTINEL")
        )
        s.set_meta(TOC_CACHE_KEY, json.dumps(build_toc(s)))
        _retire_decision_impl(s, retired.id, "subject gone")
        s.add_decision(
            _decision(
                kind=DecisionKind.CONSTRAINT,
                scope=Scope.GLOBAL,
                choice="LATE_SENTINEL",
                valid_from=datetime(2026, 2, 1, tzinfo=UTC),
            )
        )
    monkeypatch.setenv("SIDEGRAPH_DIR", str(db))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(server, "_store", None)

    text = mcp_entry._grok_session_map()

    assert text is not None and "- Test" in text
    assert "RETIRED_SENTINEL" not in text
    assert "KEPT_SENTINEL" in text and "LATE_SENTINEL" in text
    assert server._store is None


@pytest.mark.parametrize("field", ["id", "duplicate_of"])
def test_refusals_do_not_echo_an_unknown_id(store: Store, monkeypatch, field):
    secret = "AKIAIOSFODNN7EXAMPLE"
    d = store.add_decision(_decision())
    monkeypatch.setattr(server, "_store", store)
    arguments = {"id": d.id, "reason": "duplicate", field: secret}

    async def run():
        async with fastmcp.Client(server.mcp) as client:
            return await client.call_tool("retire_decision", arguments, raise_on_error=False)

    result = asyncio.run(run())
    assert result.is_error
    assert secret not in result.content[0].text
