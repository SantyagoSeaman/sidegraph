"""Shape of the shared CI workflow: cheap checks on every PR event,
the full matrix only off Draft, one aggregate gate.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[1]
_CI = _ROOT / ".github/workflows/ci.yml"
FULL_IF = "${{ github.event_name != 'pull_request' || !github.event.pull_request.draft }}"
FULL_CI_NAME = (
    "${{ (github.event_name != 'pull_request' || !github.event.pull_request.draft) "
    "&& 'full-ci' || 'full-ci (draft: full matrix not run)' }}"
)
CONCURRENCY_GROUP = (
    "ci-${{ github.event_name == 'pull_request' "
    "&& format('pr-{0}', github.event.pull_request.number) || format('run-{0}', github.run_id) }}"
)
CONCURRENCY_CANCEL = "${{ github.event_name == 'pull_request' }}"


def _load() -> dict:
    return yaml.safe_load(_CI.read_text(encoding="utf-8"))


def _on(wf: dict) -> dict:
    return wf.get("on", wf.get(True))


def test_pull_request_types_include_ready_for_review() -> None:
    events = _on(_load())
    assert events["pull_request"]["types"] == [
        "opened",
        "synchronize",
        "reopened",
        "ready_for_review",
    ]
    assert events["push"] == {"branches": ["main"]}
    assert "workflow_call" in events


def test_full_tier_jobs_share_the_draft_condition() -> None:
    jobs = _load()["jobs"]
    for name in ("test", "sdk-compatibility", "windows-core"):
        assert jobs[name]["if"] == FULL_IF
    assert "if" not in jobs["lint"]
    for name in ("store-lint", "anchor-health"):
        assert jobs[name]["if"] == "github.event_name == 'pull_request'"


def test_full_ci_needs_every_other_job() -> None:
    jobs = _load()["jobs"]
    gate = jobs["full-ci"]
    assert set(gate["needs"]) == set(jobs) - {"full-ci"}
    assert gate["if"] == "${{ always() }}"
    assert gate["name"] == FULL_CI_NAME
    assert len(gate["steps"]) == 1
    step = gate["steps"][0]
    assert "uses" not in step
    assert "${{" not in step["run"]


def test_full_ci_gate_env_matches_dependency_results() -> None:
    """A wrong binding must not let a successful job stand in for a failed one."""
    env = _load()["jobs"]["full-ci"]["steps"][0]["env"]
    assert env == {
        "FULL": FULL_IF,
        "EVENT": "${{ github.event_name }}",
        "LINT": "${{ needs.lint.result }}",
        "TEST": "${{ needs.test.result }}",
        "SDK": "${{ needs.sdk-compatibility.result }}",
        "WINDOWS": "${{ needs.windows-core.result }}",
        "STORE_LINT": "${{ needs.store-lint.result }}",
        "ANCHOR_HEALTH": "${{ needs.anchor-health.result }}",
    }


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash is unavailable")
@pytest.mark.parametrize(
    ("full", "event", "changes", "ok"),
    [
        ("true", "pull_request", {}, True),
        ("true", "pull_request", {"TEST": "skipped"}, False),
        ("true", "pull_request", {"WINDOWS": "cancelled"}, False),
        ("true", "pull_request", {"SDK": "failure"}, False),
        ("true", "pull_request", {"STORE_LINT": "skipped"}, False),
        ("true", "push", {"STORE_LINT": "skipped", "ANCHOR_HEALTH": "skipped"}, True),
        ("true", "push", {"LINT": "failure"}, False),
        ("true", "pull_request", {"TEST": ""}, False),
        (
            "false",
            "pull_request",
            {"TEST": "skipped", "SDK": "skipped", "WINDOWS": "skipped"},
            True,
        ),
        ("false", "pull_request", {"LINT": "failure"}, False),
    ],
    ids=[
        "all-success",
        "TEST=skipped",
        "WINDOWS=cancelled",
        "SDK=failure",
        "STORE_LINT=skipped-pr",
        "pr-only-skipped-push",
        "LINT=failure",
        "TEST=empty",
        "draft-success",
        "draft-lint-failure",
    ],
)
def test_full_ci_gate_script(full: str, event: str, changes: dict, ok: bool) -> None:
    step = _load()["jobs"]["full-ci"]["steps"][0]
    case_env = {
        "FULL": full,
        "EVENT": event,
        "LINT": "success",
        "TEST": "success",
        "SDK": "success",
        "WINDOWS": "success",
        "STORE_LINT": "success",
        "ANCHOR_HEALTH": "success",
        **changes,
    }
    assert set(step["env"]) == set(case_env)
    minimal_env = {key: os.environ[key] for key in ("PATH", "SYSTEMROOT") if key in os.environ}
    result = subprocess.run(
        ["bash", "-c", step["run"]],
        env={**minimal_env, **case_env},
        capture_output=True,
        text=True,
        check=False,
    )
    assert (result.returncode == 0) is ok, result.stdout + result.stderr
    if full == "false" and ok:
        assert "::notice::" in result.stdout


def test_ci_concurrency_is_pr_scoped() -> None:
    wf = _load()
    assert wf["concurrency"] == {
        "group": CONCURRENCY_GROUP,
        "cancel-in-progress": CONCURRENCY_CANCEL,
    }
    assert all("concurrency" not in job for job in wf["jobs"].values())


def test_test_jobs_report_durations() -> None:
    jobs = _load()["jobs"]
    for job, name in (
        ("test", "Run tests"),
        ("windows-core", "Test portable core and MCP contracts"),
    ):
        step = next(step for step in jobs[job]["steps"] if step.get("name") == name)
        assert "--durations=30 --durations-min=1" in step["run"]


def test_slim_jobs_fit_the_slim_time_cap() -> None:
    """Red against nothing: guards the slim cap."""
    for path in (_CI, _ROOT / ".github/workflows/session-link-gate.yml"):
        wf = yaml.safe_load(path.read_text(encoding="utf-8"))
        for job in wf["jobs"].values():
            if job["runs-on"] == "ubuntu-slim":
                assert job["timeout-minutes"] <= 15
