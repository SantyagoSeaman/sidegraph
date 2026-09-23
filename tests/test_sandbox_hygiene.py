"""Sandbox git-hygiene guard (leak-safety invariant #1, layer L6): the private
sidegraph-sandbox must never be nested inside this repo, and SIDEGRAPH_SANDBOX must resolve
outside it. Also asserts the leak-gate gitignore entries. See
design/superpowers/specs/2026-07-24-e2e-testing-sandbox-design.md §9/§11.

Also guards a second, related invariant: corpus working copies must never be nested inside this
repo, and SIDEGRAPH_SANDBOXES (plural — a different variable, see harness/sandboxes.py in the
sandbox repo) must resolve outside it. See
design/superpowers/specs/2026-07-27-sandbox-fanout-design.md §3."""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent

# Captured at import time -- i.e. during pytest collection, before any fixture runs, and in
# particular before conftest.py's autouse `_hermetic_sidegraph_env` fixture clears every
# SIDEGRAPH_*-prefixed variable ahead of each test body. Reading os.environ from inside a
# test body would see only that scrub's result (unset, unless the test itself sets it);
# this module-level capture is what lets a value actually configured in the environment
# reach the guards below.
# tests/test_twin_sync.py's SIDEGRAPH_RELEASE_GATE skipif at collection time relies on the
# same ordering.
_CONFIGURED = {name: os.environ.get(name) for name in ("SIDEGRAPH_SANDBOX", "SIDEGRAPH_SANDBOXES")}


def _assert_outside_repo(name: str, value: str) -> None:
    """The equality-and-containment check shared by SIDEGRAPH_SANDBOX and
    SIDEGRAPH_SANDBOXES: `value`, resolved, must not equal the repo root and must not have
    the repo root among its ancestors. Either half alone leaves a gap."""
    resolved = Path(value).resolve()
    root = _ROOT.resolve()
    assert resolved != root and root not in resolved.parents, (
        f"{name} ({resolved}) must be OUTSIDE the repo ({root})"
    )


def _assert_basename_not_nested(name: str, value: str) -> None:
    """The basename check shared by SIDEGRAPH_SANDBOX and SIDEGRAPH_SANDBOXES: a directory
    named like `value`'s basename must not sit directly at the repo root, catching a
    testbed or corpus root checked out under a locally-chosen name."""
    basename = Path(value).resolve().name
    assert not (_ROOT / basename).exists(), (
        f"a directory named like the configured {name} ({basename!r}) is nested inside the repo"
    )


def test_sandbox_env_points_outside_main_tree():
    val = _CONFIGURED["SIDEGRAPH_SANDBOX"]
    if not val:
        pytest.skip("SIDEGRAPH_SANDBOX not set")  # unset (e.g. public CI) => nothing to check
    _assert_outside_repo("SIDEGRAPH_SANDBOX", val)


def test_no_sandbox_dir_nested_in_repo():
    assert not (_ROOT / "sidegraph-sandbox").exists()


def test_no_sandbox_checked_out_under_the_configured_env_name_either():
    """test_no_sandbox_dir_nested_in_repo only ever catches the one hardcoded name
    'sidegraph-sandbox' -- a testbed checked out under any OTHER name would pass it silently.
    This generalizes to whatever basename SIDEGRAPH_SANDBOX actually points at, so a testbed
    nested under a locally-chosen name is still caught."""
    val = _CONFIGURED["SIDEGRAPH_SANDBOX"]
    if not val:
        pytest.skip("SIDEGRAPH_SANDBOX not set")  # unset => nothing configured to check by name
    _assert_basename_not_nested("SIDEGRAPH_SANDBOX", val)


def test_no_gitmodules_file():
    """Spec invariant #4: the testbed must never be a submodule of, or nested checkout inside,
    the OSS repo. A .gitmodules file would mean a submodule is registered."""
    assert not (_ROOT / ".gitmodules").exists()


