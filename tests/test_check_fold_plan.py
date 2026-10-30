"""The drift-check fold planner: pure planning over occurrences, a dirty set and statuses.

# see design/superpowers/specs/2026-10-29-packed-drift-checks-design.md (§3, §4, §6.2)
"""

from __future__ import annotations

import json

from sidegraph.check_fold import DecisionState, decision_states, plan_check_fold
from sidegraph.drift_checks import file_occurrence, packed_occurrences
from sidegraph.store_format import encode_body, inventory_from_disk
from tests.store_format2_support import (
    check_body,
    decision_body,
    make_store,
    put_file,
    put_shard_line,
    validator,
)

REL = ".sidegraph"
D1, D2 = "decision-one", "decision-two"
OPEN = DecisionState(False, ("decisions/decision-one.json",))
TERMINAL = DecisionState(True, ("decisions/decision-one.json",))


def cid(n: int) -> str:
    return f"01ARZ3NDEKTSV4RRFFQ69G5F{n:02d}"[:26]


A, B, C = cid(1), cid(2), cid(3)


def fpath(key: str) -> str:
    return f"verifications/{key}.json"


def fdata(body: dict) -> bytes:
    return (json.dumps(body, indent=2) + "\n").encode("utf-8")


def fobs(body: dict, *, stem: str | None = None):
    return file_occurrence(fpath(stem or body["id"]), fdata(body))


def lines(*bodies: dict | bytes) -> bytes:
    return b"".join(
        (encode_body(b).encode("utf-8") if isinstance(b, dict) else b) + b"\n" for b in bodies
    )


def plan(files=(), packed: bytes | None = None, *, dirty=frozenset(), decisions=None, problem=None):
    occs = list(files) + (packed_occurrences(packed) if packed else [])
    return plan_check_fold(
        occs,
        dirty=dirty,
        store_rel=REL,
        decisions={D1: OPEN, D2: DecisionState(False, ("decisions/decision-two.json",))}
        if decisions is None
        else decisions,
        packed=packed,
        packed_problem=problem,
    )


def root(key: str) -> str:
    return f"{REL}/{fpath(key)}"


def test_packs_committed_files():
    p = plan([fobs(check_body(A, D1)), fobs(check_body(B, D2))])
    assert p.packed == [fpath(A), fpath(B)]
    assert p.dropped == [] and p.kept == [] and p.blocked is None and p.has_work()
    assert set(p.read) == {fpath(A), fpath(B)}
    assert p.read[fpath(A)] == fdata(check_body(A, D1))


def test_kept_reasons():
    files = {
        "untracked or modified": fobs(check_body(cid(4), D1)),
        "unselectable": file_occurrence(fpath(cid(5)), b"[1]"),
        "invalid check": fobs(check_body(cid(6), D1, schema_version=99)),
        "id differs from file name": fobs(check_body(cid(7), D1), stem=cid(9)),
        "not packable": fobs(check_body(cid(10), D1, note="lone \ud800")),
        "decision missing": fobs(check_body(cid(11), "ghost")),
    }
    p = plan(list(files.values()), dirty=frozenset({root(cid(4))}))
    assert dict((reason, path) for path, reason in p.kept) == {
        "untracked or modified": fpath(cid(4)),
        "unselectable": fpath(cid(5)),
        "invalid check": fpath(cid(6)),
        "id differs from file name": fpath(cid(9)),
        "not packable": fpath(cid(10)),
        "decision missing": fpath(cid(11)),
    }
    assert p.packed == [] and p.dropped == [] and not p.has_work()


def test_nan_value_is_not_packable():
    occ = file_occurrence(fpath(A), json.dumps(check_body(A, D1, note=float("nan"))).encode())
    p = plan([occ])
    assert p.kept == [(fpath(A), "not packable")] and p.packed == []


