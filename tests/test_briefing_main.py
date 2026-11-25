"""The SessionStart briefing entry point, gates, and index-only mark (D2–D4)."""

import io
import json
import sqlite3
import sys
import time
from contextlib import closing
from datetime import timedelta
from pathlib import Path
from unittest.mock import Mock

import pytest

from sidegraph.host import hook_log
from sidegraph.host.briefing import core
from sidegraph.host.briefing.blocks import BLOCKS, FLAG_NAMES, Block
from sidegraph.hot_index import HotIndex
from sidegraph.schema import DecisionKind, DecisionStatus
from sidegraph.store import Store
from sidegraph.store_layout import (
    DRIFT_CACHE_KEY,
    FORMAT_MARKER_NAME,
    VOLATILE_STALE_KEY,
    requirement_marker_text,
)
from tests.briefing_fixtures import NOW
from tests.test_briefing_git_files import _git
from tests.test_host_subagent_start import _seed
from tests.test_hot_index import _reload_hot_pair, _Seeder, _synced_hot_pair

PAYLOAD = {
    "session_id": "s1",
    "transcript_path": "/x/s1.jsonl",
    "hook_event_name": "SessionStart",
    "source": "startup",
}


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for flag in FLAG_NAMES:
        monkeypatch.delenv(flag, raising=False)
    monkeypatch.delenv("SIDEGRAPH_UNRATIFIED", raising=False)
    monkeypatch.delenv("SIDEGRAPH_PROPOSAL_WINDOW_DAYS", raising=False)
    monkeypatch.setenv("SIDEGRAPH_TELEMETRY", "on")


@pytest.fixture
def store_dir(tmp_path: Path) -> Path:
    root = tmp_path / ".sidegraph"
    _seed(root)
    return root


def _run(
    monkeypatch,
    capsys,
    root: Path | None,
    store_dir: Path,
    payload=PAYLOAD,
    *,
    host: str | None = "claude",
    rooted: bool = False,
) -> dict:
    monkeypatch.setenv("SIDEGRAPH_DIR", str(store_dir))
    for var, value in (("CLAUDE_PROJECT_DIR", root), ("SIDEGRAPH_HOST", host)):
        if value is None:
            monkeypatch.delenv(var, raising=False)
        else:
            monkeypatch.setenv(var, str(value))
    argv = ["sidegraph-session-briefing", *(["--repo-root"] if rooted else [])]
    monkeypatch.setattr(sys, "argv", argv)
    monkeypatch.setattr(
        sys, "stdin", io.StringIO(payload if isinstance(payload, str) else json.dumps(payload))
    )
    core.main()
    output = capsys.readouterr().out
    assert len(output.splitlines()) == 1, output
    return json.loads(output)


def _printing(name: str = "protocol", lines: list[str] | None = None) -> Block:
    block = next(b for b in BLOCKS if b.name == name)
    return block._replace(render=Mock(return_value=["L1", "L2"] if lines is None else lines))


def _install(monkeypatch, *blocks: Block) -> None:
    monkeypatch.setattr(core, "BLOCKS", blocks)
    for block in blocks:
        monkeypatch.setenv(block.flag, "on")


def _mark(store_dir: Path) -> str | None:
    if not (store_dir / "index.db").exists():
        return None
    with closing(sqlite3.connect(store_dir / "index.db")) as conn:
        row = conn.execute("SELECT value FROM meta WHERE key=?", (core.MARK_KEY,)).fetchone()
    return row[0] if row else None


def _last_log(store_dir: Path) -> tuple[str, ...]:
    path = store_dir / hook_log.LOG_NAME
    if not path.exists():
        return ()
    _, hook, outcome, _, *detail = path.read_text().splitlines()[-1].split()
    return (hook, outcome, *detail)


def _snapshot(store_dir: Path) -> dict[Path, bytes]:
    return {
        path.relative_to(store_dir): path.read_bytes()
        for path in store_dir.rglob("*")
        if path.is_file()
    }


