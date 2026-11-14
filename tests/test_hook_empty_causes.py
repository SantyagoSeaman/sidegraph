"""Why a hook run delivered nothing, as the run log records it. The hook tests drive real entry
points."""

from __future__ import annotations

import sqlite3
import time
from datetime import UTC, datetime, timedelta

import pytest

from sidegraph.host import hook_log
from sidegraph.hot_index import HotIndex
from sidegraph.schema import DecisionKind, DecisionStatus
from sidegraph.store_layout import VOLATILE_STALE_KEY
from tests.test_hook_contention import _hook_case, _hook_payload
from tests.test_hook_outcome_log import _run_hook, _store_dir
from tests.test_host_pretool_records import SESSION, Project, _payload, _run, _text
from tests.test_hot_index import _Seeder


@pytest.fixture
def project(tmp_path):
    p = Project(tmp_path / "repo")
    yield p
    p.close()


def _log(store_dir) -> list[tuple[str, str, str | None]]:
    return [(r.hook, r.outcome, r.detail) for r in hook_log.read_runs(store_dir)]


# -- the cause of an empty run -----------------------------------------------------------------


def test_a_file_with_no_entity_logs_no_entity(project, monkeypatch, capsys):
    project.anchored("src/a.py")
    project.file("src/plain.py")
    assert _run(monkeypatch, capsys, project, _payload("Read", file_path="src/plain.py")) == {}
    assert _log(project.store_dir) == [("PreToolUse", "empty", "no-entity")]


def test_an_entity_whose_record_was_superseded_logs_no_record(project, monkeypatch, capsys):
    old = project.record("src/a.py", "Old rule")
    project.supersede(old, "New rule, anchored nowhere")
    assert _run(monkeypatch, capsys, project, _payload("Read", file_path="src/a.py")) == {}
    assert _log(project.store_dir) == [("PreToolUse", "empty", "no-record")]


def test_a_second_read_logs_repeat_and_the_eleventh_file_logs_cap(project, monkeypatch, capsys):
    names = [f"src/f{i}.py" for i in range(11)]
    project.anchored(*names)
    for name in names[:10]:
        _run(monkeypatch, capsys, project, _payload("Read", file_path=name))
    assert _run(monkeypatch, capsys, project, _payload("Read", file_path=names[0])) == {}
    assert _run(monkeypatch, capsys, project, _payload("Read", file_path=names[10])) == {}
    line = "cat " + " ".join(n.removeprefix("src/") for n in (names[1], names[10]))
    both = _run(
        monkeypatch,
        capsys,
        project,
        {**_payload("Bash", command=line), "cwd": str(project.root / "src")},
    )
    assert both == {}
    assert _log(project.store_dir)[10:] == [
        ("PreToolUse", "empty", "repeat"),
        ("PreToolUse", "empty", "cap"),
        ("PreToolUse", "empty", "cap"),  # one file already held, one refused by the cap
    ]


def test_repeat_and_cap_win_over_cold(project, monkeypatch, capsys):
    """Red against a cause that names ``cold`` for a file the agent already holds, or cannot
    get because it holds its ten files: on an index with no kept status ``records_for`` hands
    nothing over, so no claim is tried, and the claim rules still decide what would have
    happened."""
    names = [f"src/f{i}.py" for i in range(11)]
    project.anchored(*names)
    for name in names[:10]:
        assert "hookSpecificOutput" in _run(
            monkeypatch, capsys, project, _payload("Read", file_path=name)
        )
    _cold(project)
    assert _run(monkeypatch, capsys, project, _payload("Read", file_path=names[0])) == {}
    assert _run(monkeypatch, capsys, project, _payload("Read", file_path=names[10])) == {}
    other = _payload("Read", session="s2", file_path=names[10])
    assert _run(monkeypatch, capsys, project, other) == {}  # another agent: no claim, no cap
    assert _log(project.store_dir)[10:] == [
        ("PreToolUse", "empty", "repeat"),
        ("PreToolUse", "empty", "cap"),
        ("PreToolUse", "empty", "cold"),
    ]


@pytest.mark.parametrize("entry", ["pretool", "subagent"])
def test_a_never_synced_index_logs_cold(tmp_path, monkeypatch, capsys, entry):
    store_dir, source, transcript, _ = _hook_case(
        tmp_path / "project", monkeypatch, cold=True, telemetry="on"
    )
    assert _run_hook(entry, monkeypatch, capsys, _hook_payload(entry, source, transcript)) == {}
    hook = "PreToolUse" if entry == "pretool" else "SubagentStart"
    assert _log(store_dir) == [(hook, "empty", "cold")]


def _cold(project) -> None:
    """The index as a rebuild of a never-synced store leaves it: the flag set, nothing kept."""
    project.store.set_meta(VOLATILE_STALE_KEY, "1")


