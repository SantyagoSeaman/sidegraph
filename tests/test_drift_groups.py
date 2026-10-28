"""Grouped drift model: order, baseline blobs, diff facts, mode/type, deadline, malformed fields.
# see design/superpowers/specs/2026-10-27-grouped-drift-triage-design.md
"""

from __future__ import annotations

import os
import random
import subprocess

import pytest

from sidegraph import doctor, drift_checks, drift_groups
from tests.drift_checks_support import project  # noqa: F401
from tests.drift_groups_support import GroupRepo, entries_of, git, rid

# ruff: noqa: F811  (the imported ``project`` fixture is redefined as a test argument)


@pytest.fixture
def repo(tmp_path) -> GroupRepo:
    return GroupRepo(tmp_path)


def _finding_ids(scan, repo) -> set[str]:
    return {f.record_id for f in doctor._render_code_drift(scan, repo.store) if f.record_id}


def _ab(repo: GroupRepo) -> str:
    """A repo whose files A and B are committed; returns the capture commit."""
    return repo.commit({"A.py": "a = 1\n", "B.py": "b = 1\n", "C.py": "c = 1\n"}, "base")


def test_1_groups_by_file(repo):
    base = _ab(repo)
    for n in (1, 2, 3):
        repo.record(n, commit=base, files=["A.py"])
    repo.record(4, commit=base, files=["B.py"])
    repo.commit({"A.py": "a = 2\n", "B.py": "b = 2\n"})
    _scan, groups = repo.build()
    assert groups is not None
    by_file = entries_of(groups)
    assert set(by_file) == {"A.py", "B.py"}
    assert len(by_file["A.py"]) == 3 and len(by_file["B.py"]) == 1


def test_2_record_on_two_files_is_in_both_groups(repo):
    base = _ab(repo)
    repo.record(1, commit=base, files=["A.py", "B.py"])
    repo.commit({"A.py": "a = 2\n", "B.py": "b = 2\n"})
    scan, groups = repo.build()
    by_file = entries_of(groups)
    assert by_file["A.py"][rid(1)]["files"] == by_file["B.py"][rid(1)]["files"] == ["A.py", "B.py"]
    assert {r for g in by_file.values() for r in g} == _finding_ids(scan, repo)


def test_3_order(repo):
    base = _ab(repo)
    adr = repo.record(1, commit=base, files=["A.py"], kind="adr")
    lesson = repo.record(2, commit=base, files=["A.py"], kind="lesson")
    g1 = repo.record(3, commit=base, files=["A.py"], kind="gotcha")
    g2 = repo.record(4, commit=base, files=["A.py"], kind="gotcha")
    # B: one adr only (non-mistake group); C: one adr, one more adr -> bigger non-mistake group.
    repo.record(5, commit=base, files=["B.py"], kind="adr")
    repo.record(6, commit=base, files=["C.py"], kind="adr")
    repo.record(7, commit=base, files=["C.py"], kind="adr")
    repo.commit({"A.py": "a = 2\n", "B.py": "b = 2\n", "C.py": "c = 2\n"})
    _scan, groups = repo.build()
    out = groups.to_dict()["groups"]
    assert [g["file"] for g in out] == ["A.py", "C.py", "B.py"]
    # mistakes first (by id), then the adr, though the adr has the smallest id
    assert [r["record_id"] for r in out[0]["records"]] == [lesson, g1, g2, adr]


def test_3b_mistake_group_precedes_larger_non_mistake_group_and_ties_break_on_path(repo):
    base = _ab(repo)
    repo.record(1, commit=base, files=["A.py"], kind="gotcha")
    for n in (2, 3, 4):
        repo.record(n, commit=base, files=["B.py"], kind="adr")
    for n in (5, 6, 7):
        repo.record(n, commit=base, files=["C.py"], kind="adr")
    repo.commit({"A.py": "a = 2\n", "B.py": "b = 2\n", "C.py": "c = 2\n"})
    _scan, groups = repo.build()
    # A has a mistake; B and C tie on size, so the path decides.
    assert [g["file"] for g in groups.to_dict()["groups"]] == ["A.py", "B.py", "C.py"]


@pytest.mark.parametrize("partial", [False, True])
def test_4_every_finding_record_is_in_a_group(repo, monkeypatch, partial):
    base = _ab(repo)
    repo.record(1, commit=base, files=["A.py", "B.py"])
    repo.record(2, commit=base, files=["C.py"])
    repo.commit({"A.py": "a = 2\n", "B.py": "b = 2\n", "C.py": "c = 2\n"})
    if partial:
        monkeypatch.setattr(drift_groups, "GROUP_DEADLINE_SECONDS", 1e-9)
    scan, groups = repo.build()
    assert groups.partial is partial
    assert {r for g in entries_of(groups).values() for r in g} == _finding_ids(scan, repo)


def test_5_capture_basis_names_the_capture_blob(repo):
    base = _ab(repo)
    repo.record(1, commit=base, files=["A.py"])
    repo.commit({"A.py": "a = 2\n"})
    _scan, groups = repo.build()
    entry = entries_of(groups)["A.py"][rid(1)]
    assert entry["basis"] == "capture" and entry["capture_commit"] == base
    assert entry["diff"]["old_blob"] == git(repo.root, "rev-parse", f"{base}:A.py")
    assert entry["diff"]["new_blob"] == git(repo.root, "rev-parse", "HEAD:A.py")
    assert entry["diff"]["change"] == "content"
    assert (
        entry["diff"]["command"]
        == f"git diff {entry['diff']['old_blob']} {entry['diff']['new_blob']}"
    )


