"""Portal proposal edits and durable recovery (D8)."""

import sqlite3
import subprocess
import sys
from contextlib import contextmanager
from uuid import uuid4

import pytest

from sidegraph import server
from sidegraph.portal.context import PortalContext
from sidegraph.portal.contract import RecordCard
from sidegraph.store import Store, StoreBusyError, SupersessionConflictError
from tests.portal.test_apply import _verdicts
from tests.portal.test_edit import _bind, _bindings, _canonical, _edit_body, _post
from tests.store_record_support import _decision, _fact

_ROUTE = "edit-and-accept"


def _body(fields=None, keep=()):
    return {
        "op_key": str(uuid4()),
        "fields": fields if fields is not None else {"title": "Human version"},
        "keep_facts": list(keep),
    }


def _draft(store, *, supersedes=None):
    draft = store.add_decision(_decision(status="proposed", supersedes=supersedes))
    _bind(store, draft.id, "draft")
    return draft


def _assert_decision_version(ctx, draft, result):
    assert result.state == "done"
    with ctx.request_store() as store:
        live = store.get_decision(result.new_record_id)
        assert live.status == "accepted" and live.supersedes == draft.supersedes
        assert live.provenance.source == "human" and live.ratified_by is None
        assert store.get_decision(draft.id).status == "rejected"
        assert _bindings(store, live.id) == _bindings(store, draft.id)
        if draft.supersedes:
            assert store.get_decision(draft.supersedes).status == "superseded"
    return live


def test_draft_supersedes_nothing(portal_ctx, portal_client):
    ctx = portal_ctx()
    with ctx.request_store() as store:
        draft = _draft(store)
    result = _post(portal_client(ctx), draft.id, _body(), route=_ROUTE)
    assert _assert_decision_version(ctx, draft, result).title == "Human version"


def test_draft_supersedes_x(portal_ctx, portal_client):
    ctx = portal_ctx()
    with ctx.request_store() as store:
        original = store.add_decision(_decision(layer="business", scope="global"))
        _bind(store, original.id, "original")
        draft = store.add_decision(
            _decision(status="proposed", supersedes=original.id, layer="technical", scope="module")
        )
        _bind(store, draft.id, "draft")
    result = _post(portal_client(ctx), draft.id, _body({"layer": None}), route=_ROUTE)
    live = _assert_decision_version(ctx, draft, result)
    assert live.layer is None and live.scope == draft.scope