def test_k1_no_flag_opens_nothing(monkeypatch, capsys, tmp_path, store_dir):
    monkeypatch.setattr(core, "BLOCKS", (_printing(),))
    spies = [
        (core, "resolve_store_location"),
        (HotIndex, "open"),
        (core, "anchor_to_project"),
        (core, "_read_payload"),
        (core, "changed_files"),
    ]
    calls = []
    for obj, name in spies:
        spy = Mock(wraps=getattr(obj, name))
        monkeypatch.setattr(obj, name, spy)
        calls.append(spy)
    before = _snapshot(store_dir)
    assert _run(monkeypatch, capsys, tmp_path, store_dir, rooted=True) == {}
    assert all(spy.call_count == 0 for spy in calls)
    assert _snapshot(store_dir) == before
    assert _last_log(store_dir) == ()


def test_k2_only_the_exact_value_on_enables(monkeypatch, capsys, tmp_path, store_dir):
    monkeypatch.setattr(core, "BLOCKS", tuple(_printing(block.name) for block in BLOCKS))
    for block in BLOCKS:
        with monkeypatch.context() as env:
            env.setenv(block.flag, "on")
            assert core.enabled_blocks() == {block.name}
    for value in ("ON", "1", "true", "", " on", "one"):
        monkeypatch.setenv(FLAG_NAMES[0], value)
        assert core.enabled_blocks() == frozenset()
    opened = Mock(wraps=HotIndex.open)
    monkeypatch.setattr(HotIndex, "open", opened)
    assert _run(monkeypatch, capsys, tmp_path, store_dir) == {}
    opened.assert_not_called()


@pytest.mark.parametrize(
    ("host", "supported"),
    [
        ("claude", True),
        ("codex", True),
        ("grok", False),
        ("muse", False),
        ("unknown", False),
        (None, False),
    ],
)
def test_k3_host_gate(host, supported, monkeypatch, capsys, tmp_path, store_dir):
    _install(monkeypatch, _printing())
    monkeypatch.chdir(tmp_path)
    opened = Mock(wraps=HotIndex.open)
    anchored = Mock(wraps=core.anchor_to_project)
    monkeypatch.setattr(HotIndex, "open", opened)
    monkeypatch.setattr(core, "anchor_to_project", anchored)
    output = _run(monkeypatch, capsys, tmp_path, store_dir, host=host, rooted=True)
    if supported:
        assert output["hookSpecificOutput"]["additionalContext"] == "L1\nL2"
        opened.assert_called_once()
        anchored.assert_called_once()
        assert _mark(store_dir) is not None
    else:
        assert output == {}
        opened.assert_not_called()
        anchored.assert_not_called()
        assert _mark(store_dir) is None


@pytest.mark.parametrize(
    "case",
    [
        "no-session",
        "no-store",
        "no-index",
        "newer-store",
        "cold",
        "no-binding",
        "proposal-only",
    ],
)
def test_k4_each_gate_ends_the_run_at_once(case, monkeypatch, capsys, tmp_path):
    block = _printing()
    _install(monkeypatch, block)
    root = tmp_path / ".sidegraph"
    if case == "cold":
        root, _, _, record_path = _synced_hot_pair(tmp_path)
        _reload_hot_pair(root, record_path)
        hot = HotIndex.open(root)
        assert hot is not None
        with closing(hot):
            assert any(hot.records_for(ids) for ids in hot.anchored_files().values())
    elif case != "no-store":
        with closing(Store(root)) as store:
            store.set_meta(VOLATILE_STALE_KEY, "0")
            if case in {"no-session", "no-index", "newer-store", "proposal-only"}:
                seeder = _Seeder(store)
                status = (
                    DecisionStatus.PROPOSED if case == "proposal-only" else DecisionStatus.ACCEPTED
                )
                seeder.anchor(seeder.decision("Anchored", DecisionKind.GOTCHA, status), "a.py")
        if case == "no-index":
            (root / "index.db").unlink()
        elif case == "newer-store":
            marker = root / FORMAT_MARKER_NAME
            marker.write_text(requirement_marker_text("99.0.0"))
    # Git subprocesses may sleep while polling for exit during store setup.
    # Watch for sleeps only while the briefing runs.
    sleep = Mock()
    with monkeypatch.context() as probe:
        probe.setattr(time, "sleep", sleep)
        result = _run(monkeypatch, capsys, tmp_path, root, {} if case == "no-session" else PAYLOAD)
    assert result == {}
    sleep.assert_not_called()
    block.render.assert_not_called()
    assert _mark(root) is None
    if case == "cold":
        assert _last_log(root) == ("SessionBriefing", "empty", "cold")
    elif case in {"no-binding", "proposal-only"}:
        assert _last_log(root) == ("SessionBriefing", "empty", "no-record")
    else:
        assert _last_log(root) == ()


