"""The write gate stops minting what doctor's dangling-record check now flags (design D8).

D6 established that a fact reachable ONLY through a now-terminal decision's `supports` link
decays into something task-seeded retrieval can no longer see (retrieval.py's
`evidence=False` on the superseded one-liner) -- it survives only in `list_facts`. D6's
premise is "the gate was satisfied when the fact was WRITTEN, because its decision was live
then" -- but nothing enforced that: `add_fact` validated `supports` by mere EXISTENCE, and
`supersede_fact` inherited the predecessor's `supports` verbatim with no re-check. Two live
paths could write a fact born flagged. This closes that at all three write paths:
`propose_facts` (capture.py, agent-initiated), `_add_fact_impl` (server.py -- the primary,
human-asked path the `record-fact` skill drives, and the one with NO gate at all before this
task), and `supersede_fact`'s no-anchors path (server.py).

A fact WITH an anchor is untouched in every path -- the gate applies only when the request
itself carries no anchor (design D8's residual: "anchorless" is defined by the REQUEST).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from sidegraph.capture import propose_facts
from sidegraph.schema import Decision, DecisionKind, DecisionStatus, Provenance
from sidegraph.server import _add_fact_impl, _supersede_fact_impl
from sidegraph.store import Store


def _live_decision(store: Store) -> Decision:
    return store.add_decision(
        Decision(
            title="a live decision",
            kind=DecisionKind.ADR,
            status=DecisionStatus.ACCEPTED,
            context="c",
            choice="ch",
            valid_from=datetime.now(UTC),
            provenance=Provenance(source="manual"),
        )
    )


def _terminal_decision(store: Store) -> Decision:
    """An ACCEPTED decision, then immediately superseded -- SUPERSEDED is terminal
    (`_TERMINAL_DECISION_STATUSES`), same as REJECTED/DEPRECATED for this gate's purposes."""
    old = _live_decision(store)
    store.add_decision(
        Decision(
            title="its successor",
            kind=DecisionKind.ADR,
            status=DecisionStatus.ACCEPTED,
            context="c",
            choice="ch",
            valid_from=datetime.now(UTC),
            supersedes=old.id,
            provenance=Provenance(source="manual"),
        )
    )
    assert store.get_decision(old.id).status == DecisionStatus.SUPERSEDED
    return old


# -- per-path adapters --------------------------------------------------------------------
#
# Each returns the written fact's id on success, or raises ValueError on rejection --
# `propose_facts` signals rejection via a result object rather than an exception, so its
# adapter translates that into the same exception-based contract the other two already use,
# letting one parametrized test exercise all three paths uniformly.


def _via_propose_facts(store, *, statement, source, supports=None, anchors=None):
    draft = {"statement": statement, "source": source}
    if supports is not None:
        draft["supports"] = supports
    if anchors is not None:
        draft["anchors"] = anchors
    [result] = propose_facts([draft], store, reader=None)
    if result.status == "rejected":
        raise ValueError(result.reason)
    return result.fact_id


def _via_add_fact_impl(store, *, statement, source, supports=None, anchors=None):
    out = _add_fact_impl(
        store, None, statement=statement, source=source, supports=supports, anchors=anchors
    )
    return out["id"]


def _via_supersede_fact(store, *, statement, source, supports=None, anchors=None):
    """Builds its OWN, binding-less predecessor (reachable only via a live decision's
    `supports` link, never an anchor) so the successor's gate outcome below reflects only
    THIS call's own anchors/supports -- not bindings silently inherited from the
    predecessor (`supersede_fact`'s no-anchors path also copies the predecessor's bindings
    verbatim, which would give the successor a free pass if the predecessor had any)."""
    scaffold_decision = _live_decision(store)
    predecessor = _add_fact_impl(
        store, None, statement="scaffold predecessor", source="s", supports=[scaffold_decision.id]
    )
    out = _supersede_fact_impl(
        store,
        None,
        old_fact_id=predecessor["id"],
        statement=statement,
        source=source,
        supports=supports,
        anchors=anchors,
    )
    return out["id"]


_PATHS = {
    "propose_facts": _via_propose_facts,
    "_add_fact_impl": _via_add_fact_impl,
    "supersede_fact": _via_supersede_fact,
}


@pytest.mark.parametrize("path_name", sorted(_PATHS))
def test_anchorless_supports_only_terminal_is_rejected(tmp_path, path_name):
    store = Store(tmp_path / "s")
    terminal = _terminal_decision(store)
    write = _PATHS[path_name]
    with pytest.raises(ValueError) as exc_info:
        write(store, statement="late evidence", source="src", supports=[terminal.id])
    reason = str(exc_info.value).lower()
    assert "anchor" in reason
    assert "supports" in reason or "successor" in reason


@pytest.mark.parametrize("path_name", sorted(_PATHS))
def test_bare_no_anchor_no_supports_is_rejected(tmp_path, path_name):
    store = Store(tmp_path / "s")
    write = _PATHS[path_name]
    with pytest.raises(ValueError):
        write(store, statement="no anchor no supports", source="src", supports=[])


@pytest.mark.parametrize("path_name", sorted(_PATHS))
def test_the_same_fact_with_an_anchor_still_writes(tmp_path, path_name):
    store = Store(tmp_path / "s")
    terminal = _terminal_decision(store)
    write = _PATHS[path_name]
    fact_id = write(
        store,
        statement="anchored evidence",
        source="src",
        supports=[terminal.id],
        anchors=[{"name": "gate.py", "file_path": "src/gate.py"}],
    )
    assert store.get_fact(fact_id) is not None