def _check_basis_fixture(project):
    from tests.drift_checks_support import require_check

    root, store, decision = project
    check = require_check(root, decision.id)
    return root, store, decision, check


def test_6_check_basis_names_the_stored_blob_even_when_checked_commit_is_absent(project, tmp_path):
    root, store, decision, check = _check_basis_fixture(project)
    stored = git(root, "rev-parse", "HEAD:code.py")
    git(root, "add", ".sidegraph")
    git(root, "commit", "-q", "-m", "publish checked state")
    git(root, "reset", "--soft", decision.provenance.commit)
    (root / "code.py").write_text("def policy():\n    return 2\n", encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "squashed implementation, check and later change")
    clone = tmp_path / "clone"
    git(tmp_path, "clone", "--no-local", "--single-branch", root.as_uri(), str(clone))
    absent = subprocess.run(
        ["git", "cat-file", "-e", check["checked_commit"]], cwd=clone, capture_output=True
    )
    assert absent.returncode != 0
    scan = doctor._scan_code_drift(
        doctor._iter_records(clone / ".sidegraph", "decisions"),
        doctor._binding_entries(clone / ".sidegraph"),
        doctor._iter_records(clone / ".sidegraph", "entities"),
        clone / ".sidegraph",
        clone,
    )
    groups = drift_groups.build(
        scan,
        doctor._iter_records(clone / ".sidegraph", "decisions"),
        doctor._binding_entries(clone / ".sidegraph"),
        doctor._iter_records(clone / ".sidegraph", "entities"),
        clone / ".sidegraph",
    )
    entry = entries_of(groups)["code.py"][decision.id]
    assert entry["basis"] == "check"
    assert entry["diff"]["old_blob"] == stored
    assert entry["checked_commit"] == check["checked_commit"]


def test_7_scope_basis_model(project):
    from tests.drift_checks_support import require_check
    from tests.test_doctor_code_drift import _anchor, _commit

    root, store, decision = project
    require_check(root, decision.id)
    stored = git(root, "rev-parse", "HEAD:code.py")
    _commit(root, "dependency.py", "dependency = 1\n")
    _anchor(store, decision, "dependency.py")

    def groups_now():
        scan = doctor._scan_code_drift(
            doctor._iter_records(store.path, "decisions"),
            doctor._binding_entries(store.path),
            doctor._iter_records(store.path, "entities"),
            store.path,
            root,
        )
        return entries_of(
            drift_groups.build(
                scan,
                doctor._iter_records(store.path, "decisions"),
                doctor._binding_entries(store.path),
                doctor._iter_records(store.path, "entities"),
                store.path,
            )
        )

    now = groups_now()
    unedited = now["code.py"][decision.id]
    assert unedited["basis"] == "scope"
    assert unedited["reason"] == "checked subject or anchor scope changed"
    assert unedited["diff"]["change"] == "none" and unedited["diff"]["command"] is None
    assert unedited["diff"]["change"] != "content"
    assert now["dependency.py"][decision.id]["diff"]["unavailable"] == (
        "no stored baseline for this file"
    )
    _commit(root, "code.py", "def policy():\n    return 2\n")
    edited = groups_now()["code.py"][decision.id]
    assert edited["diff"]["change"] == "content"
    head_blob = git(root, "rev-parse", "HEAD:code.py")
    assert edited["diff"]["command"] == f"git diff {stored} {head_blob}"


def test_8_capture_basis_for_outside_scope_reason(project):
    root, store, decision, _check = _check_basis_fixture(project)
    scan = doctor._scan_code_drift(
        doctor._iter_records(store.path, "decisions"),
        doctor._binding_entries(store.path),
        doctor._iter_records(store.path, "entities"),
        store.path,
        root,
    )
    commit = decision.provenance.commit
    batch = doctor.DriftBatch(
        commit,
        False,
        [doctor.DriftEntry("x", decision.id, ["code.py", "extra.py"])],
    )
    batches, _s, _u, _h = doctor._apply_drift_checks(
        [batch],
        {commit: [(store.path / "decisions" / "x.json", decision.id, {"code.py", "extra.py"})]},
        doctor._iter_records(store.path, "decisions"),
        doctor._binding_entries(store.path),
        doctor._iter_records(store.path, "entities"),
        scan.binding_statuses or {},
        store.path,
        root,
        None,
        lambda: None,
    )
    entry = batches[0].entries[0]
    assert entry.check_reason == "changed anchored file outside checked scope"
    assert entry.basis == "capture"


def test_10_deadline_marks_everything_unavailable_and_runs_no_git(repo, monkeypatch):
    base = _ab(repo)
    repo.record(1, commit=base, files=["A.py"])
    repo.commit({"A.py": "a = 2\n"})
    scan = repo.scan()
    decisions, bindings, entities = repo.records()
    monkeypatch.setattr(drift_groups, "GROUP_DEADLINE_SECONDS", 1e-9)
    real = subprocess.run
    seen: list[list[str]] = []

    def spy(argv, *a, **k):
        seen.append(argv)
        return real(argv, *a, **k)

    monkeypatch.setattr(subprocess, "run", spy)
    groups = drift_groups.build(scan, decisions, bindings, entities, repo.store)
    assert groups.partial is True
    entry = entries_of(groups)["A.py"][rid(1)]
    assert entry["diff"]["unavailable"] == "grouping deadline reached"
    assert [a for a in seen if a and a[0] == "git"] == []


