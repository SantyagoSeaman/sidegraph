"""Every store write commits or rolls back — nothing in between (design D1/D2).

The defect: a failure mid-write returned control with the connection still owning an
uncommitted transaction, so a long-lived MCP server kept the write lock and every hook and
CLI after it got `database is locked`. Measured before the fix:
`{'caught': True, 'connection_in_transaction': True}`.

Two of the tests below are declared exceptions to "every new/changed test is red-first
against unfixed code" (spec §4 item 10) — see their docstrings: the success-path test
passes today by construction, and the two reentrancy tests cannot run at all before
``Store._mutation`` exists.
"""

from __future__ import annotations

import ast
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

import sidegraph.store as store_module
from sidegraph.schema import (
    AnchorBinding,
    Decision,
    DecisionKind,
    DecisionStatus,
    Descriptor,
    Domain,
    Entity,
    Fact,
    Initiative,
    Provenance,
)
from sidegraph.store import Store

_SOURCE_PATH = Path(store_module.__file__)


def _decision(title: str = "an adr", **overrides) -> Decision:
    base = dict(
        title=title,
        kind=DecisionKind.ADR,
        status=DecisionStatus.ACCEPTED,
        context="c",
        choice="ch",
        valid_from=datetime.now(UTC),
        provenance=Provenance(source="manual"),
    )
    base.update(overrides)
    return Decision(**base)


def _fact(statement: str = "a fact", **overrides) -> Fact:
    base = dict(
        statement=statement,
        source="test",
        status=DecisionStatus.ACCEPTED,
        valid_from=datetime.now(UTC),
        provenance=Provenance(source="manual"),
    )
    base.update(overrides)
    return Fact(**base)


def _domain(slug: str = "a-domain", **overrides) -> Domain:
    base = dict(
        slug=slug,
        title=slug,
        summary="A domain used for mutation-guard testing.",
        provenance=Provenance(source="manual"),
    )
    base.update(overrides)
    return Domain(**base)


def _proposed_decision(store: Store, title: str) -> Decision:
    return store.add_decision(_decision(title, status=DecisionStatus.PROPOSED))


def _proposed_fact(store: Store, statement: str) -> Fact:
    return store.add_fact(_fact(statement, status=DecisionStatus.PROPOSED))


# -- 1. mechanical guard on the call sites ---------------------------------------------------
#
# A hand-written list of "methods that commit" catches nothing added later -- that is
# exactly how sixteen of seventeen public writes drifted unguarded from the one
# (`supersede_domain`) that had a rollback guard. Two assertions, not one: `_commit`'s own
# body IS `self._conn.commit()` (store.py:1204), so a single combined pattern (e.g. "no
# self._conn.commit() anywhere but _mutation") would flag `_commit`'s own definition and
# fail against a CORRECT implementation -- the same mistake an earlier draft of the design
# spec itself made (spec §4 item 1).


def _attr_chain(func: ast.expr) -> tuple[str, ...]:
    """``self._commit`` -> ``("self", "_commit")``; ``self._conn.commit`` ->
    ``("self", "_conn", "commit")``. Anything else (a bare name, a subscript, ...) -> ``()``,
    which matches no chain we care about."""
    names: list[str] = []
    node = func
    while isinstance(node, ast.Attribute):
        names.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        names.append(node.id)
    else:
        return ()
    names.reverse()
    return tuple(names)


def _methods_calling(class_name: str, attr_chain: tuple[str, ...]) -> set[str]:
    """Every method of ``class_name`` (in store.py) whose body contains a textual call
    matching ``attr_chain`` (e.g. ``("self", "_commit")``) -- an AST walk, not a regex, so a
    docstring that happens to mention ``self._commit()`` in prose can never produce a false
    positive or a false negative."""
    tree = ast.parse(_SOURCE_PATH.read_text(encoding="utf-8"))
    class_node = next(
        n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name == class_name
    )
    hits: set[str] = set()
    for node in class_node.body:
        if not isinstance(node, ast.FunctionDef):
            continue
        for call in ast.walk(node):
            if isinstance(call, ast.Call) and _attr_chain(call.func) == attr_chain:
                hits.add(node.name)
    return hits


