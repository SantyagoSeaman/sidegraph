"""Hermetic test environment: strip every ``SIDEGRAPH_``-prefixed variable before each
test so the suite's pass/fail never depends on what happens to be exported in the
process that runs pytest.

The bug this guards against: the repo commits ``.claude/settings.json`` with
``env.SIDEGRAPH_RATIFY_POLICY: "auto-low-risk"`` (since ebbd93e), so every Claude Code
session in this repo exports that variable. With no ``conftest.py``, the suite
inherited it, and ``SIDEGRAPH_RATIFY_POLICY=auto-low-risk uv run pytest
tests/test_cli_import.py tests/test_cli_domains.py`` failed 10 tests that pass with
the variable unset -- the CLI's own auto-ratify summary line changed shape, and tests
asserting the old shape broke. CI never saw it: GitHub Actions does not read
``.claude/settings.json``, so it stayed green while a Claude Code session's own local
shell went red. Root cause was not the ratify-policy feature itself but that ``tests/``
had no isolation at all: any of the ~15 ``SIDEGRAPH_*`` variables the codebase reads
(``SIDEGRAPH_AUTO_ACCEPT``, ``SIDEGRAPH_CAPTURE_NUDGE``, ``SIDEGRAPH_DB``,
``SIDEGRAPH_DIR``, ``SIDEGRAPH_DRIFT_NUDGE``, ``SIDEGRAPH_GRAPH``,
``SIDEGRAPH_GREP_NUDGE``, ``SIDEGRAPH_PROPOSAL_WINDOW_DAYS``,
``SIDEGRAPH_RATIFY_NUDGE``, ``SIDEGRAPH_RATIFY_POLICY``, ``SIDEGRAPH_TELEMETRY``,
``SIDEGRAPH_TRUST_DIRTY_TREE``, ``SIDEGRAPH_UNRATIFIED``, and others) could have done
the same thing.

A test that genuinely wants one of these set must do so explicitly with
``monkeypatch.setenv``/``monkeypatch.delenv`` -- never by relying on inheritance from
the environment pytest happens to run in. The one exception is the sandbox-hygiene
guards (tests/test_sandbox_hygiene.py): their whole point is to check the environment
the suite runs in, so they read it at import time, and a test drives them through a
child pytest process, not ``monkeypatch``.

Deliberately function-scoped, not session- or module-scoped: ``monkeypatch`` itself is
a function-scoped fixture (pytest has no built-in session-scoped variant), and function
scope is also the safest choice on its own merits -- it guarantees a variable one test
sets via ``monkeypatch.setenv`` cannot leak into the next test, because this fixture
re-clears before every single test regardless of what the previous one did or how its
own teardown behaved.

Note for two known intentional exceptions this deliberately does NOT special-case:
``SIDEGRAPH_SANDBOX``/``SIDEGRAPH_SANDBOXES`` (tests/test_sandbox_hygiene.py,
tests/test_pilot_kit_corpus_fit.py) and ``SIDEGRAPH_RELEASE_GATE``
(tests/test_twin_sync.py) are opt-in gates for a developer's own sandbox checkout, read
only by test code, never by src/. Clearing them here does not weaken those checks:
tests/test_sandbox_hygiene.py captures both SIDEGRAPH_SANDBOX and SIDEGRAPH_SANDBOXES at
*import time*, before this fixture (or any fixture) runs, precisely so this scrub cannot
hide a value actually configured in the environment -- their bodies still treat "unset"
as the expected default, but now by calling ``pytest.skip`` rather than by silently
returning, so a guard that never ran is visible as skipped rather than indistinguishable
from one that ran and approved.
"""

from __future__ import annotations

import os
import shutil
import sys
from collections.abc import Iterator

import pytest

from sidegraph.gitenv import GIT_LOCAL_ENV_VARS