def test_denylist_and_sandbox_are_gitignored():
    lines = {
        line.strip()
        for line in (_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    }
    assert "/.corpus-leak-denylist" in lines
    assert "/sidegraph-sandbox/" in lines
    assert "sandboxes/" in lines


def test_sandboxes_env_points_outside_main_tree():
    """Resolves SIDEGRAPH_SANDBOXES itself, not just its basename, against the main-repo root.

    Mirrors test_sandbox_env_points_outside_main_tree above for the plural variable: a
    misconfigured value pointing anywhere inside this tree -- at any depth, under any name --
    must be rejected here. The two halves (equality and containment) are both needed; either
    alone leaves a gap. sandboxes_root (harness/sandboxes.py, sandbox repo) only ever validates
    against the *sandbox* repo root, so it accepts a value that points into this tree -- this is
    the check that closes that gap on the main-repo side.
    """
    val = _CONFIGURED["SIDEGRAPH_SANDBOXES"]
    if not val:
        pytest.skip("SIDEGRAPH_SANDBOXES not set")  # unset (e.g. public CI) => nothing to check
    _assert_outside_repo("SIDEGRAPH_SANDBOXES", val)


def test_no_sandboxes_dir_nested_in_the_main_repo():
    """Unconditional and env-free: the guard that always runs whenever this suite runs,
    regardless of configuration -- NOT at commit time; .pre-commit-config.yaml has no pytest
    hook, so nothing in the commit chain invokes this file.

    Catches the common case -- a directory literally named 'sandboxes' nested directly in this
    repo -- even when SIDEGRAPH_SANDBOXES is unset. test_sandboxes_env_points_outside_main_tree
    above covers the general case: an explicitly configured value pointing anywhere inside this
    tree, at any depth, under any name.

    tools/corpus_leak.py walks only tests/fixtures/corpus/, so a sandboxes root misconfigured
    into this tree would put corpus source where the pre-commit gate does not look.
    """
    assert not (_ROOT / "sandboxes").exists()


def test_no_sandboxes_dir_under_the_configured_env_name_either():
    """Generalizes the fixed name to whatever SIDEGRAPH_SANDBOXES points at -- but only by
    basename, at depth 1. test_sandboxes_env_points_outside_main_tree above is the general form
    (resolves the full path, any depth); this one stays as a cheap, name-based backstop.

    Mirrors test_no_sandbox_checked_out_under_the_configured_env_name_either, including its
    skip when the variable is unset. The unconditional assertion above always runs; this
    one adds to it when the variable is set.
    """
    val = _CONFIGURED["SIDEGRAPH_SANDBOXES"]
    if not val:
        pytest.skip("SIDEGRAPH_SANDBOXES not set")  # unconditional check above already ran
    _assert_basename_not_nested("SIDEGRAPH_SANDBOXES", val)


# ---------------------------------------------------------------------------
# D4 meta-tests: prove the four guards above actually see a value CONFIGURED IN THE
# ENVIRONMENT of a real pytest process, not just one set in-process via monkeypatch (which
# is all the removed test_sandboxes_env_pointing_inside_the_main_tree_is_rejected proved).
# Each meta test runs a *child* pytest process, because only a fresh process goes through
# this repo's own collection (where the import-time capture happens, see D1) and the
# autouse `_hermetic_sidegraph_env` scrub in tests/conftest.py, which is exactly the thing
# the fix must survive. See
# design/superpowers/specs/2026-09-23-sandbox-hygiene-guards-design.md §3.
# ---------------------------------------------------------------------------

_SANDBOX_GUARD_IDS = [
    "tests/test_sandbox_hygiene.py::test_sandbox_env_points_outside_main_tree",
    "tests/test_sandbox_hygiene.py::test_no_sandbox_checked_out_under_the_configured_env_name_either",
]
_SANDBOXES_GUARD_IDS = [
    "tests/test_sandbox_hygiene.py::test_sandboxes_env_points_outside_main_tree",
    "tests/test_sandbox_hygiene.py::test_no_sandboxes_dir_under_the_configured_env_name_either",
]


def _run_meta(node_ids: list[str], env: dict[str, str | None]) -> subprocess.CompletedProcess[str]:
    """Run the given guard tests in a real child pytest process with an explicit
    environment, and return the completed process.

    `-o addopts=` cancels the ini's own `-q`: without it, the child's `-q` plus the ini's
    `-q` becomes `-qq`, which prints no summary line at all, so "N passed"/"N failed"/"N
    skipped" would not be there to assert on. `PYTEST_ADDOPTS` is popped from the child's
    environment because it is NOT reset by `-o addopts=` -- with `PYTEST_ADDOPTS=-q` set
    in the parent shell, the summary line disappears the same way and these assertions go
    red on correct code, which is what happened during the design review of this file.
    Node ids select exactly the guard tests under test, so the meta-test itself is never
    among them (pytest would otherwise recurse into this file from inside the child).
    """
    child_env = dict(os.environ)
    child_env.pop("PYTEST_ADDOPTS", None)
    for name, value in env.items():
        if value is None:
            child_env.pop(name, None)
        else:
            child_env[name] = value
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "-o",
            "addopts=",
            *node_ids,
        ],
        cwd=_ROOT,
        env=child_env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_meta_sandbox_guards_reject_a_value_pointing_inside_the_repo():
    """T1. SIDEGRAPH_SANDBOX pointed at <repo>/tests: inside the repo (fails the
    containment guard) and its basename ('tests') exists at the repo root (fails the
    basename guard), so both guards should fire.

    On unfixed code the guards read os.environ from inside the test body, which the
    autouse `_hermetic_sidegraph_env` fixture in conftest.py has already scrubbed by the
    time the test body runs -- both guards see an unset variable, return early, and the
    child passes. This is red against unfixed code.
    """
    inside = str(_ROOT / "tests")
    result = _run_meta(_SANDBOX_GUARD_IDS, {"SIDEGRAPH_SANDBOX": inside})
    output = result.stdout + result.stderr
    assert result.returncode != 0, output
    assert "2 failed" in output, output
    assert "passed" not in output and "skipped" not in output, output
    assert "must be OUTSIDE the repo" in output, output
    assert "is nested" in output, output