def test_no_bare_commit_call_sites_outside_the_helper():
    commit_helper_callers = _methods_calling("Store", ("self", "_commit"))
    assert commit_helper_callers <= {"_mutation"}, (
        f"self._commit( called outside _mutation: {sorted(commit_helper_callers - {'_mutation'})}"
    )

    raw_commit_callers = _methods_calling("Store", ("self", "_conn", "commit"))
    allowed = {"_commit", "_reload_index_from_canonical"}
    assert raw_commit_callers <= allowed, (
        f"self._conn.commit() called outside {sorted(allowed)}: "
        f"{sorted(raw_commit_callers - allowed)}"
    )


# -- 2-4: rollback behavior ------------------------------------------------------------------


def test_a_failed_write_rolls_back(tmp_path, monkeypatch):
    """Inject a failure inside add_decision's PREDECESSOR write (the second of its two
    ``_write_decision`` calls -- the successor is always written first, design's ordering
    comment). Against unfixed code the exception propagates but the transaction stays open."""
    store = Store(tmp_path / "s")
    predecessor = store.add_decision(_decision("first"))

    calls = {"n": 0}
    original = Store._write_decision

    def _flaky(self, decision):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("disk on fire")
        return original(self, decision)

    monkeypatch.setattr(Store, "_write_decision", _flaky)
    successor = _decision("second")
    successor.supersedes = predecessor.id

    with pytest.raises(RuntimeError):
        store.add_decision(successor)
    assert store._conn.in_transaction is False


def test_a_failed_write_does_not_lock_out_another_store(tmp_path, monkeypatch):
    """The defect's actual consequence, not merely a cosmetic flag: after the failure above,
    an INDEPENDENT ``Store`` on the same directory must complete a write immediately.

    ``other`` is opened BEFORE the failure is induced, while canonical/index state still
    agrees, and its ``busy_timeout`` lowered right away -- this isolates the assertion to
    "another store's ORDINARY write blocks on the leaked lock" (the actual defect). Opening
    ``other`` only AFTER the failure would also trip a digest mismatch (the failed write's
    successor canonical file is already durably on disk with no matching index row --
    tolerated, see ``_mutation``'s docstring) and send it through
    ``_reload_index_from_canonical``, whose OWN 30s busy_timeout (design D3, untouched by
    this task) would make even the fixed/green case slow for a reason unrelated to what this
    test is pinning."""
    store = Store(tmp_path / "s")
    predecessor = store.add_decision(_decision("first"))

    other = Store(tmp_path / "s")
    other._conn.execute("PRAGMA busy_timeout = 300")

    calls = {"n": 0}
    original = Store._write_decision

    def _flaky(self, decision):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("disk on fire")
        return original(self, decision)

    monkeypatch.setattr(Store, "_write_decision", _flaky)
    successor = _decision("second")
    successor.supersedes = predecessor.id
    with pytest.raises(RuntimeError):
        store.add_decision(successor)

    try:
        other.add_decision(_decision("third"))
    except sqlite3.OperationalError as e:
        pytest.fail(f"a second Store could not write after the first store's failure: {e}")
    finally:
        other.close()


def test_a_failed_commit_rolls_back(tmp_path, monkeypatch):
    """H1: the commit itself is where SQLITE_BUSY lands, so a failing commit must roll back
    too, not propagate untouched. Red-first-runnable against TODAY's ``add_decision``: a
    failing ``_commit`` currently leaves ``in_transaction is True``."""
    store = Store(tmp_path / "s")

    def _boom(self):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(Store, "_commit", _boom)
    with pytest.raises(sqlite3.OperationalError):
        store.add_decision(_decision())
    assert store._conn.in_transaction is False


def test_the_success_path_still_commits(tmp_path):
    """Trivially true today (add_decision's own trailing ``self._commit()``), and must stay
    true — a helper that never commits would pass the rollback tests above perfectly.
    NOT red-first evidence (spec §4 item 10, declared exception): this passes against
    unfixed code too."""
    store = Store(tmp_path / "s")
    store.add_decision(_decision())
    assert store._conn.in_transaction is False


# -- 6. reentrancy (D2/D2a) ------------------------------------------------------------------
#
# Both tests below cannot run AT ALL against unfixed code -- there is no ``Store._mutation``
# to nest yet (``AttributeError``, not a real red-first failure). Declared exception, spec
# §4 item 10.


def test_nested_mutation_commits_once_at_the_outermost_exit(tmp_path, monkeypatch):
    store = Store(tmp_path / "s")
    commit_calls = {"n": 0}
    original_commit = Store._commit

    def _counting_commit(self):
        commit_calls["n"] += 1
        return original_commit(self)

    monkeypatch.setattr(Store, "_commit", _counting_commit)
    with store._mutation(), store._mutation():
        pass
    assert commit_calls["n"] == 1
    assert store._conn.in_transaction is False
    assert store._mutation_depth == 0