def test_k5_enabled_blocks_with_nothing_to_say(monkeypatch, capsys, tmp_path, store_dir):
    block = _printing(lines=[])
    _install(monkeypatch, block)
    assert _run(monkeypatch, capsys, tmp_path, store_dir) == {}
    block.render.assert_called_once()
    assert _last_log(store_dir) == ("SessionBriefing", "empty", "no-record")
    assert _mark(store_dir) is not None


def test_a_briefing_is_one_session_start_json_line(monkeypatch, capsys, tmp_path, store_dir):
    _install(monkeypatch, _printing())
    assert _run(monkeypatch, capsys, tmp_path, store_dir) == {
        "hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "L1\nL2"}
    }


@pytest.mark.parametrize("rooted", [False, True])
def test_codex_briefs_the_current_checkout_with_an_inherited_claude_root(
    monkeypatch, capsys, tmp_path, store_dir, rooted
):
    foreign = tmp_path / "other"
    foreign_store = foreign / ".sidegraph"
    _seed(foreign_store)
    _install(monkeypatch, _printing())
    monkeypatch.chdir(tmp_path)

    output = _run(monkeypatch, capsys, foreign, Path(".sidegraph"), host="codex", rooted=rooted)

    assert output["hookSpecificOutput"]["additionalContext"] == "L1\nL2"
    assert _mark(store_dir) is not None
    assert _mark(foreign_store) is None


def test_codex_working_set_uses_its_checkout_with_an_absolute_store(
    monkeypatch, capsys, tmp_path, store_dir
):
    checkout, foreign = tmp_path / "codex", tmp_path / "claude"
    for root, filename in ((checkout, "src/a.py"), (foreign, "src/b.py")):
        (root / "src").mkdir(parents=True)
        _git(root, "init", "-q")
        (root / filename).write_text("changed\n")
        _git(root, "add", filename)
    block = _printing("working_set")
    _install(monkeypatch, block)
    monkeypatch.chdir(checkout)

    assert _run(monkeypatch, capsys, foreign, store_dir, host="codex")
    assert block.render.call_args.args[0].working_set == ("src/a.py",)


def test_codex_session_start_uses_the_rollout_and_anchors_from_a_subdirectory(
    monkeypatch, capsys, tmp_path, store_dir
):
    _install(monkeypatch, _printing())
    _git(tmp_path, "init", "-q")
    subdir = tmp_path / "subdir"
    subdir.mkdir()
    clock = Mock(wraps=core.datetime)
    clock.now.side_effect = [NOW + timedelta(seconds=n) for n in (0, 30, 35, 40, 45, 106)]
    monkeypatch.setattr(core, "datetime", clock)
    payload = {
        "session_id": "workspace",
        "cwd": str(subdir),
        "hook_event_name": "SessionStart",
        "model": "gpt-6.2-sol",
        "permission_mode": "default",
    }
    expected = {
        "hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "L1\nL2"}
    }
    for thread, source, delivered in (
        ("rollout-first", "startup", True),
        ("rollout-first", "resume", False),
        ("rollout-first", "compact", True),
        ("rollout-first", "clear", True),
        ("rollout-second", "startup", True),
        ("rollout-second", "resume", True),
    ):
        monkeypatch.chdir(subdir)
        prior = _mark(store_dir)
        event = payload | {"source": source, "transcript_path": f"/scratch/codex/{thread}.jsonl"}
        output = _run(monkeypatch, capsys, None, store_dir, event, host="codex", rooted=True)
        assert Path.cwd() == tmp_path.resolve()
        assert output == (expected if delivered else {})
        if delivered:
            assert _last_log(store_dir) == ("SessionBriefing", "delivered")
            assert json.loads(_mark(store_dir))["id"] == thread
        else:
            assert _last_log(store_dir) == ("SessionBriefing", "empty", "duplicate")
            assert _mark(store_dir) == prior


