"""Exact-path file delivery through the PreToolUse hook."""

from __future__ import annotations

import io
import json
import sqlite3
from datetime import UTC, datetime

import pytest

from sidegraph.engine.reader import GraphifyReader
from sidegraph.host import hook_log, hooks
from sidegraph.hot_index import HotIndex
from sidegraph.schema import DecisionKind, Fact, Provenance
from sidegraph.store_layout import FILE_BOUND_LEGEND, GRAPH_FILES_STAMP_KEY, VOLATILE_STALE_KEY
from sidegraph.sync import LAST_SYNCED_KEY, sync
from tests.test_host_pretool_records import GUARD, _files_in, _payload, _run, _text
from tests.test_host_pretool_records import project as project
from tests.test_hot_index import _Seeder
from tests.test_sync_run import _write_graph


def _graph(project, version, files):
    nodes = [
        {
            "id": f"n{i}",
            "label": f"{name}()",
            "norm_label": f"{name}()",
            "file_type": "code",
            "source_file": path,
        }
        for i, (path, name) in enumerate(files)
    ]
    graph = {"built_at_commit": version, "nodes": nodes, "edges": []}
    return GraphifyReader(_write_graph(project.root, f"{version}.json", graph))


def _read(project, monkeypatch, capsys, rel):
    return _run(monkeypatch, capsys, project, _payload("Read", file_path=str(project.root / rel)))


def _cat(project, monkeypatch, capsys, *rels, session):
    return _run(
        monkeypatch, capsys, project, _payload("Bash", session, command="cat " + " ".join(rels))
    )


def _fact(project, rel, line_chars):
    source = "Synthetic fixture."
    overhead = len(f"- [fact]  — Source: {source} (id {'0' * 26})")
    fact = project.store.add_fact(
        Fact(
            statement="x" * (line_chars - overhead),
            source=source,
            status="accepted",
            valid_from=datetime.now(UTC),
            provenance=Provenance(source="manual"),
        )
    )
    _Seeder(project.store).anchor(fact, rel)
    project.file(rel)


def _three_records(project, rel, *, cold):
    if cold:
        project.file(rel)
        seed = _Seeder(project.store)
        for i in range(3):
            seed.anchor(
                seed.decision(f"Rule {i}", DecisionKind.ADR),
                rel,
                status="orphaned" if i == 2 else "live",
            )
        assert project.store.get_meta(LAST_SYNCED_KEY) is None
        assert project.store.get_meta(VOLATILE_STALE_KEY) == "1"
    else:
        for i in range(3):
            project.record(rel, f"Rule {i}", kind=DecisionKind.ADR)


def test_synced_outside_graph_file_delivers_tagged_records(project, monkeypatch, capsys):
    _three_records(project, ".gitignore", cold=False)
    sync(project.store, _graph(project, "v1", [("src/a.py", "Other")]))
    assert all(
        binding.status == "orphaned"
        for entity in project.store.iter_concrete_entities()
        for binding in project.store.bindings_for_entity(entity.entity_id)
    )
    text = _text(_read(project, monkeypatch, capsys, ".gitignore"))
    lines = text.splitlines()
    assert lines[1] == "Recorded for .gitignore (2 of 3, mistakes first):"
    assert all(line.startswith("- [adr] [file-bound] ") for line in lines[2:4])
    assert lines[4] == 'More: get_task_context(files=[".gitignore"]).'
    assert lines[-1] == FILE_BOUND_LEGEND


@pytest.mark.parametrize("rel", ["src/a.py", ".gitignore"], ids=["graph-held", "missing-file"])
def test_file_must_exist_and_be_outside_synced_graph(project, monkeypatch, capsys, rel):
    project.record(rel, "Rule with a missing graph symbol")
    sync(project.store, _graph(project, "v1", [("src/a.py", "Other")]))
    if rel == ".gitignore":
        (project.root / rel).unlink()
    assert _read(project, monkeypatch, capsys, rel) == {}


def test_never_synced_delivers_without_more_pointer(project, monkeypatch, capsys):
    _three_records(project, "src/a.py", cold=True)
    lines = _text(_read(project, monkeypatch, capsys, "src/a.py")).splitlines()
    assert lines[1] == "Recorded for src/a.py (2 of 3, mistakes first):"
    assert all(line.startswith("- [adr] [file-bound] ") for line in lines[2:4])
    assert lines[4:] == [FILE_BOUND_LEGEND]
    assert [(r.hook, r.outcome) for r in hook_log.read_runs(project.store_dir)] == [
        ("PreToolUse", "delivered")
    ]
    rel = "src/a.py"
    sync(project.store, _graph(project, "v1", [(rel, f"Sym{i}") for i in range(1, 4)]))
    after = _run(
        monkeypatch,
        capsys,
        project,
        _payload("Read", session="after-sync", file_path=str(project.root / rel)),
    )
    lines = _text(after).splitlines()
    assert lines[1] == "Recorded for src/a.py (2 of 3, mistakes first):"
    assert all(line.startswith("- [adr] Rule ") for line in lines[2:4])
    assert lines[4:] == ['More: get_task_context(files=["src/a.py"]).']


