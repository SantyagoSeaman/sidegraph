"""Git path framing must not hide canonical edits or corrupt filesystem names."""

from __future__ import annotations

import errno
import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from sidegraph import verify
from sidegraph.cli import doctor_main, verify_main
from sidegraph.gitenv import git_env
from sidegraph.schema import Decision, DecisionKind, Provenance
from sidegraph.store import Store


@pytest.fixture(params=["sha1", "sha256"])
def repo(tmp_path: Path, request) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    result = subprocess.run(
        ["git", "init", "-q", f"--object-format={request.param}"],
        cwd=root,
        env=git_env(),
        capture_output=True,
        text=True,
    )
    if (
        request.param == "sha256"
        and result.returncode != 0
        and ("unknown option" in result.stderr or "unknown hash algorithm" in result.stderr)
    ):
        pytest.skip(f"Git lacks SHA256: {result.stderr.strip()}")
    assert result.returncode == 0, result.stderr
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    return root


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=root,
        env=git_env(),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _decision(**kwargs) -> Decision:
    return Decision(
        title="Synthetic framing record",
        kind=DecisionKind.ADR,
        context="Synthetic context",
        choice="Original choice",
        valid_from=datetime(2026, 1, 10, tzinfo=UTC),
        provenance=Provenance(source="manual"),
        **kwargs,
    )


def _hot(store: Path) -> Path:
    with Store(store) as opened:
        record = opened.add_decision(_decision())
    return store / "decisions" / f"{record.id}.json"


def _commit(root: Path) -> None:
    _git(root, "add", "-A")
    _git(root, "-c", "core.hooksPath=/dev/null", "commit", "-q", "-m", "baseline")


def _edit(path: Path) -> None:
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["choice"] = "Hand edited immutable choice"
    path.write_text(json.dumps(payload) + "\n", encoding="utf-8")


def _canonical_bytes(store: Path) -> dict[str, bytes]:
    return {
        p.relative_to(store).as_posix(): p.read_bytes()
        for p in store.rglob("*")
        if p.suffix in {".json", ".jsonl"}
    }


NAMES = [
    ".sidegraph",
    "store space",
    "store[brackets]",
    "store-é",
    "store\tTAB",
    "store\nLF",
    "store\rCR",
    'store"quote',
    "store\\backslash",
    " store ",
]


@pytest.mark.parametrize("name", NAMES)
def test_actual_store_names_detect_immutable_edit(repo: Path, name: str):
    # Reverting to line/quote parsing hides this real, snapshot-valid immutable edit.
    store = repo / name
    path = _hot(store)
    _commit(repo)
    _edit(path)
    assert verify.verify_snapshot(store) == []
    before = _canonical_bytes(store)
    findings = verify.verify_against(store, "HEAD")
    assert _canonical_bytes(store) == before
    assert [(v.code, v.path) for v in findings] == [
        (verify.ILLEGAL_FIELD_CHANGE, path.relative_to(repo).as_posix())
    ]


def test_posix_undecodable_store_name_detects_edit(repo: Path):
    if os.name != "posix":
        pytest.skip("POSIX surrogateescape filesystem path required")
    initial = repo / "initial"
    path = _hot(initial)
    name = os.fsdecode(b"store-\xff")
    assert os.fsencode(name) == b"store-\xff"
    store = repo / name
    try:
        initial.rename(store)  # Close SQLite before moving to an undecodable pathname.
    except OSError as error:
        if error.errno == errno.EILSEQ:
            pytest.skip("Filesystem rejects undecodable filename bytes (EILSEQ)")
        raise
    path = store / "decisions" / path.name
    _commit(repo)
    _edit(path)
    assert verify.verify_snapshot(store) == []
    before = _canonical_bytes(store)
    findings = verify.verify_against(store, "HEAD")
    assert _canonical_bytes(store) == before
    assert [(v.code, os.fsencode(v.path)) for v in findings] == [
        (verify.ILLEGAL_FIELD_CHANGE, os.fsencode(path.relative_to(repo).as_posix()))
    ]


@pytest.mark.parametrize("store_name", [".sidegraph", "store\t\r-é"])
@pytest.mark.parametrize("segment_name", ["segment.jsonl", 'seg\n\r\t"é.jsonl'])
@pytest.mark.parametrize("change", ["edit", "delete"])
def test_real_archive_segment_names_cannot_hide_changes(
    repo: Path,
    store_name: str,
    segment_name: str,
    change: str,
):
    store = repo / store_name
    with Store(store) as opened:
        old = opened.add_decision(_decision())
        opened.ratify(old.id)
        opened.add_decision(
            Decision(
                title="Successor",
                kind=DecisionKind.ADR,
                context="Synthetic context",
                choice="Successor choice",
                valid_from=datetime(2026, 2, 1, tzinfo=UTC),
                supersedes=old.id,
                provenance=Provenance(source="manual"),
            )
        )
        opened.compact()
    original = next((store / "archive").glob("*.jsonl"))
    segment = original.rename(original.with_name(segment_name))
    assert verify.verify_snapshot(store) == []
    _commit(repo)
    if change == "delete":
        segment.unlink()
        expected = verify.ILLEGAL_DELETION
    else:
        lines = [json.loads(line) for line in segment.read_text().splitlines()]
        assert lines and any(item["id"] == old.id for item in lines)
        for item in lines:
            item["choice"] = "Changed archived immutable choice"
        segment.write_text("".join(json.dumps(item) + "\n" for item in lines))
        assert verify.verify_snapshot(store) == []
        expected = verify.ILLEGAL_FIELD_CHANGE
    before = _canonical_bytes(store)
    findings = verify.verify_against(store, "HEAD")
    assert _canonical_bytes(store) == before
    assert [(v.code, v.path) for v in findings] == [
        (expected, segment.relative_to(repo).as_posix())
    ]


