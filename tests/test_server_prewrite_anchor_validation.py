"""Every anchor-taking write path rejects an invalid anchor list before its first write.

Spec: design/superpowers/specs/2026-09-22-pre-write-anchor-validation-design.md
"""

from __future__ import annotations

import inspect
import re
from collections.abc import Callable
from pathlib import Path

import pytest

import sidegraph.server as server
from sidegraph.engine.reader import GraphifyReader
from sidegraph.schema import DecisionStatus
from sidegraph.server import (
    _add_anchors_impl,
    _add_decision_impl,
    _add_fact_impl,
    _supersede_decision_impl,
    _supersede_fact_impl,
)
from sidegraph.store import Store

FIXTURE = Path(__file__).parent / "fixtures" / "mini_graph.json"
GOOD = {"name": "Trader", "file_path": "trader/exec.py"}
NAMELESS = {"file_path": "trader/exec.py"}
_CANONICAL_DIRS = ("decisions", "facts", "bindings", "entities")

# shape id -> (anchor list, regex the ValueError message must match)
_LISTS: dict[str, tuple[list[dict], str]] = {
    "bad-relation": (
        [GOOD, {"name": "helper()", "file_path": "util/misc.py", "relation": "bogus"}],
        "relation",
    ),
    "non-string-name": ([GOOD, {"name": 123, "file_path": "util/misc.py"}], "invalid anchor"),
    "non-string-file-path": ([GOOD, {"name": "helper()", "file_path": 5}], "invalid anchor"),
    "all-nameless": ([NAMELESS], "has a name"),
}


def _canonical_snapshot(store: Store) -> dict[str, bytes]:
    root = Path(store.path)
    return {
        str(p.relative_to(root)): p.read_bytes()
        for d in _CANONICAL_DIRS
        for p in sorted((root / d).glob("*.json"))
    }


def _state(store: Store) -> tuple:
    decisions = sorted((d.id, d.status, d.valid_to) for d in store.iter_decisions())
    facts = sorted((f.id, f.status, f.valid_to) for f in store.iter_facts())
    bindings = sorted(
        (rid, len(store.bindings_for_record(rid)))
        for rid in [d[0] for d in decisions] + [f[0] for f in facts]
    )
    return decisions, facts, bindings


def _call_capturing(call: Callable[[], object]) -> ValueError | None:
    """Run ``call``; return the ``ValueError`` it raised, or ``None`` if it returned."""
    try:
        call()
    except ValueError as e:
        return e
    return None


def _decision(store, reader, anchors=None) -> dict:
    return _add_decision_impl(
        store, reader, title="t", kind="adr", context="c", choice="ch", anchors=anchors
    )


def _fact(store, reader, anchors) -> dict:
    return _add_fact_impl(store, reader, statement="s", source="measured", anchors=anchors)


# Each setup seeds the store (it may write), then returns the call under test.
_Setup = Callable[[Store, GraphifyReader], Callable[[list[dict]], dict]]


def _setup_add_decision(store, reader):
    return lambda anchors: _decision(store, reader, anchors)


def _setup_supersede_decision(store, reader):
    old = _decision(store, reader, [GOOD])
    return lambda anchors: _supersede_decision_impl(
        store,
        reader,
        old["id"],
        title="new",
        kind="adr",
        context="c2",
        choice="ch2",
        anchors=anchors,
    )


def _setup_add_fact(store, reader):
    return lambda anchors: _fact(store, reader, anchors)


def _setup_supersede_fact(store, reader):
    old = _fact(store, reader, [GOOD])
    return lambda anchors: _supersede_fact_impl(
        store, reader, old["id"], statement="s2", source="measured again", anchors=anchors
    )


def _setup_add_anchors(store, reader):
    rec = _decision(store, reader, None)
    return lambda anchors: _add_anchors_impl(store, reader, rec["id"], anchors)


_PATHS: dict[str, _Setup] = {
    "_add_decision_impl": _setup_add_decision,
    "_supersede_decision_impl": _setup_supersede_decision,
    "_add_fact_impl": _setup_add_fact,
    "_supersede_fact_impl": _setup_supersede_fact,
    "_add_anchors_impl": _setup_add_anchors,
}


@pytest.mark.parametrize("path", sorted(_PATHS))
@pytest.mark.parametrize("shape", sorted(_LISTS))
def test_invalid_anchor_list_writes_nothing(tmp_path, path, shape):
    """Canonical files and reopened state are checked before the message, so a half-write
    shows up as the failure, not a message mismatch."""
    anchors, message = _LISTS[shape]
    store_dir = tmp_path / "s"
    store = Store(store_dir)
    reader = GraphifyReader(FIXTURE)
    call = _PATHS[path](store, reader)
    before_files = _canonical_snapshot(store)
    before_state = _state(store)

    error = _call_capturing(lambda: call(anchors))

    assert _canonical_snapshot(store) == before_files
    store.close()
    assert _state(Store(store_dir)) == before_state
    assert error is not None, "expected ValueError, the call returned normally"
    assert re.search(message, str(error)), str(error)


def test_supersede_invalid_relation_keeps_predecessor_open(tmp_path):
    """A rejected supersession leaves the predecessor open and writes no successor."""
    store_dir = tmp_path / "s"
    store = Store(store_dir)
    reader = GraphifyReader(FIXTURE)
    old = _decision(store, reader, [GOOD])
    before_files = _canonical_snapshot(store)

    error = _call_capturing(
        lambda: _supersede_decision_impl(
            store,
            reader,
            old["id"],
            title="new",
            kind="adr",
            context="c2",
            choice="ch2",
            anchors=[{**GOOD, "relation": "bogus"}],
        )
    )

    assert _canonical_snapshot(store) == before_files
    store.close()
    decisions = list(Store(store_dir).iter_decisions())
    assert [d.id for d in decisions] == [old["id"]]
    assert decisions[0].status == DecisionStatus.ACCEPTED
    assert decisions[0].valid_to is None
    assert error is not None and "relation" in str(error)


def test_matrix_covers_every_anchor_taking_write_path():
    """Guard: a new ``*_impl`` with an ``anchors`` parameter must join the matrix above."""
    found = {
        name
        for name, fn in vars(server).items()
        if inspect.isfunction(fn)
        and fn.__module__ == server.__name__
        and name.endswith("_impl")
        and "anchors" in inspect.signature(fn).parameters
    }
    assert found == set(_PATHS)


def test_supersede_still_skips_a_nameless_anchor_beside_a_named_one(tmp_path):
    """Guard against over-reach: in a list with a named anchor, a nameless one is skipped."""
    reader = GraphifyReader(FIXTURE)

    def supersede(store_dir, anchors):
        store = Store(store_dir)
        old = _decision(store, reader, [GOOD])
        return _supersede_decision_impl(
            store,
            reader,
            old["id"],
            title="new",
            kind="adr",
            context="c2",
            choice="ch2",
            anchors=anchors,
        )

    control = supersede(tmp_path / "control", [GOOD])
    out = supersede(tmp_path / "s", [NAMELESS, GOOD])
    assert out["bindings"] == control["bindings"] > 0
