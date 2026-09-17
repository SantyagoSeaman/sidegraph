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


def test_get_store_concurrent_first_calls_create_exactly_one_instance(tmp_path, monkeypatch):
    import sidegraph.server as srv
    from sidegraph.store import Store as RealStore

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
