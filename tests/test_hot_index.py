"""``HotIndex``: the hook hot path's raw handle on ``index.db`` (T3, T4, T5).

PreToolUse reads a handful of rows and writes one claim and one touch. Going through
``Store`` for that meant the package import and a digest walk of every canonical file. The
handle reads the derived index directly, so the tests that matter are parity (the same
records, in the same order, that the model layer gives), atomicity (the same one-shot claim and
the exact per-agent cap) and refusal (every case where ``Store`` would heal the index, the
handle does nothing instead).

see design/superpowers/specs/2026-10-03-hot-path-light-index-design.md (D2, D3) and
design/superpowers/specs/2026-10-03-records-at-the-point-of-reading-design.md (D2, D5, D6)
"""

from __future__ import annotations

import ast
import hashlib
import inspect
import json
import os
import shutil
import sqlite3
import textwrap
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import sidegraph.host.hooks as hooks
from sidegraph import hot_index, retrieval, store_layout
from sidegraph import store as store_module
from sidegraph.hot_index import HotIndex
from sidegraph.schema import (
    SCHEMA_VERSION,
    AnchorBinding,
    Decision,
    DecisionKind,
    DecisionStatus,
    Descriptor,
    Domain,
    Entity,
    Provenance,
)
from sidegraph.store import VOLATILE_STALE_KEY, Store
from tests.test_store_requirement import above_running, write_requirement_marker

NOW = datetime.now(UTC)


