"""``sidegraph-verify --against REF`` on a packed drift-check fold: rules V1-V5 judge check
files and ``verifications/packed.shard`` lines as occurrences (survival, provenance, deadness
against the new tree).

Each test names the spec case it carries (V1-V14). The docstring of a case that already passes
before the rules existed says so; it guards behaviour, and its evidence is the named mutant.

# see design/superpowers/specs/2026-10-29-packed-drift-checks-design.md (§3, §9, §12)
"""

# ruff: noqa: F811  (imported fixtures are redefined as test arguments)
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from sidegraph.store import Store
from sidegraph.store_format import encode_body
from sidegraph.verify import (
    ILLEGAL_DELETION,
    ILLEGAL_FIELD_CHANGE,
    PACKED_CHECK_WITHOUT_SOURCE,
    Violation,
    verify_against,
)
from tests.store_format2_support import (
    OLD,
    check_body,
    codes,
    commit_all,
    decision_body,
    format1_committed,
    git,
    git_env,
    git_repo,  # noqa: F401  (pytest fixture)
    make_store,
    pin_version_0_22,  # noqa: F401  (pytest fixture)
    put_check,
    put_file,
    put_packed,
)

S1 = "01ARZ3NDEKTSV4RRFFQ69G5F01"
S5 = "01ARZ3NDEKTSV4RRFFQ69G5F05"
S9 = "01ARZ3NDEKTSV4RRFFQ69G5F09"
PACKED = "verifications/packed.shard"


def rel(cid: str) -> str:
    return f"verifications/{cid}.json"


def line(body: dict) -> bytes:
    return encode_body(body).encode("utf-8")


def base(repo: Path, *, files=(), lines=None, decisions=(("D1", "accepted"),)) -> tuple[Path, str]:
    """A committed format-2 store (REF): the decisions, the check files and packed lines."""
    store = make_store(repo)
    for did, status in decisions:
        put_file(store, "decision", decision_body(did, status=status))
    for body in files:
        put_check(store, body)
    if lines is not None:
        put_packed(store, lines)
    return store, commit_all(repo, "ref", date=OLD)


def against(store: Path, ref: str) -> list[Violation]:
    return verify_against(store, ref)


def at(found: list[Violation], code: str) -> list[str]:
    return [v.path for v in found if v.code == code]


def test_v1_compaction_commit_is_clean(git_repo: Path, pin_version_0_22: None) -> None:
    """V1: a real fold (C2 packed, the older C1 dropped) judged against the commit before it.
    Red before the change: every check-file deletion was illegal."""
    store, ref = base(
        git_repo,
        files=[check_body(S1, "D1"), check_body(S5, "D1"), check_body(S9, "D2")],
        decisions=(("D1", "accepted"), ("D2", "accepted")),
    )
    with Store(store) as s:
        report = s.maintain(settled_days=0, dry_run=False, any_branch=True, fold_checks=True)
    assert report.checks_packed and report.checks_dropped
    assert not (store / rel(S1)).exists()
    assert against(store, ref) == []


def test_v2_deleting_the_selected_check_is_illegal(git_repo: Path) -> None:
    """V2: passes before the change (every deletion was illegal); its evidence is the mutant that
    drops the deadness requirement from survival."""
    store, ref = base(git_repo, files=[check_body(S5, "D1")])
    (store / rel(S5)).unlink()
    found = against(store, ref)
    assert at(found, ILLEGAL_DELETION) == [f".sidegraph/{rel(S5)}"]


def test_v3_deleting_an_older_check_is_clean(git_repo: Path) -> None:
    """V3: red before the change (the deletion was illegal)."""
    store, ref = base(git_repo, files=[check_body(S1, "D1"), check_body(S5, "D1")])
    (store / rel(S1)).unlink()
    assert against(store, ref) == []


def test_v4_a_bound_file_with_no_body_id_is_protected_by_its_stem(git_repo: Path) -> None:
    """V4: passes before the change; evidence is the mutant keying survival by body id."""
    bound = check_body(S9, "D1")
    del bound["id"]
    store, ref = base(git_repo, files=[check_body(S5, "D1")])
    put_check(store, bound, name=S9)
    ref = commit_all(git_repo, "bound", date=OLD)
    (store / rel(S9)).unlink()
    assert at(against(store, ref), ILLEGAL_DELETION) == [f".sidegraph/{rel(S9)}"]