def test_11_object_availability(repo, monkeypatch):
    base = _ab(repo)
    repo.record(1, commit=base, files=["A.py"])
    repo.record(2, commit=base, files=["B.py"])
    repo.record(3, commit=base, files=["C.py"])
    repo.commit({"A.py": "a = 2\n", "B.py": None, "D.py": "d = 1\n"})
    repo.record(4, commit=base, files=["D.py"])
    old_a = git(repo.root, "rev-parse", f"{base}:A.py")
    scan, groups = repo.build()
    by_file = entries_of(groups)
    assert by_file["A.py"][rid(1)]["diff"]["command"].startswith("git diff ")
    assert by_file["B.py"][rid(2)]["diff"]["unavailable"] == "file deleted at HEAD"
    assert by_file["D.py"][rid(4)]["diff"]["unavailable"] == "file absent at baseline"
    # An id the batch-check reports missing.
    real = drift_checks.git_bytes_input

    def hide(root, args, data, timeout):
        out = real(root, args, data, timeout)
        if args[:2] == ["cat-file", "--batch-check"]:
            lines = [
                f"{old_a} missing" if line.startswith(old_a) else line
                for line in out.decode().splitlines()
            ]
            return ("\n".join(lines) + "\n").encode()
        return out

    monkeypatch.setattr(drift_checks, "git_bytes_input", hide)
    decisions, bindings, entities = repo.records()
    missing = entries_of(drift_groups.build(scan, decisions, bindings, entities, repo.store))
    assert missing["A.py"][rid(1)]["diff"]["unavailable"] == "baseline object not in this clone"
    assert missing["A.py"][rid(1)]["diff"]["command"] is None


def test_21_input_order_does_not_change_output(repo):
    base = _ab(repo)
    for n in range(1, 7):
        repo.record(
            n, commit=base, files=[["A.py", "B.py", "C.py"][n % 3]], kind=["adr", "gotcha"][n % 2]
        )
    other = repo.commit({"D.py": "d = 1\n"}, "second base")
    repo.record(7, commit=other, files=["D.py", "A.py"])
    repo.commit({"A.py": "a = 2\n", "B.py": "b = 2\n", "C.py": "c = 2\n", "D.py": "d = 2\n"})
    scan = repo.scan()
    decisions, bindings, entities = repo.records()
    reference = drift_groups.build(scan, decisions, bindings, entities, repo.store).to_dict()
    for seed in range(5):
        rng = random.Random(seed)
        d, e = list(decisions), list(entities)
        rng.shuffle(d)
        rng.shuffle(e)
        keys = list(bindings)
        rng.shuffle(keys)
        b = {k: bindings[k] for k in keys}
        batches = [
            batch._replace(entries=rng.sample(batch.entries, len(batch.entries)))
            for batch in scan.batches
        ]
        rng.shuffle(batches)
        shuffled = scan._replace(batches=batches)
        assert drift_groups.build(shuffled, d, b, e, repo.store).to_dict() == reference


def test_27_mode_and_type_changes_under_capture_basis(repo):
    if git(repo.root, "config", "--bool", "core.filemode") == "false":
        pytest.skip("filesystem does not track executable modes")
    base = repo.commit({"mode.sh": "x\n", "link.txt": "target\n", "swap.txt": "s\n"}, "base")
    for n, name in enumerate(["mode.sh", "link.txt", "swap.txt"], 1):
        repo.record(n, commit=base, files=[name])
    os.chmod(repo.root / "mode.sh", 0o755)
    (repo.root / "link.txt").unlink()
    (repo.root / "link.txt").symlink_to("mode.sh")
    git(repo.root, "rm", "-q", "-f", "swap.txt")
    (repo.root / "swap.txt").mkdir()
    (repo.root / "swap.txt" / "inner").write_text("i\n")
    git(repo.root, "add", "-A")
    git(repo.root, "commit", "-q", "-m", "mode, symlink, directory")
    _scan, groups = repo.build()
    by_file = entries_of(groups)
    mode = by_file["mode.sh"][rid(1)]["diff"]
    assert (mode["change"], mode["command"], mode["old_mode"], mode["new_mode"]) == (
        "mode",
        None,
        "100644",
        "100755",
    )
    link = by_file["link.txt"][rid(2)]["diff"]
    assert link["change"] == "type" and link["unavailable"] is None
    swap = by_file["swap.txt"][rid(3)]["diff"]
    assert swap["change"] == "type" and swap["unavailable"] != "file deleted at HEAD"


def test_27_check_basis_on_an_unanchored_dependency(project):
    from tests.drift_checks_support import require_check
    from tests.test_doctor_code_drift import _commit

    root, store, decision = project
    _commit(root, "dependency.py", "dependency = 1\n")
    require_check(root, decision.id, files=["code.py", "dependency.py"])
    if git(root, "config", "--bool", "core.filemode") == "false":
        pytest.skip("filesystem does not track executable modes")
    os.chmod(root / "dependency.py", 0o755)
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "chmod dependency")
    scan = doctor._scan_code_drift(
        doctor._iter_records(store.path, "decisions"),
        doctor._binding_entries(store.path),
        doctor._iter_records(store.path, "entities"),
        store.path,
        root,
    )
    groups = drift_groups.build(
        scan,
        doctor._iter_records(store.path, "decisions"),
        doctor._binding_entries(store.path),
        doctor._iter_records(store.path, "entities"),
        store.path,
    )
    entry = entries_of(groups)["dependency.py"][decision.id]
    assert entry["basis"] == "check" and entry["diff"]["change"] == "mode"


