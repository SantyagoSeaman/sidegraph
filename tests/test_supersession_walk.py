"""The successor walk that decides "is this predecessor already replaced?".

It must look at EVERY successor branch (a git merge can fork a chain), tolerate chains of
any length, and terminate on a cycle. Records here are written raw, as a merge delivers
them, without the write-time guard.
"""

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from sidegraph.schema import Decision, DecisionKind, DecisionStatus, Fact, Provenance
from sidegraph.store import Store, SupersessionConflictError

_T0 = datetime(2026, 1, 1, tzinfo=UTC)


def _rec(
    title: str,
    status: DecisionStatus,
    supersedes: str | None = None,
    rec_id: str | None = None,
    *,
    closed: bool | None = None,
    kind: str = "decision",
) -> Decision | Fact:
    """A record in ``status``. ``closed`` (default: closed unless proposed/accepted) sets
    ``valid_to``; an ``accepted`` record with ``closed=True`` is the closed-but-accepted shape."""
    if closed is None:
        closed = status not in (DecisionStatus.ACCEPTED, DecisionStatus.PROPOSED)
    common = {
        "status": status,
        "supersedes": supersedes,
        "valid_from": _T0,
        "valid_to": _T0 + timedelta(days=1) if closed else None,
        "provenance": Provenance(source="manual"),
        **({"id": rec_id} if rec_id else {}),
    }
    if kind == "fact":
        return Fact(statement=title, source="trial", **common)
    return Decision(title=title, kind=DecisionKind.ADR, context="c", choice=title, **common)


def _raw(s: Store, *records: Decision | Fact) -> None:
    for r in records:
        with s._mutation():
            if isinstance(r, Fact):
                s._write_fact(r)
            else:
                s._write_decision(r)


def _open_heads(s: Store) -> list[str]:
    return sorted(
        d.id for d in s.iter_decisions() if d.status == DecisionStatus.ACCEPTED and not d.valid_to
    )


def test_ratify_refuses_when_the_live_head_sits_on_the_other_fork_branch(tmp_path):
    s = Store(tmp_path / "s")
    S, A = DecisionStatus.SUPERSEDED, DecisionStatus.ACCEPTED
    root = _rec("root", S, rec_id="01J00000000000000000000000")
    # Two merged branches off root. The lowest id (the one LIMIT 1 picks) is a dead end;
    # the live head is under the other.
    dead = _rec("dead end", S, root.id, "01J00000000000000000000001")
    other = _rec("other", S, root.id, "01J00000000000000000000002")
    live = _rec("live", A, other.id, "01J00000000000000000000003")
    stale = _rec("stale", DecisionStatus.PROPOSED, root.id, "01J00000000000000000000004")
    _raw(s, root, dead, other, live, stale)
    assert _open_heads(s) == [live.id]

    with pytest.raises(SupersessionConflictError, match=f"superseded by {live.id}"):
        s.ratify(stale.id)
    assert s.get_decision(stale.id).status == DecisionStatus.PROPOSED
    assert _open_heads(s) == [live.id]


def test_closed_record_refusal_names_the_head_on_the_other_fork_branch(tmp_path):
    s = Store(tmp_path / "s")
    S, A = DecisionStatus.SUPERSEDED, DecisionStatus.ACCEPTED
    root = _rec("root", S, rec_id="01J00000000000000000000000")
    dead = _rec("dead end", S, root.id, "01J00000000000000000000001")
    other = _rec("other", S, root.id, "01J00000000000000000000002")
    live = _rec("live", A, other.id, "01J00000000000000000000003")
    _raw(s, root, dead, other, live)

    with pytest.raises(SupersessionConflictError, match=f"live successor {live.id}"):
        s.add_decision(_rec("next", DecisionStatus.PROPOSED, root.id))


def test_ratify_refuses_when_the_live_head_is_beyond_64_links(tmp_path):
    s = Store(tmp_path / "s")
    S = DecisionStatus.SUPERSEDED
    chain = [_rec("rev 0", S)]
    for n in range(1, 71):
        chain.append(_rec(f"rev {n}", S, chain[-1].id))
    live = _rec("rev 71", DecisionStatus.ACCEPTED, chain[-1].id)
    stale = _rec("stale", DecisionStatus.PROPOSED, chain[0].id)
    _raw(s, *chain, live, stale)

    with pytest.raises(SupersessionConflictError, match=f"superseded by {live.id}"):
        s.ratify(stale.id)
    assert s.get_decision(stale.id).status == DecisionStatus.PROPOSED
    assert _open_heads(s) == [live.id]


