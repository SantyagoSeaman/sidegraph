import subprocess

import pytest

from sidegraph.engine.reader import GraphifyReader
from sidegraph.retrieval import Seed, get_task_context
from sidegraph.server import _propose_decisions_impl, _ratify_decisions_impl
from sidegraph.store import Store
from sidegraph.sync import sync

GRAPH_A = {
    "built_at_commit": "vA",
    "nodes": [
        {
            "id": "s1",
            "label": "f_stable()",
            "norm_label": "f_stable()",
            "file_type": "code",
            "source_file": "a.py",
            "community": 1,
        },
        {
            "id": "r1",
            "label": "old_fn()",
            "norm_label": "old_fn()",
            "file_type": "code",
            "source_file": "b.py",
            "community": 1,
        },
        {
            "id": "m1",
            "label": "mover_fn()",
            "norm_label": "mover_fn()",
            "file_type": "code",
            "source_file": "c.py",
            "community": 2,
        },
        {
            "id": "d1",
            "label": "dup_fn()",
            "norm_label": "dup_fn()",
            "file_type": "code",
            "source_file": "e.py",
            "community": 2,
        },
    ],
    "links": [],
}
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


@pytest.fixture(autouse=True)
def _no_ambient_initiative(monkeypatch):
    monkeypatch.setattr("sidegraph.capture._derive_initiative", lambda: None)


def test_rebuild_heals_moved_and_flags_renamed(tmp_path):
    # The moved rung now fails closed without a resolvable repo_root (see sync.py's
    # _resolve_repo_root) -- a real git worktree lets it confirm c.py is genuinely gone,
    # same as the live checkout the fix targets. No commit needed: `git rev-parse
    # --show-toplevel` only requires a `.git` dir. Same convention as
    # test_doctor_code_drift.py's `git_repo` fixture.
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    reader_a = GraphifyReader(_write_graph(tmp_path, "a.json", GRAPH_A))
    store = Store(tmp_path / "e.db")

    # Capture two decisions against graph A (Stage-5 path): one on the mover, one on old_fn.
    results = _propose_decisions_impl(
        store,
        reader_a,
        [
            {
                "title": "mover matters",
                "kind": "gotcha",
                "context": "c",
                "choice": "keep",
                "anchors": [{"name": "mover_fn", "file_path": "c.py"}],
            },
            {
                "title": "old_fn rule",
                "kind": "lesson",
                "context": "c",
                "choice": "keep",
                "anchors": [{"name": "old_fn", "file_path": "b.py"}],
            },
        ],
        session_id="s-sync",
    )
    assert [r["status"] for r in results] == ["written", "written"]
    _ratify_decisions_impl(store, accept=[r["decision_id"] for r in results])

    # Rebuild: node ids shift, mover_fn moves c.py -> d.py, old_fn is renamed away.
    reader_b = GraphifyReader(_write_graph(tmp_path, "b.json", GRAPH_B))
    report = sync(store, reader_b)

    by_name = {o.canonical_name: o for o in report.outcomes}
    assert by_name["mover_fn"].status == "moved"
    assert by_name["old_fn"].status == "orphaned"
    stale_titles = {d["title"] for d in report.stale_decisions}
    assert "old_fn rule" in stale_titles
    assert "mover matters" not in stale_titles

    # The healed entity's decision is still retrievable by its NEW file.
    ctx = get_task_context([Seed(file_path="d.py")], store, reader_b)
    assert any("mover matters" in m for m in ctx.mistakes)

    # Idempotency: a second sync is a cheap no-op.
    assert sync(store, reader_b).skipped is True
