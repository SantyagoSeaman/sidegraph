"""Apply and preview verdicts through the Portal's D6 endpoints."""

import pytest

from sidegraph import server
from sidegraph.portal.contract import ApplyResult, PreviewResult
from sidegraph.store import StoreBusyError
from tests.store_record_support import _decision, _domain, _fact


def _verdicts(client, accept=(), reject=(), *, preview=False):
    route = "preview" if preview else "apply"
    response = client.post(
        f"/api/queue/{route}", json={"accept": list(accept), "reject": list(reject)}
    )
    assert response.status_code == 200, response.text
    model = PreviewResult if preview else ApplyResult
    return model.model_validate(response.json()).outcomes


def test_apply_stamps_bare_git_name(portal_ctx, portal_client, monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "user.name")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "Portal Author")
    ctx = portal_ctx()
    with ctx.request_store() as store:
        draft = store.add_decision(_decision(status="proposed"))
    assert _verdicts(portal_client(ctx), accept=[draft.id])[draft.id].outcome == "accepted"
    with ctx.request_store() as store:
        assert store.get_decision(draft.id).ratified_by == "Portal Author"


def test_apply_ok_refused_busy_in_one_batch(portal_ctx, portal_client, monkeypatch):
    ctx = portal_ctx()
    with ctx.request_store() as store:
        first = store.add_decision(_decision(status="proposed"))
        second = store.add_decision(_decision(status="rejected"))
        third = store.add_decision(_decision(status="proposed"))
    ratify_one = server._ratify_one

    def busy(store, id_, action, **kwargs):
        if id_ == third.id:
            raise StoreBusyError("writer is busy")
        return ratify_one(store, id_, action, **kwargs)

    monkeypatch.setattr(server, "_ratify_one", busy)
    outcomes = _verdicts(portal_client(ctx), accept=[first.id, second.id, third.id])
    assert [outcomes[item.id].outcome for item in (first, second, third)] == [
        "accepted",
        "refused",
        "busy",
    ]
    assert outcomes[second.id].error.details == {"reason": "opposite_verdict"}
    with ctx.request_store() as store:
        assert store.get_decision(first.id).status == "accepted"
        assert store.get_decision(third.id).status == "proposed"


@pytest.mark.parametrize("reject_both", [False, True])
def test_preview_agrees_with_apply(portal_ctx, portal_client, reject_both):
    ctx = portal_ctx()
    with ctx.request_store() as store:
        first, second = [store.add_decision(_decision(status="proposed")) for _ in range(2)]
        exclusive = store.add_fact(_fact(status="proposed", supports=[first.id]))
        shared = store.add_fact(_fact(status="proposed", supports=[first.id, second.id]))
    client = portal_client(ctx)
    reject = [first.id, second.id] if reject_both else [first.id]
    preview = _verdicts(client, reject=reject, preview=True)
    apply = _verdicts(client, reject=reject)
    assert preview[exclusive.id].outcome == apply[exclusive.id].outcome == "rejected"
    assert (
        preview[shared.id].outcome
        == apply[shared.id].outcome
        == ("rejected" if reject_both else "stays")
    )
    assert set(preview[shared.id].evidence_of) == set(reject)


def test_domain_accepted_meanwhile_refused(portal_ctx, portal_client):
    ctx = portal_ctx()
    with ctx.request_store() as store:
        domain = store.add_domain(_domain())
        store.ratify_domains(accept=[domain.domain_id])
    result = _verdicts(portal_client(ctx), reject=[domain.domain_id])[domain.domain_id]
    assert result.outcome == "refused"
    assert result.error.details == {"reason": "domain_accepted_meanwhile"}
    assert result.error.message == "This domain was accepted in the meantime."
    with ctx.request_store() as store:
        assert store.get_domain(domain.domain_id).status == "accepted"


def test_refusal_texts_mapped(portal_ctx, portal_client, monkeypatch):
    ctx = portal_ctx()
    with ctx.request_store() as store:
        item = store.add_decision(_decision(status="proposed"))
    monkeypatch.setattr(
        server, "_ratify_one", lambda *args, **kwargs: ("error: needs the owner's verdict", [])
    )
    outcome = _verdicts(portal_client(ctx), accept=[item.id])[item.id]
    assert outcome.outcome == "refused"
    assert outcome.error.details["reason"] == "ratification_failed"
    assert "owner's verdict" not in outcome.error.message


def test_double_apply_same_ids_reports_settled(portal_ctx, portal_client):
    ctx = portal_ctx()
    with ctx.request_store() as store:
        first, second = [store.add_decision(_decision(status="proposed")) for _ in range(2)]
        domain = store.add_domain(_domain())
    client = portal_client(ctx)
    reject = [second.id, domain.domain_id]
    _verdicts(client, accept=[first.id], reject=reject)
    repeated = _verdicts(client, accept=[first.id], reject=reject)
    assert repeated[first.id].outcome == "already_accepted"
    assert repeated[second.id].outcome == "already_rejected"
    assert repeated[domain.domain_id].outcome == "already_dropped"


