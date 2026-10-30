"""The shared occurrence model and the reader of packed drift checks.

# see design/superpowers/specs/2026-10-29-packed-drift-checks-design.md (§3, §4, §5)
"""

from __future__ import annotations

import json
import os
import sys
import threading

import pytest

from sidegraph import doctor, drift_checks
from sidegraph.schema import DriftCheck
from tests.drift_checks_support import (  # noqa: F401
    git,
    project,
    require_check,
)
from tests.store_format2_support import check_body, put_check, put_packed

# ruff: noqa: F811  (the imported ``project`` fixture is redefined as a test argument)

D1 = "decision-one"
D2 = "decision-two"


def cid(n: int) -> str:
    return f"01ARZ3NDEKTSV4RRFFQ69G5F{n:02d}"[:26].replace("O", "0")


S5, S9, S1, C1, C2 = cid(5), cid(9), cid(1), cid(21), cid(22)


@pytest.fixture
def store(tmp_path):
    path = tmp_path / ".sidegraph"
    (path / "verifications").mkdir(parents=True)
    return path


def test_fixture_ids_are_valid_checks():
    for value in (S5, S9, S1, C1, C2):
        DriftCheck.model_validate(check_body(value, D1))


def test_packed_line_is_selected(store):
    put_packed(store, [check_body(S5, D1)])
    selected = drift_checks.latest_checks(store)[D1]
    assert (selected.kind, selected.id, selected.conflict) == ("line", S5, False)
    assert selected.validated().id == S5


@pytest.mark.parametrize("file_id,line_id", [(S9, S5), (S5, S9)])
def test_highest_key_wins_both_orientations(store, file_id, line_id):
    put_check(store, check_body(file_id, D1))
    put_packed(store, [check_body(line_id, D1)])
    assert drift_checks.latest_checks(store)[D1].id == S9


def test_malformed_latest_line_keeps_advisory(store):
    put_check(store, check_body(S5, D1))
    put_packed(store, [check_body(S9, D1, rationale="")])
    selected = drift_checks.latest_checks(store)[D1]
    assert selected.id == S9
    with pytest.raises(ValueError):
        selected.validated()


def test_equal_file_and_line_are_one_check(store):
    put_check(store, check_body(S5, D1))
    put_packed(store, [check_body(S5, D1)])
    selected = drift_checks.latest_checks(store)[D1]
    assert selected.conflict is False
    assert selected.validated().id == S5


def test_differing_file_and_line_conflict(store):
    put_check(store, check_body(S5, D1))
    put_packed(store, [check_body(S5, D1, rationale="Different.")])
    selected = drift_checks.latest_checks(store)[D1]
    assert selected.conflict is True
    with pytest.raises(ValueError):
        selected.validated()


def test_conflict_across_decisions_between_file_and_line(store):
    put_check(store, check_body(S5, D1))
    put_packed(store, [check_body(S5, D2)])
    selected = drift_checks.latest_checks(store)
    assert selected[D1].conflict is True
    assert selected[D2].conflict is True


def test_conflict_across_decisions_between_two_lines(store):
    put_packed(store, [check_body(S5, D1), check_body(S5, D2)])
    selected = drift_checks.latest_checks(store)
    assert selected[D1].conflict is True
    assert selected[D2].conflict is True


def test_conflicting_keys_directly(store):
    file_vs_line = [
        drift_checks.file_occurrence(f"verifications/{S5}.json", _bytes(check_body(S5, D1))),
        *drift_checks.packed_occurrences(_bytes(check_body(S5, D2)) + b"\n"),
    ]
    assert drift_checks.conflicting_keys(file_vs_line) == {S5}
    line_vs_line = drift_checks.packed_occurrences(
        _bytes(check_body(S9, D1)) + b"\n" + _bytes(check_body(S9, D2)) + b"\n"
    )
    assert drift_checks.conflicting_keys(line_vs_line) == {S9}
    equal = drift_checks.packed_occurrences(_bytes(check_body(S1, D1)) + b"\n")
    equal += drift_checks.packed_occurrences(_bytes(check_body(S1, D1)) + b"\n")
    assert drift_checks.conflicting_keys(equal) == set()


def _bytes(body: dict) -> bytes:
    return json.dumps(body).encode()


