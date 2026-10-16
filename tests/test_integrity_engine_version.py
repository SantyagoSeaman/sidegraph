"""The engine-version advisory: a graph built by a Graphify release this Sidegraph was not tested
with. # see design/superpowers/specs/2026-10-14-graphify-upgrade-design.md (D1)"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from sidegraph import integrity
from sidegraph.cli import doctor_main
from sidegraph.doctor import ENGINE_VERSION, _registry_findings
from sidegraph.integrity import CHECKS, Inputs, _engine_untested_sentence
from sidegraph.store import Store
from tests.graph_fixtures import reader_of
from tests.test_host_integrity import context, start
from tests.test_store_requirement import running_release, write_requirement_marker

NOW = datetime.now(UTC)
TEXT = (
    "the code graph was built by Graphify 0.9.95, a release this Sidegraph was not tested with, "
    "so anchors and domains may read differently; pin Graphify to a tested release, listed under "
    '"Engine version pinning" in Sidegraph\'s Graphify integration guide'
)


def detector():
    return next(c for c in CHECKS if c.id == "engine-version")


def test_registered_after_graph_missing_for_session_and_doctor():
    ids = [c.id for c in CHECKS]
    assert ids.index("engine-version") == ids.index("graph-missing") + 1
    assert detector().surfaces == frozenset({"session", "doctor"})


def test_the_sentence_is_one_definition():
    assert _engine_untested_sentence("0.9.95") == TEXT


def test_an_untested_release_gives_a_line_and_a_doctor_finding(tmp_path):
    write_requirement_marker(tmp_path, running_release())
    reader = reader_of(tmp_path, graph={"graphify_version": "0.9.95"})
    problem = detector().detect(Inputs(store_dir=tmp_path, now=NOW, reader=reader))
    assert (problem.severity, problem.notice) == ("advisory", None)
    assert problem.line == f"Sidegraph: {TEXT}."
    assert problem.findings == ((str(reader.path), f"{TEXT}."),)
    findings = _registry_findings(tmp_path, NOW, reader, None)
    assert [(f.code, f.detail) for f in findings if f.code == ENGINE_VERSION] == [
        (ENGINE_VERSION, f"{TEXT}.")
    ]


def test_a_tested_or_unstamped_graph_is_clean(tmp_path):
    write_requirement_marker(tmp_path, running_release())
    for sub, graph in (("t", {"graphify_version": "0.9.90"}), ("u", {})):
        (tmp_path / sub).mkdir()
        reader = reader_of(tmp_path / sub, graph=graph)
        assert detector().detect(Inputs(store_dir=tmp_path, now=NOW, reader=reader)) is None


def test_no_reader_means_not_run(tmp_path):
    result = integrity.run(Inputs(store_dir=tmp_path, now=NOW), "session", (detector(),))
    assert result.problems == [] and "engine-version" not in result.clean


def test_an_unparseable_multiline_stamp_stays_one_advisory_line(tmp_path):
    reader = reader_of(tmp_path, graph={"graphify_version": "next\nbuild"})
    problem = detector().detect(Inputs(store_dir=tmp_path, now=NOW, reader=reader))
    assert problem is not None
    expected = TEXT.replace("0.9.95", "next build")
    assert problem.line == f"Sidegraph: {expected}."
    assert problem.findings == ((str(reader.path), f"{expected}."),)


def test_sessionstart_renders_the_engine_advisory_without_a_notice(tmp_path, monkeypatch, capsys):
    store_dir = tmp_path / "store"
    Store(store_dir).close()
    write_requirement_marker(store_dir, running_release())
    reader = reader_of(tmp_path, graph={"graphify_version": "0.9.95"})
    out = start(monkeypatch, capsys, store_dir, graph=reader.path, project=tmp_path)
    assert f"Sidegraph: {TEXT}." in context(out).splitlines()
    assert "Graphify 0.9.95" not in out.get("systemMessage", "")


@pytest.mark.parametrize("as_json", [False, True], ids=["text", "json"])
def test_doctor_renders_the_engine_advisory_and_check_escalates_it(tmp_path, capsys, as_json):
    store_dir = tmp_path / "store"
    Store(store_dir).close()
    write_requirement_marker(store_dir, running_release())
    reader = reader_of(tmp_path, graph={"graphify_version": "0.9.95"})
    args = ["--db", str(store_dir), "--graph", str(reader.path)]
    if as_json:
        args.append("--json")
    for check, expected_code in ((False, 0), (True, 2)):
        assert doctor_main(args + (["--check"] if check else [])) == expected_code
        out = capsys.readouterr().out
        if as_json:
            findings = [f for f in json.loads(out)["findings"] if f["code"] == ENGINE_VERSION]
            assert [(f["path"], f["detail"]) for f in findings] == [(str(reader.path), f"{TEXT}.")]
        else:
            assert f"engine-version  {reader.path}  {TEXT}." in out.splitlines()


BEHIND = (
    "the graph comes from a Graphify release that adds doc-to-code mention edges (0.9.62 or "
    "later) and this store has not been migrated (no minimum Sidegraph version); follow the "
    "migration guide for 0.14: "
    "https://github.com/SantyagoSeaman/sidegraph/blob/main/docs/guides/migrating-to-0.14.md"
)


def _store(tmp_path, marker: str | None) -> Path:
    store_dir = tmp_path / ".sidegraph"
    store_dir.mkdir()
    if marker is not None:
        (store_dir / "format").write_text(marker, encoding="utf-8")
    return store_dir


def test_a_new_engine_graph_over_an_unmigrated_store_points_to_the_guide(tmp_path):
    """Red against unfixed code: the case-1-only detector returns None for a tested 0.9.90."""
    store_dir = _store(tmp_path, "sidegraph-store 0.6.0\n")
    reader = reader_of(tmp_path, graph={"graphify_version": "0.9.90"})
    problem = detector().detect(Inputs(store_dir=store_dir, now=NOW, reader=reader))
    assert problem is not None
    assert (problem.line, problem.notice) == (f"Sidegraph: {BEHIND}.", None)
    assert (problem.summary, problem.fix) == (
        "store not migrated for the code graph's Graphify release",
        "follow the migration guide for 0.14",
    )
    findings = _registry_findings(store_dir, NOW, reader, None)
    assert [(f.code, f.detail) for f in findings if f.code == ENGINE_VERSION] == [
        (ENGINE_VERSION, f"{BEHIND}.")
    ]


def test_a_migrated_store_is_clean(tmp_path):
    """Guard: red against a check on the engine alone (discarded)."""
    store_dir = _store(tmp_path, None)
    write_requirement_marker(store_dir, running_release())
    reader = reader_of(tmp_path, graph={"graphify_version": "0.9.90"})
    assert detector().detect(Inputs(store_dir=store_dir, now=NOW, reader=reader)) is None


def test_a_legacy_graph_over_an_unmigrated_store_is_clean(tmp_path):
    """Guard (identity on a 0.9.6 graph): red against a check on the store alone (discarded)."""
    store_dir = _store(tmp_path, "sidegraph-store 0.6.0\n")
    reader = reader_of(tmp_path, graph={})
    assert detector().detect(Inputs(store_dir=store_dir, now=NOW, reader=reader)) is None


@pytest.mark.parametrize(
    "marker",
    ["sidegraph-store 1.0.0\n", "sidegraph-store 0.6.0\nrequires-sidegraph >= 0.1.0\n"],
    ids=["no-requirement-line", "0x-with-a-line"],
)
def test_a_malformed_marker_keeps_case_two_silent_and_case_one_speaking(tmp_path, marker):
    """Green on unfixed code. Red against letting StoreRequirementError out of the detector
    (discarded: integrity.run drops the whole check, case 1 included). Red against reading a
    malformed marker as "no requirement" (discarded: it would send the reader to the migration
    guide for a marker every entry point refuses)."""
    store_dir = _store(tmp_path, marker)
    tested = reader_of(tmp_path, graph={"graphify_version": "0.9.90"})
    assert detector().detect(Inputs(store_dir=store_dir, now=NOW, reader=tested)) is None
    (tmp_path / "u").mkdir()
    untested = reader_of(tmp_path / "u", graph={"graphify_version": "0.9.95"})
    problem = detector().detect(Inputs(store_dir=store_dir, now=NOW, reader=untested))
    assert problem is not None
    assert problem.line == f"Sidegraph: {TEXT}."


def test_an_untested_mention_graph_reports_both_advisory_cases(tmp_path):
    store_dir = _store(tmp_path, "sidegraph-store 0.6.0\n")
    reader = reader_of(tmp_path, graph={"graphify_version": "0.9.95"})
    problem = detector().detect(Inputs(store_dir=store_dir, now=NOW, reader=reader))
    assert problem is not None
    assert problem.line == f"Sidegraph: {TEXT}; {BEHIND}."