def test_v5_an_edited_packed_line_has_no_source(git_repo: Path) -> None:
    """V5: red before the change (the packed file was not read)."""
    store, ref = base(git_repo, files=[check_body(S5, "D1")])
    (store / rel(S5)).unlink()
    put_packed(store, [check_body(S5, "D1", rationale="edited")])
    found = against(store, ref)
    assert codes(found) == {PACKED_CHECK_WITHOUT_SOURCE, ILLEGAL_DELETION}
    assert at(found, ILLEGAL_DELETION) == [f".sidegraph/{rel(S5)}"]


def test_v6_a_hand_added_line_has_no_source(git_repo: Path) -> None:
    """V6: red before the change."""
    store, ref = base(git_repo)
    put_packed(store, [check_body(S5, "D1")])
    found = against(store, ref)
    assert codes(found) == {PACKED_CHECK_WITHOUT_SOURCE}
    assert found[0].path == f".sidegraph/{PACKED}:1"


def test_v7_recorded_and_packed_in_one_diff_has_no_source(git_repo: Path) -> None:
    """V7: the crash-after-publish state of a check that REF never held: file plus equal line.
    Red before the change."""
    store, ref = base(git_repo)
    body = check_body(S5, "D1")
    put_check(store, body)
    put_packed(store, [body])
    assert codes(against(store, ref)) == {PACKED_CHECK_WITHOUT_SOURCE}


def test_v8_rollback_is_clean(git_repo: Path) -> None:
    """V8: passes before the change; evidence is the mutant counting packed lines only."""
    body = check_body(S5, "D1")
    store, ref = base(git_repo, lines=[body])
    (store / PACKED).unlink()
    put_check(store, body)
    assert against(store, ref) == []


def test_v9_deleting_every_check_of_an_open_decision_is_illegal(git_repo: Path) -> None:
    """V9: passes before the change; evidence is the mutant judging deadness in REF."""
    store, ref = base(git_repo, files=[check_body(S1, "D1"), check_body(S5, "D1")])
    (store / rel(S1)).unlink()
    (store / rel(S5)).unlink()
    found = against(store, ref)
    assert sorted(at(found, ILLEGAL_DELETION)) == [f".sidegraph/{rel(S1)}", f".sidegraph/{rel(S5)}"]


def test_v10_deleting_an_undecodable_file_is_clean(git_repo: Path) -> None:
    """V10: red before the change."""
    store, ref = base(git_repo, files=[check_body(S5, "D1")])
    (store / rel(S1)).write_bytes(b"\xff not json")
    ref = commit_all(git_repo, "junk", date=OLD)
    (store / rel(S1)).unlink()
    assert against(store, ref) == []


def test_v10b_removing_an_undecodable_line_is_clean(git_repo: Path) -> None:
    """V10b: passes before the change (the packed file was ignored); evidence is the mutant that
    applies survival to unselectable occurrences."""
    good = check_body(S5, "D1")
    store, ref = base(git_repo, lines=[b"{broken", good])
    put_packed(store, [good])
    assert against(store, ref) == []


def test_v11_a_modified_check_file_is_illegal(git_repo: Path) -> None:
    """V11: a guard; passes before the change."""
    store, ref = base(git_repo, files=[check_body(S5, "D1")])
    put_check(store, check_body(S5, "D1", rationale="changed"))
    assert at(against(store, ref), ILLEGAL_FIELD_CHANGE) == [f".sidegraph/{rel(S5)}"]


def test_v12_format1_deleted_check_file_is_still_illegal(git_repo: Path) -> None:
    """V12: a guard; passes before the change."""
    store = format1_committed(git_repo)
    put_check(store, check_body(S5, "D2"))
    ref = commit_all(git_repo, "check", date=OLD)
    (store / rel(S5)).unlink()
    assert at(against(store, ref), ILLEGAL_DELETION) == [f".sidegraph/{rel(S5)}"]


