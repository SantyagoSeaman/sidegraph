"""Facts through the deterministic propose pipeline.
see design/superpowers/specs/2026-07-10-facts-layer-design.md"""

import pytest

from sidegraph.capture import propose, propose_facts
from sidegraph.schema import DecisionStatus
from sidegraph.store import Store

SECRET = "api_key=hunter2secret"


@pytest.fixture(autouse=True)
def _no_ambient_initiative(monkeypatch):
    """Tests must not depend on the ambient git branch (mirrors
    test_capture_propose.py's identical fixture): without this, `_derive_initiative()`
    picks up whatever branch the repo happens to be checked out on and silently adds a
    Tier-0 binding to the DECISION only (facts never inherit `initiative`, only
    `anchors`) — which breaks the binding-equality assertions below in an
    environment-dependent way (passes on `main`, fails on any feature branch).
    """
    monkeypatch.setattr("sidegraph.capture._derive_initiative", lambda: None)


def _draft(**kw):
    base = dict(
        title="use httpx",
        kind="adr",
        context="why",
        choice="httpx",
        anchors=[{"name": "client.py", "file_path": "src/client.py"}],
    )
    base.update(kw)
    return base


def test_attached_fact_written_proposed_with_supports(tmp_path):
    store = Store(tmp_path / "s")
    [res] = propose(
        [_draft(facts=[{"statement": "httpx has no built-in retry", "source": "httpx docs"}])],
        store,
        reader=None,
        session_id="sess-1",
    )
    assert res.status == "written"
    [fres] = res.facts
    assert fres.status == "written"
    fact = store.get_fact(fres.fact_id)
    assert fact.status == DecisionStatus.PROPOSED
    assert fact.supports == [res.decision_id]
    assert fact.provenance.session_id == "sess-1"


def test_attached_fact_inherits_decision_anchors_as_own_bindings(tmp_path):
    store = Store(tmp_path / "s")
    [res] = propose(
        [_draft(facts=[{"statement": "s", "source": "src"}])],
        store,
        reader=None,
    )
    [fres] = res.facts
    fact_bindings = store.bindings_for_record(fres.fact_id)
    decision_bindings = store.bindings_for_record(res.decision_id)
    assert len(fact_bindings) == len(decision_bindings) >= 1
    assert {b.entity_id for b in fact_bindings} == {b.entity_id for b in decision_bindings}


def test_attached_fact_own_anchors_override_inheritance(tmp_path):
    store = Store(tmp_path / "s")
    [res] = propose(
        [
            _draft(
                facts=[
                    {
                        "statement": "s",
                        "source": "src",
                        "anchors": [{"name": "other.py", "file_path": "src/other.py"}],
                    }
                ]
            )
        ],
        store,
        reader=None,
    )
    [fres] = res.facts
    names = set()
    for b in store.bindings_for_record(fres.fact_id):
        e = store.get_entity(b.entity_id)
        names.add(e.canonical_name)
    assert "other.py" in names and "client.py" not in names


def test_standalone_fact_requires_anchor_or_supports(tmp_path):
    store = Store(tmp_path / "s")
    [bad] = propose_facts([{"statement": "s", "source": "src"}], store, reader=None)
    assert bad.status == "rejected" and "anchor" in bad.reason.lower()


def test_standalone_fact_with_anchor_written(tmp_path):
    store = Store(tmp_path / "s")
    [ok] = propose_facts(
        [
            {
                "statement": "s",
                "source": "src",
                "anchors": [{"name": "sync.py", "file_path": "src/sync.py"}],
            }
        ],
        store,
        reader=None,
    )
    assert ok.status == "written"
    assert store.get_fact(ok.fact_id).supports == []


def test_standalone_fact_supports_existing_decision(tmp_path):
    store = Store(tmp_path / "s")
    [dres] = propose([_draft()], store, reader=None)
    [ok] = propose_facts(
        [{"statement": "s", "source": "src", "supports": [dres.decision_id]}],
        store,
        reader=None,
    )
    assert ok.status == "written"
    [got] = store.facts_for_decision(dres.decision_id)
    assert got.id == ok.fact_id


def test_fact_redaction_counts(tmp_path):
    store = Store(tmp_path / "s")
    [res] = propose(
        [_draft(facts=[{"statement": f"leaked {SECRET}", "source": f"log {SECRET}"}])],
        store,
        reader=None,
    )
    [fres] = res.facts
    assert fres.redactions == 2
    stored = store.get_fact(fres.fact_id)
    assert "hunter2secret" not in stored.statement + stored.source


def test_fact_dedup_same_statement_shared_entity(tmp_path):
    store = Store(tmp_path / "s")
    draft_fact = {"statement": "httpx has no retry", "source": "docs"}
    [r1] = propose([_draft(facts=[draft_fact])], store, reader=None)
    [r2] = propose([_draft(title="second decision", facts=[draft_fact])], store, reader=None)
    assert r1.facts[0].status == "written"
    assert r2.facts[0].status == "deduped"


def test_bad_fact_does_not_abort_decision_or_siblings(tmp_path):
    store = Store(tmp_path / "s")
    [res] = propose(
        [
            _draft(
                facts=[
                    {"statement": "", "source": "docs"},  # invalid: empty statement
                    {"statement": "good fact", "source": "docs"},
                ]
            )
        ],
        store,
        reader=None,
    )
    assert res.status == "written"
    assert [f.status for f in res.facts] == ["rejected", "written"]