def test_warm_unstamped_delivery_is_unchanged(project, monkeypatch, capsys):
    record = project.record("src/a.py", "Warm fixture")
    _graph(project, "v1", [("src/a.py", "Sym1")])
    assert project.store.get_meta(VOLATILE_STALE_KEY) == "0"
    assert project.store.get_meta(LAST_SYNCED_KEY) is None
    assert project.store.get_meta(GRAPH_FILES_STAMP_KEY) is None
    assert _text(_read(project, monkeypatch, capsys, "src/a.py")) == (
        f"{GUARD}\nRecorded for src/a.py (1 of 1, mistakes first):\n"
        f"- [gotcha] Warm fixture — Do the thing. (id {record.id})"
    )


def test_never_synced_with_oversize_fact_logs_no_record(project, monkeypatch, capsys):
    _fact(project, "src/a.py", 513)
    assert project.store.get_meta(LAST_SYNCED_KEY) is None
    assert _read(project, monkeypatch, capsys, "src/a.py") == {}
    assert [(r.hook, r.outcome, r.detail) for r in hook_log.read_runs(project.store_dir)] == [
        ("PreToolUse", "empty", "no-record")
    ]


def test_exact_candidate_without_renderable_records_reserves_no_legend(
    project, monkeypatch, capsys
):
    ordinary = ("src/a.py", "src/b.py")
    project.anchored(*ordinary)
    _fact(project, ".gitignore", 500)
    sync(project.store, _graph(project, "v1", list(zip(ordinary, ("Sym1", "Sym2"), strict=True))))
    baseline = _text(_cat(project, monkeypatch, capsys, *ordinary, session="original"))
    room = len(baseline) + len(hooks._PRETOOL_OMITTED.format(count=9999)) + 50
    monkeypatch.setattr(hooks, "HOOK_CONTEXT_MAX_CHARS", room)
    ordinary_only = _text(_cat(project, monkeypatch, capsys, *ordinary, session="tight"))
    mixed = _text(_cat(project, monkeypatch, capsys, ".gitignore", *ordinary, session="mixed"))
    assert _files_in(baseline) == list(ordinary)
    assert ordinary_only == mixed == baseline


def test_stale_membership_after_failed_sync_does_not_enable_delivery(project, monkeypatch, capsys):
    project.record("src/x.py", "Rule with a missing graph symbol")
    old = _graph(project, "v1", [("src/a.py", "Other")])
    new = _graph(project, "v2", [("src/x.py", "Other")])
    sync(project.store, old)

    def fail(*_args):
        raise RuntimeError("Synthetic graph file refresh failure")

    monkeypatch.setattr(project.store, "replace_graph_files", fail)
    sync(project.store, new)
    assert project.store.get_meta(GRAPH_FILES_STAMP_KEY) == old.sync_stamp()
    assert project.store.get_meta(LAST_SYNCED_KEY) == new.sync_stamp()
    assert _read(project, monkeypatch, capsys, "src/x.py") == {}


@pytest.mark.parametrize("line_chars", [499, 500])
def test_exact_fact_limit_includes_file_bound_tag(project, monkeypatch, capsys, line_chars):
    """499 needs exact delivery; 500 guards against admission without the tag."""
    _fact(project, ".gitignore", line_chars)
    sync(project.store, _graph(project, "v1", [("src/a.py", "Other")]))
    out = _read(project, monkeypatch, capsys, ".gitignore")
    if line_chars == 500:
        assert out == {}
    else:
        (line,) = [line for line in _text(out).splitlines() if line.startswith("- [fact]")]
        assert line.startswith("- [fact] [file-bound] ")
        assert len(line) == 512


def test_never_synced_omission_has_no_context_pointer(project, monkeypatch, capsys):
    files = ("src/a.py", "src/b.py", "src/c.py")
    seed = _Seeder(project.store)
    for rel in files:
        project.file(rel)
        seed.anchor(seed.decision("One small record", DecisionKind.ADR), rel)
    monkeypatch.setattr(hooks, "HOOK_CONTEXT_MAX_CHARS", 700)
    text = _text(_cat(project, monkeypatch, capsys, *files, session="three"))
    assert _files_in(text) == list(files[:2])
    assert text.splitlines()[-2] == FILE_BOUND_LEGEND
    assert text.splitlines()[-1] == (
        "Sidegraph: the records of 1 more file(s) were left out to fit the hook output limit."
    )


def test_never_synced_room_reserves_only_bare_omission(project, monkeypatch, capsys):
    files = ("a.py", "b.py", "c.py")
    seed = _Seeder(project.store)
    for rel in files:
        project.file(rel)
        seed.anchor(seed.decision("Small record", DecisionKind.ADR), rel)
    full = _text(_cat(project, monkeypatch, capsys, *files, session="full"))
    bare_room = 1 + len(hooks._PRETOOL_OMITTED_BARE.format(count=9999))
    monkeypatch.setattr(hooks, "HOOK_CONTEXT_MAX_CHARS", len(full) + bare_room)
    tight = _text(_cat(project, monkeypatch, capsys, *files, session="tight"))
    assert _files_in(tight) == list(files)
    assert tight == full