def test_v13_replacing_one_side_of_a_conflict_is_illegal(git_repo: Path) -> None:
    """V13: REF holds file A and line B under one key; the diff makes the line equal A. Red before
    the change (the packed file was ignored)."""
    a = check_body(S5, "D1", rationale="A")
    b = check_body(S5, "D1", rationale="B")
    store, ref = base(git_repo, files=[a], lines=[b])
    put_packed(store, [a])
    found = against(store, ref)
    assert codes(found) == {ILLEGAL_DELETION}
    assert at(found, ILLEGAL_DELETION) == [f".sidegraph/{PACKED}"]


def test_v14_an_unchanged_nan_file_is_no_finding(git_repo: Path) -> None:
    """V14: passes before the change; evidence is the mutant judging survival by strict body
    equality only (a kept NaN never equals itself)."""
    store, ref = base(git_repo, files=[check_body(S5, "D1", extra=float("nan"))])
    assert against(store, ref) == []


def test_v1_symlink_replaced_by_file_is_illegal(git_repo: Path) -> None:
    """A REF check-file symlink replaced by a regular file (git status T) stays a modification:
    only A and D leave the old branch."""
    store, _ = base(git_repo)
    folder = store / "verifications"
    folder.mkdir(exist_ok=True)
    os.symlink("elsewhere.json", folder / f"{S5}.json")
    ref = commit_all(git_repo, "link", date=OLD)
    (folder / f"{S5}.json").unlink()
    put_check(store, check_body(S5, "D1"))
    assert at(against(store, ref), ILLEGAL_FIELD_CHANGE) == [f".sidegraph/{rel(S5)}"]


def test_a_terminal_decision_in_the_new_tree_frees_its_checks(git_repo: Path) -> None:
    """Rule (b): the decision is terminal in the new tree, so deleting its selected check is
    legal. Red before the change."""
    store, ref = base(git_repo, files=[check_body(S5, "D1")])
    put_file(
        store,
        "decision",
        decision_body("D1", status="deprecated", valid_to="2026-03-01T00:00:00Z"),
    )
    (store / rel(S5)).unlink()
    assert against(store, ref) == []


def test_a_higher_key_in_the_packed_file_frees_the_older_file(git_repo: Path) -> None:
    """Rule (a): the newer check of the decision is a packed line sourced from a REF file."""
    newer = check_body(S9, "D1")
    store, ref = base(git_repo, files=[check_body(S1, "D1"), newer])
    (store / rel(S1)).unlink()
    (store / rel(S9)).unlink()
    put_packed(store, [newer])
    assert against(store, ref) == []


def fold_branch(repo: Path, store: Path, start: str, name: str, dirty: str) -> str:
    """Branch ``name`` off ``start``, fold with check ``dirty`` modified locally (so it stays),
    restore it and commit the fold."""
    git(repo, "switch", "-q", "-C", name, start)
    original = (store / rel(dirty)).read_text()
    (store / rel(dirty)).write_text(original + " ")
    with Store(store) as s:
        s.maintain(settled_days=0, dry_run=False, any_branch=True, fold_checks=True)
    (store / rel(dirty)).write_text(original)
    return commit_all(repo, name, date=OLD)


def test_both_sides_of_a_merged_fold_survive(git_repo: Path, pin_version_0_22: None) -> None:
    """Two branches each fold one of two checks; the merge resolves ``packed.shard`` to the union
    of lines and deletes both files. Judged against the base and against each parent, nothing is
    reported: every line has a source in each REF."""
    store, start = base(
        git_repo,
        files=[check_body(S1, "D1"), check_body(S5, "D2")],
        decisions=(("D1", "accepted"), ("D2", "accepted")),
    )
    x = fold_branch(git_repo, store, start, "x", S5)  # only S1 packs
    y = fold_branch(git_repo, store, start, "y", S1)  # only S5 packs
    git(git_repo, "switch", "-q", "x")
    merged = subprocess.run(
        ["git", "-C", str(git_repo), "merge", "--no-commit", "--no-ff", "y"],
        capture_output=True,
        env=git_env(),
        stdin=subprocess.DEVNULL,
        check=False,
    )
    assert merged.returncode != 0  # packed.shard was added on both sides
    ours = git(git_repo, "show", f"x:.sidegraph/{PACKED}").encode().splitlines()
    theirs = git(git_repo, "show", f"y:.sidegraph/{PACKED}").encode().splitlines()
    (store / PACKED).write_bytes(b"\n".join([*ours, *theirs]) + b"\n")
    for cid in (S1, S5):
        (store / rel(cid)).unlink(missing_ok=True)
    commit_all(git_repo, "merge", date=OLD)
    for ref in (start, x, y):
        assert against(store, ref) == [], ref


