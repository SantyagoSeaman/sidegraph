"""Opt-in real pinned secret-scanner integration, also exercised by the lint CI job.

Read the flag at collection time, before conftest's hermetic environment scrub.
Ordinary Python tests do not implicitly install the Go-backed pre-commit environment.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_ENABLED = os.environ.get("SIDEGRAPH_SECRET_SCAN_INTEGRATION") == "1"
pytestmark = [
    pytest.mark.integration,
    pytest.mark.slow,
    pytest.mark.skipif(not _ENABLED, reason="set SIDEGRAPH_SECRET_SCAN_INTEGRATION=1"),
]


def _run(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.pop("SKIP", None)
    return subprocess.run(args, cwd=repo, env=env, capture_output=True, text=True, timeout=120)


def _git(repo: Path, *args: str) -> str:
    result = _run(repo, "git", "-c", "core.hooksPath=/dev/null", *args)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


@pytest.fixture
def scan_repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "scanner@example.test")
    _git(tmp_path, "config", "user.name", "Scanner test")
    for name in (".pre-commit-config.yaml", ".gitleaks.toml", ".gitleaksignore"):
        if (_ROOT / name).exists():
            shutil.copyfile(_ROOT / name, tmp_path / name)
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-qm", "clean scanner configuration")
    return tmp_path


def _write(repo: Path, name: str, content: str) -> None:
    path = repo / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    _git(repo, "add", name)


def _token() -> str:
    # Construct a synthetic token; no credential or issuer call is involved.
    return "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"


def _scan(repo: Path, *, history: bool = False) -> subprocess.CompletedProcess[str]:
    args = [sys.executable, "-m", "pre_commit", "run"]
    args += (
        ["gitleaks-history", "--hook-stage", "manual", "--all-files"] if history else ["gitleaks"]
    )
    return _run(repo, *args, "--verbose", "--color=never")


def _assert_detected(
    result: subprocess.CompletedProcess[str],
    path: str,
    rule: str = "github-pat",
    line: int | None = None,
) -> None:
    output = result.stdout + result.stderr
    assert result.returncode == 1, output
    assert rule in output, output
    if line is not None:
        assert re.search(rf"(?m)^Line:\s+{line}\s*$", output), output
    assert path in output, output
    assert _token() not in output, "scanner findings must be redacted"


def _assert_scanned_clean(result: subprocess.CompletedProcess[str]) -> None:
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "Passed" in output, output
    assert "Skipped" not in output, output
    assert "no leaks found" in output, output


@pytest.mark.parametrize("path", [".sidegraph/facts/probe.json", "sidegraph-graph.html"])
def test_staged_secret_in_formatter_excluded_path(scan_repo: Path, path: str) -> None:
    _write(scan_repo, path, json.dumps({"token": _token()}) + "\n")
    _assert_detected(_scan(scan_repo), path)


def test_clean_store_only_change_scans(scan_repo: Path) -> None:
    _write(scan_repo, ".sidegraph/facts/probe.json", '{"content": "safe"}\n')
    _assert_scanned_clean(_scan(scan_repo))


def test_committed_secret_with_empty_index(scan_repo: Path) -> None:
    path = "settings.txt"
    _write(scan_repo, path, "token=" + _token() + "\n")
    _git(scan_repo, "commit", "-qm", "synthetic regression fixture")
    assert _git(scan_repo, "diff", "--cached", "--name-only") == ""
    _assert_detected(_scan(scan_repo, history=True), path)


def test_removed_secret_remains_visible_in_history(scan_repo: Path) -> None:
    path = "settings.txt"
    _write(scan_repo, path, "token=" + _token() + "\n")
    _git(scan_repo, "commit", "-qm", "synthetic regression fixture")
    _git(scan_repo, "rm", path)
    _git(scan_repo, "commit", "-qm", "remove synthetic fixture")
    _assert_detected(_scan(scan_repo, history=True), path)


def test_clean_committed_history_scans(scan_repo: Path) -> None:
    _assert_scanned_clean(_scan(scan_repo, history=True))


def test_historical_ignore_does_not_hide_new_secret(scan_repo: Path) -> None:
    path = "docs/superpowers/plans/2026-07-06-capture-ratification.md"
    _write(scan_repo, path, "token=" + _token() + "\n")
    _git(scan_repo, "commit", "-qm", "new synthetic token at retired fixture path")
    _assert_detected(_scan(scan_repo, history=True), path)


def test_merge_resolution_only_secret_is_detected(scan_repo: Path) -> None:
    path = "settings.txt"
    _write(scan_repo, path, "base\n")
    _git(scan_repo, "commit", "-qm", "clean base")
    main_branch = _git(scan_repo, "branch", "--show-current")
    _git(scan_repo, "checkout", "-qb", "scanner-side")
    _write(scan_repo, path, "side\n")
    _git(scan_repo, "commit", "-qm", "clean side")
    _git(scan_repo, "checkout", "-q", main_branch)
    _write(scan_repo, path, "main\n")
    _git(scan_repo, "commit", "-qm", "clean main")
    conflict = _run(
        scan_repo, "git", "-c", "core.hooksPath=/dev/null", "merge", "--no-edit", "scanner-side"
    )
    assert conflict.returncode == 1
    assert "CONFLICT" in conflict.stdout + conflict.stderr
    _write(scan_repo, path, "token=" + _token() + "\n")
    _git(scan_repo, "commit", "-qm", "resolve conflict with synthetic fixture")
    assert len(_git(scan_repo, "rev-list", "--parents", "-n", "1", "HEAD").split()) == 3
    for parent in ["HEAD^1", "HEAD^2"]:
        assert _token() not in _git(scan_repo, "show", f"{parent}:{path}")
    _assert_detected(_scan(scan_repo, history=True), path)


@pytest.mark.parametrize(
    ("rule", "line"),
    [
        ("generic-api-key", 80),
        ("generic-api-key", 1303),
        ("aws-access-token", 112),
        ("private-key", 94),
    ],
)
def test_ignored_fingerprint_rule_and_line_still_scan_new_commit(
    scan_repo: Path, rule: str, line: int
) -> None:
    path = "docs/superpowers/plans/2026-07-06-capture-ratification.md"
    if rule == "generic-api-key":
        secret = "".join(("Aa1Bb2Cc3", "Dd4Ee5Ff6", "Gg7Hh8Ii9", "Jj0Kk1Ll2"))
        payload = 'api_key="' + secret + '"\n'
    elif rule == "aws-access-token":
        secret = "AKIA" + "ZYXWVUTSRQPNMLKJ"
        payload = 'access_key="' + secret + '"\n'
    else:
        secret = "-----BEGIN RSA " + "PRIVATE KEY-----"
        payload = secret + "\n" + "synthetic" * 12 + "\n-----END RSA " + "PRIVATE KEY-----\n"
    _write(scan_repo, path, "\n" * (line - 1) + payload)
    _git(scan_repo, "commit", "-qm", "new same-rule same-line synthetic fixture")
    result = _scan(scan_repo, history=True)
    _assert_detected(result, path, rule, line)
    assert secret not in result.stdout + result.stderr, "scanner findings must be redacted"


def test_octopus_merge_only_secret_is_detected(scan_repo: Path) -> None:
    path = "settings.txt"
    _write(scan_repo, path, "safe\n")
    _git(scan_repo, "commit", "-qm", "clean base")
    main_branch = _git(scan_repo, "branch", "--show-current")
    _git(scan_repo, "checkout", "-qb", "scanner-side-a")
    _write(scan_repo, "a.txt", "safe a\n")
    _git(scan_repo, "commit", "-qm", "clean a")
    _git(scan_repo, "checkout", "-qb", "scanner-side-b", main_branch)
    _write(scan_repo, "b.txt", "safe b\n")
    _git(scan_repo, "commit", "-qm", "clean b")
    _git(scan_repo, "checkout", "-q", main_branch)
    _write(scan_repo, "main.txt", "safe main\n")
    _git(scan_repo, "commit", "-qm", "clean main")
    _git(scan_repo, "merge", "--no-commit", "scanner-side-a", "scanner-side-b")
    _write(scan_repo, path, "token=" + _token() + "\n")
    _git(scan_repo, "commit", "-qm", "merge-only synthetic fixture")
    assert len(_git(scan_repo, "rev-list", "--parents", "-n", "1", "HEAD").split()) == 4
    for parent in ["HEAD^1", "HEAD^2", "HEAD^3"]:
        assert _token() not in _git(scan_repo, "show", f"{parent}:{path}")
    _assert_detected(_scan(scan_repo, history=True), path)


def test_removed_branch_secret_remains_visible_after_merge(scan_repo: Path) -> None:
    path = "settings.txt"
    main_branch = _git(scan_repo, "branch", "--show-current")
    _git(scan_repo, "checkout", "-qb", "scanner-side")
    _write(scan_repo, path, "token=" + _token() + "\n")
    _git(scan_repo, "commit", "-qm", "synthetic branch fixture")
    _git(scan_repo, "rm", path)
    _git(scan_repo, "commit", "-qm", "remove branch fixture")
    _git(scan_repo, "checkout", "-q", main_branch)
    _git(scan_repo, "merge", "--no-ff", "--no-edit", "scanner-side")
    assert len(_git(scan_repo, "rev-list", "--parents", "-n", "1", "HEAD").split()) == 3
    assert path not in _git(scan_repo, "ls-files").splitlines()
    _assert_detected(_scan(scan_repo, history=True), path)
