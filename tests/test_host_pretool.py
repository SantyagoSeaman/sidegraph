"""``pre_tool_use`` over the older seeding helpers: what the hook prints and what it spares.

See ``docs/reference/hooks.md#sidegraph-pre-tool-use``: on a Read, Grep, Edit, Write or Bash read
command that names a file with records anchored to it, the hook hands the agent those records as a
non-blocking ``additionalContext`` block — never a hard block — once per file per agent. The
delivery tests proper (the block byte for byte, the cap, the Bash lines, the concurrent
processes) are in ``tests/test_host_pretool_records.py``; this file keeps the behaviours that
older reviews pinned and the seeding helpers other tests import.

see design/superpowers/specs/2026-10-03-records-at-the-point-of-reading-design.md
"""

import io
import json
from datetime import UTC, datetime

import sidegraph.host.hooks as hooks
from sidegraph.schema import Decision, DecisionKind, DecisionStatus, Provenance
from sidegraph.store import Store


def _run(monkeypatch, capsys, payload, db, extra_env=None):
    monkeypatch.setenv("SIDEGRAPH_DB", str(db))
    for key, value in (extra_env or {}).items():
        monkeypatch.setenv(key, value)
    stdin = payload if isinstance(payload, str) else json.dumps(payload)
    monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    hooks.pre_tool_use()
    return json.loads(capsys.readouterr().out)


def _seed_one_decision(db) -> None:
    store = Store(db)
    store.add_decision(
        Decision(
            title="Use SQLite for the store",
            kind=DecisionKind.ADR,
            status=DecisionStatus.ACCEPTED,
            context="c",
            choice="ch",
            valid_from=datetime.now(UTC),
            provenance=Provenance(source="manual"),
        )
    )


READ_PAYLOAD = {
    "session_id": "p1",
    "tool_name": "Read",
    "tool_input": {"file_path": "src/sidegraph/server.py"},
}


def _seed_server_py(db, title: str = "Never write into graph.json", **kwargs) -> None:
    """One record anchored to the file ``READ_PAYLOAD`` reads."""
    _seed_anchored_decision(db, "src/sidegraph/server.py", title, **kwargs)


def test_pretool_delivers_the_records_once_per_file(tmp_path, monkeypatch, capsys):
    """Was ``..._fires_once_per_session``: the one-shot is per file per agent now, so the second
    Read of the same file is spared and the first is not."""
    db = tmp_path / "s.db"
    _seed_server_py(db)
    first = _run(monkeypatch, capsys, READ_PAYLOAD, db)
    hso = first["hookSpecificOutput"]
    assert hso["hookEventName"] == "PreToolUse"
    assert "permissionDecision" not in hso
    assert "Never write into graph.json" in hso["additionalContext"]
    assert _run(monkeypatch, capsys, READ_PAYLOAD, db) == {}


def test_pretool_new_session_delivers_again(tmp_path, monkeypatch, capsys):
    db = tmp_path / "s.db"
    _seed_server_py(db)
    _run(monkeypatch, capsys, READ_PAYLOAD, db)
    other = dict(READ_PAYLOAD, session_id="p2")
    out = _run(monkeypatch, capsys, other, db)
    assert "hookSpecificOutput" in out


def test_pretool_respects_off_switch(tmp_path, monkeypatch, capsys):
    """Seeded with a record on the file read, so that ``{}`` can only be the switch: the
    unanchored seed this test used before would print nothing with or without it."""
    db = tmp_path / "s.db"
    _seed_server_py(db)
    out = _run(monkeypatch, capsys, READ_PAYLOAD, db, extra_env={"SIDEGRAPH_GREP_NUDGE": "off"})
    assert out == {}
    monkeypatch.delenv("SIDEGRAPH_GREP_NUDGE")
    # nothing was claimed while it was off: the same Read in the same session delivers now
    assert "hookSpecificOutput" in _run(monkeypatch, capsys, READ_PAYLOAD, db)


def test_pretool_pattern_only_grep_names_no_file(tmp_path, monkeypatch, capsys):
    """Was ``..._fires_for_grep``: a pattern-only Grep got the counting form. It names no file,
    so there are no records to deliver and the hook prints nothing."""
    db = tmp_path / "s.db"
    _seed_server_py(db)
    payload = {"session_id": "p3", "tool_name": "Grep", "tool_input": {"pattern": "foo"}}
    assert _run(monkeypatch, capsys, payload, db) == {}