@pytest.mark.parametrize(
    "point",
    [
        "before_publish",
        "after_drop_before_cascade",
        "after_version_before_bindings",
        "after_version_before_close",
        "after_version_before_fact_close",
        "after_version_before_index",
        "after_version_before_kept_fact",
        "lost_response",
    ],
)
def test_failure_points_then_retry(portal_ctx, portal_client, monkeypatch, point):
    ctx = portal_ctx()
    with ctx.request_store() as store:
        prior = store.add_decision(_decision("Original"))
        draft = _draft(store, supersedes=prior.id)
        prior_fact = store.add_fact(_fact("Old fact", supports=[prior.id]))
        kept = store.add_fact(
            _fact("Keep", status="proposed", supports=[draft.id], supersedes=prior_fact.id)
        )
        _bind(store, kept.id, "kept")
        unkept = store.add_fact(_fact("Leave", status="proposed", supports=[draft.id]))
    body = _body(keep=[kept.id])
    client = portal_client(ctx)
    write_decision = Store._write_decision
    index_decision = Store._index_write_decision
    write_fact = Store._write_fact
    copy = server._copy_bindings
    supersede_fact = server._supersede_fact_impl

    def fail():
        raise RuntimeError("injected failure")

    def patch_decision(store, decision):
        if point == "before_publish" and decision.id == draft.id:
            fail()
        if point == "after_version_before_close" and decision.id == prior.id:
            fail()
        return write_decision(store, decision)

    index_failed = False

    def patch_index(store, decision):
        nonlocal index_failed
        if (
            not index_failed
            and point == "after_version_before_index"
            and decision.id not in {draft.id, prior.id}
        ):
            index_failed = True
            fail()
        return index_decision(store, decision)

    def patch_fact(store, fact):
        if point == "after_drop_before_cascade" and fact.status == "rejected":
            fail()
        if point == "after_version_before_fact_close" and fact.id == prior_fact.id:
            fail()
        return write_fact(store, fact)

    def patch_copy(store, source_id, target_id):
        if point == "after_version_before_bindings" and source_id == draft.id:
            fail()
        return copy(store, source_id, target_id)

    def patch_kept(*args, **kwargs):
        if point == "after_version_before_kept_fact":
            fail()
        return supersede_fact(*args, **kwargs)

    before = _canonical(ctx.store_path)
    with monkeypatch.context() as patch:
        patch.setattr(Store, "_write_decision", patch_decision)
        patch.setattr(Store, "_index_write_decision", patch_index)
        patch.setattr(Store, "_write_fact", patch_fact)
        patch.setattr(server, "_copy_bindings", patch_copy)
        patch.setattr(server, "_supersede_fact_impl", patch_kept)
        first = _post(client, draft.id, body, route=_ROUTE)
        if point != "lost_response":
            assert first.state == "started"
            parts = {part.part: part.outcome for part in first.parts}
            if point == "before_publish":
                assert parts["drop_draft"] == "failed"
                assert parts["version"] == "skipped"
            elif point == "after_drop_before_cascade":
                assert parts["drop_draft"] == "done_now"
                assert parts["cascade"] == "failed"
            else:
                assert first.new_record_id is not None
                assert parts["version"] == "done_now"
                if point == "after_version_before_bindings":
                    assert parts["bindings"] == "failed"
                elif point in {"after_version_before_close", "after_version_before_index"}:
                    assert parts["close_predecessor"] == "failed"
                else:
                    assert any(
                        key.startswith("fact:") and outcome == "failed"
                        for key, outcome in parts.items()
                    )
        if point == "before_publish":
            assert _canonical(ctx.store_path) == before
    del first
    result = _post(client, draft.id, body, route=_ROUTE)
    live = _assert_decision_version(ctx, draft, result)
    with ctx.request_store() as store:
        assert [
            d.id
            for d in store.iter_decisions()
            if d.supersedes == prior.id and d.status == "accepted"
        ] == [live.id]
        (kept_new,) = store.facts_for_decision(live.id)
        assert kept_new.statement == kept.statement and kept_new.supersedes == prior_fact.id
        assert store.get_fact(prior_fact.id).status == "superseded"
        assert store.get_fact(kept.id).status == store.get_fact(unkept.id).status == "rejected"
        assert _bindings(store, kept_new.id) == _bindings(store, kept.id)


def test_hard_kill_inside_scope_not_current_then_save_as_new(portal_ctx, portal_client):
    ctx = portal_ctx()
    with ctx.request_store() as store:
        draft = _draft(store)
        store.add_fact(_fact(status="proposed", supports=[draft.id]))
    body = _body()
    killed = subprocess.run(
        [
            sys.executable,
            "-c",
            """
import os
import sys
from pathlib import Path
from sidegraph.portal.writes import edit_and_accept
from sidegraph.store import Store

def killed(*args, **kwargs):
    os._exit(81)

Store._write_fact = killed
with Store(Path(sys.argv[1])) as store:
    edit_and_accept(store, None, op_key=sys.argv[2], draft_id=sys.argv[3],
                    fields={"title": "Human version"}, keep_facts=[], author=None)
""",
            str(ctx.store_path),
            body["op_key"],
            draft.id,
        ],
        timeout=30,
        capture_output=True,
    )
    assert killed.returncode == 81, killed.stderr.decode()
    client = portal_client(ctx)
    error = _post(client, draft.id, body, route=_ROUTE, status=409)
    assert error.code == "not_current" and error.details["offer_save_as_new"] is True
    with ctx.request_store() as store:
        assert store.get_operation(body["op_key"])["state"] == "planned"
    recovery = {"op_key": str(uuid4()), "fields": body["fields"]}
    result = _post(client, draft.id, recovery, route="save-as-new")
    _assert_decision_version(ctx, draft, result)
    assert (
        _post(client, draft.id, recovery, route="save-as-new").new_record_id == result.new_record_id
    )
    with ctx.request_store() as store:
        row = store.get_operation(recovery["op_key"])
        assert row["draft_id"] is None and row["record_id"] == draft.id
        assert len(list(store.iter_decisions())) == 2


