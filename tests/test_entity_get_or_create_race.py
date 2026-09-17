"""Atomic get-or-create for entities (design D1/D3, entity-identity-uniqueness spec).

Finding 2 (external review): ``get_or_create_abstract_entity`` guarded its check-then-create
with ``self._lock`` -- a PER-INSTANCE ``RLock``. An MCP server, a hook, and a CLI each hold
their own ``Store``, so two processes can both look up ``tag:foo``, both see nothing, and
each mint a different ULID for the same logical entity. The same shape existed for concrete
entities via ``find_entity`` + ``upsert_entity`` written out longhand at two call sites.

``_race_two_stores`` below reproduces the interleaving the way two real processes actually
produce it -- not by hoping OS thread scheduling happens to collide, but by forcing it:
Store A's own lookup pauses (bounded wait) right after it has read, long enough to give
Store B a real chance to run its own lookup (and, against unfixed code, its own mint) before
A proceeds. Against fixed code, A's lookup runs inside ``BEGIN IMMEDIATE``, so B's own
attempt to begin blocks on SQLite's write lock instead of ever reaching its lookup -- A's
bounded wait times out, A proceeds and commits, and only THEN does B's blocked ``BEGIN
IMMEDIATE`` unblock, so B's lookup correctly finds A's just-committed row.

Both tests here must be observed FAILING against the unfixed code (``distinct_ids_minted ==
2``, the spec §1 reproduction shape) -- this is red-first evidence, not a declared
exception.
"""

from __future__ import annotations

import json
import threading

from sidegraph.schema import Descriptor, Entity, EntityKind
from sidegraph.store import Store

_WAIT_TIMEOUT = 2.0


class _MarkerProxy:
    """Wraps a ``Store``'s real connection. The FIRST ``execute()`` call whose SQL contains
    ``marker`` runs for real, then calls ``on_marker()`` before returning -- so the caller is
    paused (or not, depending on what ``on_marker`` does) exactly at the point two real
    processes' lookups would interleave. Only fires once per proxy: a get-or-create issues
    several statements and only the very first matching one is the lookup this test cares
    about.
    """

    def __init__(self, real, marker: str, on_marker) -> None:
        self._real = real
        self._marker = marker
        self._on_marker = on_marker
        self._fired = False

    def execute(self, sql, *args, **kwargs):
        result = self._real.execute(sql, *args, **kwargs)
        if not self._fired and self._marker in sql:
            self._fired = True
            self._on_marker()
        return result

    def __getattr__(self, name):
        return getattr(self._real, name)


def _race_two_stores(tmp_path, marker: str, call_a, call_b):
    """Run ``call_a(store_a)`` on the main thread and ``call_b(store_b)`` on a background
    thread, both against independent ``Store`` instances opened on the SAME directory
    (standing in for two separate processes), forced into the exact interleaving described
    above. Returns ``(store_a, store_b, result_a, result_b_box)`` where ``result_b_box`` is
    ``{"value": ...}`` or ``{"error": ...}``.
    """
    store_a = Store(tmp_path / "s")
    store_b = Store(tmp_path / "s")
    # B's own BEGIN IMMEDIATE (fixed code) blocks for as long as A holds the write lock --
    # A's bounded wait below plus ordinary scheduling jitter under a loaded machine. Python's
    # sqlite3 default busy_timeout is 5s; bump it generously so contention on a shared/busy
    # host can never turn "B correctly waits, then succeeds" into a spurious "database is
    # locked" (observed once in a full-suite run under load -- not a logic bug, a too-tight
    # margin. Isolated reruns of just this file always passed).
    store_b._conn.execute("PRAGMA busy_timeout = 20000")

    go_b = threading.Event()
    b_reached_lookup = threading.Event()

    def _on_a_marker() -> None:
        # Let B start racing now, then give it a bounded chance to reach ITS OWN lookup
        # before proceeding -- against fixed code B never reaches it (blocked earlier, on
        # BEGIN IMMEDIATE), so this simply times out and A proceeds anyway. Kept short (not
        # the 5s+ busy_timeout above): its only job is to give the UNFIXED case's near-instant
        # marker a chance to fire, and the shorter it is, the less exposure to contention
        # inflating how long A holds the write lock before fixed code's B can acquire it.
        go_b.set()
        b_reached_lookup.wait(_WAIT_TIMEOUT)

    def _on_b_marker() -> None:
        b_reached_lookup.set()

    store_a._conn = _MarkerProxy(store_a._conn, marker, _on_a_marker)
    store_b._conn = _MarkerProxy(store_b._conn, marker, _on_b_marker)

    result_b: dict = {}

    def _worker() -> None:
        go_b.wait(_WAIT_TIMEOUT)
        try:
            result_b["value"] = call_b(store_b)
        except BaseException as e:  # noqa: BLE001 - captured for the test to inspect
            result_b["error"] = e

    t = threading.Thread(target=_worker)
    t.start()
    result_a = call_a(store_a)
    t.join(30.0)  # generous: must exceed store_b's own busy_timeout above with headroom
    assert not t.is_alive(), "racing thread never finished -- deadlock in the interleaving"

    return store_a, store_b, result_a, result_b


