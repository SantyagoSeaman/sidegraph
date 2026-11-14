"""After a compaction the agent whose context was compacted starts its ten files again. Every
test drives the real SessionStart and PreToolUse entry points."""

from __future__ import annotations

import contextlib
import io
import json
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from sidegraph.host import hooks, launch
from sidegraph.store import Store
from tests.test_host_pretool_records import SESSION, Project, _payload, _run, _text


@pytest.fixture
def project(tmp_path):
    p = Project(tmp_path / "repo")
    yield p
    p.close()


def _session_start(
    monkeypatch, capsys, project, source: str, session: str = SESSION, agent: str | None = None
) -> dict:
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project.root))
    monkeypatch.setenv("SIDEGRAPH_DIR", str(project.store_dir))
    monkeypatch.setenv("SIDEGRAPH_GRAPH", str(project.root / "no-graph.json"))
    monkeypatch.setattr(launch, "record_launch_commit", lambda **_kwargs: None)
    payload = {"session_id": session, "source": source, "cwd": str(project.root)}
    if agent is not None:
        payload["agent_id"] = agent
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    hooks.session_start()
    return json.loads(capsys.readouterr().out)


def _read(monkeypatch, capsys, project, name: str, agent: str | None = None) -> dict:
    return _run(monkeypatch, capsys, project, _payload("Read", agent=agent, file_path=name))


@pytest.mark.parametrize("source", ["compact", "clear"])
def test_after_a_compaction_an_agent_at_the_cap_gets_records_again(
    project, monkeypatch, capsys, source
):
    """Red against the unfixed SessionStart (it deletes only the ``mcp_sent`` keys)."""
    names = [f"src/f{i}.py" for i in range(11)]
    project.anchored(*names)
    for name in names[:10]:
        assert "hookSpecificOutput" in _read(monkeypatch, capsys, project, name)
    assert _read(monkeypatch, capsys, project, names[10]) == {}
    _session_start(monkeypatch, capsys, project, source)
    assert project.meta("pretool_file:%") == {}
    after = _read(monkeypatch, capsys, project, names[10])
    assert "Gotcha of src/f10.py" in _text(after)
    again = _read(monkeypatch, capsys, project, names[0])
    assert "Gotcha of src/f0.py" in _text(again)  # its earlier delivery left the context too


@pytest.mark.parametrize("source", ["startup", "resume"])
def test_a_start_that_is_not_a_compaction_keeps_the_claims(project, monkeypatch, capsys, source):
    """Passes before and after: a guard against a reset on every SessionStart."""
    project.anchored("src/a.py")
    _read(monkeypatch, capsys, project, "src/a.py")
    _session_start(monkeypatch, capsys, project, source)
    assert len(project.meta("pretool_file:%")) == 1
    assert _read(monkeypatch, capsys, project, "src/a.py") == {}


def test_a_main_agent_compaction_leaves_subagents_and_other_sessions(project, monkeypatch, capsys):
    """Red against a reset of every agent of the session, and of every session."""
    project.anchored("src/a.py")
    _read(monkeypatch, capsys, project, "src/a.py")
    _read(monkeypatch, capsys, project, "src/a.py", agent="agent-1")
    _run(monkeypatch, capsys, project, _payload("Read", session="other", file_path="src/a.py"))
    _session_start(monkeypatch, capsys, project, "compact")
    assert sorted(project.meta("pretool_file:%")) == [
        f"pretool_file:{SESSION}:agent-1:src/a.py",
        "pretool_file:other:-:src/a.py",
    ]
    assert _read(monkeypatch, capsys, project, "src/a.py", agent="agent-1") == {}


def test_a_subagent_compaction_resets_only_that_subagent(project, monkeypatch, capsys):
    """Red against a reset keyed on the session's own agent whatever agent the notice names: a
    compacted subagent would keep its cap, and the main agent, whose context is whole, would be
    handed its files again."""
    project.anchored("src/a.py")
    _read(monkeypatch, capsys, project, "src/a.py")
    _read(monkeypatch, capsys, project, "src/a.py", agent="agent-1")
    _read(monkeypatch, capsys, project, "src/a.py", agent="agent-2")
    _session_start(monkeypatch, capsys, project, "compact", agent="agent-1")
    assert sorted(project.meta("pretool_file:%")) == [
        f"pretool_file:{SESSION}:-:src/a.py",
        f"pretool_file:{SESSION}:agent-2:src/a.py",
    ]
    again = _read(monkeypatch, capsys, project, "src/a.py", agent="agent-1")
    assert "Gotcha of src/a.py" in _text(again)
    assert _read(monkeypatch, capsys, project, "src/a.py") == {}


def test_a_repeated_notice_does_not_reset_again(project, monkeypatch, capsys):
    """Red against a reset on every compact notice: a host that sends the notice twice would
    delete the claims made after the first, and every file would be delivered a second time
    into a context that still holds it."""
    names = [f"src/f{i}.py" for i in range(10)]
    project.anchored(*names)
    _session_start(monkeypatch, capsys, project, "compact")
    for name in names:
        assert "hookSpecificOutput" in _read(monkeypatch, capsys, project, name)
    assert _session_start(monkeypatch, capsys, project, "compact") == {}  # the repeat
    assert len(project.meta("pretool_file:%")) == 10
    for name in names:
        assert _read(monkeypatch, capsys, project, name) == {}