def _deny_membership(monkeypatch):
    original = HotIndex.exact_path_files

    def unreadable(index, *args, **kwargs):
        def authorize(_action, table, *_args):
            return sqlite3.SQLITE_DENY if table == "graph_files" else sqlite3.SQLITE_OK

        index._conn.set_authorizer(authorize)
        try:
            return original(index, *args, **kwargs)
        finally:
            index._conn.set_authorizer(None)

    monkeypatch.setattr(HotIndex, "exact_path_files", unreadable)


def test_failed_membership_then_retry_cannot_shift_three_file_selection(
    project, monkeypatch, capsys
):
    files = ("external.py", "a.py", "b.py", "c.py")
    project.anchored(*files)
    graph_files = list(zip(files[1:], ("Sym2", "Sym3", "Sym4"), strict=True))
    sync(project.store, _graph(project, "v1", graph_files))
    with monkeypatch.context() as patch:
        _deny_membership(patch)
        first = _cat(project, patch, capsys, *files, session="same-call")
    assert first == {}
    assert not project.meta()
    retry = _text(_cat(project, monkeypatch, capsys, *files, session="same-call"))
    assert _files_in(retry) == list(files[1:])


def test_missing_membership_table_keeps_ordinary_multifile_delivery(project, monkeypatch, capsys):
    files = ("a.py", "b.py")
    project.anchored(*files)
    sync(project.store, _graph(project, "v1", [("a.py", "Sym1"), ("b.py", "Sym2")]))
    assert project.store.get_meta(GRAPH_FILES_STAMP_KEY) == project.store.get_meta(LAST_SYNCED_KEY)
    with project.store._mutation():
        project.store._conn.execute("DROP TABLE graph_files")
    out = _cat(project, monkeypatch, capsys, *files, session="legacy")
    assert out
    assert _files_in(_text(out)) == list(files)
    assert "[file-bound]" not in _text(out)


def test_exact_path_back_reference_carries_the_tag(project, monkeypatch, capsys):
    seed = _Seeder(project.store)
    record = seed.decision("Shared rule", DecisionKind.ADR)
    for rel in ("a.py", "b.py"):
        project.file(rel)
        seed.anchor(record, rel)
    text = _text(_cat(project, monkeypatch, capsys, "a.py", "b.py", session="shared"))
    assert f"- [adr] [file-bound] See above (id {record.id})" in text
    assert text.count(FILE_BOUND_LEGEND) == 1


def test_full_off_graph_allowance_leaves_room_for_graph_held_file(project, monkeypatch, capsys):
    paths = [f"off-{i}.txt" for i in range(11)]
    project.anchored("base.py", *paths)
    sync(project.store, _graph(project, "v1", [("base.py", "Sym1")]))
    for rel in paths[:10]:
        assert _read(project, monkeypatch, capsys, rel)
    assert _read(project, monkeypatch, capsys, paths[0]) == {}
    assert _read(project, monkeypatch, capsys, paths[-1]) == {}
    assert [row.detail for row in hook_log.read_runs(project.store_dir)][-2:] == ["repeat", "cap"]
    assert _files_in(_text(_read(project, monkeypatch, capsys, "base.py"))) == ["base.py"]

    from tests.test_cap_reset import _session_start

    _session_start(monkeypatch, capsys, project, "compact")
    assert _read(project, monkeypatch, capsys, paths[0])
    assert _read(project, monkeypatch, capsys, "base.py")
    assert _session_start(monkeypatch, capsys, project, "compact") == {}
    assert _read(project, monkeypatch, capsys, paths[0]) == {}
    assert _read(project, monkeypatch, capsys, "base.py") == {}


def test_graph_held_file_precedes_three_off_graph_candidates(project, monkeypatch, capsys):
    paths = ("off-0.txt", "off-1.txt", "off-2.txt", "base.py")
    project.anchored(*paths)
    sync(project.store, _graph(project, "v1", [("base.py", "Sym4")]))
    text = _text(_cat(project, monkeypatch, capsys, *paths, session="priority"))
    assert _files_in(text) == ["base.py", *paths[:2]]


def test_single_file_membership_failure_keeps_ordinary_delivery(project, monkeypatch, capfd):
    rel = "a.py"
    project.anchored(rel)
    sync(project.store, _graph(project, "v1", [(rel, "Sym1")]))
    _deny_membership(monkeypatch)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project.root))
    monkeypatch.setenv("SIDEGRAPH_DIR", str(project.store_dir))
    payload = _payload("Read", file_path=str(project.root / rel))
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    hooks.pre_tool_use()
    captured = capfd.readouterr()
    assert _files_in(_text(json.loads(captured.out))) == [rel]
    (diagnostic,) = captured.err.splitlines()
    assert json.loads(diagnostic) == {
        "sidegraph_hook_error": True,
        "hook": "PreToolUse",
        "stage": "delivery",
        "exception": "DatabaseError",
    }
