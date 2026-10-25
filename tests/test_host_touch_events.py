"""Touch events from the PreToolUse hook (spec D3/D4/D9).

The nudge and the recording share one handler and nothing else: the nudge keeps its
Read/Grep scope and its once-per-agent ledger, recording runs before every one of the
nudge's gates.
"""

from __future__ import annotations

import io
import json
import os
from datetime import UTC, datetime

import sidegraph.host.hooks as hooks
from sidegraph.hot_index import HotIndex
from sidegraph.schema import Decision, DecisionKind, DecisionStatus, Provenance
from sidegraph.store import Store


def _run(monkeypatch, capsys, payload, db, root, extra_env=None):
    # SIDEGRAPH_DIR, not the deprecated SIDEGRAPH_DB: config._dispatch_sidegraph_db rescues
    # a nonexistent path with a directory component to its PARENT (pinned by
    # test_config.py::test_sidegraph_db_nonexistent_decisions_db_rescued_to_parent_dir) --
    # exactly `db`'s shape on a test's first-ever write, before anything else has created it.
    # SIDEGRAPH_DIR has no such rescue: the literal `db` path is what both this hook call and
    # _events()'s direct Store(db) must agree on.
    monkeypatch.setenv("SIDEGRAPH_DIR", str(db))
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(root))
    for key, value in (extra_env or {}).items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    hooks.pre_tool_use()
    return json.loads(capsys.readouterr().out)


def _payload(tool, session="s1", **tool_input):
    return {"session_id": session, "tool_name": tool, "tool_input": dict(tool_input)}


def _events(db, session="s1"):
    store = Store(db)
    try:
        return store.retrieval_events(session)
    finally:
        store.close()


def _repo(tmp_path):
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src" / "store.py").write_text("x = 1\n")
    return root


def _seed_store(db):
    """The hook records into an existing store and never creates one (it reads the index
    directly, see ``hot_index.HotIndex``), so a test about recording starts from one."""
    Store(db).close()


def test_absolute_payload_path_is_stored_repo_relative(tmp_path, monkeypatch, capsys):
    """THE row that matters (spec row 5). The host only ever sends absolute paths, while
    every anchor is repo-relative — recording the payload verbatim yields a join that is
    permanently empty while a synthetic relative-path test stays green."""
    root = _repo(tmp_path)
    absolute = str(root / "src" / "store.py")
    assert os.path.isabs(absolute)
    _seed_store(tmp_path / "db")

    _run(monkeypatch, capsys, _payload("Read", file_path=absolute), tmp_path / "db", root)

    events = _events(tmp_path / "db")
    assert [e["key"] for e in events] == ["src/store.py"], f"got {events}"
    assert events[0]["detail"] == "Read"


def test_out_of_root_path_records_nothing_but_in_root_does(tmp_path, monkeypatch, capsys):
    """Paired: the absence half alone would be green today."""
    root = _repo(tmp_path)
    outside = tmp_path / "elsewhere.py"
    outside.write_text("y = 2\n")
    _seed_store(tmp_path / "db")

    _run(monkeypatch, capsys, _payload("Read", file_path=str(outside)), tmp_path / "db", root)
    assert _events(tmp_path / "db") == []

    _run(
        monkeypatch,
        capsys,
        _payload("Read", file_path=str(root / "src" / "store.py")),
        tmp_path / "db",
        root,
    )
    assert [e["key"] for e in _events(tmp_path / "db")] == ["src/store.py"]


def test_directory_and_pattern_only_grep_record_nothing_but_a_file_grep_does(
    tmp_path, monkeypatch, capsys
):
    root = _repo(tmp_path)
    _seed_store(tmp_path / "db")

    _run(monkeypatch, capsys, _payload("Grep", pattern="def foo"), tmp_path / "db", root)
    _run(monkeypatch, capsys, _payload("Grep", path=str(root / "src")), tmp_path / "db", root)
    assert _events(tmp_path / "db") == []

    _run(
        monkeypatch,
        capsys,
        _payload("Grep", path=str(root / "src" / "store.py")),
        tmp_path / "db",
        root,
    )
    assert [e["key"] for e in _events(tmp_path / "db")] == ["src/store.py"]


def test_symlinked_root_still_yields_a_joining_key(tmp_path, monkeypatch, capsys):
    """Red against a lexical-only relpath: with the root reached through a symlink, every
    result would start with `..` and be dropped, and D9's swallow would hide it. This repo
    is safe today, which is exactly why the test builds the symlink itself."""
    root = _repo(tmp_path)
    link = tmp_path / "link"
    link.symlink_to(root)
    _seed_store(tmp_path / "db")

    _run(
        monkeypatch,
        capsys,
        _payload("Read", file_path=str(root / "src" / "store.py")),
        tmp_path / "db",
        link,
    )

    assert [e["key"] for e in _events(tmp_path / "db")] == ["src/store.py"]