@pytest.mark.parametrize("main", [verify_main, doctor_main], ids=["verify", "doctor"])
@pytest.mark.parametrize("name", ["store\rCR", 'store\t\n"é'])
def test_cli_json_preserves_actual_changed_path(repo: Path, main, name: str, capsys):
    graph = repo / "graphify-out" / "graph.json"
    graph.parent.mkdir()
    graph.write_text('{"nodes": [], "edges": []}\n')
    store = repo / name
    path = _hot(store)
    _commit(repo)
    _edit(path)
    before = _canonical_bytes(store)
    args = ["--db", str(store), "--against", "HEAD", "--json"]
    if main is doctor_main:
        args += ["--graph", str(graph)]
    code = main(args)
    output = capsys.readouterr()
    assert _canonical_bytes(store) == before
    assert code == 2
    assert not output.err
    report = json.loads(output.out)
    assert report["clean"] is False
    assert [(v["code"], v["path"]) for v in report["violations"]] == [
        (verify.ILLEGAL_FIELD_CHANGE, path.relative_to(repo).as_posix())
    ]


def _parser():
    parser = getattr(verify, "_parse_git_name_status_z", None)
    assert callable(parser), "NUL byte parser is absent"
    return parser


@pytest.mark.parametrize(
    "stream,want",
    [
        (b"", []),
        (b"M\0.sidegraph/decisions/A.json\0", [("M", ".sidegraph/decisions/A.json")]),
        (
            b"A\0a\0D\0d\0M\0m\0T\0t\0U\0u\0",
            [("A", "a"), ("D", "d"), ("M", "m"), ("T", "t"), ("U", "u")],
        ),
        (b'M\0 x\t\n\r"\\\0', [("M", ' x\t\n\r"\\')]),
        (b"M\0caf\xc3\xa9 \0", [("M", "café ")]),
    ],
)
def test_parser_preserves_complete_fields(stream, want):
    assert _parser()(stream) == want


def test_parser_preserves_posix_bytes():
    if os.name != "posix":
        pytest.skip("POSIX surrogateescape required")
    assert _parser()(b"M\0raw-\xff\0") == [("M", "raw-\udcff")]


MALFORMED = [
    "M\0path\0",
    None,
    b"M\0path",
    b"M\0path\0D\0",
    b"\0path\0",
    b"M\0\0",
    b"\0",
    b"M\tpath\n",
    b"R100\0a\0b\0",
    b"C100\0a\0b\0",
    b"M100\0a\0",
    b"X\0a\0",
    b"B\0a\0",
    b"Q\0a\0",
    b"MM\0a\0",
    b"M\n\0a\0",
    b"M\t\0a\0",
    b"\xff\0a\0",
    b"M\0good\0D\0missing",
    b"M\0good\0X\0bad\0",
    b"M\0good\0\0bad\0",
    b"M\0good\0D\0",
]


@pytest.mark.parametrize("stream", MALFORMED)
def test_parser_rejects_entire_malformed_stream(stream):
    parser = _parser()
    with pytest.raises(ValueError):
        parser(stream)


def test_real_git_diff_boundary_is_binary_and_preserves_cr(repo: Path, monkeypatch):
    path = repo / "name\rCR.txt"
    path.write_text("original\n")
    _commit(repo)
    path.write_text("edited\n")
    oid = _git(repo, "rev-parse", "HEAD")
    original = verify._run_git
    calls = []

    def run(args, *, cwd, **kwargs):
        result = original(args, cwd=cwd, **kwargs)
        calls.append((args, cwd, kwargs, result.stdout))
        return result

    monkeypatch.setattr(verify, "_run_git", run)
    assert verify._git_diff_name_status(repo, oid, path) == [("M", "name\rCR.txt")]
    argv = ["diff", "--no-renames", "--name-status", "-z", oid, "--", str(path)]
    assert calls == [(argv, repo, {"text": False}, b"M\0name\rCR.txt\0")]
    # Existing omitted-text callers must still receive decoded text.
    decoded = original(argv, cwd=repo)
    assert decoded.stdout == "M\0name\nCR.txt\0"


@pytest.mark.parametrize(
    "stdout,returncode",
    [
        (b"M\0good\0D\0missing", 0),
        (b"M\0good\0X\0bad\0", 0),
        (b"", 7),
    ],
)
@pytest.mark.parametrize("main", [None, verify_main, doctor_main], ids=["core", "verify", "doctor"])
def test_malformed_or_failed_diff_is_operational_before_show(
    repo: Path,
    monkeypatch,
    stdout,
    returncode: int,
    main,
    capsys,
):
    store = repo / ".sidegraph"
    path = _hot(store)
    _commit(repo)
    _edit(path)
    original = verify._run_git
    commands = []

    def run(args, *, cwd, **kwargs):
        commands.append(args[0])
        if args[0] == "diff":
            return subprocess.CompletedProcess(args, returncode, stdout, b"diff failed\n")
        return original(args, cwd=cwd, **kwargs)

    monkeypatch.setattr(verify, "_run_git", run)
    before = _canonical_bytes(store)
    if main is None:
        with pytest.raises(ValueError):
            verify.verify_against(store, "HEAD")
    else:
        assert main(["--db", str(store), "--against", "HEAD", "--json"]) == 1
        output = capsys.readouterr()
        assert "failed" in output.out
        assert not output.err
    assert "diff" in commands and "show" not in commands
    assert _canonical_bytes(store) == before