def _busy_after_planning(patch, op_key):
    mutation = Store._mutation
    refused = False

    @contextmanager
    def busy(store, **kwargs):
        nonlocal refused
        if not refused and store.get_operation(op_key) is not None:
            refused = True
            raise StoreBusyError("injected reservation refusal")
        with mutation(store, **kwargs):
            yield

    patch.setattr(Store, "_mutation", busy)


@pytest.mark.parametrize("verdict", ["ratify", "drop"])
@pytest.mark.parametrize("was_busy", [False, True])
def test_foreign_verdict_between_plan_and_reservation(
    portal_ctx, portal_client, monkeypatch, verdict, was_busy
):
    from sidegraph.portal import writes

    ctx = portal_ctx()
    with ctx.request_store() as store:
        draft = _draft(store)
    body = _body()
    client = portal_client(ctx)
    reservation = writes._reservation
    raced = False
    after_foreign = {}

    def settle():
        with ctx.request_store() as other:
            getattr(other, verdict)(draft.id)
            after_foreign.update(_canonical(ctx.store_path))

    @contextmanager
    def interleave(store):
        nonlocal raced
        with reservation(store):
            yield
        if not raced:
            raced = True
            settle()

    if was_busy:
        with monkeypatch.context() as patch:
            _busy_after_planning(patch, body["op_key"])
            assert _post(client, draft.id, body, route=_ROUTE, status=503).code == "busy"
        settle()
    else:
        monkeypatch.setattr(writes, "_reservation", interleave)
    error = _post(client, draft.id, body, route=_ROUTE, status=409)
    assert error.code == "not_current"
    assert error.details.get("offer_save_as_new", False) == (verdict == "drop")
    assert _canonical(ctx.store_path) == after_foreign
    with ctx.request_store() as store:
        assert store.get_operation(body["op_key"])["state"] == "planned"


@pytest.mark.parametrize("verdict", ["drop", "unindexed_accept"])
def test_foreign_verdict_found_inside_scope_leaves_row_planned(
    portal_ctx, portal_client, monkeypatch, verdict
):
    ctx = portal_ctx()
    with ctx.request_store() as store:
        draft = _draft(store)
    body = _body()
    mutation = Store._mutation
    raced = False

    @contextmanager
    def settle_before_reservation(store, **kwargs):
        nonlocal raced
        if not raced and store.get_operation(body["op_key"]) is not None:
            raced = True
            with ctx.request_store() as other:
                if verdict == "drop":
                    other.drop(draft.id)
                else:
                    with pytest.raises(RuntimeError, match="foreign writer"), other._mutation():
                        other.ratify(draft.id)
                        raise RuntimeError("foreign writer failed after canonical publication")
        with mutation(store, **kwargs):
            yield

    monkeypatch.setattr(Store, "_mutation", settle_before_reservation)
    client = portal_client(ctx)
    error = _post(client, draft.id, body, route=_ROUTE, status=409)
    assert error.code == "not_current" and raced
    with ctx.request_store() as store:
        assert store.get_operation(body["op_key"])["state"] == "planned"
        assert len(list(store.iter_decisions())) == 1
        assert store.get_decision(draft.id).status == (
            "rejected" if verdict == "drop" else "accepted"
        )


