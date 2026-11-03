"""Every ``--json`` command on an operational error (exit 1): stdout carries exactly one JSON
object with an ``error`` key (``sidegraph-verify`` and ``sidegraph-doctor`` also keep their
success shape's keys), and the human message stays on stderr (see docs/reference/cli.md,
"Exit codes and streams")."""

import errno
import json
import os
import sqlite3
import subprocess
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

import pytest

from sidegraph import cli
from sidegraph.cli import doctor_main, verify_main
from sidegraph.schema import Decision, DecisionKind, DecisionStatus, Provenance
from sidegraph.store import Store

_KEYS = {
    "verify": {"clean", "violations"},
    "doctor": {"clean", "violations", "findings", "skipped"},
}
_MAIN = {"verify": verify_main, "doctor": doctor_main}


def _store(tmp_path: Path) -> Path:
    db = tmp_path / ".sidegraph"
    Store(db).close()
    return db


def _missing_store(tmp_path: Path) -> list[str]:
    return ["--db", str(tmp_path / "nope")]


def _outside_git(tmp_path: Path) -> list[str]:
    return ["--db", str(_store(tmp_path)), "--against", "HEAD"]  # tmp_path is no git repo


def _bad_ref(tmp_path: Path) -> list[str]:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    return ["--db", str(_store(tmp_path)), "--against", "no-such-ref"]


@pytest.mark.parametrize("cmd", ["verify", "doctor"])
@pytest.mark.parametrize(
    "case",
    [_missing_store, _outside_git, _bad_ref],
    ids=["missing-store", "outside-git", "bad-ref"],
)
def test_json_operational_error_is_one_json_object_on_stdout(cmd, case, tmp_path, capsys):
    assert _MAIN[cmd]([*case(tmp_path), "--json"]) == 1
    captured = capsys.readouterr()
    report = json.loads(captured.out)  # the whole of stdout is one JSON object
    assert set(report) == _KEYS[cmd] | {"error"}
    assert report["clean"] is False
    assert report["error"] and report["error"] == captured.err.strip()