def test_meta_sandboxes_guards_reject_a_value_pointing_inside_the_repo():
    """T2. Mirrors test_meta_sandbox_guards_reject_a_value_pointing_inside_the_repo for the
    plural SIDEGRAPH_SANDBOXES guards. Red against unfixed code for the same reason.
    """
    inside = str(_ROOT / "tests")
    result = _run_meta(_SANDBOXES_GUARD_IDS, {"SIDEGRAPH_SANDBOXES": inside})
    output = result.stdout + result.stderr
    assert result.returncode != 0, output
    assert "2 failed" in output, output
    assert "passed" not in output and "skipped" not in output, output
    assert "must be OUTSIDE the repo" in output, output
    assert "is nested" in output, output


def test_meta_guards_pass_for_a_value_outside_the_repo(tmp_path):
    """T3. A guard against over-refusal: a correctly-configured value outside the repo,
    whose basename does not collide with anything at the repo root, must pass all four
    guards -- not skip, not fail. Nothing on unfixed code: the early-return already treats
    this as fine.

    Also kills a basename guard that reads the live, scrubbed environment instead of the
    import-time capture (mutation M5, which T1 and T2 kill too): if it did, the basename
    guard would skip instead of passing, and the "skipped" assertion below would catch it.

    The basenames are random, so they cannot collide with anything at the repo root.
    """
    outside_sandbox = tmp_path / f"sandbox_repo_{uuid.uuid4().hex}"
    outside_sandboxes = tmp_path / f"sandboxes_root_{uuid.uuid4().hex}"
    outside_sandbox.mkdir()
    outside_sandboxes.mkdir()
    result = _run_meta(
        _SANDBOX_GUARD_IDS + _SANDBOXES_GUARD_IDS,
        {
            "SIDEGRAPH_SANDBOX": str(outside_sandbox),
            "SIDEGRAPH_SANDBOXES": str(outside_sandboxes),
        },
    )
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "4 passed" in output, output
    assert "skipped" not in output, output


def test_meta_guards_skip_when_unset():
    """T4. D3: an unset variable must show as skipped, not as a silent pass -- a guard
    that never ran must not look identical to one that ran and approved.

    On unfixed code the guards return early on an unset variable, which pytest reports as
    passed: this is red against unfixed code (4 passed, 0 skipped there, instead of 4
    skipped).
    """
    result = _run_meta(
        _SANDBOX_GUARD_IDS + _SANDBOXES_GUARD_IDS,
        {"SIDEGRAPH_SANDBOX": None, "SIDEGRAPH_SANDBOXES": None},
    )
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "4 skipped" in output, output


def test_meta_child_ignores_the_parents_pytest_addopts(monkeypatch):
    """T7. `_run_meta` drops PYTEST_ADDOPTS from the child's environment: with
    PYTEST_ADDOPTS=-q inherited, the child runs at -qq, prints no summary line, and every
    meta-test above goes red on correct code. Red when the pop is removed."""
    monkeypatch.setenv("PYTEST_ADDOPTS", "-q")
    result = _run_meta(
        _SANDBOX_GUARD_IDS + _SANDBOXES_GUARD_IDS,
        {"SIDEGRAPH_SANDBOX": None, "SIDEGRAPH_SANDBOXES": None},
    )
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "4 skipped" in output, output


def test_helper_assert_outside_repo_rejects_in_repo_paths_and_accepts_outside(tmp_path):
    """T5. Pins `_assert_outside_repo`'s two-part check (equality AND containment)
    directly, without going through a child process. The nested-subdirectory case folds in
    what the old (now-removed) `test_sandboxes_env_pointing_inside_the_main_tree_is_rejected`
    used to pin by calling the guard test function directly with a monkeypatched value; that
    test proved the assertion's own logic but never that a value configured in the real
    environment reaches it -- T1/T2 above prove that end to end instead.

    On unfixed code this is red only through `NameError`: `_assert_outside_repo` does not
    exist yet.
    """
    root_itself = str(_ROOT)
    nested = str(_ROOT / "tests" / "fixtures" / "corpora")
    outside = str(tmp_path / "somewhere_else")
    with pytest.raises(AssertionError, match="must be OUTSIDE the repo"):
        _assert_outside_repo("SIDEGRAPH_SANDBOX", root_itself)
    with pytest.raises(AssertionError, match="must be OUTSIDE the repo"):
        _assert_outside_repo("SIDEGRAPH_SANDBOX", nested)
    _assert_outside_repo("SIDEGRAPH_SANDBOX", outside)  # must not raise


def test_helper_assert_basename_not_nested_rejects_a_colliding_name(tmp_path):
    """T6. Pins `_assert_basename_not_nested` directly. 'tests' collides with the real
    top-level directory in this repo; a made-up name does not.

    On unfixed code this is red only through `NameError`: `_assert_basename_not_nested`
    does not exist yet.
    """
    colliding = str(tmp_path / "tests")
    non_colliding = str(tmp_path / f"not_a_repo_dir_{uuid.uuid4().hex}")
    with pytest.raises(AssertionError, match="is nested"):
        _assert_basename_not_nested("SIDEGRAPH_SANDBOX", colliding)
    _assert_basename_not_nested("SIDEGRAPH_SANDBOX", non_colliding)  # must not raise
