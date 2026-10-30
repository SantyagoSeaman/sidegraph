"""``sidegraph-verify`` snapshot layer on ``verifications/packed.shard``: damaged lines,
conflicts, dangling subjects, the marker requirement, and a non-regular or linked file.

# see design/superpowers/specs/2026-10-29-packed-drift-checks-design.md (§4, §8)
"""

from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path

import pytest

from sidegraph.store_format import marker_text
from sidegraph.store_layout import symlinked_internals
from sidegraph.verify import (
    DANGLING_DRIFT_CHECK,
    DUPLICATE_ULID,
    PACKED_CHECKS_WITHOUT_REQUIREMENT,
    PARSE_ERROR,
    REQUIREMENT_ABOVE_VERIFIER,
    SYMLINKED_STORE_ENTRY,
    verify_snapshot,
)
from tests.store_format2_support import (
    check_body,
    codes,
    decision_body,
    make_store,
    put_check,
    put_file,
    put_packed,
)

D1 = "decision-one"
D2 = "decision-two"
A = "01ARZ3NDEKTSV4RRFFQ69G5F05"
B = "01ARZ3NDEKTSV4RRFFQ69G5F09"
C = "01ARZ3NDEKTSV4RRFFQ69G5F01"
PACKED = "verifications/packed.shard"


@pytest.fixture
def store(tmp_path: Path) -> Path:
    """A major-2 store requiring 0.22.0 with decisions D1 and D2."""
    root = make_store(tmp_path)
    (root / "format").write_text(marker_text(2, "0.22.0", 64), encoding="utf-8")
    put_file(root, "decision", decision_body(D1))
    put_file(root, "decision", decision_body(D2))
    return root


def _lint(store: Path) -> list:
    """The findings, less the running version being older than the 0.22.0 fixture marker."""
    return [f for f in verify_snapshot(store) if f.code != REQUIREMENT_ABOVE_VERIFIER]


def _of(findings: list, code: str) -> list:
    return [f for f in findings if f.code == code]


@pytest.mark.parametrize(
    "line",
    [
        b"not json",
        b'{"id": "' + A.encode() + b'"}',  # unselectable: no decision id
        b'{"decision_id": "decision-one", "id": "' + A.encode() + b'"}',  # invalid body
    ],
)
def test_damaged_line_is_parse_error(store, line):
    put_packed(store, [check_body(B, D1), line])
    (hit,) = _of(verify_snapshot(store), PARSE_ERROR)
    assert hit.path.endswith(f"{PACKED}:2")


def test_unpackable_line_is_parse_error(store):
    # NaN never equals itself, so such a line could never pass a survival comparison.
    body = check_body(A, D1)
    body["extra"] = float("nan")
    put_packed(store, [json.dumps(body).encode()])
    (hit,) = _of(verify_snapshot(store), PARSE_ERROR)
    assert hit.path.endswith(f"{PACKED}:1")


def test_conflict_between_lines_is_duplicate_ulid(store):
    put_packed(store, [check_body(A, D1), check_body(A, D1, rationale="Different.")])
    (hit,) = _of(verify_snapshot(store), DUPLICATE_ULID)
    assert hit.path.endswith(f"{PACKED}:1")
    assert f"{PACKED}:2" in hit.detail


def test_conflict_between_line_and_file_is_duplicate_ulid(store):
    put_check(store, check_body(A, D1))
    put_packed(store, [check_body(A, D1, rationale="Different.")])
    (hit,) = _of(verify_snapshot(store), DUPLICATE_ULID)
    assert f"verifications/{A}.json" in hit.detail


def test_conflict_is_judged_across_decisions(store):
    put_packed(store, [check_body(A, D1), check_body(A, D2)])
    assert len(_of(verify_snapshot(store), DUPLICATE_ULID)) == 1


def test_equal_duplicates_are_clean(store):
    put_check(store, check_body(A, D1))
    put_packed(store, [check_body(A, D1), check_body(A, D1), check_body(B, D2)])
    assert _lint(store) == []


def test_dangling_line(store):
    put_packed(store, [check_body(A, "no-such-decision")])
    (hit,) = _of(verify_snapshot(store), DANGLING_DRIFT_CHECK)
    assert hit.path.endswith(f"{PACKED}:1")


