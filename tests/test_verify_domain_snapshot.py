"""sidegraph-verify checks the domain snapshot and the requirement it needs (D5.4).

see design/superpowers/specs/2026-10-14-graphify-upgrade-design.md (D5)"""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime

import pytest

from sidegraph import domain_snapshot as ds
from sidegraph.store import Store
from sidegraph.verify import (
    BAD_DOMAIN_SNAPSHOT,
    SNAPSHOT_WITHOUT_REQUIREMENT,
    verify_against,
    verify_snapshot,
)

_OURS = {BAD_DOMAIN_SNAPSHOT, SNAPSHOT_WITHOUT_REQUIREMENT}


def _snap(commit=None):
    return ds.build_snapshot(
        [],
        {},
        {},
        taken_at=datetime(2026, 10, 14, tzinfo=UTC),
        engine_version=None,
        engine_generation="legacy",
        graph_version="content:abc",
        store_commit=commit,
    )


def _store(tmp_path, requirement):
    root = tmp_path / ".sidegraph"
    Store(root).close()
    if requirement is not None:
        (root / "format").write_text(
            f"sidegraph-store 1.0.0\nrequires-sidegraph >= {requirement}\n", encoding="utf-8"
        )
    return root


def _codes(root):
    return sorted(v.code for v in verify_snapshot(root) if v.code in _OURS)


def test_a_valid_snapshot_under_the_requirement_is_clean(tmp_path):
    root = _store(tmp_path, "0.14.0")
    ds.write_snapshot(root, _snap())
    assert _codes(root) == []


@pytest.mark.parametrize("requirement", [None, "0.13.0"])
def test_a_snapshot_without_a_sufficient_requirement_is_reported(tmp_path, requirement):
    """Red against unfixed verify: no check, and an older Sidegraph still opens this store."""
    root = _store(tmp_path, requirement)
    ds.write_snapshot(root, _snap())
    assert _codes(root) == [SNAPSHOT_WITHOUT_REQUIREMENT]


@pytest.mark.parametrize("text", ["{not json", json.dumps({"format": 1})])
def test_a_malformed_snapshot_is_reported(tmp_path, text):
    root = _store(tmp_path, "0.14.0")
    ds.snapshot_path(root).write_text(text, encoding="utf-8")
    assert _codes(root) == [BAD_DOMAIN_SNAPSHOT]


def test_no_snapshot_no_finding(tmp_path):
    """Guard."""
    assert _codes(_store(tmp_path, None)) == []


def _git(cwd, *args):
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
    )


def test_against_does_not_classify_the_snapshot_file(tmp_path):
    """Guard (declared): adding, refreshing or deleting the snapshot is no transition finding."""
    _git(tmp_path, "init", "-q")
    root = _store(tmp_path, "0.14.0")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "--no-verify", "-m", "store")
    ds.write_snapshot(root, _snap())
    assert verify_against(root, "HEAD") == []
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "--no-verify", "-m", "snapshot")
    ds.write_snapshot(root, _snap("a" * 40))
    assert verify_against(root, "HEAD") == []
    ds.snapshot_path(root).unlink()
    assert verify_against(root, "HEAD") == []
