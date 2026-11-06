"""Shape of the shared CI workflow: cheap checks on every PR event,
the full matrix only off Draft, one aggregate gate.
"""

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[1]
_CI = _ROOT / ".github/workflows/ci.yml"
_PUBLISH = _ROOT / ".github/workflows/publish.yml"
HEAVY_JOBS = ("test", "sdk-compatibility", "windows-core")
FULL_IF = "${{ github.event_name != 'pull_request' || !github.event.pull_request.draft }}"
HEAVY_IF = (
    "${{ !cancelled() && (github.event_name != 'pull_request' || !github.event.pull_request.draft) "
    "&& needs.prior.outputs.mode != 'covered' && needs.prior.outputs.mode != 'light' }}"
)
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
    for name in HEAVY_JOBS:
        assert jobs[name]["if"] == HEAVY_IF
        assert jobs[name]["needs"] == "prior"
    assert "if" not in jobs["lint"]
    for name in ("store-lint", "anchor-health"):
        assert jobs[name]["if"] == "github.event_name == 'pull_request'"


def test_prior_job_shape() -> None:
    prior = _load()["jobs"]["prior"]
    assert prior["if"] == "github.event_name != 'pull_request'"
    assert prior["continue-on-error"] is True
    assert prior["timeout-minutes"] == 5
    assert prior["permissions"] == {"contents": "read", "actions": "read"}
    assert prior["runs-on"] == "ubuntu-slim"
    assert prior["outputs"] == {
        "mode": "${{ job.status == 'success' && steps.probe.outputs.mode || 'full' }}",
        "covered_by": "${{ job.status == 'success' && steps.probe.outputs.covered_by || '' }}",
    }
    checkout, setup, probe = prior["steps"]
    assert checkout["uses"].startswith("actions/checkout@")
    assert checkout["with"] == {
        "sparse-checkout": "tools/ci_covered.py",
        "sparse-checkout-cone-mode": False,
        "persist-credentials": False,
    }
    assert setup["uses"].startswith("astral-sh/setup-uv@")
    assert setup["with"]["python-version"] == "3.13"
    assert setup["with"]["enable-cache"] is False
    assert probe["id"] == "probe"
    assert "tools/ci_covered.py" in probe["run"]
    assert "--python 3.13" in probe["run"]
    assert probe["env"] == {"GITHUB_TOKEN": "${{ github.token }}"}


def test_no_job_or_gate_step_is_made_non_fatal_at_job_level() -> None:
    """A job-level `continue-on-error` would let a red job count as passed in the gate."""
    jobs = _load()["jobs"]
    for name in ("test", "sdk-compatibility", "windows-core", "full-ci"):
        assert "continue-on-error" not in jobs[name], name
    assert "continue-on-error" not in jobs["full-ci"]["steps"][0]


def test_heavy_jobs_test_the_default_ref() -> None:
    """The evidence records github.sha's tree; a heavy job must test that same tree."""
    jobs = _load()["jobs"]
    for name in HEAVY_JOBS:
        for step in jobs[name]["steps"]:
            assert "continue-on-error" not in step, (name, step.get("name"))
            if step.get("uses", "").startswith("actions/checkout@"):
                assert "ref" not in step.get("with", {}), name


def test_full_ci_needs_every_other_job() -> None:
    jobs = _load()["jobs"]
    gate = jobs["full-ci"]
    assert set(gate["needs"]) == set(jobs) - {"full-ci"}
    assert gate["if"] == "${{ always() }}"
    assert gate["name"] == FULL_CI_NAME
    assert gate["timeout-minutes"] == 8
    assert len(gate["steps"]) == 4
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
        "MODE": "${{ needs.prior.outputs.mode }}",
        "PRIOR": "${{ needs.prior.result }}",
        "COVERED_BY": "${{ needs.prior.outputs.covered_by }}",
    }


