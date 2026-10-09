"""A hand-edited record holding an unhashable value (a list or dict) where doctor expects a
string status or entity id must not crash the report: doctor skips the value and verify's
model check reports the malformed file (parse-error). Stores are seeded through Store, then
one file is hand-edited; git-backed drift tests follow ``test_sync_drift_cache.py``."""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from sidegraph.cli import doctor_main
from sidegraph.doctor import (
    CODE_DRIFT,
    EXPIRED_OPEN_VALIDITY,
    UNREFERENCED_ENTITY,
    _check_expired_open,
    _check_never_surfaced,
    curate,
)
from sidegraph.schema import (
    AnchorBinding,
    Decision,
    DecisionKind,
    DecisionStatus,
    Descriptor,
    Entity,
    Provenance,
)
from sidegraph.store import Store
from sidegraph.sync import refresh_code_drift_cache

BAD_VALUES = [["accepted"], {"a": 1}]


def _decision(store: Store, title: str, commit: str | None = None) -> Decision:
    return store.add_decision(
        Decision(
            title=title,
            kind=DecisionKind.GOTCHA,
            status=DecisionStatus.ACCEPTED,
            context="c",
            choice="ch",
            valid_from=datetime.now(UTC),
            provenance=Provenance(source="agent", commit=commit),
        )
    )


def _seed(tmp_path: Path) -> tuple[Path, Decision, Entity]:
    db = tmp_path / "store"
    s = Store(db)
    e = s.upsert_entity(
        Entity(canonical_name="alpha", descriptor=Descriptor(name="alpha", file_path="a.py"))
    )
    d = _decision(s, "an adr")
    s.add_binding(AnchorBinding(record_id=d.id, entity_id=e.entity_id, tier=2))
    s.close()
    return db, d, e


def _edit(path: Path, **fields: object) -> None:
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload.update(fields)
    path.write_text(json.dumps(payload), encoding="utf-8")


@pytest.mark.parametrize("bad", BAD_VALUES)
def test_doctor_reports_instead_of_crashing_on_an_unhashable_decision_status(tmp_path, capsys, bad):
    db, d, _e = _seed(tmp_path)
    decision_file = db / "decisions" / f"{d.id}.json"
    _edit(decision_file, status=bad)
    assert doctor_main(["--db", str(db)]) == 2  # the unfixed pass raised TypeError instead
    assert f"parse-error  {decision_file}" in capsys.readouterr().out
    assert doctor_main(["--db", str(db), "--json"]) == 2
    violations = json.loads(capsys.readouterr().out)["violations"]
    assert ("parse-error", str(decision_file)) in [(v["code"], v["path"]) for v in violations]


def test_doctor_reports_instead_of_crashing_on_an_unhashable_binding_entity_id(tmp_path, capsys):
    db, d, e = _seed(tmp_path)
    bindings = db / "bindings" / f"{d.id}.json"
    entries = json.loads(bindings.read_text(encoding="utf-8"))
    entries.append({"entity_id": ["x"], "tier": 2, "weight": 1.0})
    bindings.write_text(json.dumps(entries), encoding="utf-8")
    report = curate(db)  # the unfixed pass raised TypeError here
    assert not [f for f in report.findings if f.code == UNREFERENCED_ENTITY]
    capsys.readouterr()
    assert doctor_main(["--db", str(db)]) == 2
    out = capsys.readouterr().out
    assert f"parse-error  {bindings}" in out
    assert "Traceback" not in out


@pytest.mark.parametrize("bad", BAD_VALUES)
def test_expired_open_skips_an_unhashable_status(tmp_path, bad):
    path = tmp_path / "d.json"
    ok = (
        tmp_path / "ok.json",
        {"id": "A", "status": "accepted", "valid_to": "2020-01-01T00:00:00Z"},
    )
    findings = _check_expired_open(
        [(path, {"id": "B", "status": bad, "valid_to": "2020-01-01T00:00:00Z"}), ok],
        datetime.now(UTC),
    )
    assert [f.code for f in findings] == [EXPIRED_OPEN_VALIDITY]
    assert findings[0].path == str(ok[0])


def test_never_surfaced_skips_an_unhashable_binding_entity_id(tmp_path):
    db, d, _e = _seed(tmp_path)
    s = Store(db)
    for _ in range(3):
        s.record_retrieval([], ["a.py"])
    s.close()
    bindings = db / "bindings" / f"{d.id}.json"
    entries = json.loads(bindings.read_text(encoding="utf-8"))
    entries.append({"entity_id": ["x"], "tier": 2, "weight": 1.0})
    bindings.write_text(json.dumps(entries), encoding="utf-8")
    findings, _skipped = _check_never_surfaced(db)
    assert [f.path for f in findings] == [str(db / "decisions" / f"{d.id}.json")]


def _git(args: list[str], cwd: Path) -> None:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    assert result.returncode == 0, f"git {args} failed: {result.stderr}"


def _commit_file(repo: Path, rel: str, content: str) -> str:
    (repo / rel).write_text(content)
    _git(["add", "-A"], cwd=repo)
    _git(["commit", "-q", "-m", "c"], cwd=repo)
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.mark.parametrize("bad", BAD_VALUES)
def test_code_drift_survives_an_unhashable_status_on_another_record(tmp_path, bad):
    """The hook callers swallow the scan's error, so one list status used to switch the
    drift line off for every record. The malformed one is skipped; the rest still drift."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-q"], cwd=repo)
    _git(["config", "user.email", "t@example.com"], cwd=repo)
    _git(["config", "user.name", "T"], cwd=repo)
    commit = _commit_file(repo, "a.py", "a\n")
    store = Store(repo / ".sidegraph")
    good = _decision(store, "good", commit)
    broken = _decision(store, "broken", commit)
    e = store.upsert_entity(
        Entity(canonical_name="A", descriptor=Descriptor(name="A", file_path="a.py"))
    )
    for d in (good, broken):
        store.add_binding(AnchorBinding(record_id=d.id, entity_id=e.entity_id, tier=2))
    _commit_file(repo, "a.py", "changed\n")
    _edit(repo / ".sidegraph" / "decisions" / f"{broken.id}.json", status=bad)

    report = curate(repo / ".sidegraph", repo_root=repo)
    drift = [f for f in report.findings if f.code == CODE_DRIFT]
    assert [f.path for f in drift] == [str(repo / ".sidegraph" / "decisions" / f"{good.id}.json")]
    assert refresh_code_drift_cache(store, repo_root=repo) == 1
