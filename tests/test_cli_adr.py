"""The ADR command previews by default and requires explicit writes."""

import json
from pathlib import Path

import pytest

from tests.test_adr import bytes_under, seed


def test_default_cli_preview_is_the_written_diff_and_never_opens_store(tmp_path, capsys):
    from sidegraph.adr_cli import main

    db = seed(tmp_path)
    graph = tmp_path / "graph.json"
    graph.write_bytes(b'{"untouched": true}\n')
    out = tmp_path / "docs"
    before = bytes_under(db)
    args = ["--db", str(db), "--out", str(out), "--json"]
    assert main(args) == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview["mode"] == "preview" and preview["summary"]["add"] == 3
    assert not out.exists()
    expected = next(c for c in preview["changes"] if c["record_id"] == "ADR")["diff"]
    assert "+Choice for ADR." in expected
    assert main([*args, "--write"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["mode"] == "write"
    assert next(c for c in result["changes"] if c["record_id"] == "ADR")["diff"] == expected
    assert main(args) == 0
    assert json.loads(capsys.readouterr().out)["summary"]["unchanged"] == 3
    assert bytes_under(db) == before and graph.read_bytes() == b'{"untouched": true}\n'


def test_missing_store_is_error_but_empty_admitted_store_is_success(tmp_path, capsys):
    from sidegraph.adr_cli import main
    from sidegraph.store import Store

    missing = tmp_path / "missing"
    out = tmp_path / "docs"
    assert main(["--db", str(missing), "--out", str(out)]) == 2
    assert not missing.exists() and not out.exists()
    with Store(tmp_path / ".sidegraph"):
        pass
    capsys.readouterr()
    assert main(["--db", str(tmp_path / ".sidegraph"), "--out", str(out), "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["summary"]["add"] == 1
    assert "No eligible ADR" in report["changes"][0]["diff"]
    assert not out.exists()


@pytest.mark.parametrize(
    "options",
    [["--profile", "unknown"], ["--filename", "../{id}.md"], ["--filename", "{id.__class__}.md"]],
)
def test_bad_configuration_is_rejected_before_output(tmp_path, capsys, options):
    from sidegraph.adr_cli import main

    db = seed(tmp_path)
    before = bytes_under(db)
    assert main(["--db", str(db), "--out", str(tmp_path / "docs"), *options]) == 2
    assert not (tmp_path / "docs").exists() and bytes_under(db) == before
    assert capsys.readouterr().err


def test_madr_and_custom_template_keep_full_fixed_evidence_footer(tmp_path, capsys):
    from sidegraph.adr_cli import main

    db = seed(tmp_path)
    out = tmp_path / "docs"
    assert main(["--db", str(db), "--out", str(out), "--profile", "madr", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    text = next(c["diff"] for c in report["changes"] if c["record_id"] == "ADR")
    assert "## Context and Problem Statement" in text
    template = tmp_path / "custom.txt"
    template.write_text("$title\n$context\n$choice\n$rejected\n$consequences\n", encoding="utf-8")
    assert main(["--db", str(db), "--out", str(out), "--template", str(template), "--json"]) == 0
    text = next(
        c["diff"] for c in json.loads(capsys.readouterr().out)["changes"] if c["record_id"] == "ADR"
    )
    assert "only12 observations" in text and "- `FACT`: " in text
    assert "status: accepted" in text


def test_conflict_returns_reviewable_json_and_applies_nothing(tmp_path, capsys):
    from sidegraph.adr_cli import main

    db = seed(tmp_path)
    out = tmp_path / "docs"
    out.mkdir()
    (out / "ADR.md").write_text("Human file.\n")
    before = bytes_under(out)
    assert main(["--db", str(db), "--out", str(out), "--write", "--json"]) == 2
    report = json.loads(capsys.readouterr().out)
    assert report["summary"]["conflict"] >= 1
    assert bytes_under(out) == before


def test_wal_index_export_creates_no_auxiliary_files(tmp_path, capsys, monkeypatch):
    import sqlite3

    from sidegraph.adr_cli import main

    db = seed(tmp_path)
    with sqlite3.connect(db / "index.db") as connection:
        connection.execute("PRAGMA journal_mode=WAL")
    connection.close()
    before = bytes_under(db)
    assert "index.db-wal" not in before and "index.db-shm" not in before

    def forbidden(*args, **kwargs):
        raise AssertionError("WAL index was opened during export")

    monkeypatch.setattr("sidegraph.adr_cli._binding_status_snapshot", forbidden)
    monkeypatch.setattr("sidegraph.adr_cli.scan_code_drift", forbidden)
    assert main(["--db", str(db), "--out", str(tmp_path / "docs"), "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert all(a["state"] == "unknown" for a in report["advisories"].values())
    assert bytes_under(db) == before


@pytest.mark.parametrize("kind", ["oversized", "invalid-utf8", "directory"])
def test_invalid_template_input_is_refused_without_writes(tmp_path, capsys, kind):
    from sidegraph.adr_cli import main

    db = seed(tmp_path)
    template = tmp_path / "template.txt"
    if kind == "oversized":
        template.write_bytes(b"x" * 65_537)
    elif kind == "invalid-utf8":
        template.write_bytes(b"\xff")
    else:
        template.mkdir()
    before = bytes_under(db)
    assert (
        main(["--db", str(db), "--out", str(tmp_path / "docs"), "--template", str(template)]) == 2
    )
    assert not (tmp_path / "docs").exists() and bytes_under(db) == before


def test_directory_default_and_environment_store_are_resolved_without_creation(
    tmp_path, monkeypatch, capsys
):
    from sidegraph.adr_cli import main

    db = seed(tmp_path)
    monkeypatch.setenv("SIDEGRAPH_DIR", str(db))
    monkeypatch.chdir(tmp_path)
    assert main(["--json"]) == 0
    assert not (tmp_path / "docs").exists()
    assert json.loads(capsys.readouterr().out)["summary"]["add"] == 3


def test_public_cli_entrypoint_is_wired():
    import tomllib

    project = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text())
    assert project["project"]["scripts"]["sidegraph-export-adr"] == "sidegraph.adr_cli:main"


def test_capture_to_head_drift_is_report_only_with_real_git(tmp_path, monkeypatch, capsys):
    import subprocess

    from sidegraph.adr_cli import main
    from sidegraph.gitenv import git_env
    from sidegraph.schema import AnchorBinding, Decision, Provenance
    from sidegraph.store import Store
    from sidegraph.store_layout import VOLATILE_STALE_KEY
    from tests.test_adr import NOW

    db = seed(tmp_path)
    hooks = tmp_path / "disabled-hooks"
    hooks.mkdir()

    def git(*args):
        return subprocess.check_output(
            [
                "git",
                "-c",
                "user.name=ADR fixture",
                "-c",
                "user.email=fixture@example.invalid",
                "-c",
                "commit.gpgsign=false",
                "-c",
                f"core.hooksPath={hooks}",
                *args,
            ],
            cwd=tmp_path,
            env=git_env(),
            text=True,
        ).strip()

    git("init", "-q")
    source = tmp_path / "src/cache.py"
    source.parent.mkdir()
    source.write_text("class Cache: pass\n")
    git("add", "src/cache.py")
    git("commit", "-qm", "initial source")
    commit = git("rev-parse", "HEAD")
    with Store(db) as store:
        entity = store.find_entity("Cache", "src/cache.py")
        assert entity is not None
        store.add_decision(
            Decision(
                id="GIT",
                title="Scoped captured decision",
                kind="adr",
                status="accepted",
                context="Synthetic context.",
                choice="Keep scope.",
                valid_from=NOW,
                provenance=Provenance(source="manual", commit=commit),
            )
        )
        store.add_binding(AnchorBinding(record_id="GIT", entity_id=entity.entity_id, tier=2))
        store.set_meta(VOLATILE_STALE_KEY, "0")
    args = ["--db", str(db), "--out", str(tmp_path / "docs"), "--json"]
    before = bytes_under(db)
    assert main(args) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["advisories"]["GIT"]["state"] == "not-detected"
    source.write_text("class Cache: pass\n# changed after capture\n")
    git("add", "src/cache.py")
    git("commit", "-qm", "change source")
    assert main(args) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["advisories"]["GIT"]["state"] == "changed"
    assert second["advisories"]["GIT"]["paths"] == ["src/cache.py"]
    first_diff = next(c["diff"] for c in first["changes"] if c["record_id"] == "GIT")
    assert first_diff == next(c["diff"] for c in second["changes"] if c["record_id"] == "GIT")
    assert bytes_under(db) == before


def test_written_projection_roundtrip_does_not_create_or_change_memory(
    tmp_path, monkeypatch, capsys
):
    from sidegraph.adr_cli import main
    from sidegraph.doc_import import import_docs
    from sidegraph.engine.reader import GraphifyReader
    from sidegraph.store import Store

    db = seed(tmp_path)
    out = tmp_path / "docs/adr"
    assert main(["--db", str(db), "--out", str(out), "--write", "--json"]) == 0
    capsys.readouterr()
    monkeypatch.chdir(tmp_path)
    files = sorted(out.glob("*.md"))
    graph = tmp_path / "graph.json"
    graph.write_text(
        json.dumps(
            {
                "nodes": [
                    {
                        "id": f"doc-{i}",
                        "label": p.name,
                        "norm_label": p.name,
                        "source_file": p.relative_to(tmp_path).as_posix(),
                        "file_type": "document",
                        "community": 0,
                    }
                    for i, p in enumerate(files)
                ],
                "links": [],
            }
        ),
        encoding="utf-8",
    )
    reader = GraphifyReader(str(graph))
    with Store(db) as store:
        before = bytes_under(db)
        before_ids = {d.id for d in store.iter_decisions()}
        report = import_docs(store, reader, [str(p) for p in files], any_doc=True)
        assert report.imported == report.superseded == report.anchors_repaired == 0
        assert report.skipped_generated_adr == len(files)
        assert {d.id for d in store.iter_decisions()} == before_ids
        assert bytes_under(db) == before


def test_unreadable_advisory_index_does_not_prevent_export(tmp_path, capsys, monkeypatch):
    from sidegraph.adr_cli import main

    db = seed(tmp_path)
    before = bytes_under(db)
    original_open = Path.open

    def unreadable(path, *args, **kwargs):
        if path == db / "index.db":
            raise PermissionError("synthetic unreadable index")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", unreadable)
    assert main(["--db", str(db), "--out", str(tmp_path / "docs"), "--write", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["applied"] is True
    assert all(a["state"] == "unknown" for a in report["advisories"].values())
    assert any("unreadable" in warning for warning in report["warnings"])
    monkeypatch.undo()
    assert bytes_under(db) == before


def test_operational_failure_reports_partial_batch_and_preserves_remaining_files(
    tmp_path, capsys, monkeypatch
):
    import os

    from sidegraph.adr_cli import main

    db = seed(tmp_path)
    out = tmp_path / "docs"
    args = ["--db", str(db), "--out", str(out), "--write", "--json"]
    assert main(args) == 0
    capsys.readouterr()
    before = bytes_under(out)
    source_before = bytes_under(db)
    template = tmp_path / "custom.txt"
    template.write_text("Revised prose.\n$title\n$context\n$choice\n$rejected\n$consequences\n")
    original_replace = os.replace
    calls = []

    def fail_second(source, target):
        calls.append(target.name)
        if len(calls) == 2:
            raise OSError("synthetic second update failure")
        return original_replace(source, target)

    monkeypatch.setattr("sidegraph.adr_files.os.replace", fail_second)
    assert main([*args, "--template", str(template)]) == 2
    report = json.loads(capsys.readouterr().out)
    assert report["applied"] is False and report["partial_write_possible"] is True
    assert "second update failure" in report["error"]
    assert calls == ["ADR.md", "RULE.md"]
    assert (out / "ADR.md").read_bytes() != before["ADR.md"]
    assert (out / "RULE.md").read_bytes() == before["RULE.md"]
    assert (out / "index.md").read_bytes() == before["index.md"]
    assert not list(out.glob(".sidegraph-adr-*"))
    assert bytes_under(db) == source_before