def test_29_malformed_kind_and_title_do_not_raise(repo):
    base = _ab(repo)
    repo.record(1, commit=base, files=["A.py"], kind="gotcha")
    repo.record(2, commit=base, files=["A.py"], kind=["gotcha"], title={"x": 1})
    repo.commit({"A.py": "a = 2\n"})
    _scan, groups = repo.build()
    records = entries_of(groups)["A.py"]
    assert records[rid(2)]["kind"] is None and records[rid(2)]["title"] == ""
    order = [r["record_id"] for r in groups.to_dict()["groups"][0]["records"]]
    assert order == [rid(1), rid(2)]
    assert drift_groups.is_mistake(["gotcha"]) is False


# --- bounded blob reads -----------------------------------------------------------------


def _blobs(repo: GroupRepo, sizes: list[int]) -> dict[str, int]:
    """Write one blob per size (distinct content) and return ``{oid: size}``."""
    out = {}
    for n, size in enumerate(sizes):
        data = (f"{n}:" + "x" * size)[:size].encode().ljust(size, b"x")
        oid = (
            subprocess.run(
                ["git", "hash-object", "-w", "--stdin"],
                cwd=repo.root,
                input=data,
                capture_output=True,
                check=True,
            )
            .stdout.decode()
            .strip()
        )
        out[oid] = size
    return out


def test_blob_cap_sorted_oid_order(repo, monkeypatch):
    """Test 12 (blob-cap half): an over-cap blob is skipped; the total cap stops at the first
    id, in sorted object-id order, that would pass it, and everything after it is absent."""
    repo.commit({"a.txt": "a\n"})
    sized = _blobs(repo, [10, 10, 10, 10, 50])
    monkeypatch.setattr(drift_groups, "MAX_BLOB_BYTES", 20)
    got = drift_groups._read_candidate_blobs(repo.root, sized, lambda: 5.0)
    assert set(got) == {oid for oid, size in sized.items() if size <= 20}
    assert all(len(data) == sized[oid] for oid, data in got.items())
    monkeypatch.setattr(drift_groups, "MAX_TOTAL_BYTES", 25)
    ordered = sorted(oid for oid, size in sized.items() if size <= 20)
    got = drift_groups._read_candidate_blobs(repo.root, sized, lambda: 5.0)
    assert set(got) == set(ordered[:2])


def test_total_byte_cap_partial(repo, monkeypatch):
    """Test 30: the same ids are taken whatever the input order."""
    repo.commit({"a.txt": "a\n"})
    sized = _blobs(repo, [10, 10, 10, 10, 10, 10])
    monkeypatch.setattr(drift_groups, "MAX_TOTAL_BYTES", 35)
    expected = set(sorted(sized)[:3])
    for seed in range(5):
        keys = list(sized)
        random.Random(seed).shuffle(keys)
        shuffled = {k: sized[k] for k in keys}
        got = drift_groups._read_candidate_blobs(repo.root, shuffled, lambda: 5.0)
        assert set(got) == expected


# --- the region hint (Python) -----------------------------------------------------------

_PY = "def alpha():\n    return 1\n\n\ndef beta():\n    return 1\n"
_PY_BETA_EDITED = "def alpha():\n    return 1\n\n\ndef beta():\n    return 2\n"
_PY_ALPHA_EDITED = "def alpha():\n    return 2\n\n\ndef beta():\n    return 1\n"


def _hint(repo: GroupRepo, file: str = "m.py") -> str | None:
    _scan, groups = repo.build()
    return entries_of(groups)[file][rid(1)]["hint"]


def _py_case(repo, before, after, anchors=(("m.py", "alpha"),), file="m.py", **options):
    base = repo.commit({file: before}, "base")
    repo.record(1, commit=base, files=list(anchors), **options)
    repo.commit({file: after}, "drift")


def test_15_sibling_edit_gives_the_hint_and_never_changes_the_exit_code(repo):
    from tests.test_doctor_grouped_drift import _cli

    _py_case(repo, _PY, _PY_BETA_EDITED)
    assert _hint(repo) == "region-unchanged"
    code_with, _ = _cli(repo.store, "--check")
    real = drift_groups._hint_for
    drift_groups._hint_for = lambda *a, **k: False
    try:
        code_without, _ = _cli(repo.store, "--check")
    finally:
        drift_groups._hint_for = real
    assert code_with == code_without


def _row_decorator_edited(repo):
    _py_case(repo, "@deco\ndef alpha():\n    return 1\n", "@deco2\ndef alpha():\n    return 1\n")


def _row_multiline_decorator(repo):
    _py_case(
        repo,
        "@(\n    deco\n)\ndef alpha():\n    return 1\n\n\ndef beta():\n    return 1\n",
        "@(\n    deco\n)\ndef alpha():\n    return 1\n\n\ndef beta():\n    return 2\n",
    )


def _row_duplicate_bare_name(repo):
    twice = "def alpha():\n    return 1\n\n\ndef alpha():\n    return 2\n\n\nX = {}\n"
    _py_case(repo, twice, twice.replace("X = {}", "X = {1}"))


def _row_method_in_two_classes(repo):
    method = "    def alpha(self):\n        pass\n"
    both = f"class A:\n{method}\n\nclass B:\n{method}\n\nX = 1\n"
    _py_case(repo, both, both.replace("X = 1", "X = 2"), anchors=(("m.py", ".alpha()"),))


def _row_syntax_error_after(repo):
    _py_case(repo, _PY, _PY_BETA_EDITED + "def broken(:\n")