@pytest.fixture(autouse=True)
def _hermetic_sidegraph_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove every ``SIDEGRAPH_``-prefixed variable from the environment for the
    duration of one test. See module docstring for why and for the scope choice.
    """
    for key in [k for k in os.environ if k.startswith("SIDEGRAPH_")]:
        monkeypatch.delenv(key, raising=False)


@pytest.fixture(autouse=True)
def _git_discovery_stays_in_temp_tree(
    monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """Stop git discovery from climbing out of pytest's temp tree into an enclosing repo.

    A store under ``tmp_path`` derives no initiative because git finds no repository above
    it. With a basetemp placed inside a checkout (a reviewer's setup), discovery would walk
    up into that repository and name its branch. ``GIT_CEILING_DIRECTORIES`` is exclusive:
    a repository a test builds inside ``tmp_path`` sits below the ceiling and is still found.

    This fences only discovery that runs ``git`` from a path under the temp tree. It does not
    fence the lexical ``.git`` walk of ``config.repository_root``, which ignores the ceiling,
    and it does not move the process cwd (see ``cwd_outside_any_repository``).
    # see design/superpowers/specs/2026-09-30-initiative-from-store-repo-design.md D3
    """
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path_factory.getbasetemp().parent))


@pytest.fixture
def cwd_outside_any_repository(
    monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[None]:
    """Run the test from an empty directory that sits in no repository, whatever the basetemp.

    Opt in with ``pytest.mark.usefixtures``. It is not autouse: tests elsewhere read repository
    files by a cwd-relative path and need the checkout root as their cwd.

    Doc import resolves its repository root from the PROCESS cwd (``git rev-parse
    --show-toplevel``, else the cwd itself), not from a store path, so the
    ``GIT_CEILING_DIRECTORIES`` fence above does not reach it. Pytest starts in the checkout
    root: with the default basetemp every ``tmp_path`` document lies outside that root and is
    keyed on the path the caller passed, but with ``--basetemp`` inside the checkout (a review
    panel keeps its scratch there) the same documents are in-repo and keyed repo-relative, and
    the tests that expect the first form fail. A fresh directory under the temp tree is below
    the ceiling, so discovery cannot climb out of it in either mode; the root falls back to
    that directory and the documents stay outside it. A test that needs another cwd (a repo it
    built, a subdirectory) still calls ``monkeypatch.chdir``, which wins over this one.
    """
    cwd = tmp_path_factory.mktemp("cwd")
    before = os.getcwd()
    monkeypatch.chdir(cwd)
    yield
    os.chdir(before)  # leave the directory before removing it
    shutil.rmtree(cwd, ignore_errors=True)


@pytest.fixture(autouse=True)
def _hook_argv_is_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """The hook entry points refuse a command-line argument, and a test that calls one in
    process would otherwise see pytest's own arguments."""
    monkeypatch.setattr(sys, "argv", ["hook"])


@pytest.fixture(autouse=True)
def _no_inherited_git_repository(monkeypatch: pytest.MonkeyPatch) -> None:
    """git sets ``GIT_DIR``/``GIT_INDEX_FILE`` in hooks; the git test helpers must never
    write into a foreign repository or index."""
    for var in GIT_LOCAL_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


@pytest.fixture(autouse=True)
def _git_config_is_hermetic(
    monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """Keep the developer's own git configuration out of every test.

    A ``sidegraph.graphRefresh=false`` or a ``core.hooksPath`` in the real global config changes
    what ``sidegraph-init`` installs and what the integrity checks report, so a test that passes
    on a clean machine failed on one that had either. ``GIT_CONFIG_GLOBAL`` names an empty file
    (git then reads neither ``~/.gitconfig`` nor the XDG file), and ``GIT_CONFIG_NOSYSTEM`` drops
    the system file. A test that wants a global value points ``GIT_CONFIG_GLOBAL`` at its own file
    with ``monkeypatch``, as the git-hook tests do.
    """
    empty = tmp_path_factory.getbasetemp() / "empty.gitconfig"
    empty.touch()
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(empty))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
