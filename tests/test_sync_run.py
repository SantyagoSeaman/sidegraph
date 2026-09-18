import subprocess

from sidegraph.engine.reader import GraphifyReader
from sidegraph.schema import AnchorBinding, Descriptor, Entity
from sidegraph.store import Store
from sidegraph.sync import LAST_SYNCED_KEY, maybe_sync, sync

GRAPH_B = {
    "built_at_commit": "vB",
    "nodes": [
        # stable: same id, same file  -> unchanged
        {
            "id": "s1",
            "label": "f_stable()",
            "norm_label": "f_stable()",
            "file_type": "code",
            "source_file": "a.py",
            "community": 1,
        },
        # old_fn renamed away         -> orphaned
        {
            "id": "r2",
            "label": "new_fn()",
            "norm_label": "new_fn()",
            "file_type": "code",
            "source_file": "b.py",
            "community": 1,
        },
        # mover: moved c.py -> d.py, new id -> moved
        {
            "id": "m2",
            "label": "mover_fn()",
            "norm_label": "mover_fn()",
            "file_type": "code",
            "source_file": "d.py",
            "community": 2,
        },
        # dup_fn now exists twice     -> ambiguous
        {
            "id": "d2",
            "label": "dup_fn()",
            "norm_label": "dup_fn()",
            "file_type": "code",
            "source_file": "e.py",
            "community": 2,
        },
        {
            "id": "d3",
            "label": "dup_fn()",
            "norm_label": "dup_fn()",
            "file_type": "code",
            "source_file": "f.py",
            "community": 3,
        },
    ],
    "links": [],
}


def _write_graph(tmp_path, name, data):
    import json

    p = tmp_path / name
    p.write_text(json.dumps(data))
    return p


def _entity(store, name, file, node_id, version="vA"):
    e = Entity(
        canonical_name=name,
        descriptor=Descriptor(name=name, file_path=file),
        last_seen_node_id=node_id,
        last_seen_graph_version=version,
    )
    return store.upsert_entity(e)


def _leaf(store, entity_id, status="live"):
    from datetime import UTC, datetime

    from sidegraph.schema import Decision, DecisionKind, Provenance

    d = store.add_decision(
        Decision(
            title=f"about {entity_id}",
            kind=DecisionKind.LESSON,
            context="c",
            choice="ch",
            valid_from=datetime.now(UTC),
            provenance=Provenance(source="manual"),
        )
    )
    store.add_binding(AnchorBinding(record_id=d.id, entity_id=entity_id, tier=2, status=status))
    return d


def test_sync_gates_on_version_and_stamps(tmp_path):
    reader = GraphifyReader(_write_graph(tmp_path, "b.json", GRAPH_B))
    s = Store(tmp_path / "t.db")
    _entity(s, "f_stable", "a.py", "s1")
    first = sync(s, reader)
    assert first.skipped is False
    assert s.get_meta(LAST_SYNCED_KEY) == reader.graph_version()
    assert s.get_meta(LAST_SYNCED_KEY).startswith("vB:")
    second = sync(s, reader)  # same version -> cheap no-op
    assert second.skipped is True and second.outcomes == []
    forced = sync(s, reader, force=True)  # force reruns, all unchanged
    assert forced.skipped is False
    assert forced.counts().get("unchanged") == 1


def test_sync_reports_all_ladder_outcomes(tmp_path):
    # The moved rung fails closed without a resolvable repo_root AND committed evidence
    # (dirty-tree guard, sync.py's _committed_evidence_confirms_move) -- git-init tmp_path
    # and commit d.py so it can confirm c.py -> d.py at HEAD, same as the live checkout
    # the fix targets.
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    (tmp_path / "d.py").write_text("def mover_fn(): pass\n")
    subprocess.run(["git", "add", "d.py"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "d.py lives here now"], cwd=tmp_path, check=True)
    reader = GraphifyReader(_write_graph(tmp_path, "b.json", GRAPH_B))
    s = Store(tmp_path / "t.db")
    _entity(s, "f_stable", "a.py", "s1")  # unchanged
    _entity(s, "mover_fn", "c.py", "m1")  # moved
    _entity(s, "dup_fn", None, "d1")  # ambiguous
    _entity(s, "old_fn", "b.py", "r1")  # orphaned
    report = sync(s, reader)
    assert report.counts() == {"unchanged": 1, "moved": 1, "ambiguous": 1, "orphaned": 1}


def test_stale_scan_flags_all_orphaned_decisions(tmp_path):
    reader = GraphifyReader(_write_graph(tmp_path, "b.json", GRAPH_B))
    s = Store(tmp_path / "t.db")
    gone = _entity(s, "old_fn", "b.py", "r1")
    ok = _entity(s, "f_stable", "a.py", "s1")
    d_stale = _leaf(s, gone.entity_id)  # only anchor -> orphans -> stale
    d_ok = _leaf(s, ok.entity_id)  # stays live -> not stale
    report = sync(s, reader)
    stale_ids = {x["id"] for x in report.stale_decisions}
    assert d_stale.id in stale_ids
    assert d_ok.id not in stale_ids


def test_sync_continues_past_bad_entity(tmp_path):
    reader = GraphifyReader(_write_graph(tmp_path, "b.json", GRAPH_B))
    s = Store(tmp_path / "t.db")
    _entity(s, "f_stable", "a.py", "s1")
    _bad = _entity(s, "old_fn", "b.py", "r1")

    real_resolve = reader.resolve

    def flaky(desc):
        if desc.name == "old_fn":
            raise RuntimeError("boom")
        return real_resolve(desc)

    reader.resolve = flaky  # duck-patch the instance
    report = sync(s, reader)
    assert report.counts().get("error") == 1
    assert report.counts().get("unchanged") == 1  # the good entity still processed
    assert s.get_meta(LAST_SYNCED_KEY) == reader.graph_version()  # completed pass stamps


def test_maybe_sync_none_without_reader(tmp_path):
    assert maybe_sync(Store(tmp_path / "t.db"), None) is None