def test_pretool_silent_for_other_tools(tmp_path, monkeypatch, capsys):
    db = tmp_path / "s.db"
    _seed_server_py(db)
    for tool, tin in (
        ("Glob", {"pattern": "src/sidegraph/server.py"}),
        ("WebFetch", {"url": "src/sidegraph/server.py"}),
        ("Bash", {"command": "ls src/sidegraph/server.py"}),
    ):
        payload = {"session_id": "p4", "tool_name": tool, "tool_input": tin}
        assert _run(monkeypatch, capsys, payload, db) == {}, tool


def test_pretool_edit_and_write_of_files_without_records_print_nothing(
    tmp_path, monkeypatch, capsys
):
    """Edit and Write deliver records now (``test_host_pretool_records.py``); with none
    anchored to the file they print nothing, and a Write that creates a file has none to give."""
    db = tmp_path / "s.db"
    _seed_server_py(db)
    for i, (tool, tin) in enumerate(
        [("Edit", {"file_path": "src/x.py"}), ("Write", {"file_path": "src/y.py"})]
    ):
        payload = {"session_id": f"pew{i}", "tool_name": tool, "tool_input": tin}
        out = _run(monkeypatch, capsys, payload, db)
        assert out == {}, tool


def test_pretool_silent_when_no_string_target(tmp_path, monkeypatch, capsys):
    db = tmp_path / "s.db"
    _seed_server_py(db)
    payload = {"session_id": "p5", "tool_name": "Read", "tool_input": {}}
    out = _run(monkeypatch, capsys, payload, db)
    assert out == {}


def test_pretool_silent_when_store_empty(tmp_path, monkeypatch, capsys):
    db = tmp_path / "s.db"
    Store(db)  # create an empty store, no decisions or domains
    out = _run(monkeypatch, capsys, READ_PAYLOAD, db)
    assert out == {}


def test_pretool_use_never_raises_on_garbage_stdin(tmp_path, monkeypatch, capsys):
    db = tmp_path / "s.db"
    _seed_one_decision(db)
    out = _run(monkeypatch, capsys, "not valid json{", db)
    assert out == {}


def test_pretool_use_never_raises_on_index_failure(tmp_path, monkeypatch, capsys):
    """The hook reads through ``HotIndex``, not ``Store``: a failing open must print ``{}``.

    Telemetry is off so that the touch step, which has its own guard and would otherwise be
    the first to open the index, leaves the nudge to meet the failure and its own guard.
    """
    db = tmp_path / "s.db"
    _seed_one_decision(db)

    def boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr("sidegraph.hot_index.HotIndex.open", boom)
    out = _run(monkeypatch, capsys, READ_PAYLOAD, db, extra_env={"SIDEGRAPH_TELEMETRY": "off"})
    assert out == {}


def test_pretool_nudge_missing_session_id_allows_silently(tmp_path, monkeypatch, capsys):
    db = tmp_path / "s.db"
    _seed_one_decision(db)
    payload = {"tool_name": "Read", "tool_input": {"file_path": "x.py"}}
    out = _run(monkeypatch, capsys, payload, db)
    assert out == {}


# -- what the hook says about the file being read (whitepaper Phase-1 finding) ---------------
#
# Measured 2026-07-31 across 168 arm-A sessions: the nudge of that time FIRED (its ledger keys
# are in the store; the transcript of that Claude Code version never showed it, 2.1.259+ records
# it as `hook_additional_context` attachments) and the agent read the file anyway. 40-45% of
# sessions on code corpora paid for memory and never called retrieval, and those sessions scored
# BELOW the no-memory control. The nudge announced that memory exists, or named titles, without
# handing the agent what memory holds about the path being opened. It hands over the records now.


def _seed_anchored_decision(
    db, file_path: str, title: str, kind=DecisionKind.GOTCHA, status=None, valid_from=None
) -> None:
    from sidegraph.schema import AnchorBinding, Descriptor, Entity

    store = Store(db)
    d = store.add_decision(
        Decision(
            title=title,
            kind=kind,
            status=status or DecisionStatus.ACCEPTED,
            context="c",
            choice="ch",
            valid_from=valid_from or datetime.now(UTC),
            provenance=Provenance(source="manual"),
        )
    )
    e = store.upsert_entity(
        Entity(
            canonical_name=title.split()[0],
            descriptor=Descriptor(name=title.split()[0], file_path=file_path),
        )
    )
    store.add_binding(AnchorBinding(record_id=d.id, entity_id=e.entity_id, tier=2, status="live"))