def _synced_hot_pair(project: Path):
    """Real sync certifies one live anchor and one orphan before a canonical reload."""
    from sidegraph.engine.reader import GraphifyReader
    from sidegraph.sync import sync

    (project / "live.py").write_text("def kept():\n    return 1\n", encoding="utf-8")
    graph = project / "graphify-out" / "graph.json"
    graph.parent.mkdir()
    graph.write_text(
        json.dumps(
            {
                "directed": True,
                "multigraph": False,
                "nodes": [
                    {
                        "id": "kept",
                        "label": "kept",
                        "norm_label": "kept",
                        "file_type": "code",
                        "source_file": "live.py",
                        "source_location": "L1",
                        "community": 1,
                        "_origin": "ast",
                    }
                ],
                "links": [],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    root = project / ".sidegraph"
    store = Store(root)
    try:
        entities = []
        records = []
        for name, path, title in [
            ("kept", "live.py", "Legitimate live memory"),
            ("gone", "missing.py", "Orphan must stay silent"),
        ]:
            record = store.add_decision(
                Decision(
                    title=title,
                    kind=DecisionKind.GOTCHA,
                    status=DecisionStatus.ACCEPTED,
                    context="Before canonical reload",
                    choice="Respect computed binding status",
                    valid_from=NOW,
                    provenance=Provenance(source="manual"),
                )
            )
            entity = store.upsert_entity(
                Entity(canonical_name=name, descriptor=Descriptor(name=name, file_path=path))
            )
            store.add_binding(
                AnchorBinding(record_id=record.id, entity_id=entity.entity_id, tier=2)
            )
            records.append(record)
            entities.append(entity)
        canonical = {p.relative_to(root): p.read_bytes() for p in root.rglob("*.json")}
        graph_bytes = graph.read_bytes()
        reader = GraphifyReader(graph)
        sync(store, reader, force=True)
        assert store.get_meta(VOLATILE_STALE_KEY) == "0"
        assert [store.bindings_for_record(r.id)[0].status for r in records] == [
            "live",
            "orphaned",
        ]
        assert {p.relative_to(root): p.read_bytes() for p in root.rglob("*.json")} == canonical
        assert graph.read_bytes() == graph_bytes
        return root, reader, entities, root / "decisions" / f"{records[0].id}.json"
    finally:
        store.close()


def _reload_hot_pair(root: Path, record_path: Path) -> None:
    data = json.loads(record_path.read_text(encoding="utf-8"))
    data["context"] = "External canonical edit resets derived statuses"
    record_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    store = Store(root)
    try:
        assert store.get_meta(VOLATILE_STALE_KEY) == "1"
    finally:
        store.close()


def test_reload_withholds_hot_records_until_sync(tmp_path):
    root, reader, entities, record = _synced_hot_pair(tmp_path)
    _reload_hot_pair(root, record)
    index = HotIndex.open(root)
    assert index is not None
    try:
        assert index.records_for([e.entity_id for e in entities]) == []
        # Withholding selection does not disable the handle's independent telemetry.
        index.record_touch("reload-session", "live.py", "Read")
        assert index.claim_meta("reload-claim", "1")
        assert index.get_meta("reload-claim") == "1"
    finally:
        index.close()

    from sidegraph.sync import sync

    store = Store(root)
    try:
        canonical = {p.relative_to(root): p.read_bytes() for p in root.rglob("*.json")}
        sync(store, reader)
        assert {p.relative_to(root): p.read_bytes() for p in root.rglob("*.json")} == canonical
    finally:
        store.close()
    index = HotIndex.open(root)
    assert index is not None
    try:
        assert [
            r.decision["title"] for r in index.records_for([e.entity_id for e in entities])
        ] == ["Legitimate live memory"]
        assert index.memory_counts() == (1, 1, 1)
    finally:
        index.close()


def test_reload_withholds_hot_memory_counts(tmp_path):
    root, _reader, _entities, record = _synced_hot_pair(tmp_path)
    _reload_hot_pair(root, record)
    index = HotIndex.open(root)
    assert index is not None
    try:
        assert index.memory_counts() == (0, 0, 0)
    finally:
        index.close()


@pytest.mark.parametrize("event", ["PreToolUse", "SubagentStart"])
@pytest.mark.parametrize("reload_index", [True, False], ids=["reload", "new-graphless-store"])
def test_uncomputed_statuses_withhold_real_hook_delivery(
    tmp_path, monkeypatch, capsys, event, reload_index
):
    from sidegraph.host import subagent

    if reload_index:
        root, _reader, _entities, record = _synced_hot_pair(tmp_path)
        _reload_hot_pair(root, record)
    else:
        root = tmp_path / ".sidegraph"
        store = Store(root)
        seed = _Seeder(store)
        seed.anchor(seed.decision("Uncomputed memory", DecisionKind.GOTCHA), "missing.py")
        assert store.get_meta(VOLATILE_STALE_KEY) == "1"
        store.close()
    monkeypatch.setenv("SIDEGRAPH_DIR", str(root))
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    payload = {
        "hook_event_name": event,
        "session_id": "reload-hook-session",
        "cwd": str(tmp_path),
        "tool_name": "Read",
        "tool_input": {"file_path": str(tmp_path / "missing.py")},
    }
    monkeypatch.setattr(hooks, "_read_payload", lambda: payload)
    monkeypatch.setattr(subagent, "_read_payload", lambda: payload)
    canonical = {p.relative_to(root): p.read_bytes() for p in root.rglob("*.json")}
    (hooks.pre_tool_use if event == "PreToolUse" else subagent.subagent_start)()
    assert json.loads(capsys.readouterr().out) == {}
    assert {p.relative_to(root): p.read_bytes() for p in root.rglob("*.json")} == canonical


@pytest.mark.parametrize("surface", ["records", "counts"])
def test_reload_between_hot_queries_cannot_expose_reset_statuses(tmp_path, surface):
    root, _reader, entities, record = _synced_hot_pair(tmp_path)
    index = HotIndex.open(root)
    assert index is not None
    conn = index._conn

    class ReloadBeforeBindings:
        reloaded = False

        def execute(self, sql, parameters=()):
            if "FROM anchor_bindings" in sql and not self.reloaded:
                self.reloaded = True
                _reload_hot_pair(root, record)
            return conn.execute(sql, parameters)

        def close(self):
            conn.close()

    interleaving = ReloadBeforeBindings()
    index._conn = interleaving
    try:
        if surface == "records":
            assert index.records_for([e.entity_id for e in entities]) == []
        else:
            assert index.memory_counts() == (0, 0, 0)
        assert interleaving.reloaded
    finally:
        index.close()


def test_missing_legacy_flag_preserves_computed_hot_selection(tmp_path):
    root, _reader, entities, _record = _synced_hot_pair(tmp_path)
    conn = sqlite3.connect(root / "index.db")
    try:
        conn.execute("DELETE FROM meta WHERE key = ?", (VOLATILE_STALE_KEY,))
        conn.commit()
    finally:
        conn.close()
    index = HotIndex.open(root)
    assert index is not None
    try:
        assert [
            r.decision["title"] for r in index.records_for([e.entity_id for e in entities])
        ] == ["Legitimate live memory"]
        assert index.memory_counts() == (1, 1, 1)
    finally:
        index.close()


# -- the parity fixture ---------------------------------------------------------------------

FILE_A = "src/a.py"  # a crowded path: every status and shape the nudge filters on
FILE_B = "src/b.py"  # one accepted record, then proposals inside and outside the window
FILE_C = "src/c.py"  # nothing anchored
FILE_D = "src/d.py"  # proposals only
FILE_E = "src/e.py"  # an entity with no decision bound to it


class _Seeder:
    """Builds the fixture store. One entity per ``anchor`` call unless ``entity`` is given."""

    def __init__(self, store: Store) -> None:
        self.store = store
        self._n = 0

    def decision(
        self,
        title: str,
        kind: DecisionKind,
        status: DecisionStatus = DecisionStatus.ACCEPTED,
        *,
        age_days: float = 0.0,
        valid_to: datetime | None = None,
        supersedes: str | None = None,
    ) -> Decision:
        return self.store.add_decision(
            Decision(
                title=title,
                kind=kind,
                status=status,
                context="c",
                choice="x",
                valid_from=NOW - timedelta(days=age_days),
                valid_to=valid_to,
                supersedes=supersedes,
                provenance=Provenance(source="manual"),
            )
        )

    def anchor(
        self,
        decision: Decision,
        file_path: str,
        *,
        status: str = "live",
        entity: Entity | None = None,
    ) -> Entity:
        if entity is None:
            self._n += 1
            name = f"Sym{self._n}"
            entity = self.store.upsert_entity(
                Entity(canonical_name=name, descriptor=Descriptor(name=name, file_path=file_path))
            )
        self.store.add_binding(
            AnchorBinding(record_id=decision.id, entity_id=entity.entity_id, tier=2, status=status)
        )
        return entity


def _fixture_store(path: Path) -> Store:
    store = Store(path)
    with store._mutation():  # one transaction for the whole fixture, not one per write
        _seed_fixture(store)
        # This parity fixture hand-authors computed live/degraded/orphaned statuses.
        store.set_meta(VOLATILE_STALE_KEY, "0")
    return store


def _seed_fixture(store: Store) -> None:
    s = _Seeder(store)
    G, L, A = DecisionKind.GOTCHA, DecisionKind.LESSON, DecisionKind.ADR
    P = DecisionStatus.PROPOSED

    # FILE_A: the filters. Only the first three can surface (cap 2 shows the top two).
    s.anchor(s.decision("A gotcha, oldest accepted", G, age_days=20), FILE_A)
    s.anchor(s.decision("A lesson, newest accepted", L, age_days=0.1), FILE_A)
    s.anchor(s.decision("A decision, accepted", A, age_days=2), FILE_A)
    s.anchor(s.decision("A gotcha, proposed recently", G, P, age_days=5), FILE_A)
    s.anchor(s.decision("A decision, proposed long ago", A, P, age_days=40), FILE_A)
    s.anchor(
        s.decision("A gotcha, expired", G, valid_to=NOW - timedelta(days=1), age_days=10), FILE_A
    )
    s.anchor(s.decision("A gotcha, valid until later", G, valid_to=NOW + timedelta(days=5)), FILE_A)
    s.anchor(s.decision("A gotcha, orphaned binding", G), FILE_A, status="orphaned")
    s.anchor(s.decision("A gotcha, degraded binding", G, age_days=1), FILE_A, status="degraded")
    old = s.decision("A ruling, superseded", G, age_days=30)
    s.anchor(old, FILE_A)
    s.anchor(s.decision("A ruling, replacing it", G, age_days=3, supersedes=old.id), FILE_A)
    dropped = s.decision("A gotcha, rejected", G, P, age_days=1)
    s.anchor(dropped, FILE_A)
    store.drop(dropped.id)

    # FILE_B: one accepted record, so the cap reaches into the proposals.
    s.anchor(
        s.decision('A decision with "quotes"\nand a newline ' + "x" * 200, A, age_days=3), FILE_B
    )
    s.anchor(s.decision("B gotcha, proposed", G, P, age_days=5), FILE_B)
    s.anchor(s.decision("B lesson, proposed, just inside 30 days", L, P, age_days=29), FILE_B)
    s.anchor(s.decision("B lesson, proposed, just outside 30 days", L, P, age_days=31), FILE_B)
    # one decision bound to two entities on the same file must be listed once
    twice = s.decision("B decision bound twice", A, age_days=4)
    s.anchor(twice, FILE_B)
    s.anchor(twice, FILE_B)

    # FILE_D: proposals only, a mistake older than an ordinary kind: newest-first across both.
    s.anchor(s.decision("D decision, proposed newer", A, P, age_days=1), FILE_D)
    s.anchor(s.decision("D gotcha, proposed older", G, P, age_days=3), FILE_D)

    # FILE_E: an entity nothing is bound to.
    s.store.upsert_entity(
        Entity(canonical_name="Lonely", descriptor=Descriptor(name="Lonely", file_path=FILE_E))
    )

    # An abstract entity and a decision at the domain level: never listed by file.
    domain = store.add_domain(
        Domain(
            slug="payments",
            title="Payments",
            summary="Settlement.",
            provenance=Provenance(source="manual"),
        )
    )
    store.ratify_domains(accept=[domain.domain_id])
    store.add_domain(
        Domain(
            slug="billing",
            title="Billing",
            summary="Invoices.",
            provenance=Provenance(source="manual"),
        )
    )
    # a deprecated and a global-scope decision still count as live records
    s.decision("Global ruling", DecisionKind.CONSTRAINT)


_ENVIRONMENTS = [
    pytest.param({}, id="default"),
    pytest.param({"SIDEGRAPH_UNRATIFIED": "off"}, id="unratified-off"),
    pytest.param({"SIDEGRAPH_PROPOSAL_WINDOW_DAYS": "0"}, id="window-open"),
    pytest.param({"SIDEGRAPH_PROPOSAL_WINDOW_DAYS": "3"}, id="window-3-days"),
    pytest.param({"SIDEGRAPH_PROPOSAL_WINDOW_DAYS": "abc"}, id="window-garbage"),
    pytest.param({"SIDEGRAPH_PROPOSAL_WINDOW_DAYS": "  "}, id="window-blank"),
    pytest.param(
        {"SIDEGRAPH_UNRATIFIED": "off", "SIDEGRAPH_PROPOSAL_WINDOW_DAYS": "0"},
        id="off-beats-window",
    ),
]


# -- T3: parity with the model layer -----------------------------------------------------------


def _hot_records(index: HotIndex, rel: str) -> list[tuple]:
    """What the hook's records query returns for one file: one scan, then the file's records."""
    entity_ids = index.anchored_entities([rel]).get(rel, [])
    records = index.records_for(entity_ids)
    assert all(r.record_type == "decision" for r in records), "decision-only parity fixture"
    return [
        (
            r.decision["id"],
            r.decision["kind"],
            r.decision["title"],
            r.decision["choice"],
            r.proposed,
        )
        for r in records
    ]


def _oracle_records(store: Store, rel: str) -> list[tuple]:
    """The same list from the models: ``Store`` and ``partition_by_trust``."""
    return [
        (d.id, d.kind.value, d.title, d.choice, proposed)
        for d, proposed in hooks._records_for_path(store, rel)
    ]


@pytest.mark.parametrize("env", _ENVIRONMENTS)
def test_the_records_query_equals_the_model_layer(tmp_path, monkeypatch, env):
    """The handle must give the hook byte for byte the records and the order that the model
    layer gives: the window, ``SIDEGRAPH_UNRATIFIED=off``, mistakes first, accepted before
    proposed, newest first, one record bound twice listed once. Red against a reader that
    skips ``proposal_surfaces`` (M3 of the hot-path spec)."""
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    store = _fixture_store(tmp_path / "s")
    index = HotIndex.open(tmp_path / "s")
    assert index is not None
    seen = {}
    for rel in (FILE_A, FILE_B, FILE_C, FILE_D, FILE_E, "src/none.py", "src/a.py/"):
        got = _hot_records(index, rel)
        assert got == _oracle_records(store, rel), rel
        seen[rel] = got
    if not env:
        assert len(seen[FILE_A]) == 7 and seen[FILE_C] == [] and seen[FILE_E] == []
        assert [r[4] for r in seen[FILE_D]] == [True, True]
        # a mistake first, newest first within the bucket, accepted before any proposal
        assert [r[2] for r in seen[FILE_A]] == [
            "A gotcha, valid until later",
            "A lesson, newest accepted",
            "A gotcha, degraded binding",
            "A ruling, replacing it",
            "A gotcha, oldest accepted",
            "A decision, accepted",
            "A gotcha, proposed recently",
        ]
        assert seen[FILE_A][-1][4] is True
    index.close()


def test_the_fixture_reaches_the_clip_the_proposals_and_the_window(tmp_path):
    """Guards the guard: the parity above is only parity if the fixture hits the branches it
    names."""
    store = _fixture_store(tmp_path / "s")
    records_b = hooks._records_for_path(store, FILE_B)
    titles_b = [d.title for d, _ in records_b]
    assert any(len(t) > 90 and "\n" in t for t in titles_b)
    assert any(proposed for _, proposed in records_b)
    assert not any("just outside 30 days" in t for t in titles_b)
    assert sum("bound twice" in t for t in titles_b) == 1
    titles_a = [d.title for d, _ in hooks._records_for_path(store, FILE_A)]
    assert not any(w in t for t in titles_a for w in ("expired", "orphaned", "rejected"))
    assert not any("superseded" in t for t in titles_a)
    assert not any("long ago" in t for t in titles_a)


def test_one_scan_of_the_entities_serves_every_file_of_a_call(tmp_path):
    """D5: a Bash line naming three files costs one pass over ``entities``, not three. Red
    against a lookup that scans per file."""
    _fixture_store(tmp_path / "s").close()
    index = HotIndex.open(tmp_path / "s")
    assert index is not None
    statements: list[str] = []
    index._conn.set_trace_callback(statements.append)
    found = index.anchored_entities([FILE_A, FILE_B, FILE_C, FILE_D, FILE_E])
    scans = [s for s in statements if "FROM entities" in s]
    assert len(scans) == 1, scans
    assert set(found) == {FILE_A, FILE_B, FILE_D, FILE_E}  # FILE_C has no entity at all
    index.close()


def test_anchored_files_lists_every_anchored_file_from_one_scan(tmp_path):
    """The suffix fallback of a subagent's brief matches a token against every anchored path, so
    it needs them all, and from the same single pass ``anchored_entities`` makes."""
    _fixture_store(tmp_path / "s").close()
    index = HotIndex.open(tmp_path / "s")
    assert index is not None
    statements: list[str] = []
    index._conn.set_trace_callback(statements.append)
    found = index.anchored_files()
    assert len([s for s in statements if "FROM entities" in s]) == 1
    assert set(found) == {FILE_A, FILE_B, FILE_D, FILE_E}
    assert all(found[path] for path in found)
    assert index.anchored_entities([FILE_A, FILE_C]) == {FILE_A: found[FILE_A]}
    index.close()


def test_a_decision_whose_row_vanished_is_skipped_not_fatal(tmp_path):
    """A binding can name a record the index does not hold (a fact id, a reload mid-write):
    the model layer skips it, and so must the handle."""
    store = _fixture_store(tmp_path / "s")
    store.close()
    conn = sqlite3.connect(tmp_path / "s" / "index.db")
    conn.execute("DELETE FROM decisions WHERE data LIKE '%\"A lesson, newest accepted\"%'")
    conn.commit()
    conn.close()
    index = HotIndex.open(tmp_path / "s")
    assert index is not None
    assert "A lesson, newest accepted" not in [r[2] for r in _hot_records(index, FILE_A)]
    index.close()


# -- one copy of everything that moved ----------------------------------------------------------


def test_each_moved_value_has_one_definition():
    assert store_layout.SCHEMA_VERSION == SCHEMA_VERSION
    assert store_module.VOLATILE_STALE_KEY == store_layout.VOLATILE_STALE_KEY
    assert hot_index.VOLATILE_STALE_KEY == store_layout.VOLATILE_STALE_KEY
    assert store_module.symlinked_internals is store_layout.symlinked_internals
    assert {DecisionKind(k) for k in store_layout.MISTAKE_KINDS} == retrieval._MISTAKE_KINDS
    assert frozenset({"gotcha", "constraint", "lesson"}) == store_layout.MISTAKE_KINDS
    for kind in store_layout.MISTAKE_KINDS:
        assert DecisionKind(kind)  # a renamed kind must break here, not in the nudge
    # the records block the hook prints carries the same guard line and clips the same way
    assert retrieval.MEMORY_GUARD_LINE is store_layout.MEMORY_GUARD_LINE
    assert retrieval._clip_line is store_layout.clip_line


def test_retrieval_proposal_surfaces_delegates_to_the_layout_rule(monkeypatch):
    record = Decision(
        title="t",
        kind=DecisionKind.GOTCHA,
        status=DecisionStatus.PROPOSED,
        context="c",
        choice="x",
        valid_from=NOW - timedelta(days=45),
        provenance=Provenance(source="manual"),
    )
    assert retrieval.proposal_surfaces(record) is store_layout.proposal_surfaces(record.valid_from)
    assert retrieval.proposal_surfaces(record) is False
    monkeypatch.setenv("SIDEGRAPH_PROPOSAL_WINDOW_DAYS", "60")
    assert retrieval.proposal_surfaces(record) is True
    monkeypatch.setenv("SIDEGRAPH_UNRATIFIED", "off")
    assert retrieval.proposal_surfaces(record) is False


def test_the_hot_sql_is_defined_once_and_shared_with_the_store():
    """The claim, the meta read, the event insert and the shows upsert are one statement each, in
    ``hot_index.py``, and ``Store`` runs those same constants."""
    src = Path(store_module.__file__).parent
    shared = (
        ("CLAIM_META_SQL", Store.claim_meta),
        ("GET_META_SQL", Store.get_meta),
        ("INSERT_EVENT_SQL", Store._append_event),
        ("SHOW_UPSERT_SQL", Store.record_retrieval),
    )
    for name, method in shared:
        tree = ast.parse(textwrap.dedent(inspect.getsource(method)))
        used = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        assert name in used, f"{method.__qualname__} does not run {name}"
        assert isinstance(getattr(hot_index, name), str)
    # no second spelling of the two statements only these methods run (``update_meta_if``
    # reads meta inside its own transaction and keeps its own SELECT). Each needle is the part
    # of the statement that no sibling statement shares.
    needles = {
        "CLAIM_META_SQL": "ON CONFLICT(key) DO NOTHING",
        "INSERT_EVENT_SQL": "(session_id, at, kind, key, detail, agent)",
        "SHOW_UPSERT_SQL": "shows = shows + 1, last_shown_at = excluded.last_shown_at",
    }
    for name, needle in needles.items():
        assert needle in getattr(hot_index, name)
        homes = sorted(
            str(p.relative_to(src)) for p in src.rglob("*.py") if needle in p.read_text()
        )
        assert homes == ["hot_index.py"], (name, homes)


# -- T4: the one-shot claim is atomic ---------------------------------------------------------


def _race(path: Path, openers: list, keys: list[str]) -> dict[str, int]:
    wins: dict[str, int] = dict.fromkeys(keys, 0)
    guard = threading.Lock()
    errors: list[BaseException] = []
    barrier = threading.Barrier(len(openers))

    def worker(opener) -> None:
        try:
            handle = opener(path)
            for key in keys:
                barrier.wait()
                if handle.claim_meta(key, "v"):
                    with guard:
                        wins[key] += 1
        except BaseException as exc:  # a broken barrier must fail the test, not hang it
            errors.append(exc)
            barrier.abort()

    threads = [threading.Thread(target=worker, args=(o,)) for o in openers]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert errors == []
    return wins


def _open_hot(path: Path) -> HotIndex:
    index = HotIndex.open(path)
    assert index is not None
    return index


def test_claim_meta_is_atomic_across_hot_connections(tmp_path):
    """Eight real connections claim each key at the same instant: exactly one wins per key.
    A read-then-write claim (M4) lets several of them read "absent" first; the pattern fails
    it on the first few keys."""
    path = tmp_path / "s"
    Store(path).close()
    keys = [f"pretool_nudge:S:agent-{i}" for i in range(40)]
    wins = _race(path, [_open_hot] * 8, keys)
    assert set(wins.values()) == {1}, {k: v for k, v in wins.items() if v != 1}


def test_claim_meta_is_atomic_between_the_store_and_the_hot_handle(tmp_path):
    """A hook claiming through the handle and the server through ``Store`` share one key
    space: the same statement, so still exactly one winner."""
    path = tmp_path / "s"
    Store(path).close()
    keys = [f"pretool_nudge_path:S:agent-{i}" for i in range(40)]
    openers = [_open_hot, lambda p: Store(p)] * 4
    wins = _race(path, openers, keys)
    assert set(wins.values()) == {1}, {k: v for k, v in wins.items() if v != 1}


def test_get_meta_and_claim_meta_agree_with_the_store(tmp_path):
    path = tmp_path / "s"
    store = Store(path)
    index = _open_hot(path)
    assert index.get_meta("k") is None
    assert index.claim_meta("k", "first") is True
    assert index.claim_meta("k", "second") is False
    assert index.get_meta("k") == "first"
    assert store.get_meta("k") == "first"
    assert index.get_meta("schema_version") == SCHEMA_VERSION
    index.close()


# -- the per-agent file claim: once per key, exactly ten per prefix ---------------------------


def _prefix(session: str = "S", agent: str = "-") -> str:
    return f"pretool_file:{session}:{agent}:"


def test_claim_file_stops_at_the_cap_and_never_before(tmp_path):
    """Exactly ten files per agent: the eleventh claim fails, a repeat of a held file fails
    without spending anything, and the count in the table stays at ten. Red against a claim
    with no count (M4 of the records spec)."""
    path = tmp_path / "s"
    Store(path).close()
    index = _open_hot(path)
    results = [index.claim_file(_prefix(), f"src/f{i}.py", "t", 10) for i in range(12)]
    assert results == [True] * 10 + [False] * 2
    assert index.claim_file(_prefix(), "src/f0.py", "t", 10) is False
    count = index._conn.execute("SELECT COUNT(*) FROM meta WHERE key LIKE 'pretool_file:%'")
    assert count.fetchone()[0] == 10
    index.close()


def test_claim_file_counts_one_agent_only(tmp_path):
    """The main agent's prefix ends in ``:-:``, so ten subagents that each claimed ten files do
    not spend its cap (red against a main key with no ``-``: its prefix would be the session
    alone and match every subagent's keys, M6), and ``_`` in a session id is not a ``LIKE``
    wildcard."""
    path = tmp_path / "s"
    Store(path).close()
    index = _open_hot(path)
    for agent in range(10):
        for i in range(10):
            assert index.claim_file(_prefix("S", f"agent{agent}"), f"src/f{i}.py", "t", 10)
    assert index.claim_file(_prefix("S", "agent0"), "src/extra.py", "t", 10) is False
    assert index.claim_file(_prefix("S", "-"), "src/f0.py", "t", 10) is True
    # a session id that a LIKE pattern would fold into S_1
    for i in range(10):
        assert index.claim_file(_prefix("S_1"), f"src/f{i}.py", "t", 10)
    assert index.claim_file(_prefix("SX1"), "src/f0.py", "t", 10) is True
    assert index.claim_file(_prefix("S_1"), "src/extra.py", "t", 10) is False
    index.close()


def test_claim_file_counts_paths_outside_the_basic_plane(tmp_path):
    """The range query's end sits above every UTF-8 key: a file whose name holds an emoji still
    counts toward the cap."""
    path = tmp_path / "s"
    Store(path).close()
    index = _open_hot(path)
    for i in range(9):
        assert index.claim_file(_prefix(), f"src/f{i}.py", "t", 10)
    assert index.claim_file(_prefix(), "src/\U0001f600.py", "t", 10) is True
    assert index.claim_file(_prefix(), "src/after.py", "t", 10) is False
    index.close()


def test_claim_file_keeps_the_cap_across_concurrent_connections(tmp_path):
    """Eight real connections claim thirty files each, all at once: ten claims succeed in all,
    never eleven. A count read before the insert (a second statement) lets several connections
    count nine and all insert."""
    path = tmp_path / "s"
    Store(path).close()
    wins: list[str] = []
    guard = threading.Lock()
    errors: list[BaseException] = []
    barrier = threading.Barrier(8)

    def worker(n: int) -> None:
        try:
            handle = _open_hot(path)
            for i in range(30):
                barrier.wait()
                if handle.claim_file(_prefix(), f"src/w{n}-{i}.py", "t", 10):
                    with guard:
                        wins.append(f"{n}-{i}")
            handle.close()
        except BaseException as exc:
            errors.append(exc)
            barrier.abort()

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert errors == []
    assert len(wins) == 10, len(wins)


def test_record_touch_writes_the_row_the_store_reads_back(tmp_path):
    path = tmp_path / "s"
    store = Store(path)
    index = _open_hot(path)
    index.record_touch("sess-1", "src/a.py", "Edit", agent="agent-9")
    index.record_touch("sess-1", "src/b.py", "Read")
    index.close()
    assert [(e["key"], e["detail"]) for e in store.retrieval_events("sess-1")] == [
        ("src/a.py", "Edit"),
        ("src/b.py", "Read"),
    ]
    row = store._conn.execute("SELECT agent FROM retrieval_events ORDER BY id").fetchall()
    assert [r["agent"] for r in row] == ["agent-9", None]


# -- T5: every case the Store heals, the handle refuses -----------------------------------------


def _snapshot(*roots: Path) -> dict[str, tuple]:
    """Every file and link under the roots: kind, size, mtime and content hash."""
    out: dict[str, tuple] = {}
    for root in roots:
        if not root.exists() and not root.is_symlink():
            continue
        for p in sorted([root, *root.rglob("*")]):
            try:
                st = p.lstat()
            except FileNotFoundError:
                continue
            digest = None
            if p.is_file() and not p.is_symlink():
                digest = hashlib.sha256(p.read_bytes()).hexdigest()
            out[str(p)] = (p.is_symlink(), st.st_size, st.st_mtime_ns, digest)
    return out


def _break_missing(store: Path, elsewhere: Path) -> None:
    (store / "index.db").unlink()


def _break_symlinked_index(store: Path, elsewhere: Path) -> None:
    shutil.move(store / "index.db", elsewhere / "index.db")
    os.symlink(elsewhere / "index.db", store / "index.db")


def _break_symlinked_sidecar(store: Path, elsewhere: Path) -> None:
    # a TRUNCATE-mode index leaves a real 0-byte journal behind; replace it with the link
    (store / "index.db-journal").unlink(missing_ok=True)
    os.symlink(elsewhere / "victim-journal", store / "index.db-journal")


def _break_symlinked_directory(store: Path, elsewhere: Path) -> None:
    shutil.move(store / "decisions", elsewhere / "decisions")
    os.symlink(elsewhere / "decisions", store / "decisions")


def _break_no_agent_column(store: Path, elsewhere: Path) -> None:
    conn = sqlite3.connect(store / "index.db")
    conn.execute("ALTER TABLE retrieval_events DROP COLUMN agent")
    conn.commit()
    conn.close()


def _break_schema_mismatch(store: Path, elsewhere: Path) -> None:
    conn = sqlite3.connect(store / "index.db")
    conn.execute("UPDATE meta SET value = '0.5.0' WHERE key = 'schema_version'")
    conn.commit()
    conn.close()


def _break_corrupt(store: Path, elsewhere: Path) -> None:
    (store / "index.db").write_bytes(b"this is not a sqlite database at all\n" * 200)


def _break_empty_file(store: Path, elsewhere: Path) -> None:
    (store / "index.db").write_bytes(b"")


def _break_requirement_above_running(store: Path, elsewhere: Path) -> None:
    write_requirement_marker(store, above_running())


def _break_malformed_requirement(store: Path, elsewhere: Path) -> None:
    (store / "format").write_text("sidegraph-store 1.0.0\n", encoding="utf-8")


def _break_symlinked_marker(store: Path, elsewhere: Path) -> None:
    marker = store / "format"
    marker.rename(elsewhere / "format")
    marker.symlink_to(elsewhere / "format")


def _break_nonregular_marker(store: Path, elsewhere: Path) -> None:
    (store / "format").unlink()
    (store / "format").mkdir()


def _break_oversized_marker(store: Path, elsewhere: Path) -> None:
    (store / "format").write_bytes(b"x" * (store_layout.MAX_FORMAT_MARKER_BYTES + 1))


_REFUSED = {
    "requirement-above-running": _break_requirement_above_running,
    "malformed-requirement": _break_malformed_requirement,
    "symlinked-marker": _break_symlinked_marker,
    "nonregular-marker": _break_nonregular_marker,
    "oversized-marker": _break_oversized_marker,
    "missing-index": _break_missing,
    "symlinked-index": _break_symlinked_index,
    "symlinked-sidecar": _break_symlinked_sidecar,
    "symlinked-directory": _break_symlinked_directory,
    "no-agent-column": _break_no_agent_column,
    "schema-mismatch": _break_schema_mismatch,
    "corrupt-file": _break_corrupt,
    "empty-file": _break_empty_file,
}


def _broken_project(tmp_path: Path, how: str) -> tuple[Path, Path, Path]:
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / FILE_A).write_text("x = 1\n")
    _fixture_store(root / ".sidegraph").close()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    _REFUSED[how](root / ".sidegraph", elsewhere)
    return root, root / ".sidegraph", elsewhere


def test_the_control_opens(tmp_path):
    root = tmp_path / "repo"
    _fixture_store(root / ".sidegraph").close()
    index = HotIndex.open(root / ".sidegraph")
    assert index is not None
    index.close()


@pytest.mark.parametrize("how", sorted(_REFUSED))
def test_open_returns_none_where_the_store_would_heal(tmp_path, how):
    """Red against a design without the guards (M5): a symlinked ``index.db`` sent a claim
    and a touch into another store."""
    root, store_dir, elsewhere = _broken_project(tmp_path, how)
    assert HotIndex.open(store_dir) is None