def test_a_cycle_of_supersedes_links_terminates(tmp_path):
    s = Store(tmp_path / "s")
    S = DecisionStatus.SUPERSEDED
    a = _rec("a", S, rec_id="01J00000000000000000000001")
    b = _rec("b", S, a.id, "01J00000000000000000000002")
    c = _rec("c", S, b.id, "01J00000000000000000000003")
    a.supersedes = c.id  # a merge closed the loop: a -> b -> c -> a
    stale = _rec("stale", DecisionStatus.PROPOSED, a.id)
    _raw(s, a, b, c, stale)

    # No live head anywhere in the loop: nothing to close, so ratification goes through.
    s.ratify(stale.id)
    assert s.get_decision(stale.id).status == DecisionStatus.ACCEPTED


def test_a_cycle_with_a_live_head_still_refuses(tmp_path):
    s = Store(tmp_path / "s")
    S, A = DecisionStatus.SUPERSEDED, DecisionStatus.ACCEPTED
    a = _rec("a", S, rec_id="01J00000000000000000000001")
    b = _rec("b", S, a.id, "01J00000000000000000000002")
    c = _rec("c", S, b.id, "01J00000000000000000000003")
    a.supersedes = c.id
    live = _rec("live", A, c.id, "01J00000000000000000000004")
    stale = _rec("stale", DecisionStatus.PROPOSED, a.id)
    _raw(s, a, b, c, live, stale)

    with pytest.raises(SupersessionConflictError, match=f"superseded by {live.id}"):
        s.ratify(stale.id)


def test_a_dead_fork_branch_alone_does_not_block_ratification(tmp_path):
    s = Store(tmp_path / "s")
    S = DecisionStatus.SUPERSEDED
    root = _rec("root", S)
    dead = _rec("dead end", S, root.id)
    rejected = _rec("rejected", DecisionStatus.REJECTED, root.id)
    heir = _rec("heir", DecisionStatus.PROPOSED, root.id)
    _raw(s, root, dead, rejected, heir)

    s.ratify(heir.id)
    assert s.get_decision(heir.id).status == DecisionStatus.ACCEPTED


@pytest.mark.parametrize("kind", ["decision", "fact"])
@pytest.mark.parametrize("has_open_head", [False, True])
def test_a_closed_accepted_record_is_not_a_live_head(tmp_path, kind, has_open_head):
    s = Store(tmp_path / "s")
    S, A = DecisionStatus.SUPERSEDED, DecisionStatus.ACCEPTED
    root = _rec("root", S, rec_id="01J00000000000000000000000", kind=kind)
    if has_open_head:
        side_a = _rec("live", A, root.id, "01J00000000000000000000001", kind=kind)
    else:
        side_a = _rec(
            "dead", DecisionStatus.REJECTED, root.id, "01J00000000000000000000001", kind=kind
        )
    other = _rec("other", S, root.id, "01J00000000000000000000002", kind=kind)
    # Accepted but closed (valid_to set), one level deeper than the open head.
    closed = _rec("closed", A, other.id, "01J00000000000000000000003", closed=True, kind=kind)
    stale = _rec("stale", DecisionStatus.PROPOSED, root.id, "01J00000000000000000000004", kind=kind)
    _raw(s, root, side_a, other, closed, stale)
    table = f"{kind}s"

    if has_open_head:
        assert s._live_head_after(table, "id", root.id) == side_a.id
    else:
        assert s._live_head_after(table, "id", root.id) is None
        (s.ratify_fact if kind == "fact" else s.ratify)(stale.id)
        get = s.get_fact if kind == "fact" else s.get_decision
        assert get(stale.id).status == DecisionStatus.ACCEPTED


@pytest.mark.parametrize("table", ["decisions", "facts", "domains"])
def test_the_supersedes_lookup_uses_an_index_and_an_old_index_db_gains_it(tmp_path, table):
    s = Store(tmp_path / "s")
    plan = "SELECT 1 FROM {} WHERE supersedes = ? AND status IN ('accepted', 'superseded')"
    s.close()
    db = next((tmp_path / "s").rglob("index.db"))
    with sqlite3.connect(db) as c:  # an index.db built before the index existed
        c.execute(f"DROP INDEX idx_{table}_supersedes")
    s = Store(tmp_path / "s")  # a plain open, no rebuild
    detail = s._conn.execute("EXPLAIN QUERY PLAN " + plan.format(table), ("x",)).fetchall()
    assert f"USING INDEX idx_{table}_supersedes" in detail[0][3]
    s.close()