def test_drops_superseded_by_image_occurrence():
    p = plan([fobs(check_body(A, D1)), fobs(check_body(B, D1))])
    assert p.dropped == [fpath(A)] and p.packed == [fpath(B)]
    q = plan([fobs(check_body(A, D1))], lines(check_body(B, D1)))
    assert q.dropped == [fpath(A)] and q.packed == []
    assert q.image == lines(check_body(B, D1))


def test_superseder_outside_image_does_not_drop():
    older = fobs(check_body(A, D1))
    # dirty superseder
    p = plan([older, fobs(check_body(B, D1))], dirty=frozenset({root(B)}))
    assert p.packed == [fpath(A)] and p.dropped == []
    # kept invalid superseder
    p = plan([older, fobs(check_body(B, D1, schema_version=99))])
    assert p.packed == [fpath(A)] and p.dropped == []
    # kept (unselectable-by-decision) superseder: missing decision elsewhere is another decision
    p = plan([older, fobs(check_body(B, D1), stem=C)])
    assert p.packed == [fpath(A)] and p.dropped == []


def test_terminal_decision_drops():
    p = plan([fobs(check_body(A, D1))], decisions={D1: TERMINAL})
    assert p.dropped == [fpath(A)] and p.packed == [] and p.image == b""
    q = plan([], lines(check_body(B, D1)), decisions={D1: TERMINAL})
    assert q.dropped_lines == [B] and q.image == b"" and q.has_work()


def test_dirty_terminal_decision_does_not_drop():
    dirty = frozenset({f"{REL}/decisions/decision-one.json"})
    p = plan([fobs(check_body(A, D1))], dirty=dirty, decisions={D1: TERMINAL})
    assert p.packed == [fpath(A)] and p.dropped == []


def test_dead_line_dropped():
    p = plan([], lines(check_body(A, D1), check_body(B, D1)))
    assert p.dropped_lines == [A]
    assert p.image == lines(check_body(B, D1))
    assert p.has_work()


def test_equal_duplicate_lines_collapse_without_a_drop():
    body = check_body(A, D1)
    p = plan([], lines(body, body))
    assert p.image == lines(body) and p.dropped_lines == []


def test_blocking_damaged_line():
    p = plan([fobs(check_body(A, D1))], lines(check_body(B, D1, schema_version=99)))
    assert p.blocked and "damaged" in p.blocked
    assert p.packed == [] and p.dropped == [] and not p.has_work()


def test_dead_damaged_line_is_dropped():
    bad = check_body(A, D1, schema_version=99)
    p = plan([], lines(bad, check_body(B, D1)))
    assert p.blocked is None and p.dropped_lines == [A]
    assert p.image == lines(check_body(B, D1))
    q = plan([], lines(bad), decisions={D1: TERMINAL})
    assert q.blocked is None and q.dropped_lines == [A]


def test_unselectable_line_blocks():
    p = plan([], lines(b"not json", check_body(B, D1)))
    assert p.blocked and not p.has_work()
    q = plan([], lines(b"not json"), decisions={D1: TERMINAL})
    assert q.blocked


def test_blocking_conflict():
    other = check_body(A, D1, rationale="different")
    p = plan([fobs(check_body(A, D1))], lines(other))
    assert p.blocked and "conflict" in p.blocked
    q = plan([], lines(check_body(A, D1), other))
    assert q.blocked and "conflict" in q.blocked


def test_packed_problem_blocks():
    p = plan([fobs(check_body(A, D1))], problem="not a regular file")
    assert p.blocked and "not a regular file" in p.blocked and not p.has_work()


def test_misnamed_file_does_not_conflict_by_body_id():
    misnamed = fobs(check_body(B, D1), stem=A)  # file key A, body id B
    p = plan([misnamed], lines(check_body(B, D1, rationale="different")))
    assert p.blocked is None
    assert p.kept == [(fpath(A), "id differs from file name")]


