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

import pytest


@pytest.fixture(autouse=True)
def _hermetic_sidegraph_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove every ``SIDEGRAPH_``-prefixed variable from the environment for the
    duration of one test. See module docstring for why and for the scope choice.
    """
    for key in [k for k in os.environ if k.startswith("SIDEGRAPH_")]:
        monkeypatch.delenv(key, raising=False)