def test_a_cold_index_that_hides_only_a_hidden_proposal_logs_no_record(
    project, monkeypatch, capsys
):
    """Red against a cause taken from "a live record sits behind a cold binding": with
    ``SIDEGRAPH_UNRATIFIED=off`` a proposal is never handed over, cold index or not, so the
    cold index withheld nothing."""
    project.record("src/a.py", "Only a proposal", status=DecisionStatus.PROPOSED)
    _cold(project)
    out = _run(
        monkeypatch,
        capsys,
        project,
        _payload("Read", file_path="src/a.py"),
        env={"SIDEGRAPH_UNRATIFIED": "off"},
    )
    assert out == {}
    assert _log(project.store_dir) == [("PreToolUse", "empty", "no-record")]
    monkeypatch.delenv("SIDEGRAPH_UNRATIFIED")
    shown = _run(monkeypatch, capsys, project, _payload("Read", session="s2", file_path="src/a.py"))
    assert shown == {}
    assert _log(project.store_dir)[-1] == ("PreToolUse", "empty", "cold")  # it would surface now


def test_a_cold_index_that_hides_only_an_expired_record_logs_no_record(
    project, monkeypatch, capsys
):
    """Red against the same shortcut: an expired record is handed over by no index."""
    project.file("src/a.py")
    seed = _Seeder(project.store)
    past = datetime.now(UTC) - timedelta(days=1)
    seed.anchor(
        seed.decision("Expired rule", DecisionKind.GOTCHA, age_days=3, valid_to=past), "src/a.py"
    )
    _cold(project)
    assert _run(monkeypatch, capsys, project, _payload("Read", file_path="src/a.py")) == {}
    assert _log(project.store_dir) == [("PreToolUse", "empty", "no-record")]


def test_a_store_with_nothing_current_logs_no_record_for_a_subagent(project, monkeypatch, capsys):
    old = project.record("src/a.py", "Old rule")
    project.supersede(old, "New rule, anchored nowhere")
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project.root))
    monkeypatch.setenv("SIDEGRAPH_DIR", str(project.store_dir))
    payload = {"hook_event_name": "SubagentStart", "session_id": SESSION}
    assert _run_hook("subagent", monkeypatch, capsys, payload) == {}
    assert _log(project.store_dir) == [("SubagentStart", "empty", "no-record")]
    _cold(project)  # the same store, cold: still nothing a brief would count
    assert _run_hook("subagent", monkeypatch, capsys, payload) == {}
    assert _log(project.store_dir)[-1] == ("SubagentStart", "empty", "no-record")


def test_a_duplicate_session_start_logs_duplicate(tmp_path, monkeypatch, capsys):
    store_dir, source, transcript, _ = _hook_case(
        tmp_path / "project", monkeypatch, cold=False, telemetry="on"
    )
    payload = _hook_payload("session", source, transcript)
    assert _run_hook("session", monkeypatch, capsys, payload) != {}
    assert _run_hook("session", monkeypatch, capsys, payload) == {}
    assert _log(store_dir) == [
        ("SessionStart", "delivered", None),
        ("SessionStart", "empty", "duplicate"),
    ]


def _agent_call(source, mode: str | None) -> dict:
    payload = {
        "session_id": "read-1",
        "tool_name": "Agent",
        "tool_input": {"description": "review", "prompt": f"Review {source.name}"},
        "cwd": str(source.parent),
    }
    if mode is not None:
        payload["permission_mode"] = mode
    return payload


def test_an_agent_call_that_names_no_anchored_file_logs_no_entity(tmp_path, monkeypatch, capsys):
    store_dir, source, _transcript, _ = _hook_case(
        tmp_path / "project", monkeypatch, cold=False, telemetry="on"
    )
    payload = _agent_call(source, "default")
    payload["tool_input"]["prompt"] = "Summarise the project in one line"
    assert _run_hook("pretool", monkeypatch, capsys, payload) == {}
    assert _log(store_dir) == [("PreToolUse", "empty", "no-entity")]


def test_an_agent_call_on_a_never_synced_index_logs_cold(tmp_path, monkeypatch, capsys):
    store_dir, source, _transcript, _ = _hook_case(
        tmp_path / "project", monkeypatch, cold=True, telemetry="on"
    )
    assert _run_hook("pretool", monkeypatch, capsys, _agent_call(source, "default")) == {}
    assert _log(store_dir) == [("PreToolUse", "empty", "cold")]


