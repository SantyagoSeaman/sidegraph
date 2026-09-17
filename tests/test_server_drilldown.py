"""MCP `drill_down` — thin server wrapper over `retrieval.drill_down` (M5)."""

from __future__ import annotations

from pathlib import Path

from sidegraph.engine.reader import GraphifyReader
from sidegraph.server import _add_domain_impl, _drill_down_impl, _ratify_impl
from sidegraph.store import Store

FIXTURE = Path(__file__).parent / "fixtures" / "mini_graph.json"


def test_drill_down_impl_unknown_slug(tmp_path):
    store = Store(tmp_path / "t.db")
    out = _drill_down_impl(store, None, "no-such-slug")
    assert out == {"found": False, "candidates": []}


def test_drill_down_impl_found_domain(tmp_path):
    store = Store(tmp_path / "t.db")
    dom = _add_domain_impl(store, None, slug="payments", title="Payments", summary="s.")
    _ratify_impl(store, accept=[dom["domain_id"]])

    out = _drill_down_impl(store, None, "payments")
    assert out["found"] is True
    assert out["domain"]["slug"] == "payments"
    assert out["domain"]["status"] == "accepted"  # M6 review fold-in
    assert out["members"] == []
    assert "note" in out
    # telemetry-only key: _drill_down_impl must pop it before returning, so it never
    # reaches an agent through the MCP tool's documented contract (retrieval-telemetry
    # review fold-in — the pop was previously verified by inspection only).
    assert "decision_ids" not in out


def test_drill_down_impl_status_reflects_unratified_domain(tmp_path):
    """A domain resolved by ``find_domain_by_slug`` need not be accepted yet (it also
    matches proposed/dropped) -- ``status`` must reflect whatever was actually found."""
    store = Store(tmp_path / "t.db")
    _add_domain_impl(store, None, slug="shipping", title="Shipping", summary="s.")

    out = _drill_down_impl(store, None, "shipping")
    assert out["domain"]["status"] == "proposed"


def test_drill_down_impl_members_with_reader(tmp_path):
    store = Store(tmp_path / "t.db")
    reader = GraphifyReader(FIXTURE)
    dom = _add_domain_impl(
        store,
        reader,
        slug="trading",
        title="Trading",
        summary="s.",
        communities=["1"],
    )
    _ratify_impl(store, accept=[dom["domain_id"]])

    out = _drill_down_impl(store, reader, "trading")
    assert any("Trader" in m for m in out["members"])


def test_drill_down_tool_triggers_lazy_sync(tmp_path, monkeypatch):
    """M6 review fold-in: the ``drill_down`` MCP tool must use ``_synced_reader()`` like
    every other retrieval-facing tool (get_task_context/query_structure/query_decisions),
    not the un-synced ``_load_reader()`` -- verified the same way test_sync_wiring.py
    verifies session_start/get_task_context: a real sync run stamps LAST_SYNCED_KEY."""
    import importlib

    from sidegraph.sync import LAST_SYNCED_KEY

    db = tmp_path / "t.db"
    # SIDEGRAPH_DIR (not the deprecated SIDEGRAPH_DB) so `db` is used LITERALLY -- no
    # legacy-dispatch redirection to its parent when it doesn't exist yet (see
    # config._dispatch_sidegraph_db); back-compat itself is covered by tests/test_config.py.
    monkeypatch.setenv("SIDEGRAPH_DIR", str(db))
    monkeypatch.setenv("SIDEGRAPH_GRAPH", str(FIXTURE))

    import sidegraph.server as srv

    importlib.reload(srv)  # rebind module _store to env
    try:
        dom = srv._add_domain_impl(
            srv._get_store(),
            GraphifyReader(FIXTURE),
            slug="trading",
            title="Trading",
            summary="s.",
            communities=["1"],
        )
        srv._ratify_impl(srv._get_store(), accept=[dom["domain_id"]])
        srv.drill_down("trading")
        assert Store(db).get_meta(LAST_SYNCED_KEY).startswith("abc123:")
    finally:
        monkeypatch.delenv("SIDEGRAPH_DIR", raising=False)
        monkeypatch.delenv("SIDEGRAPH_GRAPH", raising=False)
        importlib.reload(srv)  # restore with default env