def test_a_compaction_right_after_a_start_still_resets(project, monkeypatch, capsys):
    """Red against a reset that the duplicate-SessionStart window swallows: a startup leaves
    no reset stamp, so a compact a few seconds later is a first notice."""
    project.anchored("src/a.py")
    _session_start(monkeypatch, capsys, project, "startup")
    _read(monkeypatch, capsys, project, "src/a.py")
    _session_start(monkeypatch, capsys, project, "compact")
    assert project.meta("pretool_file:%") == {}


def test_a_later_compaction_resets_again(project, monkeypatch, capsys):
    """Red against a stamp that never expires: the second compaction of a session comes long
    after the first, and it must reset as the first did."""
    project.anchored("src/a.py")
    _session_start(monkeypatch, capsys, project, "compact")
    _read(monkeypatch, capsys, project, "src/a.py")
    stamp = f"pretool_reset:{SESSION}:-:compact"
    assert stamp in project.meta("pretool_reset:%")
    long_ago = datetime.now(UTC) - timedelta(seconds=hooks._SESSION_START_DEDUPE_SECONDS + 1)
    project.store.set_meta(stamp, long_ago.isoformat())
    _session_start(monkeypatch, capsys, project, "compact")
    assert project.meta("pretool_file:%") == {}
    assert "Gotcha of src/a.py" in _text(_read(monkeypatch, capsys, project, "src/a.py"))


def test_a_notice_that_overlaps_another_deletes_no_claim_made_since(project, monkeypatch):
    """Red against a reset whose stamp check, deletion and stamp write are separate
    transactions. Another process's notice and a claim are run at the worst moment of the
    first notice, between its stamp check and its deletion. With one transaction they find
    the writer reservation taken and change nothing; whatever happens, a claim that was made
    after a reset must still be there when every notice has run."""
    project.anchored("src/a.py")
    key = f"pretool_file:{SESSION}:-:src/a.py"
    payload = {"source": "compact"}
    now = datetime.now(UTC)
    other = Store(project.store_dir, busy_timeout_seconds=0.05)
    made: list[str] = []
    original = hooks._parse_stamp
    entered: list[bool] = []

    def claim() -> None:
        if other.claim_meta(key, now.isoformat()):
            made.append(key)

    def at_the_worst_moment(raw):
        if not entered:
            entered.append(True)
            for action in (
                lambda: hooks._reset_file_claims(other, payload, SESSION, now),
                claim,
            ):
                # refused when the first notice holds the writer reservation
                with contextlib.suppress(sqlite3.OperationalError):
                    action()
        return original(raw)

    monkeypatch.setattr(hooks, "_parse_stamp", at_the_worst_moment)
    try:
        hooks._reset_file_claims(project.store, payload, SESSION, now)
        assert entered
        if not made:
            claim()
        hooks._reset_file_claims(other, payload, SESSION, now + timedelta(seconds=1))
    finally:
        other.close()
    assert made == [key]
    assert key in project.meta("pretool_file:%")


def test_a_notice_that_read_its_clock_before_the_stamp_is_a_repeat(project):
    """Red against a repeat check that only looks backwards: notice B read its clock, waited
    for the writer reservation, and found the stamp of a notice A that read its clock later
    and reset first. The stamp is newer than B's time, B is the repeat, and the claim made
    after A's reset must stay."""
    project.anchored("src/a.py")
    key = f"pretool_file:{SESSION}:-:src/a.py"
    payload = {"source": "compact"}
    now = datetime.now(UTC)
    hooks._reset_file_claims(project.store, payload, SESSION, now)  # notice A
    assert project.store.claim_meta(key, now.isoformat())  # a claim made after A's reset
    hooks._reset_file_claims(project.store, payload, SESSION, now - timedelta(milliseconds=300))
    assert key in project.meta("pretool_file:%")


def test_the_reset_stamps_expire_with_the_claims(project, monkeypatch, capsys):
    """Red against a SessionStart prune list without the ``pretool_reset:`` prefix: the stamps
    would pile up, one per agent, source and session, for ever."""
    now = datetime.now(UTC)
    for key, age in (
        ("pretool_reset:old:-:compact", 31),
        ("pretool_reset:old:agent-1:clear", 90),
        ("pretool_reset:fresh:-:compact", 1),
    ):
        project.store.set_meta(key, (now - timedelta(days=age)).isoformat())
    _session_start(monkeypatch, capsys, project, "startup", session="s-new")
    assert sorted(project.meta("pretool_reset:%")) == ["pretool_reset:fresh:-:compact"]


def test_a_reset_that_fails_before_its_stamp_deletes_nothing(project):
    """Red against a deletion committed before the stamp is written: the deletion and the
    stamp are one transaction, so a failure of the second takes the first back."""
    project.anchored("src/a.py")
    key = f"pretool_file:{SESSION}:-:src/a.py"
    project.store.set_meta(key, datetime.now(UTC).isoformat())
    with pytest.raises(sqlite3.IntegrityError):
        project.store.delete_meta_prefix_stamped(
            f"pretool_file:{SESSION}:-:",
            f"pretool_reset:{SESSION}:-:compact",
            None,
            lambda raw: False,
        )
    assert key in project.meta("pretool_file:%")
    assert project.meta("pretool_reset:%") == {}
