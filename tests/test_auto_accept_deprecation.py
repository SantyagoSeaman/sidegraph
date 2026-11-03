"""``SIDEGRAPH_AUTO_ACCEPT`` is deprecated in favour of ``SIDEGRAPH_RATIFY_POLICY=auto-all``:
SessionStart and ``sidegraph-doctor`` say so while it is on, and the fold into
``RatifyPolicy`` keeps the policy variable from ever selecting the legacy value.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from sidegraph import integrity
from sidegraph.capture import RatifyPolicy, parse_ratify_policy
from sidegraph.cli import doctor_main
from sidegraph.integrity import Inputs, run
from sidegraph.schema import DecisionStatus
from sidegraph.store import Store
from tests.test_host_integrity import context, start

CHECK = "deprecated-auto-accept"


def problems(store: Path, surface: str = "session") -> dict[str, integrity.Problem]:
    result = run(Inputs(store_dir=store, now=datetime.now(UTC)), surface)  # type: ignore[arg-type]
    return {p.check: p for p in result.problems}


@pytest.fixture
def store_dir(tmp_path: Path) -> Path:
    Store(tmp_path / "s").close()
    return tmp_path / "s"


@pytest.mark.parametrize("surface", ["session", "doctor"])
def test_warns_while_auto_accept_is_on(store_dir, monkeypatch, surface):
    monkeypatch.setenv("SIDEGRAPH_AUTO_ACCEPT", "on")
    problem = problems(store_dir, surface)[CHECK]
    assert problem.line is not None
    assert "SIDEGRAPH_RATIFY_POLICY=auto-all" in problem.line
    assert "live anchor" in problem.line
    assert problem.notice == problem.line
    assert problem.findings and problem.findings[0][1] == problem.line


@pytest.mark.parametrize("value", [None, "off", "0", "1", "true"])
def test_silent_unless_exactly_on(store_dir, monkeypatch, value):
    if value is not None:
        monkeypatch.setenv("SIDEGRAPH_AUTO_ACCEPT", value)
    assert CHECK not in problems(store_dir)


def test_session_start_and_doctor_report_it(store_dir, monkeypatch, capsys):
    monkeypatch.setenv("SIDEGRAPH_AUTO_ACCEPT", "on")
    out = start(monkeypatch, capsys, store_dir)
    assert "SIDEGRAPH_RATIFY_POLICY=auto-all" in context(out)
    doctor_main(["--db", str(store_dir), "--json"])
    codes = [f["code"] for f in json.loads(capsys.readouterr().out)["findings"]]
    assert CHECK in codes


def test_policy_variable_cannot_select_the_legacy_value():
    assert parse_ratify_policy("legacy-accept") == RatifyPolicy.MANUAL
    assert parse_ratify_policy(" auto-all ") == RatifyPolicy.AUTO_ALL


def test_only_legacy_accept_lands_accepted():
    assert RatifyPolicy.LEGACY_ACCEPT.landing_status() == DecisionStatus.ACCEPTED
    for policy in (RatifyPolicy.MANUAL, RatifyPolicy.AUTO_LOW_RISK, RatifyPolicy.AUTO_ALL):
        assert policy.landing_status() == DecisionStatus.PROPOSED
    assert [p for p in RatifyPolicy if p.auto_ratifies] == [
        RatifyPolicy.AUTO_LOW_RISK,
        RatifyPolicy.AUTO_ALL,
    ]


def test_doctor_check_does_not_fail_on_the_deprecation_finding(store_dir, monkeypatch, capsys):
    monkeypatch.setenv("SIDEGRAPH_AUTO_ACCEPT", "on")
    assert doctor_main(["--db", str(store_dir), "--check", "--json"]) == 0
    codes = [f["code"] for f in json.loads(capsys.readouterr().out)["findings"]]
    assert codes == [CHECK]


def _propose(store_dir, monkeypatch, **env):
    from sidegraph import server

    monkeypatch.setattr(server, "_store", Store(store_dir.parent / "p"))
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    draft = {"title": "t", "kind": "gotcha", "context": "c", "choice": "x", "anchors": []}
    return server.propose_decisions(drafts=[draft])


def test_propose_result_notes_the_deprecation_under_auto_accept(store_dir, monkeypatch):
    [result] = _propose(store_dir, monkeypatch, SIDEGRAPH_AUTO_ACCEPT="on")
    assert result["status"] == "written"
    assert "SIDEGRAPH_RATIFY_POLICY=auto-all" in result["deprecation"]


@pytest.mark.parametrize("env", [{}, {"SIDEGRAPH_RATIFY_POLICY": "auto-all"}])
def test_propose_result_has_no_note_otherwise(store_dir, monkeypatch, env):
    [result] = _propose(store_dir, monkeypatch, **env)
    assert "deprecation" not in result


def test_doctor_help_names_both_exempt_codes(capsys):
    with pytest.raises(SystemExit):
        doctor_main(["--help"])
    text = " ".join(capsys.readouterr().out.split())
    assert "maintenance-due" in text and "deprecated-auto-accept" in text