def test_second_operation_refused_while_first_started(portal_ctx, portal_client, monkeypatch):
    ctx = portal_ctx()
    with ctx.request_store() as store:
        draft = _draft(store)
        store.add_fact(_fact(status="proposed", supports=[draft.id]))
    first, second = _body(), _body({"title": "Different text"})
    client = portal_client(ctx)
    refresh = Store.refresh_if_stale
    mutation = Store._mutation
    observed = []
    started_write = False

    def fail_after_drop(store, fact):
        nonlocal started_write
        started_write = True
        raise RuntimeError("injected after the canonical draft rejection")

    @contextmanager
    def probe_boundary(store, **kwargs):
        try:
            with mutation(store, **kwargs):
                yield
        finally:
            if started_write and not observed and store._mutation_depth == 0:
                with Store(ctx.store_path) as contender:
                    old = contender.get_operation(first["op_key"])
                    observed.append(old)
                    with pytest.raises(sqlite3.IntegrityError):
                        contender.put_operation(
                            second["op_key"], "other", draft.id, None, [draft.id]
                        )

    def no_extra_refresh(store):
        # The direct probe runs before the request's post-failure reconciliation.
        assert not started_write or observed
        refresh(store)

    with monkeypatch.context() as patch:
        patch.setattr(Store, "_mutation", probe_boundary)
        patch.setattr(Store, "_write_fact", fail_after_drop)
        patch.setattr(Store, "refresh_if_stale", no_extra_refresh)
        partial = _post(client, draft.id, first, route=_ROUTE)
    assert partial.state == "started" and observed[0]["state"] == "started"
    error = _post(client, draft.id, second, route=_ROUTE, status=409)
    assert error.code == "operation_in_progress"
    result = _post(client, draft.id, first, route=_ROUTE)
    _assert_decision_version(ctx, draft, result)
    assert result.new_record_id == observed[0]["planned"][0]


def test_new_operation_replaces_planned_row_after_busy_refusal(
    portal_ctx, portal_client, monkeypatch
):
    ctx = portal_ctx()
    with ctx.request_store() as store:
        draft = _draft(store)
    client = portal_client(ctx)
    first, second = _body(), _body({"title": "Corrected text"})
    with monkeypatch.context() as patch:
        _busy_after_planning(patch, first["op_key"])
        assert _post(client, draft.id, first, route=_ROUTE, status=503).code == "busy"
    result = _post(client, draft.id, second, route=_ROUTE)
    assert _assert_decision_version(ctx, draft, result).title == "Corrected text"
    with ctx.request_store() as store:
        assert store.get_operation(first["op_key"]) is None


def test_replaced_operation_stops_inside_reservation(portal_ctx, portal_client, monkeypatch):
    ctx = portal_ctx()
    with ctx.request_store() as store:
        draft = _draft(store)
    first, second = _body(), _body({"title": "Corrected text"})
    client = portal_client(ctx)
    mutation = Store._mutation
    replaced = False

    @contextmanager
    def replace_before_reservation(store, **kwargs):
        nonlocal replaced
        if not replaced and store.get_operation(first["op_key"]) is not None:
            replaced = True
            result = _post(client, draft.id, second, route=_ROUTE)
            assert result.state == "done"
        with mutation(store, **kwargs):
            yield

    monkeypatch.setattr(Store, "_mutation", replace_before_reservation)
    assert _post(client, draft.id, first, route=_ROUTE, status=409).code == "operation_in_progress"
    result = _post(client, draft.id, second, route=_ROUTE)
    _assert_decision_version(ctx, draft, result)


def test_op_key_reuse_with_other_digest_422(portal_ctx, portal_client):
    ctx = portal_ctx()
    with ctx.request_store() as store:
        draft = _draft(store)
    first = _body()
    client = portal_client(ctx)
    result = _post(client, draft.id, first, route=_ROUTE)
    changed = dict(first, fields={"title": "Another version"})
    assert _post(client, draft.id, changed, route=_ROUTE, status=422).code == "op_key_reused"
    _assert_decision_version(ctx, draft, result)