@pytest.mark.parametrize(
    ("scenario", "reason"),
    [
        ("replaced", "not_current"),
        ("proposed", "pending_proposal"),
        ("evidence", "evidence_conflict"),
        ("duplicate_evidence", "evidence_conflict"),
    ],
)
def test_apply_maps_store_supersession_conflicts(portal_ctx, portal_client, scenario, reason):
    from tests.store_format2_support import put_file

    ctx = portal_ctx()
    with ctx.request_store() as store:
        if scenario in {"evidence", "duplicate_evidence"}:
            marked = store.add_decision(_decision(status="proposed"))
            previous = store.add_fact(_fact())
            if scenario == "duplicate_evidence":
                store.add_fact(
                    _fact(status="proposed", supports=[marked.id], supersedes=previous.id)
                )
            else:
                store.add_fact(_fact(supersedes=previous.id))
            draft = _fact(status="proposed", supports=[marked.id], supersedes=previous.id)
            put_file(ctx.store_path, "fact", draft.model_dump(mode="json"))
        else:
            previous = store.add_decision(
                _decision(status="accepted" if scenario == "replaced" else "proposed")
            )
            if scenario == "replaced":
                store.add_decision(_decision(supersedes=previous.id))
            marked = _decision(status="proposed", supersedes=previous.id)
            put_file(ctx.store_path, "decision", marked.model_dump(mode="json"))
    outcome = _verdicts(portal_client(ctx), accept=[marked.id])[marked.id]
    assert outcome.outcome == "refused"
    assert outcome.error.details == {"reason": reason}


def test_busy_batch_keeps_matching_settled_verdict(portal_ctx, portal_client, monkeypatch):
    ctx = portal_ctx()
    with ctx.request_store() as store:
        settled = store.add_decision(_decision())
        pending = store.add_decision(_decision(status="proposed"))
    ratify_one = server._ratify_one

    def busy(store, id_, action, **kwargs):
        if id_ == pending.id:
            raise StoreBusyError("writer is busy")
        return ratify_one(store, id_, action, **kwargs)

    monkeypatch.setattr(server, "_ratify_one", busy)
    outcomes = _verdicts(portal_client(ctx), accept=[pending.id, settled.id])
    assert outcomes[settled.id].outcome == "already_accepted"
    assert outcomes[pending.id].outcome == "busy"


def test_busy_batch_refreshes_domain_toc_for_settled_prefix(portal_ctx, portal_client, monkeypatch):
    import json

    from sidegraph.retrieval import TOC_CACHE_KEY

    ctx = portal_ctx()
    with ctx.request_store() as store:
        first = store.add_domain(_domain("first-domain"))
        pending = store.add_domain(_domain("pending-domain"))
        store.set_meta(TOC_CACHE_KEY, '{"domains":[]}')
    ratify_one = server._ratify_one

    def busy(store, id_, action, **kwargs):
        if id_ == pending.domain_id:
            raise StoreBusyError("writer is busy")
        return ratify_one(store, id_, action, **kwargs)

    monkeypatch.setattr(server, "_ratify_one", busy)
    outcomes = _verdicts(portal_client(ctx), accept=[first.domain_id, pending.domain_id])
    assert outcomes[first.domain_id].outcome == "accepted"
    assert outcomes[pending.domain_id].outcome == "busy"
    with ctx.request_store() as store:
        assert [
            domain["slug"] for domain in json.loads(store.get_meta(TOC_CACHE_KEY))["domains"]
        ] == [first.slug]


def test_nested_evidence_follows_decision_mark(portal_ctx, portal_client):
    ctx = portal_ctx()
    with ctx.request_store() as store:
        draft = store.add_decision(_decision(status="proposed"))
        fact = store.add_fact(_fact(status="proposed", supports=[draft.id]))
    client = portal_client(ctx)
    for preview in (True, False):
        assert _verdicts(client, accept=[fact.id], preview=preview)[fact.id].outcome == "stays"
    for preview in (True, False):
        result = _verdicts(client, accept=[fact.id], reject=[draft.id], preview=preview)
        assert result[fact.id].outcome == "rejected"