def test_runs_that_open_no_index_still_write_no_line(tmp_path, monkeypatch, capsys):
    """Passes before and after: a guard. A call that names no file and an Agent call in auto
    mode return before any index open, wait for no lock, and are not counted."""
    store_dir, source, _transcript, _ = _hook_case(
        tmp_path / "project", monkeypatch, cold=False, telemetry="on"
    )
    grep = {"session_id": "read-1", "tool_name": "Grep", "tool_input": {"pattern": "x"}}
    assert _run_hook("pretool", monkeypatch, capsys, grep) == {}
    opened: list[str] = []
    original = HotIndex.open
    monkeypatch.setattr(
        HotIndex,
        "open",
        classmethod(lambda cls, *a, **kw: opened.append("open") or original(*a, **kw)),
    )
    assert _run_hook("pretool", monkeypatch, capsys, _agent_call(source, "auto")) == {}
    assert opened == []
    assert _log(store_dir) == []
    assert not (store_dir / hook_log.LOG_NAME).exists()


# -- a lock taken before the cause is read -----------------------------------------------------


def test_a_lock_taken_after_an_empty_cold_read_costs_no_wait(tmp_path, monkeypatch, capsys):
    """Red against a cause read that waits for the index lock. The hook has read that it has
    nothing to hand over; another process then takes the lock; the hook answers at once and
    logs the cause as unknown. The hook's lock wait is set to three seconds here, so a cause
    read that waits fails the bound."""
    from sidegraph.host import hooks

    store_dir, source, _transcript, _ = _hook_case(
        tmp_path / "project", monkeypatch, cold=True, telemetry="on"
    )
    monkeypatch.setattr(hooks, "HOOK_BUSY_SECONDS", 3.0)
    holder = sqlite3.connect(store_dir / "index.db")
    original = HotIndex.records_for

    def read_then_lock(self, *args, **kwargs):
        records = original(self, *args, **kwargs)
        if not holder.in_transaction:
            holder.execute("BEGIN EXCLUSIVE")
        return records

    monkeypatch.setattr(HotIndex, "records_for", read_then_lock)
    try:
        started = time.monotonic()
        out = _run_hook("pretool", monkeypatch, capsys, _agent_call(source, "default"))
        elapsed = time.monotonic() - started
    finally:
        holder.rollback()
        holder.close()
    assert out == {}
    assert elapsed < 1.5
    assert _log(store_dir) == [("PreToolUse", "empty", "unknown")]


def test_a_lock_taken_after_a_refused_claim_costs_no_wait(project, monkeypatch, capsys):
    """Red against a wait on the read that tells ``repeat`` from ``cap``: the claim was
    refused (the file was delivered before), another process then takes the lock, and the
    hook answers at once with the cause unknown."""
    from sidegraph.host import hooks

    project.anchored("src/a.py")
    assert "hookSpecificOutput" in _run(
        monkeypatch, capsys, project, _payload("Read", file_path="src/a.py")
    )
    monkeypatch.setattr(hooks, "HOOK_BUSY_SECONDS", 3.0)
    holder = sqlite3.connect(project.store_dir / "index.db")
    original = HotIndex.claim_file

    def claim_then_lock(self, *args, **kwargs):
        won = original(self, *args, **kwargs)
        if not won and not holder.in_transaction:
            holder.execute("BEGIN EXCLUSIVE")
        return won

    monkeypatch.setattr(HotIndex, "claim_file", claim_then_lock)
    try:
        started = time.monotonic()
        out = _run(
            monkeypatch,
            capsys,
            project,
            {**_payload("Bash", command="cat a.py"), "cwd": str(project.root / "src")},
        )
        elapsed = time.monotonic() - started
    finally:
        holder.rollback()
        holder.close()
    assert out == {}
    assert elapsed < 1.5
    assert _log(project.store_dir)[-1] == ("PreToolUse", "empty", "unknown")


# -- a delivery cut short by a lock ------------------------------------------------------------


def test_a_delivery_cut_short_by_a_lock_is_logged_as_partial(project, monkeypatch, capsys):
    """Red against a log that says only ``lock`` for a run that handed a record over: the
    first file of the line is claimed and printed, then another process takes the write lock
    and the second claim is refused."""
    project.anchored("src/a.py", "src/b.py")
    holder = sqlite3.connect(project.store_dir / "index.db")
    original = HotIndex.claim_file
    claims: list[bool] = []

    def claim_then_lock(self, *args, **kwargs):
        won = original(self, *args, **kwargs)
        claims.append(won)
        if len(claims) == 1:
            holder.execute("BEGIN IMMEDIATE")  # held while the hook tries its second claim
        return won

    monkeypatch.setattr(HotIndex, "claim_file", claim_then_lock)
    try:
        out = _run(
            monkeypatch,
            capsys,
            project,
            {**_payload("Bash", command="cat a.py b.py"), "cwd": str(project.root / "src")},
        )
    finally:
        holder.rollback()
        holder.close()
    assert "Gotcha of src/a.py" in _text(out) and "Gotcha of src/b.py" not in _text(out)
    assert _log(project.store_dir) == [("PreToolUse", "lock", "partial")]
    assert hook_log.summarize(hook_log.read_runs(project.store_dir))["PreToolUse"]["lock"] == 1


