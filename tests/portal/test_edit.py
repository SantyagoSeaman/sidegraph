"""Version edits through the Portal's D7 endpoints."""

import json
import subprocess
from uuid import uuid4

import pytest

from sidegraph import server
from sidegraph.portal.contract import ErrorBody, OperationResult, RecordCard
from sidegraph.schema import AnchorBinding, Descriptor, Entity, Provenance
from sidegraph.store_layout import DRIFT_CACHE_KEY
from tests.store_record_support import _decision, _fact


def _bind(store, record_id, name="original"):
    entity = store.upsert_entity(
        Entity(canonical_name=name, descriptor=Descriptor(name=name, file_path=f"{name}.py"))
    )
    store.add_binding(
        AnchorBinding(
            record_id=record_id, entity_id=entity.entity_id, tier=2, weight=0.5, status="orphaned"
        )
    )


def _bindings(store, record_id):
    return [
        binding.model_dump(exclude={"record_id"})
        for binding in store.bindings_for_record(record_id)
    ]


def _canonical(store_path):
    return {path.relative_to(store_path): path.read_bytes() for path in store_path.glob("*/*.json")}


def _post(client, record_id, body, *, route="edit", status=200):
    response = client.post(f"/api/records/{record_id}/{route}", json=body)
    assert response.status_code == status, response.text
    if status == 200:
        return OperationResult.model_validate(response.json())
    return ErrorBody.model_validate(response.json()).error


def _edit_body(fields=None, carry=()):
    return {
        "op_key": str(uuid4()),
        "fields": fields if fields is not None else {"title": "Edited"},
        "carry_facts": list(carry),
    }


def test_edit_successor_fields(portal_ctx, portal_client, monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "user.name")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "Portal Author")
    ctx = portal_ctx()
    with ctx.request_store() as store:
        old = store.add_decision(_decision())
        _bind(store, old.id)
    result = _post(portal_client(ctx), old.id, _edit_body({"title": "New title", "kind": "lesson"}))
    assert result.state == "done"
    with ctx.request_store() as store:
        new = store.get_decision(result.new_record_id)
        assert new.status == "accepted" and new.supersedes == old.id
        assert new.title == "New title" and new.kind == "lesson"
        assert new.provenance.author == "Portal Author" and new.provenance.source == "human"
        assert new.ratified_by is None and new.ratified_at is None
        assert store.get_decision(old.id).status == "superseded"
        assert _bindings(store, new.id) == _bindings(store, old.id)


def test_edit_keeps_layer_scope(portal_ctx, portal_client):
    ctx = portal_ctx()
    with ctx.request_store() as store:
        old = store.add_decision(_decision(layer="technical", scope="module"))
    result = _post(portal_client(ctx), old.id, _edit_body())
    with ctx.request_store() as store:
        new = store.get_decision(result.new_record_id)
        assert (new.layer, new.scope) == (old.layer, old.scope)


def test_edit_fact(portal_ctx, portal_client):
    ctx = portal_ctx()
    with ctx.request_store() as store:
        supporter = store.add_decision(_decision())
        old = store.add_fact(_fact(supports=[supporter.id]))
        _bind(store, old.id)
    result = _post(portal_client(ctx), old.id, _edit_body({"statement": "New", "source": "Run"}))
    with ctx.request_store() as store:
        new = store.get_fact(result.new_record_id)
        assert new.statement == "New" and new.source == "Run"
        assert new.supports == old.supports and new.supersedes == old.id
        assert _bindings(store, new.id) == _bindings(store, old.id)
        assert store.get_fact(old.id).status == "superseded"


def test_edit_empty_fields_refused(portal_ctx, portal_client):
    ctx = portal_ctx()
    with ctx.request_store() as store:
        old = store.add_decision(_decision())
    error = _post(portal_client(ctx), old.id, _edit_body({}), status=409)
    assert error.code == "refused" and error.details == {"reason": "no_change"}
    assert error.message == (
        "Nothing changed. To confirm that this record still holds, record a drift check instead."
    )


def test_no_change_edit_does_not_require_a_write_reservation(portal_ctx, portal_client):
    ctx = portal_ctx()
    with ctx.request_store() as store:
        old = store.add_decision(_decision())
        with store._mutation():
            error = _post(portal_client(ctx), old.id, _edit_body({}), status=409)
    assert error.code == "refused" and error.details == {"reason": "no_change"}