def test_standalone_fact_reports_unresolved_anchor_as_orphaned(tmp_path):
    """The fact half of the orphaned-anchor report. Written because mutating this branch
    alone left the entire suite green: the code existed and nothing bit on it, which is the
    same silence the feature was added to end."""
    from pathlib import Path

    from sidegraph.engine.reader import GraphifyReader

    store = Store(tmp_path / "s")
    reader = GraphifyReader(Path(__file__).parent / "fixtures" / "mini_graph.json")
    [ok] = propose_facts(
        [
            {
                "statement": "s",
                "source": "src",
                "anchors": [{"name": "no_such_symbol_anywhere", "file_path": "trader/exec.py"}],
            }
        ],
        store,
        reader,
    )
    assert ok.status == "written"
    assert [e["canonical_name"] for e in ok.anchors_orphaned] == ["no_such_symbol_anywhere"]
    assert ok.anchors_skipped == []


def test_standalone_fact_resolved_anchor_reports_no_orphans(tmp_path):
    from pathlib import Path

    from sidegraph.engine.reader import GraphifyReader

    store = Store(tmp_path / "s")
    reader = GraphifyReader(Path(__file__).parent / "fixtures" / "mini_graph.json")
    [ok] = propose_facts(
        [
            {
                "statement": "s",
                "source": "src",
                "anchors": [{"name": "Trader", "file_path": "trader/exec.py"}],
            }
        ],
        store,
        reader,
    )
    assert ok.status == "written"
    assert ok.anchors_orphaned == []


def test_standalone_fact_without_a_graph_reports_its_orphaned_anchor(tmp_path):
    """No reader at all: the leaf is bound orphaned (never dropped) and must be reported —
    a graph-less run produces a dead anchor exactly as an unresolvable name does."""
    store = Store(tmp_path / "s")
    [ok] = propose_facts(
        [
            {
                "statement": "s",
                "source": "src",
                "anchors": [{"name": "sync.py", "file_path": "src/sync.py"}],
            }
        ],
        store,
        reader=None,
    )
    assert ok.status == "written"
    assert [e["canonical_name"] for e in ok.anchors_orphaned] == ["sync.py"]


# -- I1 (R1 improvement wave §1): _propose_fact_one's own D7.3 session_id fallback --------
# Previously wired into _propose_one only; a STANDALONE propose_facts draft (never routed
# through _propose_one -- both R1 facts came through here per the design note's measured
# defect table) landed session_id=None even with a fresh marker present.


def test_standalone_fact_falls_back_to_telemetry_session_key_when_fresh(tmp_path):
    from datetime import UTC, datetime

    from sidegraph.config import TELEMETRY_SESSION_KEY

    store = Store(tmp_path / "s")
    store.set_meta(TELEMETRY_SESSION_KEY, f"session-xyz|{datetime.now(UTC).isoformat()}")

    [ok] = propose_facts(
        [
            {
                "statement": "s",
                "source": "src",
                "anchors": [{"name": "sync.py", "file_path": "src/sync.py"}],
            }
        ],
        store,
        reader=None,
    )
    assert ok.status == "written"
    assert store.get_fact(ok.fact_id).provenance.session_id == "session-xyz"


def test_standalone_fact_explicit_session_id_wins_over_fallback(tmp_path):
    from datetime import UTC, datetime

    from sidegraph.config import TELEMETRY_SESSION_KEY

    store = Store(tmp_path / "s")
    store.set_meta(TELEMETRY_SESSION_KEY, f"marker-session|{datetime.now(UTC).isoformat()}")

    [ok] = propose_facts(
        [
            {
                "statement": "s",
                "source": "src",
                "anchors": [{"name": "sync.py", "file_path": "src/sync.py"}],
            }
        ],
        store,
        reader=None,
        session_id="explicit-session",
    )
    assert store.get_fact(ok.fact_id).provenance.session_id == "explicit-session"


def test_standalone_fact_ignores_stale_telemetry_session_marker(tmp_path):
    """Window-expired twin (declared exception, T-I2c-style): passes before AND after --
    guards over-reach the same way test_capture_propose.py's stale-marker test does."""
    from datetime import UTC, datetime, timedelta

    from sidegraph.config import TELEMETRY_SESSION_KEY

    store = Store(tmp_path / "s")
    stale = (datetime.now(UTC) - timedelta(hours=25)).isoformat()
    store.set_meta(TELEMETRY_SESSION_KEY, f"stale-session|{stale}")

    [ok] = propose_facts(
        [
            {
                "statement": "s",
                "source": "src",
                "anchors": [{"name": "sync.py", "file_path": "src/sync.py"}],
            }
        ],
        store,
        reader=None,
    )
    assert store.get_fact(ok.fact_id).provenance.session_id is None


def test_standalone_fact_without_a_graph_reasons_no_graph(tmp_path):
    """The no-reader branch has its own cause: nothing is wrong with the anchor, there is
    simply no graph to resolve against. Reporting it as ``file-not-in-graph`` would send the
    author to re-check a name that is probably fine."""
    store = Store(tmp_path / "s")
    [ok] = propose_facts(
        [
            {
                "statement": "s",
                "source": "src",
                "anchors": [{"name": "sync.py", "file_path": "src/sync.py"}],
            }
        ],
        store,
        reader=None,
    )
    assert [e["reason"] for e in ok.anchors_orphaned] == ["no-graph"]