def test_an_exception_in_a_nested_mutation_rolls_back_the_whole_thing(tmp_path):
    store = Store(tmp_path / "s")
    with pytest.raises(RuntimeError), store._mutation(), store._mutation():
        raise RuntimeError("boom")
    assert store._conn.in_transaction is False
    assert store._mutation_depth == 0


# -- Task 2: every remaining write method also rolls back ------------------------------------
#
# "The write API" is derived from the SAME scan `test_no_bare_commit_call_sites_outside_the_
# helper` runs, not hand-listed: whichever mechanism a method currently uses -- the old bare
# `with self._lock: ... self._commit()` or the new `with self._mutation():` -- it shows up in
# one of these two scans, so a method mid-migration from one to the other, or a brand-new
# write method added later, is covered by construction (spec §4 item 1's whole point) instead
# of by remembering to update a hand-written list.
#
# ``ratify_domains`` is deliberately excluded from this generic set: design D2a wraps it PER
# ITEM inside its two loops, not once around the whole method, so one bad item must NOT roll
# back items already durably committed -- the opposite of what this generic assertion checks.
# It gets its own dedicated test below instead (mirroring the reentrancy tests above).


def _write_api_methods() -> set[str]:
    return (
        _methods_calling("Store", ("self", "_commit"))
        | _methods_calling("Store", ("self", "_mutation"))
    ) - {"_mutation"}


class _FailAfterMarker:
    """Stand-in for ``store._conn``: delegates everything to the real connection. On the
    ``(skip + 1)``-th ``execute`` call whose SQL contains ``marker``, it lets the statement
    run for real FIRST and THEN raises -- not before. Raising before delegating would mean
    the real connection never actually performed the write, so `in_transaction` could stay
    False for a reason that has nothing to do with rollback (a vacuous pass — measured: with
    an eager raise, ``add_fact``/``ratify``/``mark_captured``/etc. all "passed" against
    UNFIXED code, because their very first SQL write was the intercepted one and nothing
    ever touched the real connection). Executing for real first guarantees there is always
    something genuine to roll back, so the assertion means what it says for every case.

    ``sqlite3.Connection`` is a C extension type -- both ``sqlite3.Connection.execute = ...``
    and ``some_conn.execute = ...`` raise ``TypeError: ... immutable type`` / ``...
    read-only``, so it cannot be monkeypatched directly. Swapping the plain (mutable)
    ``Store._conn`` attribute for a proxy is the generic way around that, and gives every
    rollback case the SAME failure-injection mechanism, keyed only on "which table does this
    write touch" -- the same fact the mechanical guard test's own SQL literals already
    document, so no method needs its own bespoke monkeypatch target."""

    def __init__(self, real: sqlite3.Connection, marker: str, skip: int = 0) -> None:
        self._real = real
        self._marker = marker
        self._skip = skip

    def execute(self, sql: str, *args: object, **kwargs: object) -> object:
        result = self._real.execute(sql, *args, **kwargs)
        if self._marker in sql:
            if self._skip > 0:
                self._skip -= 1
            else:
                raise RuntimeError(f"injected failure: {self._marker!r}")
        return result

    def __getattr__(self, name: str) -> object:
        return getattr(self._real, name)


def _raise_when_sql_contains(store: Store, marker: str, *, skip: int = 0) -> None:
    store._conn = _FailAfterMarker(store._conn, marker, skip=skip)


def _case_add_decision(store: Store) -> tuple[str, object]:
    return "INSERT INTO decisions", lambda: store.add_decision(_decision("case-add-decision"))


def _case_upsert_entity(store: Store) -> tuple[str, object]:
    return "INSERT INTO entities", lambda: store.upsert_entity(
        Entity(canonical_name="case-upsert-entity")
    )


def _case_get_or_create_abstract_entity(store: Store) -> tuple[str, object]:
    # Injected AFTER skip=1 lets the lookup SELECT through for real (empty, so the method
    # proceeds to mint) -- the marker below fires on the mint's own INSERT.
    return "INSERT INTO entities", lambda: store.get_or_create_abstract_entity(
        "tag:case-get-or-create-abstract"
    )