def test_the_block_names_what_memory_holds_about_this_path(tmp_path, monkeypatch, capsys):
    """Was ``test_nudge_names_what_memory_holds_about_this_path``: the record itself, its kind,
    and the file it is anchored to, behind the guard line."""
    db = tmp_path / "s.db"
    _seed_server_py(db)
    out = _run(monkeypatch, capsys, READ_PAYLOAD, db)
    text = out["hookSpecificOutput"]["additionalContext"]
    assert text.startswith("[Sidegraph memory: stored project records")
    assert "Recorded for src/sidegraph/server.py (1 of 1, mistakes first):" in text
    assert "- [gotcha] Never write into graph.json — ch (id " in text


def test_two_mistakes_lead_and_the_adr_follows_the_rest_stay_behind_more(
    tmp_path, monkeypatch, capsys
):
    """Was ``test_nudge_puts_mistakes_first_and_caps_the_list``: mistakes-first is the
    product's one hard ranking guarantee and the block must not invert it. With a second ADR
    behind them the block shows the two mistakes and ONE ADR (three, since the first two are
    mistakes), leaves the other ADR to the "More:" line, and stays a short block."""
    db = tmp_path / "s.db"
    path = "src/sidegraph/server.py"
    _seed_anchored_decision(db, path, "Adr one choice", kind=DecisionKind.ADR)
    _seed_anchored_decision(db, path, "Adr two choice", kind=DecisionKind.ADR)
    _seed_anchored_decision(db, path, "Gotcha bites here", kind=DecisionKind.GOTCHA)
    _seed_anchored_decision(db, path, "Lesson learned twice", kind=DecisionKind.LESSON)
    out = _run(monkeypatch, capsys, READ_PAYLOAD, db)
    text = out["hookSpecificOutput"]["additionalContext"]
    lines = text.splitlines()
    assert "Gotcha bites here" in lines[2] or "Gotcha bites here" in lines[3]
    assert "Lesson learned twice" in lines[2] or "Lesson learned twice" in lines[3]
    assert "(3 of 4, mistakes first)" in lines[1]
    assert ("Adr one choice" in lines[4]) != ("Adr two choice" in lines[4])
    assert lines[5] == f'More: get_task_context(files=["{path}"]).'
    assert len(text) < 1100


def test_the_block_is_spent_on_accepted_before_proposed(tmp_path, monkeypatch, capsys):
    """Was ``test_pretool_cap_is_spent_on_accepted_before_proposed``: a proposal comes after
    every accepted record, so a block that shows two leaves the newest proposed one to the
    "More:" line."""
    db = tmp_path / "s.db"
    path = "src/sidegraph/server.py"
    _seed_anchored_decision(db, path, "Accepted gotcha", kind=DecisionKind.GOTCHA)
    _seed_anchored_decision(db, path, "Accepted ADR", kind=DecisionKind.ADR)
    _seed_anchored_decision(
        db,
        path,
        "Newest proposed gotcha",
        kind=DecisionKind.GOTCHA,
        status=DecisionStatus.PROPOSED,
    )
    assert [(d.title, proposed) for d, proposed in hooks._records_for_path(Store(db), path)] == [
        ("Accepted gotcha", False),
        ("Accepted ADR", False),
        ("Newest proposed gotcha", True),
    ]
    text = _run(monkeypatch, capsys, READ_PAYLOAD, db)["hookSpecificOutput"]["additionalContext"]
    assert "Accepted gotcha" in text and "Accepted ADR" in text
    assert "Newest proposed gotcha" not in text
    assert "(2 of 3, mistakes first)" in text


def test_a_read_of_an_unanchored_file_prints_nothing_and_spends_nothing(
    tmp_path, monkeypatch, capsys
):
    """Was ``test_generic_nudge_does_not_consume_the_path_specific_one`` (review finding 3,
    measured: 94% of first Read/Grep calls on this repo land on an unanchored spec or plan). A
    read of a file with no records prints nothing and takes nothing from the agent, so the first
    read of an anchored file in the same session still gets its block."""
    db = tmp_path / "s.db"
    _seed_anchored_decision(db, "src/sidegraph/retrieval.py", "Budget is char-based")
    assert _run(monkeypatch, capsys, READ_PAYLOAD, db) == {}  # server.py: nothing anchored
    covered = {
        "session_id": "p1",
        "tool_name": "Read",
        "tool_input": {"file_path": "src/sidegraph/retrieval.py"},
    }
    second = _run(monkeypatch, capsys, covered, db)  # SAME session, anchored path
    assert "Budget is char-based" in second["hookSpecificOutput"]["additionalContext"]
    assert _run(monkeypatch, capsys, covered, db) == {}  # and that file is delivered once


