"""Shape of the shared CI workflow: cheap checks on every PR event,
the full matrix only off Draft, one aggregate gate.
"""

import os
import shutil
import subprocess
import sys
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
    assert events["push"] == {"branches": ["main", "release/**"]}
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


def test_sdk_compatibility_skips_a_pinned_package() -> None:
    steps = _load()["jobs"]["sdk-compatibility"]["steps"]
    # The checkout comes first: the skip step reads pyproject.toml from the work tree.
    assert steps[0]["uses"].startswith("actions/checkout@")
    skip = steps[1]
    assert skip["name"] == "Skip on a pinned package"
    assert "tomllib" in skip["run"]
    assert "pinned package: the SDK range is tested in the source repository" in skip["run"]
    gate = f"steps.{skip['id']}.outputs.pinned != 'true'"
    assert "if" not in skip
    for step in steps[2:]:
        assert step.get("if") == gate, step.get("name")


def _skip_step_script() -> str:
    """The Python the `Skip on a pinned package` step feeds to `python3 -` (its heredoc body)."""
    run = _load()["jobs"]["sdk-compatibility"]["steps"][1]["run"]
    start = run.index("<<'PY'\n") + len("<<'PY'\n")
    return run[start : run.rindex("\nPY")] + "\n"


def _run_skip_step(tmp_path: Path, dependencies: list[str]) -> tuple[str, str]:
    """Execute the step's script in a tree holding a pyproject.toml with these dependencies.

    Returns (what the step wrote to GITHUB_OUTPUT, what it printed).
    """
    quoted = ", ".join(f'"{entry}"' for entry in dependencies)
    (tmp_path / "pyproject.toml").write_text(
        f'[project]\nname = "x"\nversion = "1"\ndependencies = [{quoted}]\n', encoding="utf-8"
    )
    output = tmp_path / "github_output"
    output.write_text("", encoding="utf-8")
    result = subprocess.run(
        [sys.executable, "-I", "-"],
        input=_skip_step_script(),
        cwd=tmp_path,
        env={**os.environ, "GITHUB_OUTPUT": str(output)},
        capture_output=True,
        text=True,
        check=True,
    )
    return output.read_text(encoding="utf-8"), result.stdout


SKIP_MESSAGE = "pinned package: the SDK range is tested in the source repository"


def test_skip_step_runs_the_sdk_job_on_a_ranged_package(tmp_path: Path) -> None:
    written, printed = _run_skip_step(tmp_path, ["pydantic>=2.9", "fastmcp>=3.4.10,<4"])
    assert written == "pinned=false\n"
    assert SKIP_MESSAGE not in printed


def test_skip_step_runs_the_sdk_job_when_one_dependency_is_ranged(tmp_path: Path) -> None:
    written, _ = _run_skip_step(tmp_path, ["pydantic==2.9.0", "fastmcp>=3.4.10,<4"])
    assert written == "pinned=false\n"


def test_skip_step_skips_a_fully_pinned_package_with_markers(tmp_path: Path) -> None:
    written, printed = _run_skip_step(
        tmp_path, ["pydantic==2.9.0", "colorama==0.4.6 ; sys_platform == 'win32'"]
    )
    assert written == "pinned=true\n"
    assert printed.strip() == SKIP_MESSAGE


def test_skip_step_does_not_count_an_equals_sign_inside_a_marker(tmp_path: Path) -> None:
    written, _ = _run_skip_step(
        tmp_path, ["pydantic==2.9.0", "colorama>=0.4 ; sys_platform == 'win32'"]
    )
    assert written == "pinned=false\n"