def test_fold_between_listing_and_reads_keeps_newest(store, monkeypatch):
    put_packed(store, [check_body(C1, D1)])
    put_check(store, check_body(C2, D1))

    def fold(label: str) -> None:
        if label == "checks:before_file_reads":
            put_packed(store, [check_body(C2, D1)])
            (store / "verifications" / f"{C2}.json").unlink()

    monkeypatch.setattr(drift_checks, "seam", fold)
    assert drift_checks.latest_checks(store)[D1].id == C2


def _returns_within(seconds: float, call, release=None) -> tuple[bool, BaseException | None]:
    """Run ``call`` in a daemon thread; whether it returned in time, and what it raised."""
    outcome: list[BaseException | None] = []

    def run() -> None:
        try:
            call()
            outcome.append(None)
        except BaseException as exc:  # noqa: BLE001 - the caller inspects it
            outcome.append(exc)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(seconds)
    if thread.is_alive():
        if release is not None:
            release()  # unblock a read stuck on a FIFO so the thread can end
            thread.join(2)
        return False, None
    return True, outcome[0]


@pytest.mark.parametrize("shape", ["fifo", "dir", "symlink"])
def test_non_regular_packed_raises(store, shape):
    target = store / "verifications" / "packed.shard"
    release = None
    if shape == "fifo":
        if sys.platform == "win32":
            pytest.skip("no FIFOs on Windows")
        os.mkfifo(target)

        def release() -> None:
            os.close(os.open(target, os.O_RDWR))

    elif shape == "dir":
        target.mkdir()
    else:
        real = store / "real.shard"
        real.write_bytes(b"\n".join(map(_bytes, [check_body(S5, D1)])) + b"\n")
        try:
            target.symlink_to(real)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks unavailable")
    inventory: list[drift_checks.CheckInventory] = []
    returned, error = _returns_within(
        5, lambda: inventory.append(drift_checks.load_check_inventory(store)), release
    )
    assert returned, "load_check_inventory blocked on a non-regular packed.shard"
    assert error is None
    assert inventory[0].packed_problem == "not a regular file"
    returned, error = _returns_within(5, lambda: drift_checks.latest_checks(store), release)
    assert returned, "latest_checks blocked on a non-regular packed.shard"
    assert isinstance(error, ValueError)


def test_file_key_is_stem(store):
    put_check(store, check_body(S5, D1))
    put_check(store, check_body(S9, D1), name=S1)  # stem S1 < S5
    selected = drift_checks.latest_checks(store)[D1]
    assert selected.id == S5
    assert selected.validated().id == S5


def test_file_whose_body_id_differs_from_a_higher_stem_is_malformed(store):
    put_check(store, check_body(S5, D1))
    put_check(store, check_body(S1, D1), name=S9)
    selected = drift_checks.latest_checks(store)[D1]
    assert selected.id == S9
    with pytest.raises(ValueError):
        selected.validated()


def test_bom_file_is_unselectable(store):
    path = put_check(store, check_body(S9, D1))
    path.write_bytes(b"\xef\xbb\xbf" + path.read_bytes())
    put_check(store, check_body(S5, D1))
    inventory = drift_checks.load_check_inventory(store)
    bom = next(o for o in inventory.occurrences if o.path.endswith(f"{S9}.json"))
    assert bom.selectable is False and bom.key is None
    assert drift_checks.latest_checks(store)[D1].id == S5


def test_is_packable():
    assert drift_checks.is_packable({"a": 1, "b": "é"}) is True
    assert drift_checks.is_packable({"a": "\ud800"}) is False
    assert drift_checks.is_packable({"a\ud800": 1}) is False
    assert drift_checks.is_packable({"a": float("nan")}) is False
    assert drift_checks.is_packable({"a": 1e400}) is True


def test_invalid_lines_are_selected_before_validation_never_skipped(store):
    put_check(store, check_body(S5, D1))
    put_packed(store, [b"not json", b'{"id": "x"}', check_body(S9, D1, subject_digest="bad")])
    inventory = drift_checks.load_check_inventory(store)
    assert [o.selectable for o in inventory.occurrences if o.kind == "line"] == [
        False,
        False,
        True,
    ]
    assert drift_checks.latest_checks(store)[D1].id == S9