def test_prints_the_answer_before_finishing_the_run_log(monkeypatch, capsys, tmp_path, store_dir):
    _install(monkeypatch, _printing())
    events = []
    stdout_write = sys.stdout.write
    stdout_flush = sys.stdout.flush
    finish = hook_log.Run.finish

    def write(text):
        events.append("stdout")
        return stdout_write(text)

    def flush():
        events.append("flush")
        return stdout_flush()

    def record(run, delivered):
        assert delivered
        events.append("log")
        return finish(run, delivered)

    monkeypatch.setattr(sys.stdout, "write", write)
    monkeypatch.setattr(sys.stdout, "flush", flush)
    monkeypatch.setattr(hook_log.Run, "finish", record)
    assert _run(monkeypatch, capsys, tmp_path, store_dir)
    assert events.index("stdout") < events.index("flush") < events.index("log")


def test_a_failed_stdout_flush_is_logged_as_an_error(monkeypatch, capsys, tmp_path, store_dir):
    _install(monkeypatch, _printing())
    with monkeypatch.context() as capture:

        def fail_once():
            capture.undo()
            raise OSError("stdout closed")

        capture.setattr(sys.stdout, "flush", fail_once)
        assert _run(monkeypatch, capsys, tmp_path, store_dir)
    assert _last_log(store_dir) == ("SessionBriefing", "error")


@pytest.mark.parametrize("column", ["data", "id"])
def test_an_undecodable_index_row_does_not_hide_an_accepted_constraint(
    monkeypatch,
    capsys,
    tmp_path,
    store_dir,
    column,
):
    block = _printing("constraints")
    _install(monkeypatch, block)
    with closing(Store(store_dir)) as store:
        seeder = _Seeder(store)
        healthy = seeder.decision("Anchored", DecisionKind.CONSTRAINT)
        seeder.anchor(healthy, "constraint.py")
        broken = seeder.decision("Unanchored", DecisionKind.CONSTRAINT)
    with closing(sqlite3.connect(store_dir / "index.db")) as conn, conn:
        conn.execute(
            f"UPDATE decisions SET {column}=CAST(? AS TEXT) WHERE id=?", (b"\xff", broken.id)
        )
    assert _run(monkeypatch, capsys, tmp_path, store_dir)
    accepted = block.render.call_args.args[0].accepted
    ids = {row["id"] for row in accepted}
    assert healthy.id in ids and broken.id not in ids


def test_k7_the_mark_table():
    before = NOW - timedelta(days=1)
    prior = NOW - timedelta(days=2)

    def mark(at):
        return json.dumps(
            {
                "id": "s1",
                "at": at.isoformat(),
                "start": before.isoformat(),
                "prev_start": prior.isoformat(),
            }
        )

    for current in (
        None,
        "not json",
        "[1]",
        '{"id": "s1", "at": "yesterday"}',
        json.dumps({"id": "s1", "at": NOW.isoformat(), "start": "2026-11-19"}),
    ):
        new, duplicate, since = core._decide_mark(current, "s1", "startup", NOW)
        assert not duplicate and since is None
        assert json.loads(new) == {
            "id": "s1",
            "at": NOW.isoformat(),
            "start": NOW.isoformat(),
            "prev_start": None,
        }

    new, duplicate, since = core._decide_mark(mark(before), "s2", "startup", NOW)
    assert not duplicate and since == before
    assert json.loads(new) == {
        "id": "s2",
        "at": NOW.isoformat(),
        "start": NOW.isoformat(),
        "prev_start": before.isoformat(),
    }
    for source in ("startup", "resume", "fork", None):
        assert core._decide_mark(mark(NOW - timedelta(seconds=59)), "s1", source, NOW) == (
            None,
            True,
            prior,
        )
        new, duplicate, since = core._decide_mark(
            mark(NOW - timedelta(seconds=61)), "s1", source, NOW
        )
        assert not duplicate and since == prior
        assert json.loads(new) == json.loads(mark(NOW))
    for source in ("compact", "clear"):
        new, duplicate, since = core._decide_mark(
            mark(NOW - timedelta(seconds=30)), "s1", source, NOW
        )
        assert not duplicate and since == prior
        assert json.loads(new) == json.loads(mark(NOW))