def test_worktree_writes_get_no_reader(portal_ctx, portal_client, monkeypatch, tmp_path):
    root = tmp_path / "root"
    root.mkdir()
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
            "--allow-empty",
            "-m",
            "Test",
        ],
        check=True,
    )
    worktree = tmp_path / "linked"
    subprocess.run(
        ["git", "-C", str(root), "worktree", "add", "-q", "--detach", str(worktree)],
        check=True,
    )
    graph = root / "graphify-out" / "graph.json"
    graph.parent.mkdir()
    graph.write_text('{"nodes":[],"links":[]}\n')
    ctx = portal_ctx(store_path=worktree / ".sidegraph", graph_path=graph, borrowed_from=root)
    with ctx.request_store() as store:
        accepted = store.add_decision(_decision("Accepted"))
        draft = _draft(store)
        rejected = store.add_decision(_decision("Rejected", status="rejected"))

    def unexpected_reader(self):
        pytest.fail("A Portal write must not request a borrowed reader")

    monkeypatch.setattr(PortalContext, "reader", unexpected_reader)
    client = portal_client(ctx)
    _post(client, accepted.id, _edit_body())
    result = _post(client, draft.id, _body(), route=_ROUTE)
    _assert_decision_version(ctx, draft, result)
    _post(client, rejected.id, {"op_key": str(uuid4()), "fields": {}}, route="save-as-new")
    from tests.portal.test_apply import _verdicts

    _verdicts(client, reject=[rejected.id])


def test_kept_evidence_reissued_unkept_rejected_shared_untouched(portal_ctx, portal_client):
    ctx = portal_ctx()
    with ctx.request_store() as store:
        other = store.add_decision(_decision("Other"))
        draft = _draft(store)
        old = store.add_fact(_fact("Original fact", supports=[other.id]))
        kept = store.add_fact(
            _fact("Kept", supports=[draft.id], status="proposed", supersedes=old.id)
        )
        _bind(store, kept.id, "evidence")
        unkept = store.add_fact(_fact("Unkept", supports=[draft.id], status="proposed"))
        shared = store.add_fact(_fact("Shared", supports=[draft.id, other.id], status="proposed"))
    result = _post(portal_client(ctx), draft.id, _body(keep=[kept.id]), route=_ROUTE)
    live = _assert_decision_version(ctx, draft, result)
    with ctx.request_store() as store:
        (new,) = store.facts_for_decision(live.id)
        assert new.supersedes == old.id and new.supports == [live.id] and new.status == "accepted"
        assert _bindings(store, new.id) == _bindings(store, kept.id)
        assert store.get_fact(old.id).status == "superseded"
        assert store.get_fact(kept.id).status == store.get_fact(unkept.id).status == "rejected"
        assert store.get_fact(shared.id) == shared


def test_card_evidence_becomes_keepable_after_other_supporter_is_rejected(
    portal_ctx, portal_client
) -> None:
    ctx = portal_ctx()
    with ctx.request_store() as store:
        first = store.add_decision(_decision("First", status="proposed"))
        draft = _draft(store)
        shared = store.add_fact(_fact("Shared", supports=[first.id, draft.id], status="proposed"))
    client = portal_client(ctx)

    path = f"/api/records/{draft.id}"
    before = client.get(path)
    assert before.status_code == 200, before.text
    assert _verdicts(client, reject=[first.id])[first.id].outcome == "rejected"
    after = client.get(path)
    assert after.status_code == 200, after.text
    card = RecordCard.model_validate(after.json())
    assert card.keepable_evidence == [shared.id]
    assert [fact.id for fact in card.evidence] == [shared.id]
    assert before.json()["keepable_evidence"] == []
    assert client.get(f"/api/records/{first.id}").json()["keepable_evidence"] == []

    result = _post(client, draft.id, _body(keep=card.keepable_evidence), route=_ROUTE)
    live = _assert_decision_version(ctx, draft, result)
    with ctx.request_store() as store:
        (kept,) = store.facts_for_decision(live.id)
        assert kept.status == "accepted" and kept.statement == shared.statement
        assert kept.supports == [live.id]