def test_doctor_error_object_says_the_advisory_checks_were_skipped(tmp_path, capsys):
    assert doctor_main([*_missing_store(tmp_path), "--json"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["findings"] == [] and report["skipped"] == ["advisory-checks"]


@pytest.mark.parametrize("cmd", ["verify", "doctor"])
def test_a_failed_against_keeps_the_snapshot_violations(cmd, tmp_path, capsys):
    argv = _bad_ref(tmp_path)
    (tmp_path / ".sidegraph" / "decisions" / "01ARZ3NDEKTSV4RRFFQ69G5FAV.json").write_text(
        "{not json", encoding="utf-8"
    )
    assert _MAIN[cmd]([*argv, "--json"]) == 1
    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert report["error"] == captured.err.strip()
    paths = [v["path"] for v in report["violations"]]
    assert len(paths) == 1 and paths[0].endswith("decisions/01ARZ3NDEKTSV4RRFFQ69G5FAV.json")


@pytest.mark.parametrize(
    ("days", "message"),
    [
        ("-1", "--stale-days must be >= 0, got -1"),
        ("999999999", "--stale-days is too large to date, got 999999999"),
        (str(10**12), f"--stale-days is too large to date, got {10**12}"),
    ],
)
def test_doctor_json_bad_stale_days_is_one_json_object(days, message, tmp_path, capsys):
    assert doctor_main(["--db", str(_store(tmp_path)), "--stale-days", days, "--json"]) == 1
    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert set(report) == _KEYS["doctor"] | {"error"}
    assert report["error"] == captured.err.strip() == message


@pytest.mark.parametrize("cmd", ["verify", "doctor"])
def test_text_operational_error_leaves_stdout_empty(cmd, tmp_path, capsys):
    assert _MAIN[cmd](_missing_store(tmp_path)) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("store not readable (")


# The other --json commands: the error object carries only ``error`` — their success shapes
# hold counts and lists that an aborted run has no honest value for.
def _sync_no_graph(tmp_path: Path) -> list[str]:
    return ["--db", str(_store(tmp_path)), "--graph", str(tmp_path / "no.json")]


def _sync_store_is_a_file(tmp_path: Path) -> list[str]:
    graph = tmp_path / "graph.json"
    graph.write_text('{"nodes": [], "links": []}', encoding="utf-8")
    db = tmp_path / "store-file"
    db.write_text("not a store", encoding="utf-8")
    return ["--db", str(db), "--graph", str(graph)]


def _corrupt_index_store(tmp_path: Path) -> Path:
    db = _store(tmp_path)
    for side in db.glob("index.db-*"):
        side.unlink()
    (db / "index.db").write_bytes(b"not a sqlite database " * 64)
    return db


def _malformed_row_store(tmp_path: Path) -> Path:
    """A structurally valid index whose one decision row does not parse: ``Store()`` opens it,
    and reading the records fails."""
    db = tmp_path / ".sidegraph"
    s = Store(db)
    s.add_decision(
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
    s.close()
    with closing(sqlite3.connect(db / "index.db")) as conn, conn:
        conn.execute("UPDATE decisions SET data = 'not json at all'")
    return db


ONLY_ERROR = [
    ("sync-no-graph", cli.sync_main, _sync_no_graph),
    ("sync-check-no-graph", cli.sync_main, lambda t: [*_sync_no_graph(t), "--check"]),
    ("sync-store-is-a-file", cli.sync_main, _sync_store_is_a_file),
    ("stats-bad-window", cli.stats_main, lambda t: ["--db", str(_store(t)), "--window", "0"]),
    ("stats-no-index", cli.stats_main, _missing_store),
    ("stats-corrupt-index", cli.stats_main, lambda t: ["--db", str(_corrupt_index_store(t))]),
    ("viz-no-store", cli.viz_main, _missing_store),
    ("viz-corrupt-index", cli.viz_main, lambda t: ["--db", str(_corrupt_index_store(t))]),
    ("viz-malformed-row", cli.viz_main, lambda t: ["--db", str(_malformed_row_store(t))]),
    ("blame-bad-range", cli.blame_main, lambda t: ["a.py", "--range", "x"]),
    # Both pass a digit check but not int(): a double minus and a superscript digit.
    ("blame-double-minus-range", cli.blame_main, lambda t: ["a.py", "--range=--1,2"]),
    ("blame-superscript-range", cli.blame_main, lambda t: ["a.py", "--range=²,2"]),
    ("blame-outside-git", cli.blame_main, lambda t: ["a.py", "--db", str(t / "nope")]),
]


@pytest.mark.parametrize(("name", "main", "argv"), ONLY_ERROR, ids=[c[0] for c in ONLY_ERROR])
def test_other_json_commands_print_one_error_object(
    name, main, argv, tmp_path, monkeypatch, capsys
):
    monkeypatch.chdir(tmp_path)  # blame runs git in the working directory: no repository here
    assert main([*argv(tmp_path), "--json"]) == 1
    captured = capsys.readouterr()
    report = json.loads(captured.out)  # the whole of stdout is one JSON object
    assert set(report) == {"error"}, name
    assert report["error"] and report["error"] == captured.err.strip(), name


@pytest.mark.parametrize(("name", "main", "argv"), ONLY_ERROR, ids=[c[0] for c in ONLY_ERROR])
def test_other_commands_without_json_leave_stdout_empty(
    name, main, argv, tmp_path, monkeypatch, capsys
):
    monkeypatch.chdir(tmp_path)
    assert main(argv(tmp_path)) == 1
    captured = capsys.readouterr()
    assert captured.out == "", name
    assert captured.err.strip(), name


def test_sync_failed_is_one_error_object(tmp_path, monkeypatch, capsys):
    def boom(*args, **kwargs):
        raise RuntimeError("rebind exploded")

    monkeypatch.setattr("sidegraph.cli.sync", boom)
    graph = tmp_path / "graph.json"
    graph.write_text('{"nodes": [], "links": []}', encoding="utf-8")
    argv = ["--db", str(_store(tmp_path)), "--graph", str(graph), "--json"]
    assert cli.sync_main(argv) == 1
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"error": "sync failed: rebind exploded"}
    assert captured.err.strip() == "sync failed: rebind exploded"


@pytest.mark.skipif(os.name == "nt", reason="POSIX modes")
@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores modes")
def test_stats_unlistable_record_dir_is_one_error_object(tmp_path, capsys):
    db = _store(tmp_path)
    decisions = db / "decisions"
    decisions.chmod(0o000)
    try:
        assert cli.stats_main(["--db", str(db), "--json"]) == 1
        captured = capsys.readouterr()
    finally:
        decisions.chmod(0o755)
    report = json.loads(captured.out)
    assert report == {"error": captured.err.strip()}
    assert report["error"].startswith(f"cannot read the store ({decisions})")


@pytest.mark.parametrize("name", ["decisions", "shards"])
def test_stats_self_referencing_record_dir_is_one_error_object(name, tmp_path, capsys):
    db = _store(tmp_path)
    loop = db / name
    if loop.exists():
        loop.rmdir()
    try:
        os.symlink(loop, loop)  # ELOOP on every probe, root or not
    except OSError as e:  # Windows without the symlink privilege
        pytest.skip(f"cannot create a symlink here: {e}")
    assert cli.stats_main(["--db", str(db), "--json"]) == 1
    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert report == {"error": captured.err.strip()}
    assert report["error"].startswith(f"cannot read the store ({loop}): ")


def _eio(path: Path) -> OSError:
    return OSError(errno.EIO, "Input/output error", str(path))


# Injected, so these run on Windows and as root too: an OSError other than PermissionError from
# a store read is the same operational error, never a traceback.
@pytest.mark.parametrize("target", ["require_listable_record_dirs", "build_report"])
def test_stats_injected_os_error_is_one_error_object(target, tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)  # isolate the injected error from checkout graph advice
    db = _store(tmp_path)
    broken = db / "decisions" / "x.json"
    monkeypatch.setattr(f"sidegraph.cli.{target}", mock.Mock(side_effect=_eio(broken)))
    assert cli.stats_main(["--db", str(db), "--json"]) == 1
    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert report == {"error": captured.err.strip()}
    assert report["error"] == f"cannot read the store ({broken}): Input/output error"


def test_doctor_injected_os_error_in_the_advisory_checks(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)  # isolate the injected error from checkout graph advice
    db = _store(tmp_path)
    monkeypatch.setattr("sidegraph.cli.curate", mock.Mock(side_effect=_eio(db / "x.json")))
    assert doctor_main(["--db", str(db), "--json"]) == 1
    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert report["error"] == captured.err.strip()
    assert report["error"].startswith(f"store not readable ({db}): ")
    assert report["skipped"] == ["advisory-checks"] and report["clean"] is False


def test_viz_injected_os_error_is_one_error_object(tmp_path, monkeypatch, capsys):
    db = _store(tmp_path)
    monkeypatch.setattr("sidegraph.cli.build_graph", mock.Mock(side_effect=_eio(db / "x")))
    assert cli.viz_main(["--db", str(db), "--json"]) == 1
    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert report == {"error": captured.err.strip()}
    assert report["error"].startswith(f"store not readable ({db}): ")
