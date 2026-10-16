"""``server._get_store()`` memoization race (review Important-2b): fastmcp 3 dispatches
sync ``@mcp.tool`` calls onto worker threads (see store.py's own threading note), so a
cold-start process can have several threads race ``_get_store()``'s check-then-set at
once. A bare ``if _store is None: _store = Store(...)`` is not atomic -- two threads can
both observe ``None``, both construct a ``Store``, and the loser's instance (and its open
sqlite connection) leaks while callers disagree on which instance is "the" store.
"""

from __future__ import annotations

import threading
import time

import pytest


def test_first_mcp_use_in_nested_git_creates_only_root_store(tmp_path, monkeypatch):
    import sidegraph.server as srv

    root = tmp_path / "project"
    (root / ".git").mkdir(parents=True)
    nested = root / "nested"
    nested.mkdir()
    monkeypatch.chdir(nested)
    monkeypatch.setenv("SIDEGRAPH_DIR", ".sidegraph")
    monkeypatch.setattr(srv, "_store", None)
    store = srv._get_store()
    try:
        assert (root / ".sidegraph" / "format").is_file()
        assert not (nested / ".sidegraph").exists()
    finally:
        store.close()


@pytest.mark.parametrize("selection", ["default", "absolute-dir", "absolute-db"])
def test_missing_mcp_store_outside_git_requires_explicit_setup(tmp_path, monkeypatch, selection):
    import sidegraph.server as srv

    launch = tmp_path / "plain"
    launch.mkdir()
    monkeypatch.chdir(launch)
    selected = launch / ".sidegraph"
    if selection != "default":
        selected = tmp_path / "elsewhere" / ".sidegraph"
        variable = "SIDEGRAPH_DIR" if selection == "absolute-dir" else "SIDEGRAPH_DB"
        monkeypatch.setenv(variable, str(selected))
    monkeypatch.setattr(srv, "_store", None)
    try:
        with pytest.raises(ValueError, match="sidegraph-init") as caught:
            srv._get_store()
        assert str(selected) in str(caught.value)
        assert srv._store is None
        assert not selected.exists()
    finally:
        if srv._store is not None:
            srv._store.close()


@pytest.mark.parametrize("legacy", ["sidegraph.db", ".sidegraph/decisions.db"])
def test_missing_nested_legacy_mcp_destination_needs_setup(tmp_path, monkeypatch, legacy):
    import sidegraph.server as srv

    root = tmp_path / "project"
    (root / ".git").mkdir(parents=True)
    nested = root / "nested"
    nested.mkdir()
    monkeypatch.chdir(nested)
    monkeypatch.setenv("SIDEGRAPH_DB", legacy)
    monkeypatch.setattr(srv, "_store", None)
    try:
        with pytest.raises(ValueError, match="sidegraph-init") as caught:
            srv._get_store()
        message = str(caught.value)
        assert str(root) in message
        assert "SIDEGRAPH_DB" in message
        assert srv._store is None
        assert not (nested / ".sidegraph").exists()
        assert not (root / ".sidegraph").exists()
    finally:
        if srv._store is not None:
            srv._store.close()


def test_explicit_cli_setup_allows_next_mcp_call_without_restart(tmp_path, monkeypatch):
    import sidegraph.server as srv
    from sidegraph.cli import init_main

    selected = tmp_path / "plain" / ".sidegraph"
    monkeypatch.setenv("SIDEGRAPH_DIR", str(selected))
    monkeypatch.setattr(srv, "_store", None)
    with pytest.raises(ValueError, match="sidegraph-init"):
        srv._get_store()
    assert not selected.exists()
    assert init_main(["--db", str(selected), "--no-hooks", "--no-settings"]) == 0
    store = srv._get_store()
    try:
        assert store is srv._store
        assert (selected / "format").is_file()
    finally:
        store.close()


@pytest.mark.parametrize("selection", ["absolute-other-root", "parent", "multipart"])
def test_mcp_creation_uses_exact_lexical_destination_parent(tmp_path, monkeypatch, selection):
    import sidegraph.server as srv

    root = tmp_path / "project"
    (root / ".git").mkdir(parents=True)
    monkeypatch.chdir(root)
    monkeypatch.setattr(srv, "_store", None)
    if selection == "absolute-other-root":
        other = tmp_path / "other"
        (other / ".git").mkdir(parents=True)
        selected = other / ".sidegraph"
        monkeypatch.setenv("SIDEGRAPH_DIR", str(selected))
        store = srv._get_store()
        store.close()
        assert (selected / "format").is_file()
        assert not (root / ".sidegraph").exists()
    else:
        value = "../shared" if selection == "parent" else ".config/sidegraph"
        selected = tmp_path / "shared" if selection == "parent" else root / value
        monkeypatch.setenv("SIDEGRAPH_DIR", value)
        with pytest.raises(ValueError, match="sidegraph-init"):
            srv._get_store()
        assert not selected.exists()
        assert srv._store is None


def test_get_store_concurrent_first_calls_create_exactly_one_instance(tmp_path, monkeypatch):
    import sidegraph.server as srv
    from sidegraph.store import Store as RealStore

    (tmp_path / ".git").mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SIDEGRAPH_DIR", raising=False)
    monkeypatch.delenv("SIDEGRAPH_DB", raising=False)
    monkeypatch.setattr(srv, "_store", None)

    created = []
    created_lock = threading.Lock()
    real_init = RealStore.__init__  # captured BEFORE patching -- see below

    def slow_init(self, *args, **kwargs):
        # Widen the check-then-set race window so a missing lock reliably shows up as
        # more than one constructed instance, instead of depending on GIL scheduling luck.
        # Calls the captured `real_init`, NOT `RealStore.__init__` (which would resolve to
        # this very patch post-setattr and recurse forever).
        time.sleep(0.05)
        real_init(self, *args, **kwargs)
        with created_lock:
            created.append(self)

    monkeypatch.setattr(RealStore, "__init__", slow_init)

    results = []
    results_lock = threading.Lock()
    barrier = threading.Barrier(8)

    def worker():
        barrier.wait()
        store = srv._get_store()
        with results_lock:
            results.append(store)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(results) == 8
    assert len(created) == 1, f"Store.__init__ ran {len(created)} times, expected exactly 1"
    assert len({id(s) for s in results}) == 1, (
        "callers disagree on which Store instance is THE store"
    )
    for s in results:
        s.close()
