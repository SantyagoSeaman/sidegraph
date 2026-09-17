"""Server-side journal writes: seeds and show_anchors (spec D2/D5)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from sidegraph.config import TELEMETRY_SESSION_KEY
from sidegraph.engine.reader import GraphifyReader
from sidegraph.schema import AnchorBinding, Decision, DecisionKind, Descriptor, Entity, Provenance
from sidegraph.server import (
    _add_domain_impl,
    _drill_down_impl,
    _get_task_context_impl,
    _ratify_impl,
)
from sidegraph.store import Store

FIXTURE = Path(__file__).parent / "fixtures" / "mini_graph.json"


def _store_with_anchored_mistake(tmp_path):
    """A gotcha bound to a Trader entity whose descriptor points at `trader/exec.py` —
    the same shape `tests/test_server_telemetry.py` builds."""
    reader = GraphifyReader(FIXTURE)
    store = Store(tmp_path / "t.db")
    entity = store.upsert_entity(
        Entity(
            canonical_name="Trader",
            descriptor=Descriptor(name="Trader", file_path="trader/exec.py"),
        )
    )
    decision = Decision(
        title="deadlock",
        kind=DecisionKind.GOTCHA,
        context="c",
        choice="order locks",
        valid_from=datetime.now(UTC),
        provenance=Provenance(source="manual"),
    )
    store._write_decision(decision)
    store._conn.commit()
    store.add_binding(AnchorBinding(record_id=decision.id, entity_id=entity.entity_id, tier=2))
    return store, reader, decision


def _publish_key(store, session_id="sess-1", age=timedelta(0)):
    store.set_meta(
        TELEMETRY_SESSION_KEY,
        f"{session_id}|{(datetime.now(UTC) - age).isoformat()}",
    )


def _store_with_domain_decision(tmp_path):
    """A domain decision reachable via `drill_down` (same construction
    `tests/test_server_telemetry.py::_store_with_domain_decision` uses), ALSO bound to a
    file entity: `get_or_create_abstract_entity` mints the `domain:<slug>` entity itself
    with no `descriptor` (no file), so a decision anchored to it alone would produce no
    `show_anchor` event to assert against. The second, file-anchored binding gives
    `_anchor_paths` something to resolve regardless of which path `drill_down` used to find
    the decision -- `_anchor_paths` walks ALL of a record's bindings, not just the one that
    made it reachable."""
    store = Store(tmp_path / "t.db")
    dom = _add_domain_impl(store, None, slug="payments", title="Payments", summary="s.")
    _ratify_impl(store, accept=[dom["domain_id"]])
    decision = Decision(
        title="use postgres",
        kind=DecisionKind.ADR,
        context="c",
        choice="ch",
        valid_from=datetime.now(UTC),
        provenance=Provenance(source="manual"),
    )
    store._write_decision(decision)
    store._conn.commit()
    domain_entity = store.find_abstract_entity("domain:payments")
    store.add_binding(
        AnchorBinding(
            record_id=decision.id, entity_id=domain_entity.entity_id, tier=1, status="live"
        )
    )
    file_entity = store.upsert_entity(
        Entity(
            canonical_name="Ledger",
            descriptor=Descriptor(name="Ledger", file_path="payments/ledger.py"),
        )
    )
    store.add_binding(
        AnchorBinding(record_id=decision.id, entity_id=file_entity.entity_id, tier=2, status="live")
    )
    return store, decision


def test_drill_down_domain_seed_writes_no_seed_event(tmp_path):
    """Spec §4 Storage, unexercised until now: "A seed with no file (`drill_down`'s
    `domain:<slug>`) writes no event." `tests/test_server_telemetry.py`'s own drill_down
    test never publishes a session key, so it never reaches the journal write at all --
    this is the first test to exercise the journal path for `drill_down`. `show_anchor`
    events must still land (the domain seed is filtered, not the whole call); paired with
    the plain `seed` assertion so a writer that dropped every seed -- not just domain ones
    -- would also fail this test, not just pass it vacuously."""
    store, decision = _store_with_domain_decision(tmp_path)
    _publish_key(store)

    out = _drill_down_impl(store, None, "payments")

    assert out["found"] is True
    events = store.retrieval_events("sess-1")
    assert [e for e in events if e["kind"] == "seed"] == []
    assert ("show_anchor", "payments/ledger.py", decision.id) in [
        (e["kind"], e["key"], e["detail"]) for e in events
    ]
    store.close()


def test_a_retrieval_writes_seed_and_show_anchor_events(tmp_path):
    store, reader, decision = _store_with_anchored_mistake(tmp_path)
    _publish_key(store)

    _get_task_context_impl(store, reader, ["trader/exec.py"], None, 4000, 6000)

    events = store.retrieval_events("sess-1")
    assert ("seed", "trader/exec.py", None) in [(e["kind"], e["key"], e["detail"]) for e in events]
    assert ("show_anchor", "trader/exec.py", decision.id) in [
        (e["kind"], e["key"], e["detail"]) for e in events
    ]
    store.close()


def test_no_key_writes_no_events_but_still_writes_the_counters(tmp_path):
    """Paired (spec row 2): the absence half alone is green today. An event that can never
    correlate is noise — but the aggregate counters must keep working regardless."""
    store, reader, decision = _store_with_anchored_mistake(tmp_path)

    _get_task_context_impl(store, reader, ["trader/exec.py"], None, 4000, 6000)

    assert store.retrieval_events() == []
    assert store.retrieval_shows().get(decision.id) == 1

    _publish_key(store)
    _get_task_context_impl(store, reader, ["trader/exec.py"], None, 4000, 6000)
    assert store.retrieval_events("sess-1") != []
    store.close()


def test_a_stale_or_malformed_key_writes_no_events_while_a_fresh_one_does(tmp_path):
    store, reader, _ = _store_with_anchored_mistake(tmp_path)

    _publish_key(store, age=timedelta(hours=13))
    _get_task_context_impl(store, reader, ["trader/exec.py"], None, 4000, 6000)
    assert store.retrieval_events() == []

    store.set_meta(TELEMETRY_SESSION_KEY, "no-separator-and-no-timestamp")
    _get_task_context_impl(store, reader, ["trader/exec.py"], None, 4000, 6000)
    assert store.retrieval_events() == []

    _publish_key(store)
    _get_task_context_impl(store, reader, ["trader/exec.py"], None, 4000, 6000)
    assert store.retrieval_events("sess-1") != []
    store.close()


def test_telemetry_off_writes_no_events_while_on_writes_them(tmp_path, monkeypatch):
    store, reader, _ = _store_with_anchored_mistake(tmp_path)
    _publish_key(store)

    monkeypatch.setenv("SIDEGRAPH_TELEMETRY", "off")
    render = _get_task_context_impl(store, reader, ["trader/exec.py"], None, 4000, 6000)
    assert store.retrieval_events() == []
    assert isinstance(render, str) and render, "retrieval still returns its render"

    monkeypatch.delenv("SIDEGRAPH_TELEMETRY")
    _get_task_context_impl(store, reader, ["trader/exec.py"], None, 4000, 6000)
    assert store.retrieval_events("sess-1") != []
    store.close()


def test_an_absolute_seed_is_stored_repo_relative(tmp_path):
    """The blocker a fresh reviewer caught after three rounds missed it. Seeds come from
    agent input verbatim; an agent that passes absolute paths writes absolute seed keys, the
    seed set stops matching any anchor, and §1's circularity defense fails — every seeded
    anchor touched later counts as a redirect, inflating the deliverable in the flattering
    direction."""
    store, reader, _ = _store_with_anchored_mistake(tmp_path)
    _publish_key(store)
    absolute = str(Path(store.path).parent / "trader" / "exec.py")

    _get_task_context_impl(store, reader, [absolute], None, 4000, 6000)

    seeds = [e["key"] for e in store.retrieval_events("sess-1") if e["kind"] == "seed"]
    assert seeds == ["trader/exec.py"], f"got {seeds}"
    store.close()


def test_an_absolute_seed_normalizes_the_same_way_in_both_tables(tmp_path):
    """Fix-wave finding: `_record` wrote RAW seeds into the aggregate `retrieval_seeds`
    counter and NORMALIZED seeds into the `retrieval_events` journal from the SAME call --
    probed as `get_task_context(files=["/abs/.../trader/exec.py"])` producing
    `retrieval_events.seed == "trader/exec.py"` but `retrieval_seeds.seed ==
    "/abs/.../trader/exec.py"`. Doctor's `never-surfaced` check joins `retrieval_seeds.seed`
    against the repo-relative `descriptor.file_path`, so an agent that seeds with absolute
    paths permanently read 0 "people asked there" for that file -- the counter is
    cumulative and never resets. Both tables must see the same normalized key."""
    store, reader, _ = _store_with_anchored_mistake(tmp_path)
    _publish_key(store)
    absolute = str(Path(store.path).parent / "trader" / "exec.py")

    _get_task_context_impl(store, reader, [absolute], None, 4000, 6000)

    assert store.retrieval_seed_queries() == {"trader/exec.py": 1}
    journal_seeds = [e["key"] for e in store.retrieval_events("sess-1") if e["kind"] == "seed"]
    assert journal_seeds == ["trader/exec.py"]
    store.close()


def test_an_out_of_root_seed_is_dropped_while_an_in_root_absolute_one_is_kept(tmp_path):
    """Direction matters and is OPPOSITE to the touch rule: dropping a touch only
    under-counts, but dropping a seed shrinks the seed set and OVER-counts redirects. So
    normalize seeds rather than discarding them; discard only what no anchor could ever
    equal in any form."""
    store, reader, _ = _store_with_anchored_mistake(tmp_path)
    _publish_key(store)
    root = Path(store.path).parent
    # `root` is `Path(store.path).parent`, i.e. `tmp_path` itself here (the store lives at
    # `tmp_path/"t.db"`, mirroring production's `<repo_root>/.sidegraph`) — so an "outside"
    # path must live outside `tmp_path`, not merely in a different subdirectory of it (a
    # sibling of "trader/" under the SAME tmp_path would still normalize to an in-root
    # relative path and contradict this test's own name). `tmp_path.parent` is pytest's
    # shared per-session temp root, genuinely outside this test's store root.
    outside = str(tmp_path.parent / "elsewhere" / "other.py")

    _get_task_context_impl(store, reader, [outside], None, 4000, 6000)
    assert [e for e in store.retrieval_events("sess-1") if e["kind"] == "seed"] == []

    _get_task_context_impl(store, reader, [str(root / "trader" / "exec.py")], None, 4000, 6000)
    seeds = [e["key"] for e in store.retrieval_events("sess-1") if e["kind"] == "seed"]
    assert seeds == ["trader/exec.py"]
    store.close()


def test_an_event_write_failure_does_not_fail_the_retrieval(tmp_path, monkeypatch):
    store, reader, _ = _store_with_anchored_mistake(tmp_path)
    _publish_key(store)

    def boom(*args, **kwargs):
        raise RuntimeError("journal is on fire")

    monkeypatch.setattr(Store, "record_retrieval_events", boom)
    render = _get_task_context_impl(store, reader, ["trader/exec.py"], None, 4000, 6000)
    assert isinstance(render, str) and render
    store.close()