def test_a_lock_that_refuses_the_brief_read_logs_a_plain_lock(tmp_path, monkeypatch, capsys):
    """Red against a ``lock`` line that carries a cause (it said ``no-record`` for a file that
    has records): the lock refused the read, so nothing is known about why the brief is empty."""
    from sidegraph.host import hooks

    store_dir, source, _transcript, _ = _hook_case(
        tmp_path / "project", monkeypatch, cold=False, telemetry="on"
    )
    monkeypatch.setattr(hooks, "HOOK_BUSY_SECONDS", 0.05)
    holder = sqlite3.connect(store_dir / "index.db")
    original = HotIndex.records_for

    def lock_then_read(self, *args, **kwargs):
        if not holder.in_transaction:
            holder.execute("BEGIN EXCLUSIVE")  # held before the hook reads the file's records
        return original(self, *args, **kwargs)

    monkeypatch.setattr(HotIndex, "records_for", lock_then_read)
    try:
        out = _run_hook("pretool", monkeypatch, capsys, _agent_call(source, "default"))
    finally:
        holder.rollback()
        holder.close()
    assert out == {}
    assert _log(store_dir) == [("PreToolUse", "lock", None)]


# -- the line ----------------------------------------------------------------------------------


def test_the_detail_is_the_fifth_field_of_an_empty_or_a_lock_line_only(tmp_path):
    store = _store_dir(tmp_path / "store")
    hook_log.record(store, "PreToolUse", "empty", 7, "cap")
    hook_log.record(store, "PreToolUse", "delivered", 8, "cap")  # a detail on a delivery: dropped
    hook_log.record(store, "PreToolUse", "empty", 9, "Not A Cause")  # not the shape: dropped
    hook_log.record(store, "PreToolUse", "empty", 10)
    hook_log.record(store, "PreToolUse", "lock", 11, "partial")
    hook_log.record(store, "PreToolUse", "error", 12, "partial")
    rows = (store / hook_log.LOG_NAME).read_text(encoding="ascii").split("\n")[1:]
    assert [row.split(" ")[1:] for row in rows] == [
        ["PreToolUse", "empty", "7", "cap"],
        ["PreToolUse", "delivered", "8"],
        ["PreToolUse", "empty", "9"],
        ["PreToolUse", "empty", "10"],
        ["PreToolUse", "lock", "11", "partial"],
        ["PreToolUse", "error", "12"],
    ]


def test_a_run_writes_partial_only_for_a_lock_that_followed_a_delivery(tmp_path):
    store = _store_dir(tmp_path / "store")
    for delivered, cause in ((True, None), (False, None), (False, "cap")):
        run = hook_log.Run("PreToolUse")
        run.store_dir = store
        run.cause = cause
        error = sqlite3.OperationalError("database is locked")
        error.sqlite_errorcode = sqlite3.SQLITE_BUSY  # type: ignore[attr-defined]
        run.note(error)
        run.finish(delivered)
    assert [(r.outcome, r.detail) for r in hook_log.read_runs(store)] == [
        ("lock", "partial"),
        ("lock", None),
        ("lock", None),  # a cause set before the lock was seen is a guess: not written
    ]


def test_read_runs_accepts_four_and_five_fields(tmp_path):
    store = _store_dir(tmp_path / "store")
    (store / hook_log.LOG_NAME).write_text(
        "\n1000 PreToolUse empty 5"  # an older writer's line
        "\n1001 PreToolUse empty 5 cold"
        "\n1002 PreToolUse empty 5 newer-cause"  # a cause this version does not know: kept
        "\n1007 PreToolUse empty 5 a-cause-longer-than-the-bound"  # 29 characters: not a row
        "\n1003 PreToolUse delivered 5 cold"  # a detail on a delivery: not a row
        "\n1004 PreToolUse empty 5 COLD"  # not the shape
        "\n1005 PreToolUse empty 5 cold extra"  # six fields
        "\n1008 PreToolUse lock 600 partial"
        "\n1006 SubagentStart empty 5 no-record",
        encoding="ascii",
    )
    rows = hook_log.read_runs(store)
    assert [(r.at_ms, r.detail) for r in rows] == [
        (1000, None),
        (1001, "cold"),
        (1002, "newer-cause"),
        (1008, "partial"),
        (1006, "no-record"),
    ]
    assert hook_log.empty_causes(rows) == {
        "PreToolUse": {"cold": 1, "newer-cause": 1, "unrecorded": 1},
        "SubagentStart": {"no-record": 1},
    }
    summary = hook_log.summarize(rows)["PreToolUse"]
    assert (summary["empty"], summary["lock"]) == (3, 1)  # the summary's shape is as before