def _row_syntax_error_before(repo):
    _py_case(repo, _PY + "def broken(:\n", _PY_BETA_EDITED)


def _row_module_constant(repo):
    _py_case(
        repo, "CONST = 1\n" + _PY, "CONST = 1\n" + _PY_BETA_EDITED, anchors=(("m.py", "CONST"),)
    )


def _row_orphaned_sibling_anchor(repo):
    before = _PY + "\n\ndef gamma():\n    return 1\n"
    base = repo.commit({"m.py": before}, "base")
    repo.record(1, commit=base, files=[("m.py", "alpha"), ("m.py", "beta")])
    repo._rows[-1] = (rid(1), repo.entity("m.py", "beta"), "orphaned")
    repo.commit({"m.py": before.replace("gamma():\n    return 1", "gamma():\n    return 2")})


def _row_file_level_anchor(repo):
    _py_case(repo, _PY, _PY_BETA_EDITED, anchors=("m.py",))


def _row_file_absent_at_baseline(repo):
    base = repo.commit({"other.py": "x = 1\n"}, "base")
    repo.record(1, commit=base, files=[("m.py", "alpha")])
    repo.commit({"m.py": _PY}, "add m.py")
    repo.commit({"m.py": _PY_BETA_EDITED}, "edit m.py")


def _row_swift(repo):
    _py_case(repo, _PY, _PY_BETA_EDITED, anchors=(("m.swift", "alpha"),), file="m.swift")


def _row_lone_cr(repo):
    _py_case(repo, _PY, _PY_BETA_EDITED + "x = 1\ry = 2\n")


def _row_mode_only(repo):
    base = repo.commit({"m.py": _PY}, "base")
    repo.record(1, commit=base, files=[("m.py", "alpha")])
    os.chmod(repo.root / "m.py", 0o755)
    git(repo.root, "add", "-A")
    git(repo.root, "commit", "-q", "-m", "chmod")


def _row_anchored_function_edited(repo):
    _py_case(repo, _PY, _PY_ALPHA_EDITED)


def _row_tier_one_anchor(repo):
    _py_case(repo, _PY, _PY_BETA_EDITED, tier=1)


def _row_statuses_uncomputed(repo):
    _py_case(repo, _PY, _PY_BETA_EDITED)
    repo.write_index = lambda: None  # no index.db at all: statuses cannot be certified


_NEGATIVE_ROWS = [
    _row_anchored_function_edited,
    _row_decorator_edited,
    _row_multiline_decorator,
    _row_duplicate_bare_name,
    _row_method_in_two_classes,
    _row_syntax_error_after,
    _row_syntax_error_before,
    _row_module_constant,
    _row_orphaned_sibling_anchor,
    _row_statuses_uncomputed,
    _row_file_level_anchor,
    _row_file_absent_at_baseline,
    _row_swift,
    _row_lone_cr,
    _row_mode_only,
    _row_tier_one_anchor,
]


@pytest.mark.parametrize("row", _NEGATIVE_ROWS, ids=lambda r: r.__name__.removeprefix("_row_"))
def test_16_hint_negatives(repo, row):
    row(repo)
    _scan, groups = repo.build()
    entry = next(r for g in entries_of(groups).values() for r in g.values())
    assert entry["hint"] is None


@pytest.mark.parametrize(
    "name,source,edited",
    [
        ("alpha", _PY, _PY_BETA_EDITED),
        ("alpha()", _PY, _PY_BETA_EDITED),
        (
            ".run()",
            "class K:\n    def run(self):\n        pass\n\n\nX = 1\n",
            "class K:\n    def run(self):\n        pass\n\n\nX = 2\n",
        ),
        (
            "K.run",
            "class K:\n    def run(self):\n        pass\n\n    def other(self):\n        pass\n",
            "class K:\n    def run(self):\n        pass\n\n    def other(self):\n        1\n",
        ),
    ],
)
def test_17_name_forms_resolve(repo, name, source, edited):
    _py_case(repo, source, edited, anchors=(("m.py", name),))
    assert _hint(repo) == "region-unchanged"


def test_28_inspected_dependency_without_an_anchor_gets_no_hint(project):
    from sidegraph.integrity import VOLATILE_STALE_KEY
    from tests.drift_checks_support import require_check
    from tests.test_doctor_code_drift import _commit

    root, store, decision = project
    _commit(root, "dependency.py", "dependency = 1\n")
    require_check(root, decision.id, files=["code.py", "dependency.py"])
    store.set_meta(VOLATILE_STALE_KEY, "0")  # statuses computed, so H1 is what decides
    _commit(root, "dependency.py", "dependency = 2\n")
    scan = doctor._scan_code_drift(
        doctor._iter_records(store.path, "decisions"),
        doctor._binding_entries(store.path),
        doctor._iter_records(store.path, "entities"),
        store.path,
        root,
    )
    assert not scan.binding_statuses_uncomputed
    groups = drift_groups.build(
        scan,
        doctor._iter_records(store.path, "decisions"),
        doctor._binding_entries(store.path),
        doctor._iter_records(store.path, "entities"),
        store.path,
    )
    entry = entries_of(groups)["dependency.py"][decision.id]
    assert (entry["basis"], entry["diff"]["change"], entry["hint"]) == ("check", "content", None)