@pytest.mark.parametrize("fact", [False, True], ids=["decision", "fact"])
def test_edit_equal_fields_refused(portal_ctx, portal_client, fact):
    ctx = portal_ctx()
    with ctx.request_store() as store:
        old = store.add_decision(_decision(layer="technical"))
        fields = {"title": old.title, "layer": "technical", "rejected": None}
        if fact:
            old = store.add_fact(_fact(supports=[old.id]))
            fields = {"statement": old.statement, "source": old.source}
    error = _post(portal_client(ctx), old.id, _edit_body(fields), status=409)
    assert error.code == "refused" and error.details == {"reason": "no_change"}


def test_edit_no_change_leaves_canonical_files_and_operations_unchanged(portal_ctx, portal_client):
    ctx = portal_ctx()
    with ctx.request_store() as store:
        old = store.add_decision(_decision())
        _bind(store, old.id)
        operations = store._conn.execute("SELECT * FROM portal_operations").fetchall()
    before = _canonical(ctx.store_path)

    body = _edit_body({})
    response = portal_client(ctx).post(f"/api/records/{old.id}/edit", json=body)

    with ctx.request_store() as store:
        after_operations = store._conn.execute("SELECT * FROM portal_operations").fetchall()
    assert (_canonical(ctx.store_path), after_operations) == (before, operations)
    assert response.status_code == 409


def test_edit_carrying_all_evidence_without_field_changes_is_refused(portal_ctx, portal_client):
    ctx = portal_ctx()
    with ctx.request_store() as store:
        old = store.add_decision(_decision())
        first = store.add_fact(_fact(supports=[old.id]))
        second = store.add_fact(_fact(supports=[old.id]))
    body = _edit_body({}, carry=[second.id, first.id, second.id])
    error = _post(portal_client(ctx), old.id, body, status=409)
    assert error.code == "refused" and error.details == {"reason": "no_change"}


def test_edit_omitting_all_evidence_without_field_changes_succeeds(portal_ctx, portal_client):
    ctx = portal_ctx()
    with ctx.request_store() as store:
        old = store.add_decision(_decision())
        fact = store.add_fact(_fact(supports=[old.id]))
    result = _post(portal_client(ctx), old.id, _edit_body({}))
    assert result.state == "done"
    with ctx.request_store() as store:
        assert store.facts_for_decision(result.new_record_id) == []
        assert store.get_fact(fact.id) == fact


@pytest.mark.parametrize("fields", [None, {}], ids=["changed", "unchanged"])
def test_edit_pending_evidence_refused(portal_ctx, portal_client, fields):
    ctx = portal_ctx()
    with ctx.request_store() as store:
        old = store.add_decision(_decision())
        pending = store.add_fact(_fact(status="proposed", supports=[old.id]))
    before = _canonical(ctx.store_path)
    error = _post(portal_client(ctx), old.id, _edit_body(fields), status=409)
    assert error.code == "pending_evidence" and error.details == {
        "ids": [pending.id],
        "queue": True,
    }
    assert _canonical(ctx.store_path) == before


def test_edit_carried_fact_with_own_pending_successor_refused_before_any_write(
    portal_ctx, portal_client
):
    ctx = portal_ctx()
    with ctx.request_store() as store:
        old = store.add_decision(_decision())
        other = store.add_decision(_decision())
        evidence = store.add_fact(_fact(supports=[old.id]))
        pending = store.add_fact(
            _fact(status="proposed", supports=[other.id], supersedes=evidence.id)
        )
    before = _canonical(ctx.store_path)
    error = _post(portal_client(ctx), old.id, _edit_body(carry=[evidence.id]), status=409)
    assert error.code == "evidence_conflict"
    assert error.details == {"id": evidence.id, "pending": pending.id}
    assert _canonical(ctx.store_path) == before


def test_edit_carries_accepted_evidence(portal_ctx, portal_client):
    ctx = portal_ctx()
    with ctx.request_store() as store:
        old = store.add_decision(_decision())
        other = store.add_decision(_decision())
        fact = store.add_fact(_fact(supports=[old.id, other.id]))
        _bind(store, fact.id)
        left = store.add_fact(_fact(supports=[old.id]))
    result = _post(portal_client(ctx), old.id, _edit_body({}, carry=[fact.id]))
    with ctx.request_store() as store:
        new = store.facts_for_decision(result.new_record_id)[0]
        assert new.supersedes == fact.id and new.supports == [result.new_record_id, other.id]
        assert store.get_fact(left.id).status == "accepted"
        assert _bindings(store, new.id) == _bindings(store, fact.id)