def _canonical_entity_files(tmp_path) -> list[dict]:
    return [json.loads(f.read_text()) for f in sorted((tmp_path / "s" / "entities").glob("*.json"))]


def test_two_stores_racing_one_abstract_name_mint_one_id(tmp_path):
    store_a, store_b, result_a, result_b = _race_two_stores(
        tmp_path,
        marker="WHERE canonical_name",
        call_a=lambda s: s.get_or_create_abstract_entity("tag:race"),
        call_b=lambda s: s.get_or_create_abstract_entity("tag:race"),
    )
    store_a.close()
    store_b.close()

    assert "error" not in result_b, f"racing store raised: {result_b.get('error')}"
    result_b_value = result_b["value"]

    ids_minted = {result_a.entity_id, result_b_value.entity_id}
    assert len(ids_minted) == 1, (
        f"two distinct ids minted for the same logical entity: {ids_minted}"
    )

    files = [
        f
        for f in _canonical_entity_files(tmp_path)
        if f["kind"] == "abstract" and f["canonical_name"] == "tag:race"
    ]
    assert len(files) == 1, f"expected exactly one canonical file, found {len(files)}"

    # A reopened store's index (rebuilt straight from the canonical files) must agree too --
    # not merely "no crash", but genuinely one row.
    reopened = Store(tmp_path / "s")
    try:
        found = reopened.find_abstract_entity("tag:race")
        assert found is not None
        assert found.entity_id in ids_minted
        rows = reopened._conn.execute(
            "SELECT data FROM entities WHERE canonical_name = ?", ("tag:race",)
        ).fetchall()
        abstract_rows = [
            r for r in rows if Entity.model_validate_json(r["data"]).kind == EntityKind.ABSTRACT
        ]
        assert len(abstract_rows) == 1
    finally:
        reopened.close()


def test_two_stores_racing_one_concrete_descriptor_mint_one_id(tmp_path):
    descriptor = Descriptor(name="widget.Frobnicator", file_path="src/widget.py")

    store_a, store_b, result_a, result_b = _race_two_stores(
        tmp_path,
        marker="SELECT data FROM entities",
        call_a=lambda s: s.get_or_create_entity(descriptor),
        call_b=lambda s: s.get_or_create_entity(descriptor),
    )
    store_a.close()
    store_b.close()

    assert "error" not in result_b, f"racing store raised: {result_b.get('error')}"
    result_b_value = result_b["value"]

    ids_minted = {result_a.entity_id, result_b_value.entity_id}
    assert len(ids_minted) == 1, (
        f"two distinct ids minted for the same logical descriptor: {ids_minted}"
    )

    files = [
        f
        for f in _canonical_entity_files(tmp_path)
        if f["kind"] == "concrete"
        and f["descriptor"] is not None
        and f["descriptor"]["name"] == descriptor.name
        and f["descriptor"]["file_path"] == descriptor.file_path
    ]
    assert len(files) == 1, f"expected exactly one canonical file, found {len(files)}"

    reopened = Store(tmp_path / "s")
    try:
        found = reopened.find_entity(descriptor.name, descriptor.file_path)
        assert found is not None
        assert found.entity_id in ids_minted
    finally:
        reopened.close()