def test_outside_git_keeps_all():
    p = plan_check_fold(
        [fobs(check_body(A, D1)), fobs(check_body(B, D1))],
        dirty=None,
        store_rel=REL,
        decisions={D1: TERMINAL},
        packed=None,
        packed_problem=None,
    )
    assert p.kept == [(fpath(A), "outside a git work tree"), (fpath(B), "outside a git work tree")]
    assert p.packed == [] and p.dropped == [] and p.blocked is None and not p.has_work()


def test_image_is_sorted_canonical_and_verbatim():
    old = check_body(A, D1)
    assert "declared_sections" not in old
    states = {D1: OPEN, D2: OPEN, "decision-three": OPEN}
    new = check_body(C, "decision-three")
    p = plan([fobs(new), fobs(old)], lines(check_body(B, D2)), decisions=states)
    assert p.image == lines(old, check_body(B, D2), new)
    assert b"declared_sections" not in p.image
    assert p.image.split(b"\n")[0].decode() == encode_body(old)
    assert p.current == lines(check_body(B, D2))


def test_decision_states_from_an_inventory(tmp_path):
    store = make_store(tmp_path)
    put_file(store, "decision", decision_body("open-one"))
    put_file(store, "decision", decision_body("old-one", status="rejected"))
    states = decision_states(inventory_from_disk(store, validate=validator))
    assert states["open-one"] == DecisionState(False, ("decisions/open-one.json",))
    assert states["old-one"].terminal and states["old-one"].paths == ("decisions/old-one.json",)
    assert set(states) == {"open-one", "old-one"}


def test_outside_git_wins_over_a_packed_problem():
    p = plan_check_fold(
        [fobs(check_body(B, D1)), fobs(check_body(A, D1))],
        dirty=None,
        store_rel=REL,
        decisions={D1: OPEN},
        packed=None,
        packed_problem="not a regular file",
    )
    assert p.blocked is None and not p.has_work()
    assert p.kept == [(fpath(A), "outside a git work tree"), (fpath(B), "outside a git work tree")]
    assert p.packed == [] and p.dropped == []


def test_terminal_statuses_match_the_store():
    from sidegraph import check_fold, store

    assert {s.value for s in store._TERMINAL_DECISION_STATUSES} == check_fold._TERMINAL


def test_decision_states_for_a_shard_held_decision(tmp_path):
    store = make_store(tmp_path)
    path = put_shard_line(store, "decision", "shard-one", decision_body("shard-one"))
    states = decision_states(inventory_from_disk(store, validate=validator))
    assert states["shard-one"] == DecisionState(False, (f"shards/{path.name}",))


def test_dead_line_leaves_read_without_the_packed_file():
    p = plan([fobs(check_body(C, D2))], lines(check_body(A, D1), check_body(B, D1)))
    assert p.dropped_lines == [A]
    assert set(p.read) == {fpath(C)}


def test_upper_case_suffix_file_packs():
    """``<id>.JSON`` (listed on Windows) has ``id == stem`` and packs like ``<id>.json``."""
    upper = file_occurrence(f"verifications/{A}.JSON", fdata(check_body(A, D1)))
    p = plan([upper])
    assert p.kept == [] and p.packed == [f"verifications/{A}.JSON"]


def test_skipped_files_are_kept_as_not_regular():
    skipped = ("verifications/" + B + ".json",)
    p = plan_check_fold(
        [fobs(check_body(A, D1))],
        dirty=frozenset(),
        store_rel=REL,
        decisions={D1: OPEN},
        packed=None,
        packed_problem=None,
        skipped=skipped,
    )
    assert p.kept == [(skipped[0], "not a regular file")] and p.packed == [fpath(A)]
    outside = plan_check_fold(
        [],
        dirty=None,
        store_rel=REL,
        decisions={},
        packed=None,
        packed_problem=None,
        skipped=skipped,
    )
    assert outside.kept == [(skipped[0], "outside a git work tree")]