SKIPPED_MATRIX = {
    "TEST": "skipped",
    "SDK": "skipped",
    "WINDOWS": "skipped",
    "STORE_LINT": "skipped",
    "ANCHOR_HEALTH": "skipped",
}
COVERED = {
    "MODE": "covered",
    "PRIOR": "success",
    "COVERED_BY": "https://github.com/o/r/actions/runs/7",
    **SKIPPED_MATRIX,
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
        ("true", "push", COVERED, True),
        ("true", "push", {**COVERED, "MODE": "light"}, True),
        ("true", "push", {**COVERED, "TEST": "success"}, False),
        ("true", "push", {**COVERED, "PRIOR": "failure"}, False),
        ("true", "push", {**COVERED, "LINT": "failure"}, False),
        ("true", "push", {**COVERED, "STORE_LINT": "success"}, False),
        ("true", "pull_request", {"MODE": "full"}, False),
        ("true", "push", {"MODE": "bogus"}, False),
        ("true", "push", {"MODE": "full"}, True),
        ("true", "push", {"PRIOR": "failure", "MODE": "", "STORE_LINT": "skipped"}, True),
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
        "covered",
        "light",
        "covered-but-test-ran",
        "covered-prior-failed",
        "covered-lint-failed",
        "covered-store-lint-ran",
        "pr-with-mode",
        "unknown-mode",
        "explicit-full",
        "prior-failed-non-fatal",
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
        "MODE": "",
        "PRIOR": "skipped",
        "COVERED_BY": "",
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
    if ok and (full == "false" or changes.get("MODE") in ("covered", "light")):
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


def _eval_if(
    expr: str, ctx: dict[str, object], *, success: bool = True, cancelled: bool = False
) -> bool:
    """Evaluate a `${{ }}` condition of these workflows against fixed context values.

    `success()` and `cancelled()` are separate inputs: a step after a failed one sees
    `success()` false while the run is not cancelled.
    """
    assert expr.startswith("${{") and expr.endswith("}}"), expr
    body = expr[3:-2].strip()
    for path in sorted(ctx, key=len, reverse=True):
        body = body.replace(path, repr(ctx[path]))
    body = re.sub(r"startsWith\(([^,]+),\s*([^)]+)\)", r"(\1).startswith(\2)", body)
    body = body.replace("!cancelled()", repr(not cancelled)).replace("success()", repr(success))
    body = body.replace("&&", " and ").replace("||", " or ")
    body = re.sub(r"!(?!=)", "not ", body)
    return bool(eval(body, {"__builtins__": {}}, {}))  # noqa: S307


def _ctx(
    event: str = "push",
    *,
    draft: bool = False,
    ref: str = "refs/heads/main",
    mode: str = "",
    test: str = "success",
    sdk: str = "success",
    windows: str = "success",
) -> dict[str, object]:
    return {
        "github.event_name": event,
        "github.event.pull_request.draft": draft,
        "github.ref": ref,
        "needs.prior.outputs.mode": mode,
        "needs.test.result": test,
        "needs.sdk-compatibility.result": sdk,
        "needs.windows-core.result": windows,
    }


def _evidence_steps() -> tuple[dict, dict, dict, dict]:
    gate = _load()["jobs"]["full-ci"]
    steps = gate["steps"]
    assert [step.get("id") for step in steps[1:3]] == ["tree", "upload"]
    return steps[0], steps[1], steps[2], steps[3]


def test_evidence_steps() -> None:
    _, tree, upload, warning = _evidence_steps()
    assert tree["if"].startswith("${{ success() && ")
    assert tree["continue-on-error"] is True
    assert tree["timeout-minutes"] == 1
    assert upload["continue-on-error"] is True
    assert upload["timeout-minutes"] == 2
    assert upload["if"] == "${{ steps.tree.outcome == 'success' }}"
    assert upload["uses"].startswith("actions/upload-artifact@")
    assert upload["with"] == {
        "name": "full-ci-tree-${{ steps.tree.outputs.tree }}",
        "path": "${{ runner.temp }}/full-ci-evidence/evidence.json",
        "if-no-files-found": "error",
        "retention-days": 30,
    }
    # The lookup and the recorded sha are the tested commit, never a pull request's head sha.
    assert "git/commits/$GITHUB_SHA" in tree["run"]
    assert '--arg sha "$GITHUB_SHA"' in tree["run"]
    assert "${{" not in tree["run"]
    assert "steps.tree.outcome == 'failure'" in warning["if"]
    assert "steps.upload.outcome == 'failure'" in warning["if"]
    gate = _load()["jobs"]["full-ci"]
    assert sum(step.get("timeout-minutes", 0) for step in gate["steps"]) < gate["timeout-minutes"]


@pytest.mark.parametrize(
    ("ctx", "expected"),
    [
        (_ctx("pull_request", ref="refs/pull/1/merge"), True),
        (_ctx("pull_request", draft=True, ref="refs/pull/1/merge"), False),
        (_ctx(mode="full"), True),
        (_ctx(mode=""), True),
        (_ctx(mode="covered"), False),
        (_ctx(mode="light"), False),
        (_ctx(ref="refs/tags/v1.2.3"), False),
        (_ctx(mode="", test="skipped"), False),
        (_ctx(mode="full", sdk="skipped"), False),
        (_ctx(mode="", windows="failure"), False),
        (_ctx("pull_request", ref="refs/pull/1/merge", test="failure"), False),
    ],
    ids=[
        "ready-pr",
        "draft-pr",
        "push-full",
        "push-empty-mode",
        "push-covered",
        "push-light",
        "tag-build",
        "empty-mode-test-skipped",
        "full-sdk-skipped",
        "empty-mode-windows-failed",
        "pr-test-failed",
    ],
)
def test_evidence_if_predicate(ctx: dict[str, object], expected: bool) -> None:
    _, tree, _, _ = _evidence_steps()
    assert _eval_if(tree["if"], ctx) is expected


@pytest.mark.parametrize(("success", "cancelled"), [(False, False), (False, True)])
def test_evidence_if_needs_a_passing_gate(success: bool, cancelled: bool) -> None:
    """After a failed gate step the heavy jobs are green but no evidence may be written."""
    _, tree, _, _ = _evidence_steps()
    assert _eval_if(tree["if"], _ctx(mode="full"), success=success, cancelled=cancelled) is False


@pytest.mark.parametrize(
    ("ctx", "expected"),
    [
        (_ctx(mode="covered"), False),
        (_ctx(mode="light"), False),
        (_ctx(mode=""), True),
        (_ctx(mode="full"), True),
        (_ctx("pull_request", draft=True, mode=""), False),
        (_ctx("pull_request", mode=""), True),
    ],
    ids=["covered", "light", "empty", "full", "draft-pr", "ready-pr"],
)
def test_heavy_if_predicate(ctx: dict[str, object], expected: bool) -> None:
    for name in HEAVY_JOBS:
        assert _eval_if(_load()["jobs"][name]["if"], ctx) is expected


def test_publish_test_job_permissions() -> None:
    wf = yaml.safe_load(_PUBLISH.read_text(encoding="utf-8"))
    assert wf["permissions"] == {"contents": "read"}
    assert wf["jobs"]["test"]["permissions"] == {"contents": "read", "actions": "read"}
    for name in ("build", "publish"):
        assert "actions" not in wf["jobs"][name].get("permissions", {})