def test_a_higher_key_that_is_unselectable_does_not_kill_the_older_check(git_repo: Path) -> None:
    """Deadness counts selectable occurrences only: a higher stem holding a non-object body, and
    another holding an unsafe decision id, a stem and a packed id that are no check id, never
    make S1 dead. A guard with no mutant: an unselectable occurrence carries no decision id,
    so counting it in rule (a) changes nothing today."""
    store, ref = base(git_repo, files=[check_body(S1, "D1")])
    (store / rel(S5)).write_text("[1, 2]\n", encoding="utf-8")
    (store / rel(S9)).write_text(json.dumps(check_body(S9, "../D1")) + "\n", encoding="utf-8")
    put_check(store, check_body("zzz", "D1"))  # a stem that is no check id: unselectable
    put_packed(store, [check_body("zzz-not-an-id", "D1")])  # a line whose id is no check id
    (store / rel(S1)).unlink()
    assert at(against(store, ref), ILLEGAL_DELETION) == [f".sidegraph/{rel(S1)}"]


def test_a_git_error_listing_ref_is_an_operational_error(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failing ``git ls-tree`` is not an empty REF (which would silence every survival check)."""
    store, ref = base(git_repo, files=[check_body(S5, "D1")])
    real = subprocess.run

    def broken(cmd, *a, **kw):  # type: ignore[no-untyped-def]
        if cmd[:2] == ["git", "ls-tree"]:
            return subprocess.CompletedProcess(cmd, 128, b"", b"fatal: bad object")
        return real(cmd, *a, **kw)

    monkeypatch.setattr(subprocess, "run", broken)
    with pytest.raises(ValueError, match="bad object"):
        against(store, ref)


def test_a_missing_blob_is_an_operational_error(
    git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``git cat-file --batch`` prints ``<oid> missing`` and exits 0; that is a ValueError, not an
    IndexError."""
    store, ref = base(git_repo, files=[check_body(S5, "D1")])
    real = subprocess.run

    def missing(cmd, *a, **kw):  # type: ignore[no-untyped-def]
        if cmd[:3] == ["git", "cat-file", "--batch"]:
            return subprocess.CompletedProcess(cmd, 0, b"abc missing\n", b"")
        return real(cmd, *a, **kw)

    monkeypatch.setattr(subprocess, "run", missing)
    with pytest.raises(ValueError, match="did not return blob"):
        against(store, ref)


def test_a_symlinked_verifications_directory_is_not_an_abort(git_repo: Path) -> None:
    """``verify_against`` stops at a symlinked store internal before any check rule runs, so the
    snapshot layer's finding is the only one: the behaviour before the check rules."""
    store, ref = base(git_repo, files=[check_body(S5, "D1")])
    elsewhere = git_repo / "elsewhere"
    (store / "verifications").rename(elsewhere)
    os.symlink(elsewhere, store / "verifications")
    assert against(store, ref) == []


def test_a_verifications_file_is_reported_not_raised(git_repo: Path) -> None:
    """A ``verifications`` replaced by a regular file: the check rules judge an empty new
    inventory (the REF check is gone) instead of aborting on the listing."""
    store, ref = base(git_repo, files=[check_body(S5, "D1")])
    (store / rel(S5)).unlink()
    (store / "verifications").rmdir()
    (store / "verifications").write_bytes(b"not a folder\n")
    found = against(store, ref)
    assert rel(S5) in " ".join(at(found, ILLEGAL_DELETION))