def test_inventory_reports_packed_bytes_and_line_numbers(store):
    path = put_packed(store, [check_body(S5, D1), b"", check_body(S9, D1)])
    inventory = drift_checks.load_check_inventory(store)
    assert inventory.packed == path.read_bytes()
    assert [o.line_no for o in inventory.occurrences] == [1, 3]


def test_doctor_names_packed_check_by_id(project, monkeypatch):
    root, store, decision = project
    require_check(root, decision.id)
    folder = store.path / "verifications"
    (path,) = list(folder.glob("*.json"))
    body = json.loads(path.read_text(encoding="utf-8"))

    def invalid(*_a, **_k):
        raise ValueError("invalid")

    # the file case
    git(root, "commit", "--allow-empty", "-q", "-m", "x")
    _drift(root)
    monkeypatch.setattr(doctor.drift_checks, "scope_leaves", invalid)
    file_entry = _entry(store.path)
    assert file_entry.check_id == body["id"]
    # the packed case: the same body, moved into the packed file
    path.unlink()
    put_packed(store.path, [body])
    packed_entry = _entry(store.path)
    assert packed_entry.check_id == body["id"] == file_entry.check_id


def _drift(root) -> None:
    (root / "code.py").write_text("def policy():\n    return 2\n", encoding="utf-8")
    git(root, "add", "code.py")
    git(root, "commit", "-q", "-m", "drift")


def _entry(store_path):
    batches = doctor.scan_code_drift(store_path).batches
    return next(entry for batch in batches for entry in batch.entries)


def test_upper_case_suffix_keys_by_stem():
    """On Windows the listing also returns ``<id>.JSON``; its key is the stem, as ``Path.stem``
    gave it before the packed reader."""
    data = json.dumps(check_body(S9, D1)).encode()
    upper = drift_checks.file_occurrence(f"verifications/{S9}.JSON", data)
    lower = drift_checks.file_occurrence(f"verifications/{S9}.json", data)
    assert upper.key == lower.key == S9 and upper.selectable


def test_check_file_names_follow_the_listing_case_rule(monkeypatch):
    """The listing, the REF reader and the maintenance notice share one name rule: the
    platform's glob rule (case-insensitive only where ``normcase`` folds case)."""
    assert drift_checks.is_check_file_name(f"{S9}.json")
    assert not drift_checks.is_check_file_name("packed.shard")
    assert drift_checks.is_check_file_name(f"{S9}.JSON") is (os.path.normcase("A") == "a")
    monkeypatch.setattr(os.path, "normcase", str.lower)  # Windows' rule
    assert drift_checks.is_check_file_name(f"{S9}.JSON")


@pytest.mark.parametrize("shape", ["file", "fifo"])
def test_unlistable_verifications_is_a_problem_not_an_error(tmp_path, shape):
    """A ``verifications`` entry that cannot be listed blocks the check fold through
    ``packed_problem``; it never raises out of the inventory."""
    store = tmp_path / ".sidegraph"
    store.mkdir()
    if shape == "fifo":
        if sys.platform == "win32":
            pytest.skip("no FIFOs on Windows")
        os.mkfifo(store / "verifications")
    else:
        (store / "verifications").write_bytes(b"not a folder")
    inventory = drift_checks.load_check_inventory(store)
    assert inventory.occurrences == [] and inventory.packed is None
    assert (inventory.packed_problem or "").startswith("unreadable: ")
    with pytest.raises(ValueError):
        drift_checks.latest_checks(store)


@pytest.mark.skipif(sys.platform == "win32", reason="no POSIX modes on Windows")
@pytest.mark.parametrize(
    "mode",
    [
        0o300,  # enterable, not listable: the packed lstat alone would succeed
        0o400,  # listable, not enterable: the listing succeeds and the packed lstat fails
    ],
)
def test_unreadable_verifications_is_a_problem_not_an_error(store, mode):
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("root reads any folder")
    put_check(store, check_body(S5, D1))
    folder = store / "verifications"
    folder.chmod(mode)
    try:
        inventory = drift_checks.load_check_inventory(store)
    finally:
        folder.chmod(0o755)
    assert (inventory.packed_problem or "").startswith("unreadable: ")
