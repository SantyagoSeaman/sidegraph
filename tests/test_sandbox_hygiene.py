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
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent


def test_sandbox_env_points_outside_main_tree():
    val = os.environ.get("SIDEGRAPH_SANDBOX")
    if not val:
        return  # unset (e.g. public CI) => nothing to check
    sandbox = Path(val).resolve()
    root = _ROOT.resolve()
    assert sandbox != root and root not in sandbox.parents, (
        f"SIDEGRAPH_SANDBOX ({sandbox}) must be OUTSIDE the repo ({root})"
    )


def test_no_sandbox_dir_nested_in_repo():
    assert not (_ROOT / "sidegraph-sandbox").exists()


def test_no_sandbox_checked_out_under_the_configured_env_name_either():
    """test_no_sandbox_dir_nested_in_repo only ever catches the one hardcoded name
    'sidegraph-sandbox' -- a testbed checked out under any OTHER name would pass it silently.
    This generalizes to whatever basename SIDEGRAPH_SANDBOX actually points at, so a testbed
    nested under a locally-chosen name is still caught."""
    val = os.environ.get("SIDEGRAPH_SANDBOX")
    if not val:
        return  # unset (e.g. public CI) => nothing configured to check by name
    basename = Path(val).resolve().name
    assert not (_ROOT / basename).exists(), (
        f"a directory named like the configured testbed ({basename!r}) is nested inside the repo"
    )


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
    val = os.environ.get("SIDEGRAPH_SANDBOXES")
    if not val:
        return  # unset (e.g. public CI) => nothing to check
    sandboxes = Path(val).resolve()
    root = _ROOT.resolve()
    assert sandboxes != root and root not in sandboxes.parents, (
        f"SIDEGRAPH_SANDBOXES ({sandboxes}) must be OUTSIDE the repo ({root})"
    )


def test_sandboxes_env_pointing_inside_the_main_tree_is_rejected(monkeypatch):
    """SIDEGRAPH_SANDBOXES is unset on the owner's machine and in CI (see the spec's §3), which
    makes test_sandboxes_env_points_outside_main_tree's assertion above vacuous as run -- it has
    never actually gone red in the committed suite. This pins the value the Task 7 review used
    to demonstrate the hole (SIDEGRAPH_SANDBOXES pointing at a subdirectory of this repo, which
    passes sandboxes_root, passes both name-based guards, and is not matched by the .gitignore
    backstop) and proves the containment assertion catches it, so the closure is proven by the
    suite and not by a review transcript.
    """
    val = str(_ROOT / "tests" / "fixtures" / "corpora")
    monkeypatch.setenv("SIDEGRAPH_SANDBOXES", val)
    with pytest.raises(AssertionError, match="must be OUTSIDE the repo"):
        test_sandboxes_env_points_outside_main_tree()


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

    Mirrors test_no_sandbox_checked_out_under_the_configured_env_name_either for its
    generalization only — NOT for its early `return`. The unconditional assertion above always
    runs; this one adds to it when the variable is set.
    """
    val = os.environ.get("SIDEGRAPH_SANDBOXES")
    if not val:
        return  # the unconditional check above already ran; this one only ADDS coverage
    basename = Path(val).resolve().name
    assert not (_ROOT / basename).exists(), (
        f"a directory named like the configured sandboxes root ({basename!r}) is nested in the repo"
    )