def test_the_mark_is_persisted_across_starts(monkeypatch, capsys, tmp_path, store_dir):
    block = _printing("changes")
    _install(monkeypatch, block)
    t1 = NOW
    t2 = t1 + timedelta(minutes=5)
    clock = Mock(wraps=core.datetime)
    clock.now.side_effect = [t1, t2, t2 + timedelta(seconds=10), t2 + timedelta(seconds=30)]
    monkeypatch.setattr(core, "datetime", clock)
    first = _run(monkeypatch, capsys, tmp_path, store_dir)
    assert first
    assert block.render.call_args.args[0].since_is_fallback
    assert json.loads(_mark(store_dir)) == {
        "id": "s1",
        "at": t1.isoformat(),
        "start": t1.isoformat(),
        "prev_start": None,
    }
    s2 = PAYLOAD | {"session_id": "s2", "transcript_path": "/x/s2.jsonl"}
    assert _run(monkeypatch, capsys, tmp_path, store_dir, s2) == first
    ctx = block.render.call_args.args[0]
    assert ctx.since == t1 and not ctx.since_is_fallback
    saved = _mark(store_dir)
    expected = {
        "id": "s2",
        "at": t2.isoformat(),
        "start": t2.isoformat(),
        "prev_start": t1.isoformat(),
    }
    assert json.loads(saved) == expected
    assert _run(monkeypatch, capsys, tmp_path, store_dir, s2) == {}
    assert block.render.call_count == 2
    assert _last_log(store_dir) == ("SessionBriefing", "empty", "duplicate")
    assert _mark(store_dir) == saved
    assert _run(monkeypatch, capsys, tmp_path, store_dir, s2 | {"source": "compact"}) == first
    ctx = block.render.call_args.args[0]
    assert ctx.since == t1 and not ctx.since_is_fallback
    assert json.loads(_mark(store_dir)) == expected | {
        "at": (t2 + timedelta(seconds=30)).isoformat()
    }
    assert clock.now.call_count == 4


def test_a_refused_mark_write_still_briefs_under_the_fallback(
    monkeypatch, capsys, tmp_path, store_dir
):
    block = _printing("threads")
    _install(monkeypatch, block)
    monkeypatch.setattr(core, "changed_files", Mock(return_value=()))
    with closing(sqlite3.connect(store_dir / "index.db")) as other:
        other.execute("BEGIN IMMEDIATE")
        assert _run(monkeypatch, capsys, tmp_path, store_dir)
    assert block.render.call_args.args[0].since_is_fallback is True
    assert _mark(store_dir) is None
    assert _last_log(store_dir) == ("SessionBriefing", "delivered")


def test_a_lock_during_the_reads_ends_the_run_as_lock(monkeypatch, capsys, tmp_path, store_dir):
    block = _printing()
    _install(monkeypatch, block)

    def refused(self, ids, *, on_error):
        error = sqlite3.OperationalError("database is locked")
        error.sqlite_errorcode = sqlite3.SQLITE_BUSY
        self.lock_lost = True
        on_error(error)
        return []

    monkeypatch.setattr(HotIndex, "records_for", refused)
    assert _run(monkeypatch, capsys, tmp_path, store_dir) == {}
    block.render.assert_not_called()
    assert _last_log(store_dir) == ("SessionBriefing", "lock")
    assert _mark(store_dir) is None