def test_edit_carried_fact_failure_then_retry(portal_ctx, portal_client, monkeypatch):
    ctx = portal_ctx()
    with ctx.request_store() as store:
        old = store.add_decision(_decision())
        fact = store.add_fact(_fact(supports=[old.id]))
    body = _edit_body(carry=[fact.id])
    client = portal_client(ctx)

    def fail(*args, **kwargs):
        raise RuntimeError("injected")

    with monkeypatch.context() as patch:
        patch.setattr(server, "_supersede_fact_impl", fail)
        partial = _post(client, old.id, body)
    assert partial.state == "started" and partial.new_record_id is not None
    assert any(p.outcome == "failed" and p.part.startswith("fact:") for p in partial.parts)
    result = _post(client, old.id, body)
    assert result.state == "done" and result.new_record_id == partial.new_record_id
    with ctx.request_store() as store:
        assert [d.id for d in store.iter_decisions() if d.supersedes == old.id] == [
            result.new_record_id
        ]
        assert len([f for f in store.iter_facts() if f.supersedes == fact.id]) == 1
        assert store.get_fact(fact.id).status == "superseded"


def test_edit_not_current_409_with_head(portal_ctx, portal_client):
    ctx = portal_ctx()
    with ctx.request_store() as store:
        old = store.add_decision(_decision())
        head = store.add_decision(_decision(supersedes=old.id))
    before = _canonical(ctx.store_path)
    error = _post(portal_client(ctx), old.id, _edit_body(), status=409)
    assert error.code == "not_current" and error.details == {"head": head.id}
    assert _canonical(ctx.store_path) == before


@pytest.mark.parametrize("fields", [None, {}], ids=["changed", "unchanged"])
def test_edit_pending_successor_409(portal_ctx, portal_client, fields):
    ctx = portal_ctx()
    with ctx.request_store() as store:
        old = store.add_decision(_decision())
        pending = store.add_decision(_decision(supersedes=old.id, status="proposed"))
    before = _canonical(ctx.store_path)
    error = _post(portal_client(ctx), old.id, _edit_body(fields), status=409)
    assert error.code == "pending_successor" and error.details == {"pending": pending.id}
    assert _canonical(ctx.store_path) == before


def test_edit_drifted_flag_in_card(portal_ctx, portal_client):
    ctx = portal_ctx()
    root = ctx.store_path.parent
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=Tester",
            "-c",
            "user.email=test@example.test",
            "commit",
            "-q",
            "--no-gpg-sign",
            "--allow-empty",
            "-m",
            "Current code",
        ],
        check=True,
    )
    head = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
    with ctx.request_store() as store:
        old = store.add_decision(_decision(provenance=Provenance(source="manual", commit="f" * 40)))
        store.set_meta(DRIFT_CACHE_KEY, json.dumps({"by_commit": {"f" * 40: [old.id]}}))
    client = portal_client(ctx)
    response = client.get(f"/api/records/{old.id}")
    assert RecordCard.model_validate(response.json()).drifted
    result = _post(client, old.id, _edit_body())
    response = client.get(f"/api/records/{result.new_record_id}")
    card = RecordCard.model_validate(response.json())
    assert card.record.provenance.commit == head
    assert not card.drifted


def test_edit_round_trips_unicode_exactly(portal_ctx, portal_client):
    ctx = portal_ctx()
    with ctx.request_store() as store:
        old = store.add_decision(_decision())
    fields = {"title": "Изменение", "choice": "🐍 " + "д" * 20_000, "rejected": "💡 другой"}
    result = _post(portal_client(ctx), old.id, _edit_body(fields))
    with ctx.request_store() as store:
        new = store.get_decision(result.new_record_id)
        assert new.model_dump(include=set(fields)) == fields


def test_double_submit_same_op_key_writes_once(portal_ctx, portal_client):
    ctx = portal_ctx()
    with ctx.request_store() as store:
        old = store.add_decision(_decision())
    client = portal_client(ctx)
    body = _edit_body()
    first = _post(client, old.id, body)
    second = _post(client, old.id, body)
    assert first.new_record_id == second.new_record_id and second.state == "done"
    with ctx.request_store() as store:
        assert [d.id for d in store.iter_decisions() if d.supersedes == old.id] == [
            first.new_record_id
        ]


