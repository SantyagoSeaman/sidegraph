"""Guards the hermetic-test-environment invariant (see conftest.py): no
``SIDEGRAPH_``-prefixed variable may reach a test's ``os.environ`` just because the
process pytest itself runs in already had one -- from the developer's shell, or from
the repo's committed ``.claude/settings.json`` (``env.SIDEGRAPH_RATIFY_POLICY``, since
ebbd93e). A test that wants a variable must set it explicitly with ``monkeypatch``.

Without the autouse fixture in ``conftest.py``, this goes red as soon as the *outer*
process exports a ``SIDEGRAPH_`` variable before pytest's own per-test fixtures ever
run -- which is exactly the bug this file exists to catch: ``SIDEGRAPH_RATIFY_POLICY=
auto-low-risk uv run pytest tests/test_cli_import.py tests/test_cli_domains.py``
failed 10 tests while the same files passed 94 with the variable unset, purely because
``tests/`` had no ``conftest.py`` and so inherited the ambient environment.

``_simulate_ambient_sidegraph_env`` below is session-scoped and sets the variables
directly on ``os.environ`` (not via ``monkeypatch``, which is function-scoped) so its
setup runs once, before any per-test fixture -- standing in for "the parent shell/CI
runner already had this exported when pytest started", not for something a test itself
opts into.
"""

from __future__ import annotations

import os

import pytest


@pytest.fixture(scope="session", autouse=True)
def _simulate_ambient_sidegraph_env():
    """Set two SIDEGRAPH_ variables directly on the real environment before any
    per-test fixture runs, standing in for a shell export or the committed
    .claude/settings.json env block. Restored at session end either way."""
    added = {"SIDEGRAPH_RATIFY_POLICY": "auto-low-risk", "SIDEGRAPH_AUTO_ACCEPT": "1"}
    previous = {k: os.environ.get(k) for k in added}
    os.environ.update(added)
    try:
        yield
    finally:
        for key, old_value in previous.items():
            if old_value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old_value


def test_no_sidegraph_env_leaks_into_a_test():
    leaked = sorted(k for k in os.environ if k.startswith("SIDEGRAPH_"))
    assert leaked == [], (
        f"SIDEGRAPH_ variable(s) leaked into the test environment: {leaked}. "
        "A hermetic conftest.py fixture should have stripped them before this test ran."
    )


def test_a_variable_a_test_sets_via_monkeypatch_does_not_leak_into_the_next_test(
    monkeypatch,
):
    monkeypatch.setenv("SIDEGRAPH_RATIFY_POLICY", "auto-low-risk")
    assert os.environ["SIDEGRAPH_RATIFY_POLICY"] == "auto-low-risk"


def test_previous_tests_monkeypatch_setenv_did_not_survive():
    # Runs after the test above in file order. Proves function scope: what one test
    # sets via monkeypatch is gone by the time the next test's fixtures re-clear.
    assert "SIDEGRAPH_RATIFY_POLICY" not in os.environ