@pytest.mark.parametrize(
    ("marker", "expected"),
    [
        (marker_text(1, "0.16.0", None), True),
        (marker_text(1, "0.22.0", None), True),  # the requirement alone is not enough
        (marker_text(2, "0.21.0", 64), True),
        (marker_text(2, "0.22.0", 64), False),
        (marker_text(2, "0.23.1", 64), False),
    ],
)
def test_packed_without_requirement(store, marker, expected):
    (store / "format").write_text(marker, encoding="utf-8")
    put_packed(store, [check_body(A, D1)])
    found = _of(verify_snapshot(store), PACKED_CHECKS_WITHOUT_REQUIREMENT)
    assert bool(found) is expected


def test_no_packed_file_needs_no_requirement(store):
    (store / "format").write_text(marker_text(2, "0.21.0", 64), encoding="utf-8")
    assert PACKED_CHECKS_WITHOUT_REQUIREMENT not in codes(verify_snapshot(store))


@pytest.mark.parametrize("shape", ["fifo", "dir"])
def test_non_regular_packed_is_parse_error(store, shape):
    target = store / PACKED
    target.parent.mkdir(exist_ok=True)
    if shape == "fifo":
        if sys.platform == "win32":
            pytest.skip("no FIFOs on Windows")
        os.mkfifo(target)
    else:
        target.mkdir()
    out: list = []
    thread = threading.Thread(target=lambda: out.append(verify_snapshot(store)), daemon=True)
    thread.start()
    thread.join(5)
    if thread.is_alive():
        if shape == "fifo":
            os.close(os.open(target, os.O_RDWR))  # unblock the stuck reader
            thread.join(2)
        pytest.fail("verify_snapshot blocked on a non-regular packed.shard")
    (hit,) = _of(out[0], PARSE_ERROR)
    assert hit.path.endswith(PACKED)


def test_symlinked_packed_is_reported(store):
    real = store / "real.shard"
    real.write_bytes(b"")
    (store / "verifications").mkdir(exist_ok=True)
    try:
        (store / PACKED).symlink_to(real)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    assert symlinked_internals(store) == [PACKED]
    found = _of(verify_snapshot(store), SYMLINKED_STORE_ENTRY)
    assert [f.path for f in found] == [str(store / PACKED)]


def test_damaged_duplicates_report_both_findings(store):
    first = check_body(A, D1, rationale="")  # invalid: an empty rationale
    second = check_body(A, D1, rationale="", required_files=[])
    put_packed(store, [first, second])
    found = _lint(store)
    assert len(_of(found, PARSE_ERROR)) == 2
    assert len(_of(found, DUPLICATE_ULID)) == 1


@pytest.mark.skipif(sys.platform == "win32", reason="no POSIX modes on Windows")
def test_unreadable_packed_is_parse_error(store):
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("root reads any file")
    path = put_packed(store, [check_body(A, D1)])
    path.chmod(0)
    try:
        found = _lint(store)
    finally:
        path.chmod(0o644)
    (hit,) = _of(found, PARSE_ERROR)
    assert hit.path.endswith(PACKED)


def test_linked_check_file_does_not_hide_packed_findings(store):
    real = store / "real.json"
    real.write_text("{}", encoding="utf-8")
    (store / "verifications").mkdir(exist_ok=True)
    try:
        (store / "verifications" / f"{B}.json").symlink_to(real)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    put_packed(store, [b"not json"])
    found = _lint(store)
    assert codes(found) >= {SYMLINKED_STORE_ENTRY, PARSE_ERROR}


@pytest.mark.parametrize(
    ("marker", "remedy", "absent"),
    [
        (marker_text(2, "0.21.0", 64), "sidegraph-require --sidegraph 0.22.0", "sidegraph-migrate"),
        (
            marker_text(1, "0.16.0", None),
            "run `sidegraph-migrate`, or remove verifications/packed.shard",
            "sidegraph-require",
        ),
    ],
)
def test_packed_without_requirement_remedy_fits_the_format(store, marker, remedy, absent):
    """On a format-1 store ``sidegraph-require`` cannot clear the finding, so the remedy there
    is the migration (or removing the file)."""
    (store / "format").write_text(marker, encoding="utf-8")
    put_packed(store, [check_body(A, D1)])
    (hit,) = _of(verify_snapshot(store), PACKED_CHECKS_WITHOUT_REQUIREMENT)
    assert remedy in hit.detail and absent not in hit.detail