def test_k13_the_flag_the_gate_and_the_reads_are_one_snapshot(monkeypatch, capsys, tmp_path):
    root, *_ = _synced_hot_pair(tmp_path)
    block = _printing("threads")
    _install(monkeypatch, block)
    opened = HotIndex.open
    trace: list[str] = []

    def traced(*args, **kwargs):
        index = opened(*args, **kwargs)
        assert index is not None
        index._conn.set_trace_callback(trace.append)
        return index

    refused = []
    gate = core.context.has_accepted_anchor

    def try_commit(index, on_error):
        result = gate(index, on_error)
        with closing(sqlite3.connect(root / "index.db", timeout=0)) as conn:
            conn.execute("UPDATE meta SET value='1' WHERE key=?", (VOLATILE_STALE_KEY,))
            try:
                conn.commit()
            except sqlite3.OperationalError:
                refused.append(True)
        return result

    def git_files(*args):
        trace.append("changed_files")
        return ()

    monkeypatch.setattr(HotIndex, "open", traced)
    monkeypatch.setattr(core.context, "has_accepted_anchor", try_commit)
    monkeypatch.setattr(core, "changed_files", git_files)
    assert _run(monkeypatch, capsys, tmp_path, root)
    assert refused == [True]
    assert block.render.call_args.args[0].by_file.keys() == {"live.py"}
    begin = trace.index("BEGIN")
    commit = trace.index("COMMIT")
    read = trace[begin + 1 : commit]
    assert trace.count("BEGIN") == 1
    flag = next(i for i, sql in enumerate(read) if "volatile_stale" in sql)
    gate_query = next(i for i, sql in enumerate(read) if "FROM anchor_bindings" in sql)
    rows_query = read.index("SELECT CAST(id AS BLOB), CAST(data AS BLOB) FROM decisions")
    assert flag < gate_query < rows_query
    assert commit < trace.index("BEGIN IMMEDIATE") < trace.index("changed_files")


def test_only_the_gate_reads_when_the_protocol_is_alone(monkeypatch, capsys, tmp_path, store_dir):
    _install(monkeypatch, _printing())
    rows = Mock(wraps=HotIndex.rows)
    git = Mock(wraps=core.changed_files)
    meta_calls = []
    get_meta = HotIndex.get_meta

    def meta(index, key):
        meta_calls.append(key)
        return get_meta(index, key)

    monkeypatch.setattr(HotIndex, "rows", rows)
    monkeypatch.setattr(HotIndex, "get_meta", meta)
    monkeypatch.setattr(core, "changed_files", git)
    assert _run(monkeypatch, capsys, tmp_path, store_dir)
    rows.assert_not_called()
    git.assert_not_called()
    assert DRIFT_CACHE_KEY not in meta_calls
    assert meta_calls == [VOLATILE_STALE_KEY]


def test_k15_git_failing_does_not_empty_the_briefing(monkeypatch, capsys, tmp_path, store_dir):
    blocks = tuple(_printing(name, [name]) for name in ("working_set", "abandoned", "threads"))
    _install(monkeypatch, *blocks)
    git = Mock(return_value=())
    monkeypatch.setattr(core, "changed_files", git)
    out = _run(monkeypatch, capsys, tmp_path, store_dir)
    git.assert_called_once()
    text = out["hookSpecificOutput"]["additionalContext"]
    for block in blocks:
        ctx = block.render.call_args.args[0]
        assert ctx.working_set == () and ctx.by_file
        assert block.name in text
    assert ctx.queue_count > 0 and ctx.since is not None


def test_a_session_started_in_a_subdirectory(monkeypatch, capsys, tmp_path, store_dir):
    _install(monkeypatch, _printing("working_set"))
    _git(tmp_path, "init", "-b", "main")
    sub = tmp_path / "sub"
    sub.mkdir()
    monkeypatch.chdir(sub)
    git = Mock(return_value=())
    monkeypatch.setattr(core, "changed_files", git)
    assert _run(monkeypatch, capsys, None, Path(".sidegraph"))
    git.assert_called_once_with(tmp_path, store_dir)


@pytest.mark.parametrize("failure", ["payload", "read_parts", "compose"])
def test_k16_the_entry_never_raises(failure, monkeypatch, capsys, tmp_path, store_dir):
    _install(monkeypatch, _printing())
    if failure == "read_parts":
        monkeypatch.setattr(core.context, "read_parts", Mock(side_effect=ValueError("bad data")))
    elif failure == "compose":
        monkeypatch.setattr(core, "compose", Mock(side_effect=ValueError("bad render")))
    assert (
        _run(monkeypatch, capsys, tmp_path, store_dir, "{" if failure == "payload" else PAYLOAD)
        == {}
    )