@pytest.mark.parametrize("case", ["fresh", "superseding", "orphaned", "supported"])
def test_standalone_fact_cases(portal_ctx, portal_client, case):
    ctx = portal_ctx()
    with ctx.request_store() as store:
        old = store.add_fact(_fact("Older")) if case == "superseding" else None
        supported = store.add_decision(_decision()) if case == "supported" else None
        draft = store.add_fact(
            _fact(
                "Draft",
                status="proposed",
                supersedes=old.id if old else None,
                supports=[supported.id] if supported else [],
            )
        )
        if not supported:
            _bind(store, draft.id)
    result = _post(portal_client(ctx), draft.id, _body({"statement": "Corrected"}), route=_ROUTE)
    with ctx.request_store() as store:
        new = store.get_fact(result.new_record_id)
        assert new.status == "accepted" and new.statement == "Corrected"
        assert new.supports == draft.supports and new.supersedes == draft.supersedes
        assert store.get_fact(draft.id).status == "rejected"
        assert _bindings(store, new.id) == _bindings(store, draft.id)
        if old:
            assert store.get_fact(old.id).status == "superseded"
        if case == "orphaned":
            assert _bindings(store, new.id)[0]["status"] == "orphaned"


def test_busy_at_reservation_refuses_before_any_write(portal_ctx, portal_client, monkeypatch):
    ctx = portal_ctx()
    with ctx.request_store() as store:
        draft = _draft(store)
    body = _body()
    before = _canonical(ctx.store_path)
    with monkeypatch.context() as patch:
        _busy_after_planning(patch, body["op_key"])
        assert _post(portal_client(ctx), draft.id, body, route=_ROUTE, status=503).code == "busy"
    assert _canonical(ctx.store_path) == before
    with ctx.request_store() as store:
        assert store.get_decision(draft.id).status == "proposed"
        assert store.get_operation(body["op_key"])["state"] == "planned"


def test_retry_waiting_on_failed_write_reads_canonical_draft(portal_ctx, monkeypatch):
    from sidegraph.portal.writes import edit_and_accept

    ctx = portal_ctx()
    with ctx.request_store() as retrying, ctx.request_store() as failing:
        draft = _draft(retrying)
        retrying.add_fact(_fact(status="proposed", supports=[draft.id]))
        args = dict(
            op_key=str(uuid4()),
            draft_id=draft.id,
            fields={"title": "Corrected"},
            keep_facts=[],
            author=None,
        )
        failing.refresh_if_stale()
        mutation = Store._mutation
        refresh = Store.refresh_if_stale
        write_decision = Store._write_decision
        write_fact = Store._write_fact
        interleaved = False
        fails_after_drop = False
        draft_writes = []

        def count_drop(store, decision):
            if decision.id == draft.id and decision.status == "rejected":
                draft_writes.append(store)
            return write_decision(store, decision)

        def fail_cascade(store, fact):
            nonlocal fails_after_drop
            if store is failing:
                fails_after_drop = True
                raise RuntimeError("cascade was interrupted")
            return write_fact(store, fact)

        def deferred_refresh(store):
            if store is not failing or not fails_after_drop:
                refresh(store)

        @contextmanager
        def interleave(store, **kwargs):
            nonlocal interleaved
            if store is retrying and not interleaved and store.get_operation(args["op_key"]):
                interleaved = True
                partial = edit_and_accept(failing, None, **args)
                assert partial.state == "started"
            with mutation(store, **kwargs):
                yield

        monkeypatch.setattr(Store, "_mutation", interleave)
        monkeypatch.setattr(Store, "_write_decision", count_drop)
        monkeypatch.setattr(Store, "_write_fact", fail_cascade)
        monkeypatch.setattr(Store, "refresh_if_stale", deferred_refresh)
        result = edit_and_accept(retrying, None, **args)
        assert result.state == "done"
        assert draft_writes == [failing]
    _assert_decision_version(ctx, draft, result)