def _case_get_or_create_entity(store: Store) -> tuple[str, object]:
    return "INSERT INTO entities", lambda: store.get_or_create_entity(
        Descriptor(name="case-get-or-create-entity", file_path="case.py")
    )


def _case_add_fact(store: Store) -> tuple[str, object]:
    return "INSERT INTO facts", lambda: store.add_fact(_fact("case-add-fact"))


def _case_add_binding(store: Store) -> tuple[str, object]:
    entity = store.upsert_entity(Entity(canonical_name="case-binding-entity"))
    decision = store.add_decision(_decision("case-binding-target"))
    return "INSERT INTO anchor_bindings", lambda: store.add_binding(
        AnchorBinding(record_id=decision.id, entity_id=entity.entity_id, tier=2)
    )


def _case_add_domain(store: Store) -> tuple[str, object]:
    return "INSERT INTO domains", lambda: store.add_domain(_domain("case-add-domain"))


def _case_refresh_domain_communities(store: Store) -> tuple[str, object]:
    domain = store.add_domain(_domain("case-refresh-domain"))
    return "INSERT INTO domains", lambda: store.refresh_domain_communities(
        domain.domain_id, ["community-1"]
    )


def _case_ratify(store: Store) -> tuple[str, object]:
    d = _proposed_decision(store, "case-ratify-target")
    return "INSERT INTO decisions", lambda: store.ratify(d.id)


def _case_drop(store: Store) -> tuple[str, object]:
    d = _proposed_decision(store, "case-drop-target")
    return "INSERT INTO decisions", lambda: store.drop(d.id)


def _case_ratify_fact(store: Store) -> tuple[str, object]:
    f = _proposed_fact(store, "case-ratify-fact-target")
    return "INSERT INTO facts", lambda: store.ratify_fact(f.id)


def _case_drop_fact(store: Store) -> tuple[str, object]:
    f = _proposed_fact(store, "case-drop-fact-target")
    return "INSERT INTO facts", lambda: store.drop_fact(f.id)


def _case_mark_captured(store: Store) -> tuple[str, object]:
    return "INSERT OR IGNORE INTO capture_sessions", lambda: store.mark_captured("case-session")


def _case_record_retrieval(store: Store) -> tuple[str, object]:
    return "INSERT INTO retrieval_shows", lambda: store.record_retrieval(["r1"], ["seed1"])


def _case_append_event(store: Store) -> tuple[str, object]:
    return "INSERT INTO retrieval_events", lambda: store._append_event(
        "case-session", "touch", "case.py", "Read"
    )


def _case_record_retrieval_events(store: Store) -> tuple[str, object]:
    return "INSERT INTO retrieval_events", lambda: store.record_retrieval_events(
        "case-session", ["seed1"], [("rec-1", "case.py")]
    )


def _case_prune_retrieval_events(store: Store) -> tuple[str, object]:
    store.record_touch("case-prune-session", "old.py", "Read")
    return "DELETE FROM retrieval_events", lambda: store.prune_retrieval_events(older_than_days=0)


def _case_upsert_initiative(store: Store) -> tuple[str, object]:
    return "INSERT INTO initiatives", lambda: store.upsert_initiative(
        Initiative(name="case-initiative")
    )


def _case_set_meta(store: Store) -> tuple[str, object]:
    return "INSERT INTO meta", lambda: store.set_meta("case-custom-key", "v1")


def _case_compact(store: Store) -> tuple[str, object]:
    old = store.add_decision(_decision("case-compact-old"))
    store.add_decision(_decision("case-compact-new", supersedes=old.id))  # closes `old`
    # The only SQL write compact() ever does is the trailing digest touch (design: archive
    # segment + hot-file removal are filesystem operations, not index rows) -- so that is
    # the one marker available to inject a mid-operation failure here.
    return "INSERT INTO meta", lambda: store.compact()


def _case_supersede_domain(store: Store) -> tuple[str, object]:
    old = store.add_domain(_domain("case-supersede-old"))
    new = _domain("case-supersede-new", supersedes=old.domain_id)
    return "INSERT INTO domains", lambda: store.supersede_domain(old.domain_id, new)


