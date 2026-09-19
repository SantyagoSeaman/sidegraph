"""sidegraph-stats: one screen of text, and a --json twin that agrees with it."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta

import pytest

from sidegraph.cli import stats_main
from sidegraph.stats.model import build_report
from sidegraph.stats.render import render_text
from sidegraph.store import Store


@pytest.fixture(autouse=True)
def _hermetic_graph(tmp_path, monkeypatch):
    """The default graph path is `graphify-out/graph.json` RELATIVE to the cwd, and pytest runs
    from the repo root, which has a real one. Without this, every test that compares against
    `build_report(..., None)` would silently measure this repository's own graph."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SIDEGRAPH_GRAPH", raising=False)


def _store_snapshot(store_dir):
    """Every path under the store directory with its size, mtime and content hash.

    The store's own directory is what a write by this command would touch, and it is
    untracked in every fixture here, so `git status` cannot see a write inside it. The hash
    catches a changed file, the mtime catches one rewritten with the same bytes, and the path
    set catches a created or removed one (directories included)."""
    import hashlib

    out = {}
    for p in sorted(store_dir.rglob("*")):
        rel = str(p.relative_to(store_dir))
        if p.is_dir():
            out[rel] = "dir"
        else:
            st = p.stat()
            out[rel] = (st.st_size, st.st_mtime_ns, hashlib.sha256(p.read_bytes()).hexdigest())
    return out