def test_edit_null_clears_optional_fields(portal_ctx, portal_client):
    ctx = portal_ctx()
    with ctx.request_store() as store:
        old = store.add_decision(_decision(layer="technical", rejected="old", consequences="old"))
    fields = dict.fromkeys(("layer", "rejected", "consequences"))
    result = _post(portal_client(ctx), old.id, _edit_body(fields))
    with ctx.request_store() as store:
        assert store.get_decision(result.new_record_id).model_dump(include=set(fields)) == fields


def test_edit_retry_refuses_to_carry_evidence_onto_a_closed_version(
    portal_ctx, portal_client, monkeypatch
):
    ctx = portal_ctx()
    with ctx.request_store() as store:
        old = store.add_decision(_decision())
        other = store.add_decision(_decision("Other supporter"))
        fact = store.add_fact(_fact(supports=[old.id, other.id]))
    body = _edit_body(carry=[fact.id])
    client = portal_client(ctx)

    def fail(*args, **kwargs):
        raise RuntimeError("carry did not start")

    with monkeypatch.context() as patch:
        patch.setattr(server, "_supersede_fact_impl", fail)
        partial = _post(client, old.id, body)
    with ctx.request_store() as store:
        head = store.add_decision(_decision(supersedes=partial.new_record_id))
    before = _canonical(ctx.store_path)
    error = _post(client, old.id, body, status=409)
    assert error.code == "not_current" and error.details["head"] == head.id
    assert _canonical(ctx.store_path) == before


def test_concurrent_edit_same_key_replays_success(portal_ctx, portal_client, monkeypatch):
    from sidegraph.store import Store

    ctx = portal_ctx()
    with ctx.request_store() as store:
        old = store.add_decision(_decision())
    client = portal_client(ctx)
    body = _edit_body()
    get_operation = Store.get_operation
    raced = None

    def interleave(store, key):
        nonlocal raced
        row = get_operation(store, key)
        if row is None and raced is None:
            raced = False
            raced = _post(client, old.id, body)
        return row

    monkeypatch.setattr(Store, "get_operation", interleave)
    result = _post(client, old.id, body)
    assert result.new_record_id == raced.new_record_id


def test_no_change_edit_refused_after_concurrent_ratification(
    portal_ctx, portal_client, monkeypatch
):
    from sidegraph.portal import writes

    ctx = portal_ctx()
    with ctx.request_store() as store:
        old = store.add_decision(_decision(status="proposed"))
    reservation = writes._reservation
    before = None

    def ratify_before_reservation(store):
        nonlocal before
        if before is None:
            with ctx.request_store() as other:
                other.ratify(old.id)
            before = _canonical(ctx.store_path)
        return reservation(store)

    monkeypatch.setattr(writes, "_reservation", ratify_before_reservation)
    body = _edit_body({})
    error = _post(portal_client(ctx), old.id, body, status=409)
    assert error.code == "refused" and error.details == {"reason": "no_change"}
    assert _canonical(ctx.store_path) == before
    with ctx.request_store() as store:
        assert store.get_operation(body["op_key"]) is None


def test_edit_preflight_reads_record_and_evidence_together(portal_ctx, portal_client, monkeypatch):
    from sidegraph import store as store_module
    from sidegraph.portal import writes

    # Let the second connection commit while the first holds a read snapshot.
    monkeypatch.setattr(store_module, "INDEX_JOURNAL_PRAGMA", "PRAGMA journal_mode=WAL")
    ctx = portal_ctx()
    with ctx.request_store() as store:
        old = store.add_decision(_decision())
        fact = store.add_fact(_fact(supports=[old.id]))
    client = portal_client(ctx)
    get_record = writes._get_record
    interleaved = False
    successor = None

    def publish_after_read(store, record_id):
        nonlocal interleaved, successor
        record = get_record(store, record_id)
        if record_id == old.id and not interleaved:
            interleaved = True
            successor = _post(client, old.id, _edit_body(carry=[fact.id])).new_record_id
        return record

    monkeypatch.setattr(writes, "_get_record", publish_after_read)
    body = _edit_body({})
    error = _post(client, old.id, body, status=409)
    assert successor is not None
    assert error.code == "not_current" and error.details == {"head": successor}
    with ctx.request_store() as store:
        assert store.get_operation(body["op_key"]) is None