# name -> (store) -> (sql_marker, zero-arg thunk to invoke under the marker patch). Setup
# (creating any prerequisite decision/entity/domain) always happens BEFORE the caller installs
# the patch, over the real connection -- only the returned thunk is meant to run under it.
_ROLLBACK_CASES = {
    "add_decision": _case_add_decision,
    "upsert_entity": _case_upsert_entity,
    "get_or_create_abstract_entity": _case_get_or_create_abstract_entity,
    "get_or_create_entity": _case_get_or_create_entity,
    "add_fact": _case_add_fact,
    "add_binding": _case_add_binding,
    "add_domain": _case_add_domain,
    "refresh_domain_communities": _case_refresh_domain_communities,
    "ratify": _case_ratify,
    "drop": _case_drop,
    "ratify_fact": _case_ratify_fact,
    "drop_fact": _case_drop_fact,
    "mark_captured": _case_mark_captured,
    "record_retrieval": _case_record_retrieval,
    "_append_event": _case_append_event,
    "record_retrieval_events": _case_record_retrieval_events,
    "prune_retrieval_events": _case_prune_retrieval_events,
    "upsert_initiative": _case_upsert_initiative,
    "compact": _case_compact,
    "set_meta": _case_set_meta,
    "supersede_domain": _case_supersede_domain,
}


@pytest.mark.parametrize("method_name", sorted(_write_api_methods() - {"ratify_domains"}))
def test_every_write_method_rolls_back_on_failure(tmp_path, method_name):
    """Every method the mechanical scan calls part of the write API (`_write_api_methods`)
    must roll back on a mid-operation failure -- the behavioral counterpart to the syntactic
    guard above. A method with no case registered fails loudly by name, rather than silently
    dropping out of coverage, so this stays complete as methods are converted (Task 2) or
    added (later)."""
    store = Store(tmp_path / "s")
    case = _ROLLBACK_CASES.get(method_name)
    if case is None:
        pytest.fail(
            f"{method_name!r} is part of the write API (calls _commit or _mutation) but has "
            "no rollback case registered in _ROLLBACK_CASES -- add one so it stays covered"
        )
    marker, thunk = case(store)
    _raise_when_sql_contains(store, marker)
    with pytest.raises(RuntimeError):
        thunk()
    assert store._conn.in_transaction is False, method_name


# -- ratify_domains (D2a): commits per item, not once for the whole batch ---------------------


def test_ratify_domains_commits_per_item_so_one_bad_id_leaves_earlier_items_durable(tmp_path):
    """D2a: one ``_mutation()`` PER ITEM, inside the loop -- not one around the whole method.
    A mid-item failure on the SECOND item must not undo the FIRST item's already-committed
    accept, and must roll back the SECOND item's own (uncommitted) index change instead of
    leaking an open transaction.

    ``ratify_domains``'s per-item ``except ValueError`` does not catch this injected
    ``RuntimeError`` (unchanged by this task -- a non-``ValueError`` failure was always going
    to propagate out of the whole call, so it does here too). What D2a actually changes: that
    propagation does NOT also retroactively undo the FIRST item -- which one ``_mutation()``
    wrapped around the whole method would have done -- and the second item's own partial
    write still rolls back rather than leaking the lock the same way any other write does.
    """
    store = Store(tmp_path / "s")
    good = store.add_domain(_domain("case-ratify-domains-good"))
    bad = store.add_domain(_domain("case-ratify-domains-bad"))

    # Let the FIRST accept's domain-index write through for real (skip=1, so `good` commits
    # normally); the SECOND's (`bad`'s) own index write executes for real too, then raises --
    # a genuine partial write to roll back, not merely an exception with nothing behind it.
    _raise_when_sql_contains(store, "INSERT INTO domains", skip=1)

    with pytest.raises(RuntimeError):
        store.ratify_domains(accept=[good.domain_id, bad.domain_id])

    assert store._conn.in_transaction is False
    # `good`'s accept already committed, in its OWN earlier `_mutation()` scope, before
    # `bad`'s failure -- unaffected by it (the D2a property: per-item, not whole-method).
    assert store.get_domain(good.domain_id).status.value == "accepted"
    # `bad`'s own index row is the one that must have rolled back -- read from THIS store
    # (the index that owns the transaction), not a fresh reread: `bad`'s canonical FILE
    # already flipped durably before the injected failure (write-then-raise, matching where
    # a real failure lands, after `_write_domain_canonical`), so a fresh store would
    # self-heal via `_reload_index_from_canonical` and pick it up off disk -- a different,
    # already-tested concern (see `_reload_index_from_canonical`'s own tests), not what this
    # test pins.
    assert store.get_domain(bad.domain_id).status.value == "proposed"