def test_text_and_json_come_from_one_report(tmp_path, capsys, monkeypatch):
    store = Store(tmp_path / ".sidegraph")
    store.record_touch("s1", "a.py", "Read")
    store.close()
    monkeypatch.setenv("SIDEGRAPH_DIR", str(tmp_path / ".sidegraph"))

    assert stats_main(["--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert stats_main([]) == 0
    text = capsys.readouterr().out

    assert payload["activation"]["sessions_total"] == 1
    assert text == render_text(build_report(tmp_path / ".sidegraph", None, window_days=30))


def test_reading_stats_changes_nothing(tmp_path, monkeypatch):
    """The store's own files are created by Store(), not by stats — so the baseline is the
    store directory BEFORE the command. Asserted on that directory itself, not on git: the
    whole `.sidegraph/` is untracked here, so a write inside it leaves `git status` untouched
    (a mutant that made `stats_main` write `decisions/MUTANT.json` passed the old check).
    """
    from datetime import UTC, datetime

    from sidegraph.schema import Decision, DecisionKind, DecisionStatus, Provenance

    repo = tmp_path / "repo"
    repo.mkdir()
    store_dir = repo / ".sidegraph"
    store = Store(store_dir)
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
    store.record_retrieval_events("s1", seeds=["a.py"], shows=[("r1", "a.py")])
    store.record_touch("s1", "a.py", "Edit")
    store.close()
    monkeypatch.setenv("SIDEGRAPH_DIR", str(store_dir))
    monkeypatch.chdir(repo)
    before = _store_snapshot(store_dir)
    assert "decisions" in before and "index.db" in before, "the snapshot sees the store"

    assert stats_main([]) == 0
    assert stats_main(["--json"]) == 0

    assert _store_snapshot(store_dir) == before


def test_a_pull_that_changed_a_record_is_stated_and_the_index_left_alone(
    tmp_path, capsys, monkeypatch
):
    """The command may not open a Store, so it cannot refresh the index after a `git pull`.
    It says so on both surfaces instead of printing the numbers from before the pull."""
    from datetime import UTC, datetime

    from sidegraph.schema import Decision, DecisionKind, DecisionStatus, Provenance

    store_dir = tmp_path / ".sidegraph"
    store = Store(store_dir)
    d = store.add_decision(
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
    monkeypatch.setenv("SIDEGRAPH_DIR", str(store_dir))
    assert stats_main([]) == 0
    assert "accepted: 1 decision" in capsys.readouterr().out

    path = store_dir / "decisions" / f"{d.id}.json"
    data = json.loads(path.read_text())
    data["status"] = "rejected"
    path.write_text(json.dumps(data, indent=2) + "\n")
    index_before = (store_dir / "index.db").read_bytes()

    assert stats_main([]) == 0
    text = capsys.readouterr().out
    assert "accepted: 1 decision" not in text, "the number from before the pull"
    assert "the index is behind the store files → sidegraph-init" in text

    assert stats_main(["--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["index_stale"] is True and payload["memory"] is None
    assert (store_dir / "index.db").read_bytes() == index_before, "stated, never repaired"


def test_a_narrowed_window_prints_no_false_absence(tmp_path, capsys, monkeypatch):
    """A 20-day-old touch under `--window 7`: the window is empty, the journal is not, and the
    screen may not say the journal has nothing in it."""
    store_dir = tmp_path / ".sidegraph"
    store = Store(store_dir)
    store.record_touch("old", "a.py", "Read")
    store.close()
    twenty_days_ago = (datetime.now(UTC) - timedelta(days=20)).isoformat()
    with closing(sqlite3.connect(store_dir / "index.db")) as conn, conn:
        conn.execute("UPDATE retrieval_events SET at = ?", (twenty_days_ago,))
    monkeypatch.setenv("SIDEGRAPH_DIR", str(store_dir))

    assert stats_main(["--window", "7"]) == 0
    text = capsys.readouterr().out
    assert "window: 7 days" in text.splitlines()[0]
    assert "retained" not in text.splitlines()[0]
    assert "no sessions recorded in this window" in text
    assert "no files touched in this window" in text
    assert "recorded yet" not in text

    assert stats_main(["--window", "30"]) == 0
    assert "1 session" in capsys.readouterr().out, "the same journal, read with a wide window"


def test_an_uninitialized_store_is_an_operational_error(tmp_path, monkeypatch):
    monkeypatch.setenv("SIDEGRAPH_DIR", str(tmp_path / "missing"))
    assert stats_main([]) == 2


def test_a_nonpositive_window_is_rejected_before_any_io(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("SIDEGRAPH_DIR", str(tmp_path / "missing"))
    assert stats_main(["--window", "0"]) == 2
    # Exit 2 alone proves nothing here: a missing store also exits 2. The message names the
    # cause, so this fails if the flag stops being what rejects the run.
    err = capsys.readouterr().err
    assert "--window" in err and "no store index" not in err
    assert not (tmp_path / "missing").exists(), "a rejected flag must create no store"


def test_window_zero_is_rejected_against_a_real_store_too(tmp_path, capsys, monkeypatch):
    """Against a missing store the exit 2 can come from the store, so the boundary itself
    (`<= 0`, not `< 0`) is only exercised here, where nothing else can supply it."""
    Store(tmp_path / ".sidegraph").close()
    monkeypatch.setenv("SIDEGRAPH_DIR", str(tmp_path / ".sidegraph"))

    assert stats_main(["--window", "0"]) == 2
    out = capsys.readouterr()
    assert out.out == "" and "--window" in out.err

    assert stats_main(["--window", "1"]) == 0, "the smallest valid window is accepted"


# --- what the brief did not spell out ---------------------------------------------------


def test_a_rejected_window_says_which_flag_and_prints_nothing_on_stdout(
    tmp_path, capsys, monkeypatch
):
    monkeypatch.setenv("SIDEGRAPH_DIR", str(tmp_path / "missing"))
    assert stats_main(["--window", "-3"]) == 2
    out = capsys.readouterr()
    assert out.out == ""
    assert "--window" in out.err


def test_a_window_too_large_to_date_is_rejected_not_a_traceback(tmp_path, capsys, monkeypatch):
    """`now - timedelta(days=N)` overflows past year 1, and that OverflowError would surface
    from deep inside the aggregator as a stack trace."""
    monkeypatch.setenv("SIDEGRAPH_DIR", str(tmp_path / "missing"))
    assert stats_main(["--window", "999999999"]) == 2
    assert "--window" in capsys.readouterr().err
    assert not (tmp_path / "missing").exists()


def test_a_missing_store_names_the_command_that_creates_it(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("SIDEGRAPH_DIR", str(tmp_path / "missing"))
    assert stats_main([]) == 2
    out = capsys.readouterr()
    assert out.out == ""
    assert "sidegraph-init" in out.err
    assert not (tmp_path / "missing").exists(), "stats never creates a store"


def test_a_store_directory_with_no_index_is_an_operational_error(tmp_path, capsys, monkeypatch):
    """A fresh clone carries the committed records but not the derived, gitignored index.
    `sidegraph-init` opens a Store, which rebuilds it — so that is the right next step."""
    store_dir = tmp_path / ".sidegraph"
    Store(store_dir).close()
    (store_dir / "index.db").unlink()
    monkeypatch.setenv("SIDEGRAPH_DIR", str(store_dir))

    assert stats_main([]) == 2
    assert "sidegraph-init" in capsys.readouterr().err
    assert not (store_dir / "index.db").exists(), "stats must not rebuild the index itself"


def test_db_flag_wins_over_the_environment_like_every_sibling(tmp_path, capsys, monkeypatch):
    real = tmp_path / "real"
    store = Store(real)
    store.record_touch("s1", "a.py", "Read")
    store.close()
    monkeypatch.setenv("SIDEGRAPH_DIR", str(tmp_path / "elsewhere"))

    assert stats_main(["--db", str(real), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["activation"]["sessions_total"] == 1


def test_an_index_from_before_the_render_journal_degrades_and_says_so(
    tmp_path, capsys, monkeypatch
):
    """A store created before this feature has no `render_events` table. That is a state every
    upgrading user passes through, so it reports what it can — exit 0, no traceback."""
    store_dir = tmp_path / ".sidegraph"
    store = Store(store_dir)
    for i in range(6):
        store.record_retrieval_events(f"s{i}", seeds=["a.py"], shows=[(f"r{i}", "a.py")])
    store.close()
    ten_days_ago = (datetime.now(UTC) - timedelta(days=10)).isoformat()
    with closing(sqlite3.connect(store_dir / "index.db")) as conn:
        # Journal rows carry the real clock; without this the window is under the 3-day floor
        # and the activation block would be the one-line "too little to summarize" instead.
        conn.execute("UPDATE retrieval_events SET at = ?", (ten_days_ago,))
        conn.execute("DROP TABLE render_events")
        conn.commit()
    monkeypatch.setenv("SIDEGRAPH_DIR", str(store_dir))

    assert stats_main([]) == 0
    out = capsys.readouterr()
    assert "not recorded yet" in out.out
    assert "dropped by it" not in out.out
    assert out.err == ""

    assert stats_main(["--json"]) == 0
    assert json.loads(capsys.readouterr().out)["activation"]["render_journal"] is False


def test_an_unrecognised_index_is_an_operational_error_not_a_traceback(
    tmp_path, capsys, monkeypatch
):
    store_dir = tmp_path / ".sidegraph"
    Store(store_dir).close()
    (store_dir / "index.db").write_bytes(b"this is not a sqlite file" * 100)
    monkeypatch.setenv("SIDEGRAPH_DIR", str(store_dir))

    assert stats_main([]) == 2
    out = capsys.readouterr()
    assert out.out == ""
    assert "index.db" in out.err


def test_a_corrupt_graph_is_not_reported_as_unbuilt(tmp_path, capsys, monkeypatch):
    store_dir = tmp_path / ".sidegraph"
    Store(store_dir).close()
    graph = tmp_path / "graph.json"
    graph.write_text("{ not json")
    monkeypatch.setenv("SIDEGRAPH_DIR", str(store_dir))
    monkeypatch.setenv("SIDEGRAPH_GRAPH", str(graph))

    assert stats_main([]) == 0
    text = capsys.readouterr().out
    assert "could not be read" in text
    assert "not built yet" not in text


def test_a_missing_graph_is_reported_as_unbuilt(tmp_path, capsys, monkeypatch):
    store_dir = tmp_path / ".sidegraph"
    Store(store_dir).close()
    monkeypatch.setenv("SIDEGRAPH_DIR", str(store_dir))
    monkeypatch.setenv("SIDEGRAPH_GRAPH", str(tmp_path / "nope.json"))

    assert stats_main([]) == 0
    assert "not built yet → sidegraph-init" in capsys.readouterr().out


def test_the_graph_flag_overrides_the_environment(tmp_path, capsys, monkeypatch):
    store_dir = tmp_path / ".sidegraph"
    Store(store_dir).close()
    graph = tmp_path / "graph.json"
    graph.write_text(json.dumps({"nodes": [], "links": []}))
    monkeypatch.setenv("SIDEGRAPH_DIR", str(store_dir))
    monkeypatch.setenv("SIDEGRAPH_GRAPH", str(tmp_path / "nope.json"))

    assert stats_main(["--graph", str(graph), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["graph"]["available"] is True


def test_the_window_flag_reaches_the_report(tmp_path, capsys, monkeypatch):
    Store(tmp_path / ".sidegraph").close()
    monkeypatch.setenv("SIDEGRAPH_DIR", str(tmp_path / ".sidegraph"))
    assert stats_main(["--window", "7", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["window_days"] == 7


def _six_asking_sessions(store_dir, *, render_rows):
    """Six sessions that asked for memory ten days ago, with `render_rows` render events."""
    store = Store(store_dir)
    for i in range(6):
        store.record_retrieval_events(f"s{i}", seeds=["a.py"], shows=[(f"r{i}", "a.py")])
        for _ in range(render_rows):
            store.record_render_event(
                f"s{i}",
                intent=None,
                selected=1,
                emitted=1,
                degraded=0,
                dropped_for_budget=0,
                chars_used=10,
                had_rejected=False,
                had_superseded=False,
            )
    store.close()
    ten_days_ago = (datetime.now(UTC) - timedelta(days=10)).isoformat()
    with closing(sqlite3.connect(store_dir / "index.db")) as conn:
        conn.execute("UPDATE retrieval_events SET at = ?", (ten_days_ago,))
        conn.execute("UPDATE render_events SET at = ?", (ten_days_ago,))
        conn.commit()


def test_an_empty_render_journal_is_not_printed_as_zeroes_but_a_real_zero_is(
    tmp_path, capsys, monkeypatch
):
    """The state every upgrading user first runs the command in: the table exists, the server
    that fills it has not run. It must not read as "the budget never clipped anything"."""
    empty = tmp_path / "empty" / ".sidegraph"
    _six_asking_sessions(empty, render_rows=0)
    monkeypatch.setenv("SIDEGRAPH_DIR", str(empty))
    assert stats_main([]) == 0
    text = capsys.readouterr().out
    assert "not recorded yet" in text
    assert "shortened" not in text and "0 lookups" not in text

    recorded = tmp_path / "recorded" / ".sidegraph"
    _six_asking_sessions(recorded, render_rows=1)
    monkeypatch.setenv("SIDEGRAPH_DIR", str(recorded))
    assert stats_main([]) == 0
    text = capsys.readouterr().out
    assert "0 shortened to fit the budget, 0 dropped by it" in text
    assert "not recorded yet" not in text


def test_intent_reaches_the_json_and_never_the_screen(tmp_path, capsys, monkeypatch):
    store_dir = tmp_path / ".sidegraph"
    store = Store(store_dir)
    for i, intent in enumerate(["check-plan", "check-plan", "explain-why", None]):
        store.record_render_event(
            f"s{i}",
            intent=intent,
            selected=1,
            emitted=1,
            degraded=0,
            dropped_for_budget=0,
            chars_used=10,
            had_rejected=False,
            had_superseded=False,
        )
    store.close()
    monkeypatch.setenv("SIDEGRAPH_DIR", str(store_dir))

    assert stats_main(["--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["activation"]["self_reported_intents"] == {"check-plan": 2, "explain-why": 1}

    assert stats_main([]) == 0
    assert "check-plan" not in capsys.readouterr().out


# --- a relative --graph belongs to the store's project, not to the shell (finding 9) --------


def _copy_graph(dest_dir):
    """A parseable graph with nodes in it, at `<dest_dir>/graphify-out/graph.json`."""
    import shutil
    from pathlib import Path

    target = dest_dir / "graphify-out" / "graph.json"
    target.parent.mkdir(parents=True)
    shutil.copy(Path(__file__).parent / "fixtures" / "mini_graph.json", target)
    return target


def test_a_relative_graph_resolves_against_the_stores_project_not_the_shell(
    tmp_path, capsys, monkeypatch
):
    """The reproduction: a fresh empty store elsewhere printed THIS shell's graph beside its
    own `0 decisions`. The store's project has no graph, the shell's directory has one."""
    project = tmp_path / "project"
    Store(project / ".sidegraph").close()
    shell = tmp_path / "shell"
    shell.mkdir()
    _copy_graph(shell)
    monkeypatch.chdir(shell)
    monkeypatch.delenv("SIDEGRAPH_GRAPH", raising=False)

    assert stats_main(["--db", str(project / ".sidegraph"), "--json"]) == 0
    graph = json.loads(capsys.readouterr().out)["graph"]
    assert graph["available"] is False and graph["nodes"] is None, "the shell's graph leaked in"

    assert stats_main(["--db", str(project / ".sidegraph")]) == 0
    assert "not built yet" in capsys.readouterr().out


def test_a_relative_graph_is_found_beside_its_own_store_from_anywhere(
    tmp_path, capsys, monkeypatch
):
    project = tmp_path / "project"
    Store(project / ".sidegraph").close()
    _copy_graph(project)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    monkeypatch.delenv("SIDEGRAPH_GRAPH", raising=False)

    assert stats_main(["--db", str(project / ".sidegraph"), "--json"]) == 0
    graph = json.loads(capsys.readouterr().out)["graph"]
    assert graph["available"] is True and graph["nodes"] > 0


def test_an_absolute_graph_is_left_alone(tmp_path, capsys, monkeypatch):
    project = tmp_path / "project"
    Store(project / ".sidegraph").close()
    other = _copy_graph(tmp_path / "other")
    monkeypatch.chdir(tmp_path)

    assert stats_main(["--db", str(project / ".sidegraph"), "--graph", str(other), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["graph"]["available"] is True


def test_the_graph_env_var_is_anchored_the_same_way(tmp_path, capsys, monkeypatch):
    project = tmp_path / "project"
    Store(project / ".sidegraph").close()
    _copy_graph(project)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SIDEGRAPH_GRAPH", "graphify-out/graph.json")

    assert stats_main(["--db", str(project / ".sidegraph"), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["graph"]["available"] is True


# --- a record row the index cannot parse keeps the exit-2 contract (finding 10) ---------------


def _store_with_one_of_each(tmp_path):
    from datetime import UTC, datetime

    from sidegraph.schema import (
        AnchorBinding,
        Decision,
        DecisionKind,
        DecisionStatus,
        Descriptor,
        Fact,
        Provenance,
    )

    store_dir = tmp_path / ".sidegraph"
    store = Store(store_dir)
    e = store.get_or_create_entity(Descriptor(name="f", file_path="a.py"))
    d = store.add_decision(
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
    f = store.add_fact(
        Fact(
            statement="s",
            source="src",
            status=DecisionStatus.ACCEPTED,
            valid_from=datetime.now(UTC),
            provenance=Provenance(source="manual"),
        )
    )
    store.add_binding(AnchorBinding(record_id=d.id, entity_id=e.entity_id, tier=2))
    store.close()
    return store_dir, {"decisions": d.id, "facts": f.id, "entities": e.entity_id}


@pytest.mark.parametrize("table", ["decisions", "facts", "entities"])
@pytest.mark.parametrize("damage", ["not json at all", '{"valid": "json", "wrong": "shape"}'])
def test_an_unparseable_record_row_is_an_operational_error_naming_file_and_record(
    tmp_path, capsys, monkeypatch, table, damage
):
    store_dir, ids = _store_with_one_of_each(tmp_path)
    with closing(sqlite3.connect(store_dir / "index.db")) as conn, conn:
        conn.execute(f"UPDATE {table} SET data = ?", (damage,))
    monkeypatch.setenv("SIDEGRAPH_DIR", str(store_dir))

    assert stats_main([]) == 2
    out = capsys.readouterr()
    assert out.out == "", "nothing on stdout: the report was not produced"
    assert "index.db" in out.err and ids[table] in out.err and table in out.err
    assert "Traceback" not in out.err

    assert stats_main(["--json"]) == 2
    assert capsys.readouterr().out == ""


def test_an_unparseable_binding_row_names_both_ends_of_the_binding(tmp_path, capsys, monkeypatch):
    store_dir, ids = _store_with_one_of_each(tmp_path)
    with closing(sqlite3.connect(store_dir / "index.db")) as conn, conn:
        conn.execute("UPDATE anchor_bindings SET data = 'nope'")
    monkeypatch.setenv("SIDEGRAPH_DIR", str(store_dir))

    assert stats_main([]) == 2
    err = capsys.readouterr().err
    assert ids["decisions"] in err and ids["entities"] in err and "anchor_bindings" in err


def test_json_states_the_same_absences_as_the_text(tmp_path, capsys, monkeypatch):
    """Recording off, no render journal and no graph: the text withholds each figure, and the
    JSON a script reads must not print a zero in its place."""
    store = Store(tmp_path / ".sidegraph")
    store.record_touch("s1", "a.py", "Read")
    store.close()
    monkeypatch.setenv("SIDEGRAPH_DIR", str(tmp_path / ".sidegraph"))
    monkeypatch.setenv("SIDEGRAPH_TELEMETRY", "off")

    assert stats_main(["--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert stats_main([]) == 0
    text = capsys.readouterr().out

    assert "recording is off" in text
    assert payload["activation"] is None and payload["retained_days"] is None
    assert payload["reach"]["files_touched"] is None
    assert payload["graph"]["available"] is False
    assert (payload["graph"]["nodes"], payload["graph"]["files"]) == (None, None)
    assert payload["memory"]["decisions"] == 0, "a store-derived count is a measurement"


# --- what --db promises (fix wave 2, finding 5) -----------------------------------------


def _help_of(main, capsys) -> str:
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
    return " ".join(capsys.readouterr().out.split())  # argparse wraps to the terminal width


def test_the_stats_help_says_db_is_a_directory_and_a_legacy_file_needs_a_store_open(capsys):
    """`--db legacy.db` exits 2 looking for `legacy.db/index.db`: this command never opens a
    `Store`, and migration happens when one opens. The shared help promises otherwise."""
    text = _help_of(stats_main, capsys)
    assert "store directory" in text
    assert "legacy" in text and "sidegraph-init" in text
    assert "one-time migration" not in text


def test_a_legacy_file_path_is_refused_not_migrated(tmp_path, capsys):
    legacy = tmp_path / "legacy.db"
    legacy.write_bytes(b"")

    assert stats_main(["--db", str(legacy)]) == 2
    assert "no store index" in capsys.readouterr().err
    assert legacy.read_bytes() == b"", "nothing was migrated or written"


def test_the_shared_db_help_still_promises_migration_where_it_is_true(capsys):
    """The commands that open a `Store` do migrate a legacy file; only this one must not
    inherit that sentence."""
    from sidegraph.cli import export_okf_main

    assert "one-time migration" in _help_of(export_okf_main, capsys)
