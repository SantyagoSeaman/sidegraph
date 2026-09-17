import io
import json
from pathlib import Path

import sidegraph.host.hooks as hooks
from sidegraph.schema import Descriptor, Entity
from sidegraph.store import Store
from sidegraph.sync import LAST_SYNCED_KEY

FIXTURE = Path(__file__).parent / "fixtures" / "mini_graph.json"


def test_session_start_triggers_lazy_sync(tmp_path, monkeypatch, capsys):
    db = tmp_path / "s.db"
    store = Store(db)
    store.upsert_entity(
        Entity(
            canonical_name="Trader",
            descriptor=Descriptor(name="Trader", file_path="trader/exec.py"),
            last_seen_node_id="stale-id",
            last_seen_graph_version="old",
        )
    )
    monkeypatch.setenv("SIDEGRAPH_DB", str(db))
    monkeypatch.setenv("SIDEGRAPH_GRAPH", str(FIXTURE))
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    hooks.session_start()
    json.loads(capsys.readouterr().out)  # still valid hook output
    assert Store(db).get_meta(LAST_SYNCED_KEY).startswith("abc123:")  # sync ran + stamped
    assert (
        Store(db)
        .get_entity(store.find_entity("Trader", "trader/exec.py").entity_id)
        .last_seen_node_id
        == "m_cls"
    )  # mapping refreshed


def test_session_start_survives_sync_failure(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SIDEGRAPH_DB", str(tmp_path / "s.db"))
    monkeypatch.setenv("SIDEGRAPH_GRAPH", str(FIXTURE))
    monkeypatch.setattr("sys.stdin", io.StringIO(""))

    called = []

    def boom(*a, **k):
        called.append(True)
        raise RuntimeError("boom")

    monkeypatch.setattr("sidegraph.sync.maybe_sync", boom)
    hooks.session_start()
    out = json.loads(capsys.readouterr().out)
    assert called  # the patch actually intercepted
    assert "hookSpecificOutput" in out  # map still injected


def test_get_task_context_tool_shell_syncs(tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    # SIDEGRAPH_DIR (not the deprecated SIDEGRAPH_DB) so `db` is used LITERALLY -- no
    # legacy-dispatch redirection to its parent when it doesn't exist yet (see
    # config._dispatch_sidegraph_db); back-compat itself is covered by tests/test_config.py.
    monkeypatch.setenv("SIDEGRAPH_DIR", str(db))
    monkeypatch.setenv("SIDEGRAPH_GRAPH", str(FIXTURE))
    import importlib

    import sidegraph.server as srv

    importlib.reload(srv)  # rebind module _store to env
    try:
        result = srv._get_task_context_with_sync(files=["trader/exec.py"])
        assert isinstance(result, str)
        assert Store(db).get_meta(LAST_SYNCED_KEY).startswith("abc123:")
    finally:
        monkeypatch.delenv("SIDEGRAPH_DIR", raising=False)
        monkeypatch.delenv("SIDEGRAPH_GRAPH", raising=False)
        importlib.reload(srv)  # restore with default env
