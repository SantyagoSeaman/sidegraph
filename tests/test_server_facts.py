"""Direct fact write paths (add_fact / supersede_fact) + propose facts param.
see design/superpowers/specs/2026-07-10-facts-layer-design.md"""

from __future__ import annotations

from sidegraph.schema import AnchorBinding, DecisionStatus
from sidegraph.server import (
    _add_decision_impl,
    _add_fact_impl,
    _get_entity_history_impl,
    _list_facts_impl,
    _propose_decisions_impl,
    _supersede_fact_impl,
    supersede_fact,
)
from sidegraph.store import Store

SECRET = "api_key=hunter2secret"


def test_add_fact_lands_accepted_and_redacts(tmp_path):
    store = Store(tmp_path / "srv.db")
    # An anchor is required (design D8): a bare add_fact with no anchor and no supports is
    # unreachable the moment it lands (see test_fact_reachability_gate.py) -- irrelevant to
    # what THIS test pins (redaction), so any anchor satisfies it.
    out = _add_fact_impl(
        store,
        None,
        statement=f"limit is 100 rps ({SECRET})",
        source=f"vendor docs {SECRET}",
        anchors=[{"name": "vendor.py", "file_path": "src/vendor.py"}],
    )
    assert out["redactions"] == 2
    fact = store.get_fact(out["id"])
    assert fact.status == DecisionStatus.ACCEPTED
    assert "hunter2secret" not in fact.statement + fact.source


def test_add_fact_no_graph_binds_orphaned_leaf(tmp_path):
    # Unlike add_decision (which drops anchors without a graph), add_fact writes an
    # orphaned leaf that heals later — the spec forbids repeating that asymmetry.
    store = Store(tmp_path / "srv.db")
    out = _add_fact_impl(
        store,
        None,
        statement="s",
        source="src",
        anchors=[{"name": "sync.py", "file_path": "src/sync.py"}],
    )
    bindings = store.bindings_for_record(out["id"])
    assert len(bindings) == 1 and bindings[0].status == "orphaned"


def test_add_fact_validates_supports(tmp_path):
    store = Store(tmp_path / "srv.db")
    try:
        _add_fact_impl(store, None, statement="s", source="src", supports=["01NOPE"])
        raise AssertionError("expected ValueError")
    except ValueError:
        pass


# -- I1 (R1 improvement wave §1): _add_fact_impl gains the D7.3 session_id fallback --------


def test_add_fact_impl_falls_back_to_telemetry_session_key_when_fresh(tmp_path):
    from datetime import UTC, datetime

    from sidegraph.config import TELEMETRY_SESSION_KEY

    store = Store(tmp_path / "srv.db")
    store.set_meta(TELEMETRY_SESSION_KEY, f"session-xyz|{datetime.now(UTC).isoformat()}")

    out = _add_fact_impl(
        store, None, statement="s", source="src", anchors=[{"name": "a.py", "file_path": "a.py"}]
    )

    assert store.get_fact(out["id"]).provenance.session_id == "session-xyz"


def test_add_fact_impl_explicit_session_id_wins_over_fallback(tmp_path):
    from datetime import UTC, datetime

    from sidegraph.config import TELEMETRY_SESSION_KEY

    store = Store(tmp_path / "srv.db")
    store.set_meta(TELEMETRY_SESSION_KEY, f"marker-session|{datetime.now(UTC).isoformat()}")

    out = _add_fact_impl(
        store,
        None,
        statement="s",
        source="src",
        anchors=[{"name": "a.py", "file_path": "a.py"}],
        session_id="explicit-session",
    )

    assert store.get_fact(out["id"]).provenance.session_id == "explicit-session"


def test_add_fact_impl_ignores_stale_telemetry_session_marker(tmp_path):
    """Window-expired twin (declared exception): passes before AND after."""
    from datetime import UTC, datetime, timedelta

    from sidegraph.config import TELEMETRY_SESSION_KEY

    store = Store(tmp_path / "srv.db")
    stale = (datetime.now(UTC) - timedelta(hours=25)).isoformat()
    store.set_meta(TELEMETRY_SESSION_KEY, f"stale-session|{stale}")

    out = _add_fact_impl(
        store, None, statement="s", source="src", anchors=[{"name": "a.py", "file_path": "a.py"}]
    )

    assert store.get_fact(out["id"]).provenance.session_id is None