def test_records_are_newest_first_within_the_mistakes_bucket(tmp_path, monkeypatch, capsys):
    """Was ``test_titles_are_newest_first_within_the_mistakes_bucket`` (review finding 2): scan
    order is ULID mint order, so without an explicit sort the block spent itself on the OLDEST
    records of a busy path. Four gotchas, three shown: the oldest is the one left out."""
    from datetime import timedelta

    db = tmp_path / "s.db"
    path = "src/sidegraph/server.py"
    old = datetime.now(UTC) - timedelta(days=30)
    for title, when in (
        ("Oldest gotcha", old),
        ("Middle gotcha", old + timedelta(days=10)),
        ("Newer gotcha", old + timedelta(days=20)),
    ):
        _seed_anchored_decision(db, path, title, valid_from=when)
    _seed_anchored_decision(db, path, "Newest gotcha")
    text = _run(monkeypatch, capsys, READ_PAYLOAD, db)["hookSpecificOutput"]["additionalContext"]
    assert "Newest gotcha" in text and "Newer gotcha" in text and "Middle gotcha" in text
    assert "Oldest gotcha" not in text


def test_proposed_record_is_tagged_unratified(tmp_path, monkeypatch, capsys):
    """Review finding 5: retrieval always tags a PROPOSED record; the block must not present an
    unreviewed draft as settled memory."""
    db = tmp_path / "s.db"
    _seed_anchored_decision(
        db, "src/sidegraph/server.py", "Draft ruling", status=DecisionStatus.PROPOSED
    )
    text = _run(monkeypatch, capsys, READ_PAYLOAD, db)["hookSpecificOutput"]["additionalContext"]
    assert "Draft ruling [unratified]" in text


def test_title_is_clipped_and_collapsed_to_one_line(tmp_path, monkeypatch, capsys):
    """Was ``test_title_is_clipped_and_sanitised`` (review findings 6 and 7): one ADR-scale
    title must not swallow the block, and a newline inside a title must not break its line. The
    old form wrapped the title in quotes, so it replaced the quotes in it; this one does not
    quote, so a title keeps its own."""
    db = tmp_path / "s.db"
    _seed_anchored_decision(
        db, "src/sidegraph/server.py", 'Never write\ninto "graph.json" ' + "x " * 100
    )
    text = _run(monkeypatch, capsys, READ_PAYLOAD, db)["hookSpecificOutput"]["additionalContext"]
    record = text.splitlines()[2]
    assert record.startswith('- [gotcha] Never write into "graph.json" x x')
    assert "…" in record
    assert len(text.splitlines()) == 3  # guard, header, one record: no stray line from the title


# -- Codex: one umbrella session id, several threads ----------------------------------------
#
# Codex's payload `session_id` is the workspace session, not this session: it spans days and
# covers every thread under it (see hooks._session_identity). The one-shot ledger is keyed on
# the session, so reading that field directly spent the nudge for a whole workspace on the
# first thread that read a file.

_CODEX_ROLLOUTS = (
    "/w/.codex/sessions/2026/09/19/rollout-2026-09-19T12-00-31-01a0b953-2d7c.jsonl",
    "/w/.codex/sessions/2026/09/19/rollout-2026-09-19T12-16-06-01a0b961-7463.jsonl",
)


def test_each_codex_thread_gets_its_own_block(tmp_path, monkeypatch, capsys):
    """Red against a ledger keyed on payload['session_id']: the second thread — a different
    session reporting the same umbrella id — was silently treated as already delivered to."""
    db = tmp_path / "s.db"
    _seed_server_py(db)
    umbrella = "01a0b3e2-39d2-7180-86a5-408a6f9ce058"

    outs = [
        _run(
            monkeypatch,
            capsys,
            dict(READ_PAYLOAD, session_id=umbrella, transcript_path=rollout),
            db,
        )
        for rollout in _CODEX_ROLLOUTS
    ]

    assert all("hookSpecificOutput" in out for out in outs), outs