@pytest.mark.parametrize("point", ["transaction_rollback", "commit"])
def test_failure_that_rolls_back_reservation_preserves_started(
    portal_ctx, portal_client, monkeypatch, point
):
    ctx = portal_ctx()
    with ctx.request_store() as store:
        draft = _draft(store)
        store.add_fact(_fact(status="proposed", supports=[draft.id]))
    body = _body()
    client = portal_client(ctx)
    write_fact = Store._write_fact
    commit = Store._commit
    mutation = Store._mutation
    failed = False
    challenged = False

    def full_rollback(store, fact):
        nonlocal failed
        if point == "transaction_rollback" and fact.status == "rejected":
            failed = True
            store._conn.rollback()
            raise sqlite3.OperationalError("Simulated whole-transaction rollback")
        return write_fact(store, fact)

    def commit_failure(store):
        nonlocal failed
        operation = store.get_operation(body["op_key"])
        if point == "commit" and operation is not None and operation["state"] == "done":
            failed = True
            raise sqlite3.OperationalError("Simulated commit failure")
        commit(store)

    with ctx.request_store() as contender:

        @contextmanager
        def probe_boundary(store, **kwargs):
            nonlocal challenged
            try:
                with mutation(store, **kwargs):
                    yield
            finally:
                if failed and not challenged and store._mutation_depth == 0:
                    challenged = True
                    if point == "commit":
                        contender.refresh_if_stale()
                    assert contender.get_operation(body["op_key"])["state"] == "planned"
                    error = SupersessionConflictError if point == "commit" else StoreBusyError
                    with pytest.raises(error):
                        contender.put_operation(
                            str(uuid4()), "different", draft.id, None, [str(uuid4())]
                        )

        with monkeypatch.context() as patch:
            patch.setattr(Store, "_write_fact", full_rollback)
            patch.setattr(Store, "_commit", commit_failure)
            patch.setattr(Store, "_mutation", probe_boundary)
            partial = _post(client, draft.id, body, route=_ROUTE)
    assert challenged
    assert partial.state == "started"
    with ctx.request_store() as store:
        assert store.get_operation(body["op_key"])["state"] == "started"
        assert store.get_decision(draft.id).status == "rejected"
    error = _post(client, draft.id, _body({"title": "Other text"}), route=_ROUTE, status=409)
    assert error.code == "operation_in_progress"
    result = _post(client, draft.id, body, route=_ROUTE)
    _assert_decision_version(ctx, draft, result)


def test_keep_evidence_with_no_surviving_outside_supporter(portal_ctx, portal_client):
    ctx = portal_ctx()
    with ctx.request_store() as store:
        rejected = store.add_decision(_decision(status="rejected"))
        draft = _draft(store)
        evidence = store.add_fact(_fact(status="proposed", supports=[rejected.id, draft.id]))
    result = _post(portal_client(ctx), draft.id, _body(keep=[evidence.id]), route=_ROUTE)
    _assert_decision_version(ctx, draft, result)
    with ctx.request_store() as store:
        (new,) = store.facts_for_decision(result.new_record_id)
        assert new.status == "accepted" and new.supports == [result.new_record_id]
        assert store.get_fact(evidence.id).status == "rejected"


def test_nested_proposed_evidence_cannot_be_edited_and_accepted(portal_ctx, portal_client):
    ctx = portal_ctx()
    with ctx.request_store() as store:
        parent = _draft(store)
        evidence = store.add_fact(_fact(status="proposed", supports=[parent.id]))
    before = _canonical(ctx.store_path)
    result = _post(
        portal_client(ctx),
        evidence.id,
        _body({"statement": "Edited"}),
        route=_ROUTE,
        status=409,
    )
    assert result.code == "refused" and result.details["reason"] == "nested_evidence"
    assert _canonical(ctx.store_path) == before


def test_invalid_correction_keeps_an_existing_plan(portal_ctx, portal_client, monkeypatch):
    ctx = portal_ctx()
    with ctx.request_store() as store:
        draft = _draft(store)
    client = portal_client(ctx)
    first, invalid = _body(), _body(keep=["missing"])
    with monkeypatch.context() as patch:
        _busy_after_planning(patch, first["op_key"])
        busy = _post(client, draft.id, first, route=_ROUTE, status=503)
    assert busy.code == "busy"
    assert busy.details.get("retry_with_new_key") is True
    error = _post(client, draft.id, invalid, route=_ROUTE, status=409)
    assert error.code == "evidence_conflict"
    with ctx.request_store() as store:
        assert store.get_operation(first["op_key"])["state"] == "planned"
        assert store.get_operation(invalid["op_key"]) is None