def test_recording_survives_every_nudge_gate(tmp_path, monkeypatch, capsys):
    """Four legs, one per nudge-only exit (tool_name, ledger, empty store, env). The
    exit on a missing file path is untargetable — any payload recordable under D3 passes it."""
    root = _repo(tmp_path)
    db = tmp_path / "db"
    target = str(root / "src" / "store.py")
    _seed_store(db)

    # empty store — an empty store has no memory to nudge toward, but still records.
    _run(monkeypatch, capsys, _payload("Read", file_path=target), db, root)
    assert len(_events(db)) == 1

    store = Store(db)
    store.add_decision(
        Decision(
            title="t",
            kind=DecisionKind.ADR,
            status=DecisionStatus.ACCEPTED,
            context="c",
            choice="ch",
            valid_from=datetime.now(UTC),
            provenance=Provenance(source="manual"),
        )
    )
    store.close()

    # ledger — the nudge fires once; the 2nd and 3rd Read still record.
    _run(monkeypatch, capsys, _payload("Read", file_path=target), db, root)
    _run(monkeypatch, capsys, _payload("Read", file_path=target), db, root)
    assert len(_events(db)) == 3

    # tool_name — the nudge never fires for Edit; recording does.
    _run(monkeypatch, capsys, _payload("Edit", file_path=target), db, root)
    assert [e["detail"] for e in _events(db)][-1] == "Edit"

    # env — the grep nudge is off entirely; recording is unaffected.
    _run(
        monkeypatch,
        capsys,
        _payload("Write", file_path=target),
        db,
        root,
        {"SIDEGRAPH_GREP_NUDGE": "off"},
    )
    assert [e["detail"] for e in _events(db)][-1] == "Write"


def test_payload_without_session_id_records_nothing_but_with_one_does(
    tmp_path, monkeypatch, capsys
):
    root = _repo(tmp_path)
    db = tmp_path / "db"
    target = str(root / "src" / "store.py")

    _seed_store(db)

    # No mutant turns this red: the NOT NULL column backs the gate. It pins the user-visible
    # end-to-end behaviour only.
    no_id = {"tool_name": "Read", "tool_input": {"file_path": target}}
    # SIDEGRAPH_DIR — same reasoning as _run() above: SIDEGRAPH_DB's legacy-dispatch rescue
    # would otherwise defang this test's absence assertion below.
    monkeypatch.setenv("SIDEGRAPH_DIR", str(db))
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(root))
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(no_id)))
    hooks.pre_tool_use()
    capsys.readouterr()
    store = Store(db)
    assert store.retrieval_events() == []
    store.close()

    _run(monkeypatch, capsys, _payload("Read", file_path=target), db, root)
    assert len(_events(db)) == 1


def test_empty_session_id_records_nothing_but_a_real_one_does(tmp_path, monkeypatch, capsys):
    """An empty `session_id` records nothing. The `not session_id` gate in the hook is
    defence in depth behind `_session_identity`, which already turns `""` into `None`; a
    `None` would then hit the `NOT NULL` column and the swallowed `IntegrityError`. No single
    mutant of either layer turns this red, only removing both does, so the test pins the
    end-to-end behaviour rather than one branch."""
    root = _repo(tmp_path)
    db = tmp_path / "db"
    target = str(root / "src" / "store.py")

    _seed_store(db)

    _run(monkeypatch, capsys, _payload("Read", session="", file_path=target), db, root)
    store = Store(db)
    assert store.retrieval_events() == []  # unfiltered: no row under ANY session id
    store.close()

    _run(monkeypatch, capsys, _payload("Read", file_path=target), db, root)
    assert len(_events(db)) == 1


def test_telemetry_off_records_nothing_while_on_records(tmp_path, monkeypatch, capsys):
    root = _repo(tmp_path)
    db = tmp_path / "db"
    target = str(root / "src" / "store.py")
    _seed_store(db)

    out = _run(
        monkeypatch,
        capsys,
        _payload("Read", file_path=target),
        db,
        root,
        {"SIDEGRAPH_TELEMETRY": "off"},
    )
    assert _events(db) == []
    assert isinstance(out, dict)  # the hook still answered normally

    monkeypatch.delenv("SIDEGRAPH_TELEMETRY", raising=False)
    _run(monkeypatch, capsys, _payload("Read", file_path=target), db, root)
    assert len(_events(db)) == 1


def test_an_index_failure_does_not_break_the_hook(tmp_path, monkeypatch, capsys):
    """The hook writes through ``HotIndex``, not ``Store``: a failing touch write must still
    leave the hook printing valid JSON."""
    root = _repo(tmp_path)
    _seed_store(tmp_path / "db")

    def boom(*args, **kwargs):
        raise RuntimeError("index is on fire")

    monkeypatch.setattr(HotIndex, "record_touch", boom)
    out = _run(
        monkeypatch,
        capsys,
        _payload("Read", file_path=str(root / "src" / "store.py")),
        tmp_path / "db",
        root,
    )
    assert isinstance(out, dict)  # valid JSON, hook did not crash


def test_a_codex_touch_is_recorded_against_its_thread_not_the_workspace(
    tmp_path, monkeypatch, capsys
):
    """Codex's payload `session_id` is the umbrella workspace session — stable across days and
    every thread beneath it (hooks._session_identity). Recording against it put a whole
    store's history in one bucket: 1928 of 1929 events in a live store, across 27 hours.
    Red against reading that field directly."""
    db = tmp_path / "db"
    root = tmp_path
    (root / "a.py").write_text("x = 1\n")
    _seed_store(db)
    umbrella = "01a0b3e2-39d2-7180-86a5-408a6f9ce058"
    thread = "rollout-2026-09-19T12-16-06-01a0b961-7463"
    payload = {
        "session_id": umbrella,
        "tool_name": "Read",
        "tool_input": {"file_path": str(root / "a.py")},
        "transcript_path": f"/w/.codex/sessions/2026/09/19/{thread}.jsonl",
    }

    _run(monkeypatch, capsys, payload, db, root)

    assert _events(db, umbrella) == []
    assert len(_events(db, thread)) == 1