def test_apply_preserves_unindexed_foreign_acceptance(portal_ctx, portal_client, monkeypatch):
    from contextlib import contextmanager

    from sidegraph.store import Store

    ctx = portal_ctx()
    with ctx.request_store() as store:
        draft = store.add_decision(_decision(status="proposed"))
    mutation = Store._mutation
    raced = False

    @contextmanager
    def interleave(store, **kwargs):
        nonlocal raced
        if not raced:
            raced = True
            with (
                ctx.request_store() as foreign,
                pytest.raises(RuntimeError, match="foreign writer"),
                mutation(foreign),
            ):
                foreign.ratify(draft.id)
                raise RuntimeError("foreign writer failed after publishing")
        with mutation(store, **kwargs):
            yield

    monkeypatch.setattr(Store, "_mutation", interleave)
    result = _verdicts(portal_client(ctx), reject=[draft.id])[draft.id]
    assert result.outcome == "refused"
    with ctx.request_store() as store:
        assert store.get_decision(draft.id).status == "accepted"


def test_busy_batch_preserves_a_proposed_records_refusal(portal_ctx, portal_client, monkeypatch):
    from tests.store_format2_support import put_file

    ctx = portal_ctx()
    with ctx.request_store() as store:
        previous = store.add_decision(_decision())
        store.add_decision(_decision(supersedes=previous.id))
        conflict = _decision(status="proposed", supersedes=previous.id)
        put_file(ctx.store_path, "decision", conflict.model_dump(mode="json"))
        pending = store.add_decision(_decision(status="proposed"))
    ratify_one = server._ratify_one

    def busy(store, id_, action, **kwargs):
        if id_ == pending.id:
            raise StoreBusyError("writer is busy")
        return ratify_one(store, id_, action, **kwargs)

    monkeypatch.setattr(server, "_ratify_one", busy)
    outcomes = _verdicts(portal_client(ctx), accept=[conflict.id, pending.id])
    assert outcomes[conflict.id].outcome == "refused"
    assert outcomes[conflict.id].error.details == {"reason": "not_current"}
    assert outcomes[pending.id].outcome == "busy"


def test_sqlite_busy_at_commit_retains_verdict_prefix(portal_ctx, portal_client, monkeypatch):
    import sqlite3

    from sidegraph.store import Store

    ctx = portal_ctx()
    with ctx.request_store() as store:
        first, second, third = [store.add_decision(_decision(status="proposed")) for _ in range(3)]
    commit = Store._commit
    failed = False

    def busy_commit(store):
        nonlocal failed
        if not failed and store.get_decision(second.id).status == "accepted":
            failed = True
            error = sqlite3.OperationalError("reader blocked commit")
            error.sqlite_errorcode = sqlite3.SQLITE_BUSY
            raise error
        commit(store)

    monkeypatch.setattr(Store, "_commit", busy_commit)
    result = _verdicts(portal_client(ctx), accept=[first.id, second.id, third.id])
    assert [result[item.id].outcome for item in (first, second, third)] == [
        "accepted",
        "accepted",
        "busy",
    ]
    with ctx.request_store() as store:
        assert (
            store.get_decision(first.id).status
            == store.get_decision(second.id).status
            == "accepted"
        )
        assert store.get_decision(third.id).status == "proposed"


def test_apply_rolls_back_the_index_when_ratifier_refuses_after_publication(
    portal_ctx, portal_client, monkeypatch
):
    import sqlite3

    from sidegraph.store import Store

    ctx = portal_ctx()
    with ctx.request_store() as store:
        draft = store.add_decision(_decision(status="proposed"))
        fact = store.add_fact(_fact(status="proposed", supports=[draft.id]))

    def fail(store, fact):
        raise ValueError("interrupted cascade")

    monkeypatch.setattr(Store, "_write_fact", fail)
    result = _verdicts(portal_client(ctx), accept=[draft.id])
    assert result[draft.id].outcome == "refused"
    assert result[draft.id].error.details == {"reason": "ratification_failed"}
    with sqlite3.connect(ctx.store_path / "index.db") as index:
        (status,) = index.execute(
            "SELECT status FROM decisions WHERE id = ?", (draft.id,)
        ).fetchone()
        assert status == "proposed"
    with ctx.request_store() as store:
        assert store.get_decision(draft.id).status == "accepted"
        assert store.get_fact(fact.id).status == "proposed"


def test_replaying_settled_domain_repairs_toc_after_commit_contention(
    portal_ctx, portal_client, monkeypatch
):
    import json
    import sqlite3

    from sidegraph.retrieval import TOC_CACHE_KEY
    from sidegraph.store import Store

    ctx = portal_ctx()
    with ctx.request_store() as store:
        domain = store.add_domain(_domain())
        store.set_meta(TOC_CACHE_KEY, '{"domains":[]}')
    commit = Store._commit

    def busy_commit(store):
        if store.get_domain(domain.domain_id).status == "accepted":
            error = sqlite3.OperationalError("reader blocked commit")
            error.sqlite_errorcode = sqlite3.SQLITE_BUSY
            raise error
        commit(store)

    client = portal_client(ctx)
    with monkeypatch.context() as patch:
        patch.setattr(Store, "_commit", busy_commit)
        assert _verdicts(client, accept=[domain.domain_id])[domain.domain_id].outcome == "accepted"
    with ctx.request_store() as store:
        assert json.loads(store.get_meta(TOC_CACHE_KEY))["domains"] == []
    assert (
        _verdicts(client, accept=[domain.domain_id])[domain.domain_id].outcome == "already_accepted"
    )
    with ctx.request_store() as store:
        assert json.loads(store.get_meta(TOC_CACHE_KEY))["domains"][0]["slug"] == domain.slug