def test_completed_retry_reports_all_parts_and_requires_version(portal_ctx, portal_client):
    ctx = portal_ctx()
    with ctx.request_store() as store:
        prior = store.add_decision(_decision("Prior"))
        draft = _draft(store, supersedes=prior.id)
    body = _body()
    client = portal_client(ctx)
    complete = _post(client, draft.id, body, route=_ROUTE)
    replay = _post(client, draft.id, body, route=_ROUTE)
    assert replay.new_record_id == complete.new_record_id
    assert [part.part for part in replay.parts] == [part.part for part in complete.parts]
    assert all(part.outcome == "done" for part in replay.parts)

    (ctx.store_path / "decisions" / f"{complete.new_record_id}.json").unlink()
    error = _post(client, draft.id, body, route=_ROUTE, status=404)
    assert error.code == "not_found"


def test_planned_publication_resumes_after_recovery_commit_could_not_run(
    portal_ctx, portal_client, monkeypatch
):
    ctx = portal_ctx()
    with ctx.request_store() as store:
        draft = _draft(store)
    body = _body()
    client = portal_client(ctx)
    commit = Store._commit

    def blocked(store):
        row = store.get_operation(body["op_key"])
        if row is not None and row["state"] in {"done", "started"}:
            error = sqlite3.OperationalError("reserved reader")
            error.sqlite_errorcode = sqlite3.SQLITE_BUSY
            raise error
        commit(store)

    with monkeypatch.context() as patch:
        patch.setattr(Store, "_commit", blocked)
        assert _post(client, draft.id, body, route=_ROUTE, status=503).code == "busy"
    with ctx.request_store() as store:
        row = store.get_operation(body["op_key"])
        assert row["state"] == "planned"
        (published,) = row["planned"]
        assert store.get_decision(published).status == "accepted"
    stale = _post(client, draft.id, _body(), route=_ROUTE, status=409)
    assert stale.code == "not_current"
    assert stale.details == {"head": published}
    result = _post(client, draft.id, body, route=_ROUTE)
    assert result.new_record_id == published
    _assert_decision_version(ctx, draft, result)


def test_preflight_rejects_over_budget_inherited_fact_supports(portal_ctx, portal_client):
    ctx = portal_ctx()
    with ctx.request_store() as store:
        parent = store.add_decision(_decision())
        draft = store.add_fact(_fact(status="proposed", supports=[parent.id] * 4096))
    before = _canonical(ctx.store_path)
    response = _post(
        portal_client(ctx), draft.id, _body({"statement": "Correction"}), route=_ROUTE, status=422
    )
    assert response.code == "invalid"
    assert _canonical(ctx.store_path) == before


def test_busy_started_retry_requires_the_same_key(portal_ctx, portal_client, monkeypatch):
    ctx = portal_ctx()
    with ctx.request_store() as store:
        draft = _draft(store)
    client = portal_client(ctx)
    body = _body()

    def fail(*args, **kwargs):
        raise RuntimeError("before first publication")

    with monkeypatch.context() as patch:
        patch.setattr(server, "_ratify_impl", fail)
        assert _post(client, draft.id, body, route=_ROUTE).state == "started"
    with monkeypatch.context() as patch:
        _busy_after_planning(patch, body["op_key"])
        busy = _post(client, draft.id, body, route=_ROUTE, status=503)
    assert busy.code == "busy" and busy.details == {"retry_with_same_key": True}
    _assert_decision_version(ctx, draft, _post(client, draft.id, body, route=_ROUTE))


def test_edit_and_accept_reports_current_head_for_stale_predecessor(portal_ctx, portal_client):
    from tests.store_format2_support import put_file

    ctx = portal_ctx()
    with ctx.request_store() as store:
        original = store.add_decision(_decision())
        middle = store.add_decision(_decision(supersedes=original.id))
        head = store.add_decision(_decision(supersedes=middle.id))
        draft = _decision(status="proposed", supersedes=original.id)
        put_file(ctx.store_path, "decision", draft.model_dump(mode="json"))
    error = _post(portal_client(ctx), draft.id, _body(), route=_ROUTE, status=409)
    assert error.code == "not_current" and error.details == {"head": head.id}
