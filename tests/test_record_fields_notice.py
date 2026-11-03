"""``record-fields-requirement``: a doctor-only advisory when the store's requirement is below
``RECORD_FIELDS_REQUIREMENT``. Silent while the constant is at its baseline, so no existing store
is told anything for no reason.
see docs/reference/stability.md ("Mixed versions")
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

import sidegraph
from sidegraph import integrity
from sidegraph.cli import doctor_main
from sidegraph.store import Store
from sidegraph.store_format import FORMAT2_MIN_SIDEGRAPH, marker_text
from tests.store_format2_support import decision_body, make_store, put_file, put_shard_line
from tests.test_doctor_integrity import _settled_healthy as settled_healthy

CODE = "record-fields-requirement"


def problems(store: Path, surface: str = "doctor") -> dict[str, integrity.Problem]:
    result = integrity.run(
        integrity.Inputs(store_dir=store, now=datetime.now(UTC)),  # type: ignore[arg-type]
        surface,  # type: ignore[arg-type]
    )
    return {p.check: p for p in result.problems}


def ids_of(store: Path, surface: str = "doctor") -> set[str]:
    return set(problems(store, surface))


def test_a_fresh_store_of_this_release_gets_no_finding(tmp_path):
    store = Store(tmp_path / ".sidegraph")
    store.close()
    assert CODE not in ids_of(store.path, "doctor")


@pytest.fixture
def baseline(monkeypatch):
    monkeypatch.setattr(integrity, "RECORD_FIELDS_REQUIREMENT", FORMAT2_MIN_SIDEGRAPH)


def test_an_existing_format2_store_gets_no_finding_at_the_baseline(tmp_path, baseline):
    store = make_store(tmp_path)
    put_file(store, "decision", decision_body("D1"))
    assert CODE not in ids_of(store, "doctor")


@pytest.fixture
def raised(monkeypatch):
    monkeypatch.setattr(integrity, "RECORD_FIELDS_REQUIREMENT", "0.99.0")
    monkeypatch.setattr(sidegraph, "__version__", "0.99.0")


def test_a_constant_above_the_running_release_is_silent(tmp_path, monkeypatch):
    """A development build of the release that raises the constant: `sidegraph-require` would
    refuse the version the advice names, so there is no advice to give yet."""
    monkeypatch.setattr(integrity, "RECORD_FIELDS_REQUIREMENT", "0.99.0")
    monkeypatch.setattr(sidegraph, "__version__", "0.98.0")
    store = make_store(tmp_path)
    put_file(store, "decision", decision_body("D1"))
    assert CODE not in ids_of(store)


def test_a_store_below_a_raised_constant_is_named_with_the_command(tmp_path, raised):
    store = make_store(tmp_path)
    put_file(store, "decision", decision_body("D1"))
    found = problems(store)[CODE]
    assert found.severity == "advisory"
    assert "sidegraph-require --sidegraph 0.99.0" in found.line
    assert CODE not in ids_of(store, "session")


def test_the_finding_clears_once_the_requirement_is_raised(tmp_path, raised):
    store = make_store(tmp_path)
    put_file(store, "decision", decision_body("D1"))
    (store / "format").write_text(marker_text(2, "0.99.0", 64), encoding="utf-8")
    assert CODE not in ids_of(store, "doctor")


def test_an_empty_store_gets_no_finding(tmp_path, raised):
    assert CODE not in ids_of(make_store(tmp_path), "doctor")


def test_a_store_without_a_requirement_is_not_nagged_at_the_baseline(tmp_path, baseline):
    store = make_store(tmp_path, fmt=1)  # requires 0.16.0, below the baseline
    put_file(store, "decision", decision_body("D1"))
    assert CODE not in ids_of(store)


def test_a_packed_only_store_gets_the_finding(tmp_path, raised):
    store = make_store(tmp_path)
    put_shard_line(store, "decision", "D1", decision_body("D1"))
    assert not list((store / "decisions").glob("*.json"))
    assert CODE in ids_of(store)


def test_a_store_with_only_empty_shard_files_is_silent(tmp_path, raised):
    store = make_store(tmp_path)
    (store / "shards").mkdir()
    (store / "shards" / "00.shard").write_bytes(b"")
    assert CODE not in ids_of(store)


def test_check_exits_2_only_once_the_constant_is_raised(tmp_path, monkeypatch, capsys, baseline):
    """An otherwise healthy store: the new finding is the only reason for the exit code."""
    db = settled_healthy(tmp_path)
    capsys.readouterr()
    assert doctor_main(["--db", str(db), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["findings"] == []
    assert doctor_main(["--db", str(db), "--check"]) == 0
    monkeypatch.setattr(integrity, "RECORD_FIELDS_REQUIREMENT", "0.99.0")
    monkeypatch.setattr(sidegraph, "__version__", "0.99.0")
    capsys.readouterr()
    doctor_main(["--db", str(db), "--json"])
    codes = [f["code"] for f in json.loads(capsys.readouterr().out)["findings"]]
    assert codes == [CODE]
    assert doctor_main(["--db", str(db), "--check"]) == 2