def test_apply_rechecks_nesting_after_a_branch_state_change(portal_ctx, portal_client, monkeypatch):
    from contextlib import contextmanager

    from sidegraph.store import Store
    from tests.store_format2_support import put_file

    ctx = portal_ctx()
    with ctx.request_store() as store:
        parent = store.add_decision(_decision())
        fact = store.add_fact(_fact(status="proposed", supports=[parent.id]))
    mutation = Store._mutation
    raced = False

    @contextmanager
    def interleave(store, **kwargs):
        nonlocal raced
        if not raced:
            raced = True
            draft = parent.model_copy(update={"status": "proposed"})
            put_file(ctx.store_path, "decision", draft.model_dump(mode="json", warnings=False))
            with ctx.request_store():
                pass
        with mutation(store, **kwargs):
            yield

    monkeypatch.setattr(Store, "_mutation", interleave)
    assert _verdicts(portal_client(ctx), accept=[fact.id])[fact.id].outcome == "stays"
    with ctx.request_store() as store:
        assert store.get_fact(fact.id).status == "proposed"


def test_exclusive_lock_keeps_the_apply_prefix(portal_ctx, portal_client, monkeypatch):
    import sqlite3
    from contextlib import contextmanager

    from sidegraph.portal import writes

    ctx = portal_ctx()
    with ctx.request_store() as store:
        first, second = [store.add_decision(_decision(status="proposed")) for _ in range(2)]
    reserve = writes._reservation
    blocker = None

    @contextmanager
    def interleave(store):
        nonlocal blocker
        with reserve(store):
            yield
        if blocker is None:
            blocker = sqlite3.connect(ctx.store_path / "index.db", check_same_thread=False)
            blocker.execute("BEGIN EXCLUSIVE")

    monkeypatch.setattr(writes, "_reservation", interleave)
    try:
        outcomes = _verdicts(portal_client(ctx), accept=[first.id, second.id])
        assert outcomes[first.id].outcome == "accepted"
        assert outcomes[second.id].outcome == "busy"
    finally:
        if blocker is not None:
            blocker.rollback()
            blocker.close()


def test_apply_observes_evidence_under_the_verdict_reservation(
    portal_ctx, portal_client, monkeypatch
):
    from contextlib import contextmanager

    from sidegraph.portal import writes
    from tests.store_format2_support import put_file

    ctx = portal_ctx()
    with ctx.request_store() as store:
        marked, other = [store.add_decision(_decision(status="proposed")) for _ in range(2)]
        moved = store.add_fact(_fact(status="proposed", supports=[marked.id]))
    reserve = writes._reservation
    added = None

    @contextmanager
    def interleave(store):
        nonlocal added
        if added is None:
            with ctx.request_store() as foreign:
                added = foreign.add_fact(_fact(status="proposed", supports=[marked.id]))
            edited = moved.model_copy(update={"supports": [other.id]})
            put_file(ctx.store_path, "fact", edited.model_dump(mode="json"))
        with reserve(store):
            yield

    monkeypatch.setattr(writes, "_reservation", interleave)
    results = _verdicts(portal_client(ctx), accept=[marked.id])
    assert moved.id not in results
    assert results[added.id].outcome == "accepted"
    assert results[added.id].evidence_of == [marked.id]


def test_interrupted_cascade_reports_unwritten_evidence_busy(
    portal_ctx, portal_client, monkeypatch
):
    from sidegraph.store import Store

    ctx = portal_ctx()
    with ctx.request_store() as store:
        draft = store.add_decision(_decision(status="proposed"))
        facts = [store.add_fact(_fact(status="proposed", supports=[draft.id])) for _ in range(2)]
    write = Store._write_fact
    attempted = []

    def fail_second(store, fact):
        attempted.append(fact.id)
        if len(attempted) == 2:
            raise StoreBusyError("interrupted cascade")
        return write(store, fact)

    monkeypatch.setattr(Store, "_write_fact", fail_second)
    outcomes = _verdicts(portal_client(ctx), accept=[draft.id])
    assert outcomes[draft.id].outcome == "accepted"
    assert set(attempted) == {fact.id for fact in facts}
    assert outcomes[attempted[0]].outcome == "accepted"
    assert outcomes[attempted[1]].outcome == "busy"
    assert outcomes[attempted[1]].error.code == "busy"