def test_supersede_fact_inherits_bindings_verbatim(tmp_path):
    store = Store(tmp_path / "srv.db")
    old = _add_fact_impl(
        store,
        None,
        statement="old",
        source="src",
        anchors=[{"name": "sync.py", "file_path": "src/sync.py", "relation": "creates"}],
    )
    new = _supersede_fact_impl(store, None, old_fact_id=old["id"], statement="new", source="src2")
    old_b = store.bindings_for_record(old["id"])
    new_b = store.bindings_for_record(new["id"])
    assert {(b.entity_id, b.tier, b.weight, b.relation, b.status) for b in new_b} == {
        (b.entity_id, b.tier, b.weight, b.relation, b.status) for b in old_b
    }
    assert store.get_fact(old["id"]).status == DecisionStatus.SUPERSEDED


def test_supersede_fact_explicit_anchors_no_reader_binds_orphaned(tmp_path):
    # Proves the explicit-anchors branch dispatches through _bind_fact_anchors (which
    # binds an orphaned leaf with no reader), not _resolve_anchors' silent no-op.
    store = Store(tmp_path / "srv.db")
    # `old` needs its own anchor (design D8) purely to satisfy the reachability gate --
    # irrelevant to what this test pins (the successor's explicit-anchors dispatch).
    old = _add_fact_impl(
        store,
        None,
        statement="old",
        source="src",
        anchors=[{"name": "old.py", "file_path": "src/old.py"}],
    )
    new = _supersede_fact_impl(
        store,
        None,
        old_fact_id=old["id"],
        statement="new",
        source="src2",
        anchors=[{"name": "sync.py", "file_path": "src/sync.py"}],
    )
    bindings = store.bindings_for_record(new["id"])
    assert len(bindings) == 1 and bindings[0].status == "orphaned"


# -- I1 (R1 improvement wave §1): _supersede_fact_impl gains session_id/author + the D7.3
# fallback. Measured defect: no session_id/author params existed at all, despite the
# docstring claiming this "Mirrors _supersede_decision_impl exactly" -- the params must
# exist before a mirror is possible. NOTE (deviation from the spec's literal "session_id/
# author/source" wording): `source` here already names the FACT's own epistemics text
# (e.g. "benchmark run") -- the same collision `_add_fact_impl`/`add_fact` already avoid by
# never exposing a provenance-source override (hardcoded "human", the human-asked path).
# This mirrors that precedent: session_id/author only, provenance source stays "human".


def test_supersede_fact_impl_accepts_session_id_and_author_params(tmp_path):
    store = Store(tmp_path / "srv.db")
    old = _add_fact_impl(
        store, None, statement="old", source="src", anchors=[{"name": "a.py", "file_path": "a.py"}]
    )

    new = _supersede_fact_impl(
        store,
        None,
        old_fact_id=old["id"],
        statement="new",
        source="src2",
        session_id="sess-1",
        author="alex",
    )

    replacement = store.get_fact(new["id"])
    assert replacement.provenance.session_id == "sess-1"
    assert replacement.provenance.author == "alex"


def test_supersede_fact_impl_falls_back_to_telemetry_session_key_when_fresh(tmp_path):
    from datetime import UTC, datetime

    from sidegraph.config import TELEMETRY_SESSION_KEY

    store = Store(tmp_path / "srv.db")
    old = _add_fact_impl(
        store, None, statement="old", source="src", anchors=[{"name": "a.py", "file_path": "a.py"}]
    )
    store.set_meta(TELEMETRY_SESSION_KEY, f"session-xyz|{datetime.now(UTC).isoformat()}")

    new = _supersede_fact_impl(store, None, old_fact_id=old["id"], statement="new", source="src2")

    assert store.get_fact(new["id"]).provenance.session_id == "session-xyz"


def test_supersede_fact_impl_explicit_session_id_wins_over_fallback(tmp_path):
    from datetime import UTC, datetime

    from sidegraph.config import TELEMETRY_SESSION_KEY

    store = Store(tmp_path / "srv.db")
    old = _add_fact_impl(
        store, None, statement="old", source="src", anchors=[{"name": "a.py", "file_path": "a.py"}]
    )
    store.set_meta(TELEMETRY_SESSION_KEY, f"marker-session|{datetime.now(UTC).isoformat()}")

    new = _supersede_fact_impl(
        store,
        None,
        old_fact_id=old["id"],
        statement="new",
        source="src2",
        session_id="explicit-session",
    )

    assert store.get_fact(new["id"]).provenance.session_id == "explicit-session"


def test_supersede_fact_impl_ignores_stale_telemetry_session_marker(tmp_path):
    """Window-expired twin (declared exception): passes before AND after."""
    from datetime import UTC, datetime, timedelta

    from sidegraph.config import TELEMETRY_SESSION_KEY

    store = Store(tmp_path / "srv.db")
    old = _add_fact_impl(
        store, None, statement="old", source="src", anchors=[{"name": "a.py", "file_path": "a.py"}]
    )
    stale = (datetime.now(UTC) - timedelta(hours=25)).isoformat()
    store.set_meta(TELEMETRY_SESSION_KEY, f"stale-session|{stale}")

    new = _supersede_fact_impl(store, None, old_fact_id=old["id"], statement="new", source="src2")

    assert store.get_fact(new["id"]).provenance.session_id is None