def test_12_hint_half_oversize_blob_gives_no_hint_but_keeps_the_command(repo, monkeypatch):
    _py_case(repo, _PY, _PY_BETA_EDITED)
    monkeypatch.setattr(drift_groups, "MAX_BLOB_BYTES", 10)
    _scan, groups = repo.build()
    entry = entries_of(groups)["m.py"][rid(1)]
    assert entry["hint"] is None and entry["diff"]["command"].startswith("git diff ")
    monkeypatch.setattr(drift_groups, "MAX_BLOB_BYTES", 1 << 20)
    monkeypatch.setattr(drift_groups, "MAX_TOTAL_BYTES", 10)
    _scan, groups = repo.build()
    assert entries_of(groups)["m.py"][rid(1)]["hint"] is None


# --- the region hint (Markdown) ----------------------------------------------------------

_MD_SIB = "# Target\ntext\n\n# Other\nx\n"


def _md_case(repo, before, after, name="Target"):
    _py_case(repo, before, after, anchors=(("doc.md", name),), file="doc.md")


_FENCE_IN_ITEM = "# Target\n\n- item\n\n  ```\n  # Fake\n  ```\n\npayload\n\n# Next\nz\n"
_QUOTED_AND_LISTED = (
    "# Target\n\n> # Note\n\nafter the quote\n\n- # item\n\nafter the list\n\n# Other\nx\n"
)
_MD_ROWS = [
    ("sibling_edited", _MD_SIB, _MD_SIB.replace("x\n", "y\n"), "Target", "region-unchanged"),
    ("own_section_edited", _MD_SIB, _MD_SIB.replace("text", "TEXT"), "Target", None),
    (
        "subsection_edited",
        "# Target\n## Sub\nbody\n\n# Other\nx\n",
        "# Target\n## Sub\nBODY\n\n# Other\nx\n",
        "Target",
        None,
    ),
    (
        "fence_in_list_item",
        _FENCE_IN_ITEM,
        _FENCE_IN_ITEM.replace("payload", "PAYLOAD"),
        "Target",
        None,
    ),
    (
        "setext_anchor",
        "Target\n======\ntext\n\n# Other\nx\n",
        "Target\n======\ntext\n\n# Other\ny\n",
        "Target",
        "region-unchanged",
    ),
    (
        "heading_in_html_block",
        "# Target\n\n<div>\n# x\n</div>\n\n# Other\ny\n",
        "# Target\n\n<div>\n# x\n</div>\n\n# Other\nz\n",
        "x",
        None,
    ),
    (
        "duplicate_heading",
        "# Same\na\n\n# Same\nb\n\n# Other\nx\n",
        "# Same\na\n\n# Same\nb\n\n# Other\ny\n",
        "Same",
        None,
    ),
    (
        "front_matter",
        "---\nk: v\n---\n# Target\ntext\n\n# Other\nx\n",
        "---\nk: v\n---\n# Target\ntext\n\n# Other\ny\n",
        "Target",
        "region-unchanged",
    ),
    (
        "quote_and_item_headings_text_after_quote",
        _QUOTED_AND_LISTED,
        _QUOTED_AND_LISTED.replace("after the quote", "AFTER THE QUOTE"),
        "Target",
        None,
    ),
    (
        "quote_and_item_headings_text_after_list",
        _QUOTED_AND_LISTED,
        _QUOTED_AND_LISTED.replace("after the list", "AFTER THE LIST"),
        "Target",
        None,
    ),
    (
        "anchored_heading_in_block_quote",
        "> # Target\n> body\n\n# Other\nx\n",
        "> # Target\n> body\n\n# Other\ny\n",
        "Target",
        None,
    ),
]


@pytest.mark.parametrize(
    "before,after,name,expected",
    [row[1:] for row in _MD_ROWS],
    ids=[row[0] for row in _MD_ROWS],
)
def test_18_markdown_hint(repo, before, after, name, expected):
    _md_case(repo, before, after, name)
    assert _hint(repo, "doc.md") == expected


def test_18_fence_in_list_item_span_covers_the_whole_section():
    spans = drift_groups._markdown_spans(_FENCE_IN_ITEM, ["Target"])
    assert spans is not None
    start, end = spans["Target"]
    lines = _FENCE_IN_ITEM.split("\n")
    assert lines[start] == "# Target" and "payload" in lines[start:end]
    assert "# Next" not in lines[start:end]


def _md_many(repo, count: int) -> None:
    base = repo.commit({"doc.md": _MD_SIB}, "base")
    for n in range(1, count + 1):
        repo.record(n, commit=base, files=[("doc.md", "Target")])
    repo.commit({"doc.md": _MD_SIB.replace("x\n", "y\n")}, "drift")


def test_18_each_blob_is_parsed_once_however_many_records_anchor_into_it(repo, monkeypatch):
    _md_many(repo, 5)
    calls: list[int] = []
    real = drift_groups._markdown_index
    monkeypatch.setattr(drift_groups, "_markdown_index", lambda t: calls.append(1) or real(t))
    _scan, groups = repo.build()
    hints = {r["hint"] for r in entries_of(groups)["doc.md"].values()}
    assert hints == {"region-unchanged"}
    assert len(calls) == 2  # the baseline blob and the HEAD blob, not two per record


def test_18_hint_parsing_counts_against_the_grouping_deadline(repo, monkeypatch):
    """A parse that outlasts the deadline stops the hint step: the rest get no hint and the
    result is partial, never a hang past the budget."""
    _md_many(repo, 3)
    clock = [0.0]
    monkeypatch.setattr(drift_groups.time, "monotonic", lambda: clock[0])
    real = drift_groups._markdown_index

    def slow(text):
        clock[0] += drift_groups.GROUP_DEADLINE_SECONDS + 1
        return real(text)

    monkeypatch.setattr(drift_groups, "_markdown_index", slow)
    _scan, groups = repo.build()
    assert groups.partial
    hints = [r["hint"] for r in entries_of(groups)["doc.md"].values()]
    assert hints.count("region-unchanged") <= 1 and None in hints