def test_no_change_edit_refuses_retry_after_evidence_moved(portal_ctx, portal_client, monkeypatch):
    ctx = portal_ctx()
    with ctx.request_store() as store:
        old = store.add_decision(_decision())
        other = store.add_decision(_decision("Other supporter"))
        fact = store.add_fact(_fact(supports=[old.id]))
    client = portal_client(ctx)
    body = _edit_body({})

    def fail(*args, **kwargs):
        raise RuntimeError("interrupted before publication")

    with monkeypatch.context() as patch:
        patch.setattr(server, "_supersede_decision_impl", fail)
        partial = _post(client, old.id, body)
    assert partial.state == "started" and partial.new_record_id is None
    with ctx.request_store() as store:
        store.add_fact(_fact(supports=[other.id], supersedes=fact.id))
        operation = store.get_operation(body["op_key"])
    before = _canonical(ctx.store_path)

    error = _post(client, old.id, body, status=409)
    assert error.code == "refused" and error.details == {"reason": "no_change"}
    assert _canonical(ctx.store_path) == before
    with ctx.request_store() as store:
        assert store.get_operation(body["op_key"]) == operation


def test_edit_retry_does_not_certify_competing_successors(portal_ctx, portal_client, monkeypatch):
    from sidegraph.store import Store

    ctx = portal_ctx()
    with ctx.request_store() as store:
        prior = store.add_decision(_decision())
    body = _edit_body()
    client = portal_client(ctx)
    write = Store._write_decision

    def stop_before_close(store, decision):
        if decision.id == prior.id:
            raise RuntimeError("close did not start")
        return write(store, decision)

    with monkeypatch.context() as patch:
        patch.setattr(Store, "_write_decision", stop_before_close)
        partial = _post(client, prior.id, body)
    assert partial.state == "started" and partial.new_record_id
    with ctx.request_store() as store:
        sibling = store.add_decision(_decision(supersedes=prior.id))
        competitor = store.add_decision(_decision(supersedes=sibling.id))
    before = _canonical(ctx.store_path)
    result = _post(client, prior.id, body, status=409)
    assert result.code == "not_current" and result.details["head"] == competitor.id
    assert _canonical(ctx.store_path) == before


def test_edit_refuses_unavailable_source_bindings(portal_ctx, portal_client):
    ctx = portal_ctx()
    with ctx.request_store() as store:
        old = store.add_decision(_decision())
        _bind(store, old.id)
    path = ctx.store_path / "bindings" / f"{old.id}.json"
    path.write_text("invalid JSON\n")
    before = _canonical(ctx.store_path)
    error = _post(portal_client(ctx), old.id, _edit_body(), status=409)
    assert error.code == "refused" and error.details["reason"] == "bindings"
    assert _canonical(ctx.store_path) == before


def test_edit_retry_requires_a_human_publication_at_the_planned_id(
    portal_ctx, portal_client, monkeypatch
):
    from tests.store_format2_support import put_file

    ctx = portal_ctx()
    with ctx.request_store() as store:
        old = store.add_decision(_decision())
        fact = store.add_fact(_fact(supports=[old.id]))
    client = portal_client(ctx)
    body = _edit_body(carry=[fact.id])

    def fail(*args, **kwargs):
        raise RuntimeError("evidence write interrupted")

    with monkeypatch.context() as patch:
        patch.setattr(server, "_supersede_fact_impl", fail)
        partial = _post(client, old.id, body)
    with ctx.request_store() as store:
        new = store.get_decision(partial.new_record_id)
        new.provenance.source = "agent"
        put_file(ctx.store_path, "decision", new.model_dump(mode="json"))
    before = _canonical(ctx.store_path)
    assert _post(client, old.id, body, status=409).code == "not_current"
    assert _canonical(ctx.store_path) == before


def test_completed_edit_rejects_changed_predecessor_and_binding_attrs(portal_ctx, portal_client):
    from tests.store_format2_support import put_file

    ctx = portal_ctx()
    with ctx.request_store() as store:
        prior = store.add_decision(_decision())
        _bind(store, prior.id)
    client = portal_client(ctx)
    body = _edit_body()
    result = _post(client, prior.id, body)
    bindings = ctx.store_path / "bindings" / f"{result.new_record_id}.json"
    original_bindings = bindings.read_bytes()
    bindings.write_bytes(original_bindings.replace(b"0.5", b"0.8"))
    assert _post(client, prior.id, body, status=409).code == "not_current"
    bindings.write_bytes(original_bindings)
    with ctx.request_store() as store:
        revision = store.get_decision(result.new_record_id)
        revision.supersedes = None
        put_file(ctx.store_path, "decision", revision.model_dump(mode="json"))
        put_file(ctx.store_path, "decision", prior.model_dump(mode="json"))
    assert _post(client, prior.id, body, status=409).code == "not_current"