def test_supersede_fact_mcp_tool_wrapper_passes_session_id_and_author(tmp_path, monkeypatch):
    """The MCP tool surface (`supersede_fact`, not `_supersede_fact_impl`) gains the same
    optional params, additive for MCP callers (spec §6 blast radius)."""
    import sidegraph.server as server_module

    store = Store(tmp_path / "srv.db")
    monkeypatch.setattr(server_module, "_store", store)
    old = _add_fact_impl(
        store, None, statement="old", source="src", anchors=[{"name": "a.py", "file_path": "a.py"}]
    )

    out = supersede_fact(
        old_fact_id=old["id"],
        statement="new",
        source="src2",
        session_id="sess-mcp",
        author="alex",
    )

    assert store.get_fact(out["id"]).provenance.session_id == "sess-mcp"
    assert store.get_fact(out["id"]).provenance.author == "alex"


def test_propose_decisions_tool_accepts_standalone_facts(tmp_path):
    store = Store(tmp_path / "srv.db")
    results = _propose_decisions_impl(
        store,
        None,
        drafts=[{"title": "t", "kind": "adr", "context": "c", "choice": "ch"}],
        facts=[
            {"statement": "s", "source": "src", "anchors": [{"name": "a.py", "file_path": "a.py"}]}
        ],
    )
    assert any("fact_id" in r for r in results)
    # standalone-fact results are appended AFTER the decision results, never interleaved.
    assert "fact_id" in results[-1]


def test_list_facts_default_excludes_terminal(tmp_path):
    store = Store(tmp_path / "srv.db")
    # Both need an anchor (design D8's reachability gate) -- irrelevant to what this test
    # pins (list_facts' terminal-exclusion), any anchor satisfies it.
    kept = _add_fact_impl(
        store, None, statement="keep", source="s", anchors=[{"name": "k.py", "file_path": "k.py"}]
    )
    superseded = _add_fact_impl(
        store, None, statement="old", source="s2", anchors=[{"name": "o.py", "file_path": "o.py"}]
    )
    _supersede_fact_impl(store, None, superseded["id"], statement="new", source="s3")
    out = _list_facts_impl(store)
    ids = [f["id"] for f in out]
    assert kept["id"] in ids
    assert superseded["id"] not in ids
    assert all("statement" in f and "status" in f for f in out)
    everything = _list_facts_impl(store, include_superseded=True)
    assert superseded["id"] in [f["id"] for f in everything]


def test_list_facts_newest_first(tmp_path):
    store = Store(tmp_path / "srv.db")
    # Anchors satisfy design D8's reachability gate -- irrelevant to what this test pins
    # (list_facts' ordering).
    first = _add_fact_impl(
        store, None, statement="a", source="s", anchors=[{"name": "a.py", "file_path": "a.py"}]
    )
    second = _add_fact_impl(
        store, None, statement="b", source="s", anchors=[{"name": "b.py", "file_path": "b.py"}]
    )
    out = _list_facts_impl(store)
    assert [f["id"] for f in out][:2] == [second["id"], first["id"]] or (
        out[0]["valid_from"] >= out[1]["valid_from"]
    )


def test_get_entity_history_includes_facts_with_record_type(tmp_path):
    store = Store(tmp_path / "srv.db")

    # The fact's own anchor mints (and orphan-binds) the entity — add_fact writes an
    # orphaned Tier-2 leaf even with no reader (unlike add_decision, which no-ops
    # anchoring entirely without one — see test_add_fact_no_graph_binds_orphaned_leaf
    # above). The decision is bound to that SAME entity directly, since
    # _add_decision_impl's own anchoring is a no-op with reader=None; the point pinned
    # here is "one decision + one fact bound to the same entity, both in history, typed",
    # not the anchoring path itself.
    f = _add_fact_impl(
        store,
        None,
        statement="s",
        source="src",
        anchors=[{"name": "fee_gate.py", "file_path": "src/fee_gate.py"}],
    )
    entity_id = store.bindings_for_record(f["id"])[0].entity_id

    d = _add_decision_impl(store, None, title="t", kind="adr", context="c", choice="ch")
    store.add_binding(AnchorBinding(record_id=d["id"], entity_id=entity_id, tier=2))

    history = _get_entity_history_impl(store, entity_id)
    types = {h["id"]: h["record_type"] for h in history}
    assert types.get(d["id"]) == "decision"
    assert types.get(f["id"]) == "fact"  # was silently dropped before this wave