def test_18_a_last_parse_past_the_deadline_still_marks_the_result_partial(repo, monkeypatch):
    """No later check would notice an overrun by the final parse: the hint step checks the
    deadline once more after its loop, so a slow build is never reported complete."""
    _md_many(repo, 1)
    clock = [0.0]
    monkeypatch.setattr(drift_groups.time, "monotonic", lambda: clock[0])
    real = drift_groups._markdown_index
    parses: list[int] = []

    def slow(text):
        parses.append(1)
        if len(parses) == 2:  # the HEAD blob, the last parse of the build
            clock[0] += drift_groups.GROUP_DEADLINE_SECONDS + 1
        return real(text)

    monkeypatch.setattr(drift_groups, "_markdown_index", slow)
    _scan, groups = repo.build()
    assert len(parses) == 2
    assert groups.partial


def test_18_blocked_markdown_it_gives_no_hint_and_no_error(repo, monkeypatch):
    import sys

    _md_case(repo, _MD_SIB, _MD_SIB.replace("x\n", "y\n"))
    monkeypatch.setitem(sys.modules, "markdown_it", None)
    assert _hint(repo, "doc.md") is None


# --- review fixes: partial-clone gate, error reasons, mode/submodule rendering -------------


def _base_drift(repo):
    base = repo.commit({"A.py": "a = 1\n"}, "base")
    repo.record(1, commit=base, files=["A.py"])
    repo.commit({"A.py": "a = 2\n"}, "drift")
    return base


@pytest.mark.parametrize(
    "key,value",
    [("remote.origin.promisor", "true"), ("extensions.partialClone", "origin")],
)
def test_gate_recognises_promisor_remotes_and_partial_clone_config(repo, monkeypatch, key, value):
    _base_drift(repo)
    git(repo.root, "config", key, value)
    monkeypatch.setattr(drift_checks, "git_version", lambda *a, **k: (2, 44))
    budget = drift_groups._Budget(5.0)
    assert drift_groups._partial_clone_blocks_probes(repo.root, budget) is True
    monkeypatch.setattr(drift_checks, "git_version", lambda *a, **k: (2, 45))
    assert drift_groups._partial_clone_blocks_probes(repo.root, budget) is False


def test_gate_is_open_for_a_full_clone_on_old_git(repo, monkeypatch):
    _base_drift(repo)
    monkeypatch.setattr(drift_checks, "git_version", lambda *a, **k: (2, 44))
    assert drift_groups._partial_clone_blocks_probes(repo.root, drift_groups._Budget(5.0)) is False


def test_blocked_gate_runs_before_any_tree_or_object_lookup(repo, monkeypatch):
    _base_drift(repo)
    git(repo.root, "config", "remote.origin.promisor", "true")
    monkeypatch.setattr(drift_checks, "git_version", lambda *a, **k: (2, 44))
    scan = repo.scan()
    decisions, bindings, entities = repo.records()
    real = subprocess.run
    seen: list[list[str]] = []

    def spy(argv, *a, **k):
        if isinstance(argv, list) and argv and argv[0] == "git":
            seen.append(argv)
        return real(argv, *a, **k)

    monkeypatch.setattr(subprocess, "run", spy)
    groups = drift_groups.build(scan, decisions, bindings, entities, repo.store)
    monkeypatch.setattr(subprocess, "run", real)
    verbs = {next(a for a in argv[1:] if not a.startswith("-")) for argv in seen}
    assert not verbs & {"ls-tree", "cat-file"}
    entry = entries_of(groups)["A.py"][rid(1)]
    assert entry["diff"]["unavailable"] == "diff unavailable (partial clone, Git older than 2.45)"
    assert entry["hint"] is None and groups.partial is True


def test_object_probe_failures_keep_an_explicit_reason(repo, monkeypatch):
    _base_drift(repo)

    def boom(*_a, **_k):
        raise ValueError("Git check unavailable")

    def late(*_a, **_k):
        raise drift_groups._Unavailable("grouping deadline reached")

    for patch, reason in ((boom, "git unavailable"), (late, "grouping deadline reached")):
        monkeypatch.setattr(drift_groups, "_batch_check", patch)
        _scan, groups = repo.build()
        diff = entries_of(groups)["A.py"][rid(1)]["diff"]
        assert diff["unavailable"] == reason
        assert groups.partial is True


def test_content_and_mode_change_renders_both(repo):
    if git(repo.root, "config", "--bool", "core.filemode") == "false":
        pytest.skip("filesystem does not track executable modes")
    base = repo.commit({"run.sh": "echo 1\n"}, "base")
    repo.record(1, commit=base, files=["run.sh"])
    (repo.root / "run.sh").write_text("echo 2\n")
    os.chmod(repo.root / "run.sh", 0o755)
    git(repo.root, "add", "-A")
    git(repo.root, "commit", "-q", "-m", "content and mode")
    scan, groups = repo.build()
    diff = entries_of(groups)["run.sh"][rid(1)]["diff"]
    assert diff["change"] == "content" and diff["command"].startswith("git diff ")
    assert (diff["old_mode"], diff["new_mode"]) == ("100644", "100755")
    text = "\n".join(drift_groups.render_groups_text(groups))
    assert "[diff 1]" in text and "mode changed 100644→100755" in text


def test_submodule_pointer_change_is_a_named_change(repo):
    first, second = "a" * 40, "b" * 40
    repo.commit({"x.txt": "x\n"}, "base")
    git(repo.root, "update-index", "--add", "--cacheinfo", f"160000,{first},sub")
    base = repo.commit({}, "add submodule")
    repo.record(1, commit=base, files=["sub"])
    git(repo.root, "update-index", "--cacheinfo", f"160000,{second},sub")
    repo.commit({}, "bump submodule")
    _scan, groups = repo.build()
    diff = entries_of(groups)["sub"][rid(1)]["diff"]
    assert diff["change"] == "submodule" and diff["command"] is None
    text = "\n".join(drift_groups.render_groups_text(groups))
    assert f"submodule commit changed {first[:8]}→{second[:8]}" in text


def test_current_object_missing_label(repo, monkeypatch):
    _base_drift(repo)
    new_blob = git(repo.root, "rev-parse", "HEAD:A.py")
    real = drift_checks.git_bytes_input

    def hide(root, args, data, timeout):
        out = real(root, args, data, timeout)
        if args[:2] == ["cat-file", "--batch-check"]:
            lines = [
                f"{new_blob} missing" if ln.startswith(new_blob) else ln
                for ln in out.decode().splitlines()
            ]
            return ("\n".join(lines) + "\n").encode()
        return out

    monkeypatch.setattr(drift_checks, "git_bytes_input", hide)
    _scan, groups = repo.build()
    assert entries_of(groups)["A.py"][rid(1)]["diff"]["unavailable"] == (
        "current object not in this clone"
    )


def test_capture_tree_failure_label(repo, monkeypatch):
    base = _base_drift(repo)
    real = drift_checks.tree_entries_all

    def flaky(root, commit, paths, timeout):
        if commit == base:
            raise ValueError("Git check unavailable")
        return real(root, commit, paths, timeout)

    monkeypatch.setattr(drift_checks, "tree_entries_all", flaky)
    _scan, groups = repo.build()
    assert entries_of(groups)["A.py"][rid(1)]["diff"]["unavailable"] == (
        "baseline object not in this clone"
    )


def test_total_cap_stops_at_the_first_overflow_not_later_fits(repo, monkeypatch):
    repo.commit({"a.txt": "a\n"})
    sized = _blobs(repo, [40, 3, 40, 3, 40, 3, 40, 3])
    ordered = sorted(sized)
    cap = 45
    total, expected = 0, set()
    for oid in ordered:
        if total + sized[oid] > cap:
            break
        total += sized[oid]
        expected.add(oid)
    skipping = set()
    total = 0
    for oid in ordered:
        if total + sized[oid] <= cap:
            total += sized[oid]
            skipping.add(oid)
    assert expected != skipping  # the premise: stopping and skipping differ here
    monkeypatch.setattr(drift_groups, "MAX_TOTAL_BYTES", cap)
    got = drift_groups._read_candidate_blobs(repo.root, sized, lambda: 5.0)
    assert set(got) == expected


# --- review round 2 ------------------------------------------------------------------------


def test_deadline_while_loading_hint_blobs_marks_the_result_partial(repo, monkeypatch):
    _py_case(repo, _PY, _PY_BETA_EDITED)

    def late(*_a, **_k):
        raise drift_groups._Unavailable("grouping deadline reached")

    monkeypatch.setattr(drift_groups, "_read_candidate_blobs", late)
    scan, groups = repo.build()
    assert groups.partial is True
    assert entries_of(groups)["m.py"][rid(1)]["hint"] is None


def test_tree_lookups_receive_the_deadline_callable_not_its_value(repo, monkeypatch):
    _base_drift(repo)
    seen = []
    real = drift_checks.tree_entries_all

    def spy(root, commit, paths, timeout):
        seen.append(timeout)
        return real(root, commit, paths, timeout)

    monkeypatch.setattr(drift_checks, "tree_entries_all", spy)
    repo.build()
    assert len(seen) == 2 and all(callable(t) for t in seen)


def test_directory_at_both_ends_is_named_not_all_null(repo, monkeypatch):
    """The scan never lists a bare directory, so the trees are faked to reach the branch."""
    base = _base_drift(repo)

    def trees(root, commit, paths, timeout):
        oid = ("a" if commit == base else "b") * 40
        return {path: (oid, "040000") for path in paths}

    monkeypatch.setattr(drift_checks, "tree_entries_all", trees)
    _scan, groups = repo.build()
    diff = entries_of(groups)["A.py"][rid(1)]["diff"]
    assert diff["change"] is None
    assert diff["unavailable"] == "path is a directory at both ends"


def test_unreadable_config_counts_as_partial_and_unreadable_version_as_old(repo, monkeypatch):
    def broken(*_a, **_k):
        raise ValueError("Git check unavailable")

    budget = drift_groups._Budget(5.0)
    monkeypatch.setattr(drift_checks, "partial_clone_configured", broken)
    monkeypatch.setattr(drift_checks, "git_version", lambda *a, **k: (2, 44))
    assert drift_groups._partial_clone_blocks_probes(repo.root, budget) is True
    monkeypatch.setattr(drift_checks, "partial_clone_configured", lambda *a, **k: True)
    monkeypatch.setattr(drift_checks, "git_version", broken)
    assert drift_groups._partial_clone_blocks_probes(repo.root, budget) is True
    monkeypatch.setattr(drift_checks, "git_version", lambda *a, **k: None)
    assert drift_groups._partial_clone_blocks_probes(repo.root, budget) is True
