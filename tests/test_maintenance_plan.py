"""The maintenance planner: entry conditions, skips, key survival, eligibility, deletions
(T-C3 and the planner half of T-C1).

Every test builds a real store on disk and plans over its inventory; the planner is pure.
# see design/superpowers/specs/2026-10-20-packed-store-format-design.md (§6.2, §9 T-C1, T-C3)
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from sidegraph.maintenance import (
    Eligibility,
    EntryRefusal,
    LineMove,
    Plan,
    Skip,
    check_entry,
    plan_maintenance,
    self_healing_counts,
)
from sidegraph.store_format import (
    Inventory,
    bucket,
    bucket_name,
    encode_line,
    inventory_from_disk,
)
from sidegraph.store_rules import effective_all
from tests.store_format2_support import (
    bindings_body,
    decision_body,
    domain_body,
    entity_body,
    fact_body,
    make_store,
    put_archive,
    put_file,
    put_shard_line,
    validator,
)

N = 64
STORE_REL = ".sidegraph"
ELIG_ALL = Eligibility(0, 0, None, frozenset(), STORE_REL)
DAY = 86400


def inv_of(store: Path) -> Inventory:
    return inventory_from_disk(store, validate=validator)


def plan_of(store: Path, elig: Eligibility = ELIG_ALL) -> Plan:
    return plan_maintenance(inv_of(store), elig, N)


def line_of(t: str, i: str, body: object) -> bytes:
    return encode_line(t, i, body)


def kept_paths(p: Plan) -> list[str]:
    return [k for k, _ in p.kept]


def kept_reason(p: Plan, path: str) -> str:
    return dict(p.kept)[path]


def archive_line(t: str, body: dict) -> dict:
    return {"record_type": t, **body}


def other_bucket(i: str) -> int:
    return (bucket(i, N) + 1) % N


def shard_name_of(i: str) -> str:
    return bucket_name(bucket(i, N))


def misplaced_name(i: str) -> str:
    return bucket_name(other_bucket(i))


# -- Eligibility -------------------------------------------------------------------------------


def test_eligibility_without_clock_requires_zero_days() -> None:
    with pytest.raises(ValueError):
        Eligibility(7, 0, None, frozenset(), STORE_REL)
    assert Eligibility(0, 0, None, frozenset(), STORE_REL).eligible("facts/a.json")


def test_eligibility_prefixes_the_store_path() -> None:
    elig = Eligibility(
        1, 10 * DAY, {".sidegraph/facts/a.json": 0, "facts/b.json": 0}, frozenset(), STORE_REL
    )
    assert elig.eligible("facts/a.json")
    assert not elig.eligible("facts/b.json")  # a store-relative path is never looked up bare


# -- T-C3: entry conditions ----------------------------------------------------------------------


def _clean_store(tmp_path: Path) -> Path:
    store = make_store(tmp_path)
    put_shard_line(store, "fact", "01A", fact_body("01A"))
    return store


@pytest.mark.parametrize(
    ("state", "prefix"),
    [
        ("gap", "gap:"),
        ("bad_line_unrecoverable_id", "bad-shard-line:"),
        ("bad_line_recoverable_id", "bad-shard-line:"),
        ("conflict_region", "shard-conflict:"),
        ("case_collision_listing", "shard-name-case:"),
        ("case_collision_git", "shard-name-case:"),
        ("bucket_out_of_range", "bucket-out-of-range:"),
    ],
)
def test_entry_conditions_refuse(tmp_path: Path, state: str, prefix: str) -> None:  # T-C3
    store = _clean_store(tmp_path)
    git_paths: list[str] | None = None
    shards = store / "shards"
    if state == "gap":  # one identity, two shard lines with differing bodies
        put_shard_line(store, "fact", "01B", fact_body("01B"))
        put_shard_line(store, "fact", "01B", fact_body("01B", statement="other"))
    elif state == "bad_line_unrecoverable_id":
        put_shard_line(store, "fact", "01B", fact_body("01B"))
        path = shards / shard_name_of("01B")
        path.write_bytes(path.read_bytes() + b"this is not a shard line\n")
    elif state == "bad_line_recoverable_id":
        path = shards / shard_name_of("01B")
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(b'{"type":"fact","id":"01B","body":\n')
    elif state == "conflict_region":
        path = shards / shard_name_of("01A")
        good = path.read_bytes()
        path.write_bytes(b"<<<<<<< ours\n" + good + b"=======\n" + good + b">>>>>>> theirs\n")
    elif state == "case_collision_listing":
        (shards / "0A.shard").write_bytes(b"")
    elif state == "bucket_out_of_range":  # read without a marker's count, planned for n=16
        (shards / "3f.shard").write_bytes(b"")
        with pytest.raises(EntryRefusal) as refusal:
            check_entry(inventory_from_disk(store, validate=validator, assume_major=2), None, 16)
        assert any(r.startswith(prefix) for r in refusal.value.reasons)
        return
    else:
        git_paths = [".sidegraph/shards/0a.shard", ".sidegraph/shards/0A.shard"]
    with pytest.raises(EntryRefusal) as refusal:
        check_entry(inv_of(store), git_paths)
    assert any(r.startswith(prefix) for r in refusal.value.reasons), refusal.value.reasons


def test_entry_accepts_a_clean_store_and_a_stray(tmp_path: Path) -> None:
    store = _clean_store(tmp_path)
    put_shard_line(store, "fact", "01B", fact_body("01B"), bucket="stray.shard")
    put_shard_line(store, "fact", "01C", fact_body("01C"), bucket=misplaced_name("01C"))
    shards = [f".sidegraph/shards/{shard_name_of('01A')}", ".sidegraph/shards/stray.shard"]
    check_entry(inv_of(store), shards)  # misplaced lines and stray files are healed, not refused
    check_entry(inv_of(store), None)


# -- T-C3: skipped identities ----------------------------------------------------------------------

X = "01SKIPPED"
Y = "01OTHERFOLD"


def _skipped_fixture(store: Path, finding: str, where: str) -> tuple[str, str, list[str]]:
    """Build the skipped identity ``(type, id)`` with ``finding`` and its copy at ``where``;
    return the type, id and the store-relative paths of its per-record files and segments."""
    guarded: list[str] = []

    def place(t: str, body: object) -> None:
        name = {
            "bucket": None,
            "file_equal_to_shard": None,
            "misplaced": misplaced_name(X),
            "stray": "stray.shard",
        }[where]
        put_shard_line(store, t, X, body, bucket=name)

    if finding == "shadow-divergence":
        place("decision", decision_body(X, status="rejected"))
        guarded.append(str(put_file(store, "decision", decision_body(X, status="accepted"))))
        return "decision", X, guarded
    if finding == "binding-entry-dropped":
        place("bindings", bindings_body("E1", "E2"))
        guarded.append(str(put_file(store, "bindings", bindings_body("E1"), record_id=X)))
        return "bindings", X, guarded
    shard = decision_body(X, status="accepted")
    if finding == "duplicate-ulid:file-archive":
        shard = decision_body(X, status="proposed")
        place("decision", shard)
        guarded.append(str(put_file(store, "decision", shard)))
        guarded.append(
            str(
                put_archive(
                    store, "seg", [archive_line("decision", decision_body(X, status="rejected"))]
                )
            )
        )
    elif finding == "duplicate-ulid:shard-archive":
        place("decision", shard)
        guarded.append(
            str(
                put_archive(
                    store, "seg", [archive_line("decision", decision_body(X, status="rejected"))]
                )
            )
        )
    else:  # two differing archive lines
        place("decision", decision_body(X, status="rejected"))
        guarded.append(
            str(
                put_archive(
                    store,
                    "seg",
                    [  # each compatible with the shard line, differing from each other
                        archive_line("decision", decision_body(X, status="rejected")),
                        archive_line(
                            "decision", decision_body(X, status="rejected", valid_to=None)
                        ),
                    ],
                )
            )
        )
    if where == "file_equal_to_shard" and not (store / "decisions" / f"{X}.json").exists():
        guarded.append(str(put_file(store, "decision", shard)))
    return "decision", X, guarded


_FINDINGS = [
    "shadow-divergence",
    "duplicate-ulid:file-archive",
    "duplicate-ulid:shard-archive",
    "duplicate-ulid:two-archive",
    "binding-entry-dropped",
]
# A file equal to its shard line is by definition not a shadow-divergence, and a bindings file
# equal to its shard line drops no entry: those two findings have no such shape.
_CASES = [
    (f, w)
    for f in _FINDINGS
    for w in ("bucket", "misplaced", "stray", "file_equal_to_shard")
    if not (w == "file_equal_to_shard" and f in ("shadow-divergence", "binding-entry-dropped"))
]


@pytest.mark.parametrize(("finding", "where"), _CASES)
def test_skipped_identity_untouched_others_folded(  # T-C3
    tmp_path: Path, finding: str, where: str
) -> None:
    store = make_store(tmp_path)
    t, i, guarded = _skipped_fixture(store, finding, where)
    put_file(store, "fact", fact_body(Y))  # another identity, eligible
    inv = inv_of(store)
    skipped_lines = {o.raw_bytes for o in inv.occurrences if o.kind == "shard" and o.record_id == X}
    assert skipped_lines
    p = plan_maintenance(inv, ELIG_ALL, N)
    assert Skip(t, i, finding.split(":")[0]) in p.skipped
    # nothing of the identity is moved, rewritten, inserted, or deleted
    assert not [m for m in p.moves if m.record_id == X]
    assert not [w for w in p.replacements + p.inserts if w.record_id == X]
    rel = {Path(g).relative_to(store).as_posix() for g in guarded}
    assert not rel & set(p.file_deletions)
    assert not rel & set(p.segment_deletions)
    # every shard line of the skipped identity stays: byte for byte in its bucket file, or in
    # its stray file (which is in no bucket)
    in_target = {row[2] for rows in p.target.values() for row in rows if row[1] == X}
    if where == "stray":
        assert not in_target
    else:
        assert in_target == skipped_lines
        home = next(b for b, rows in p.target.items() if any(r[1] == X for r in rows))
        assert home == (other_bucket(X) if where == "misplaced" else bucket(X, N))
    # the others fold
    assert [w.record_id for w in p.inserts] == [Y]
    assert "facts/01OTHERFOLD.json" in p.file_deletions


def test_stray_file_is_healed_not_refused(tmp_path: Path) -> None:  # T-C3
    store = make_store(tmp_path)
    put_shard_line(store, "fact", "01A", fact_body("01A"), bucket="stray.shard")
    inv = inv_of(store)
    check_entry(inv, None)
    p = plan_maintenance(inv, ELIG_ALL, N)
    assert p.moves == [
        LineMove(
            "fact", "01A", "stray.shard", bucket("01A", N), line_of("fact", "01A", fact_body("01A"))
        )
    ]
    assert [row[1] for row in p.target[bucket("01A", N)]] == ["01A"]


def test_unparsed_lines_are_never_dropped(tmp_path: Path) -> None:  # T-C3
    store = make_store(tmp_path)
    put_shard_line(store, "fact", "01A", fact_body("01A"))
    path = store / "shards" / shard_name_of("01A")
    junk = b'{"type":"fact","id":"01BAD","body":'
    nonsense = b"not a line at all"
    path.write_bytes(path.read_bytes() + junk + b"\n" + nonsense + b"\n")
    p = plan_of(store)
    flat = [row[2] for rows in p.target.values() for row in rows]
    assert junk in flat and nonsense in flat
    assert not p.moves and not p.replacements


# -- T-C1: the key rule ----------------------------------------------------------------------------


def _keys_lost_fixture(store: Path, case: str) -> tuple[str, list[str]]:
    """Return the file path (store-relative) that must stay and any segment that must too."""
    if case == "descriptor.language":
        s = entity_body("01E", descriptor={"name": "f", "file_path": "a.py", "language": "py"})
        f = entity_body("01E", descriptor={"name": "f", "file_path": "a.py"})
        put_shard_line(store, "entity", "01E", s)
        put_file(store, "entity", f)
        return "entities/01E.json", []
    if case == "valid_to_null":
        put_shard_line(store, "decision", "01D", decision_body("01D", valid_to=None))
        put_file(store, "decision", decision_body("01D", status="accepted"))
        return "decisions/01D.json", []
    if case == "bindings_entry_key":
        put_shard_line(store, "bindings", "01B", [{"entity_id": "E1", "tier": 2, "weight": 0.5}])
        put_file(store, "bindings", [{"entity_id": "E1", "tier": 2}], record_id="01B")
        return "bindings/01B.json", []
    if case == "supports_element":
        put_shard_line(store, "fact", "01F", fact_body("01F", supports=["a", "b"]))
        put_file(store, "fact", fact_body("01F", supports=["a"]))
        return "facts/01F.json", []
    if case == "path_prefixes_element":
        put_shard_line(store, "domain", "01M", domain_body("01M", path_prefixes=["x", "y"]))
        put_file(store, "domain", domain_body("01M", path_prefixes=["x"]))
        return "domains/01M.json", []
    # seed_anchors0_null: the file lacks a null key the shard line has; the archive line carries
    # a null key the shard line lacks, so its segment stays too
    anchor = {"name": "n", "file_path": "a.py"}
    put_shard_line(
        store,
        "domain",
        "01M",
        domain_body("01M", status="dropped", seed_anchors=[{**anchor, "language": None}]),
    )
    put_file(store, "domain", domain_body("01M", status="dropped", seed_anchors=[anchor]))
    put_archive(
        store,
        "seg",
        [
            archive_line(
                "domain",
                domain_body("01M", status="dropped", seed_anchors=[{**anchor, "framework": None}]),
            )
        ],
    )
    return "domains/01M.json", ["archive/seg.jsonl"]


@pytest.mark.parametrize(
    "case",
    [
        "descriptor.language",
        "valid_to_null",
        "bindings_entry_key",
        "supports_element",
        "path_prefixes_element",
        "seed_anchors0_null",
    ],
)
def test_fold_kept_when_keys_would_be_lost(tmp_path: Path, case: str) -> None:  # T-C1
    store = make_store(tmp_path)
    file_path, segments = _keys_lost_fixture(store, case)
    p = plan_of(store)
    assert file_path in kept_paths(p) and file_path not in p.file_deletions
    assert not p.replacements and not p.inserts
    for segment in segments:
        assert segment in kept_paths(p) and segment not in p.segment_deletions


def test_two_entry_bindings_file_folds(tmp_path: Path) -> None:  # no crash (rev 6 sorted())
    store = make_store(tmp_path)
    put_shard_line(
        store, "bindings", "01B", [{"entity_id": "E1", "tier": 2}, {"entity_id": "E2", "tier": 1}]
    )
    put_file(
        store,
        "bindings",
        [{"entity_id": "E1", "tier": 1}, {"entity_id": "E2", "tier": 2}],
        record_id="01B",
    )
    p = plan_of(store)
    assert "bindings/01B.json" in p.file_deletions
    assert [w.record_id for w in p.replacements] == ["01B"]


def test_two_entries_one_entity_shard_folds_under_one_entry_file(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    put_shard_line(
        store, "bindings", "01B", [{"entity_id": "E1", "tier": 2}, {"entity_id": "E1", "tier": 1}]
    )
    put_file(store, "bindings", [{"entity_id": "E1", "tier": 1}], record_id="01B")
    p = plan_of(store)
    assert "bindings/01B.json" in p.file_deletions


def _superseding_store(tmp_path: Path, shard_null_key: bool) -> tuple[Path, Inventory]:
    store = make_store(tmp_path)
    shard = decision_body("01D")
    if shard_null_key:
        shard["valid_to"] = None
    put_shard_line(store, "decision", "01D", shard)
    put_archive(store, "seg", [archive_line("decision", decision_body("01D", status="rejected"))])
    return store, inv_of(store)


def test_superseding_replacement_dropping_null_key_keeps_both(tmp_path: Path) -> None:
    store, inv = _superseding_store(tmp_path, shard_null_key=True)
    assert effective_all(inv)[("decision", "01D")].source.kind == "archive"  # archive is served
    p = plan_maintenance(inv, ELIG_ALL, N)
    assert "archive/seg.jsonl" in kept_paths(p) and not p.segment_deletions
    assert not p.replacements


def test_superseding_replacement_keeps_keys_replaces_and_deletes(tmp_path: Path) -> None:
    store, inv = _superseding_store(tmp_path, shard_null_key=False)
    p = plan_maintenance(inv, ELIG_ALL, N)
    assert [(w.record_id, w.origin) for w in p.replacements] == [("01D", "archive/seg.jsonl")]
    assert p.segment_deletions == ["archive/seg.jsonl"]


# -- T-C1: segments --------------------


@pytest.mark.parametrize(
    "line", ["unreadable", "unknown_type", "nonterminal", "not_deletable", "skipped"]
)
def test_segment_kept(tmp_path: Path, line: str) -> None:
    store = make_store(tmp_path)
    good = decision_body("01G", status="rejected")
    put_shard_line(store, "decision", "01G", good)
    lines: list[dict | bytes] = [archive_line("decision", good)]
    if line == "unreadable":
        lines.append(b'{"record_type":"decision","id":')
    elif line == "unknown_type":
        lines.append({"record_type": "initiative", "id": "01I"})
    elif line == "nonterminal":
        put_shard_line(store, "decision", "01N", decision_body("01N"))
        lines.append(archive_line("decision", decision_body("01N", status="proposed")))
    elif line == "not_deletable":  # compatible, but the archive line carries a key the shard lacks
        put_shard_line(store, "decision", "01K", decision_body("01K", status="rejected"))
        lines.append(
            archive_line("decision", decision_body("01K", status="rejected", valid_to=None))
        )
    else:
        put_shard_line(store, "decision", "01S", decision_body("01S", status="rejected"))
        put_file(store, "decision", decision_body("01S", status="accepted"))
        lines.append(archive_line("decision", decision_body("01S", status="rejected")))
    put_archive(store, "seg", lines)
    p = plan_of(store)
    assert "archive/seg.jsonl" in kept_paths(p) and not p.segment_deletions
    expected = {
        "unreadable": "unreadable line",
        "unknown_type": "unknown record type",
        "nonterminal": "nonterminal line",
        "not_deletable": "not deletable",
        "skipped": "skipped identity",
    }[line]
    assert kept_reason(p, "archive/seg.jsonl") == expected


def test_segment_of_deletable_terminal_lines_is_deleted(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    good = decision_body("01G", status="rejected")
    put_shard_line(store, "decision", "01G", good)
    put_archive(store, "seg", [archive_line("decision", good)])
    assert plan_of(store).segment_deletions == ["archive/seg.jsonl"]


def test_archive_only_identity_becomes_a_line_and_its_segment_goes(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    body = decision_body("01G", status="rejected")
    put_archive(store, "seg", [archive_line("decision", body)])
    p = plan_of(store)
    assert [(w.record_id, w.origin, w.raw_line) for w in p.inserts] == [
        ("01G", "archive/seg.jsonl", line_of("decision", "01G", body))
    ]
    assert p.segment_deletions == ["archive/seg.jsonl"]


def test_mixed_segment_packs_other_record(tmp_path: Path) -> None:  # T-M4 shape
    store = make_store(tmp_path)
    put_shard_line(store, "decision", X, decision_body(X, status="rejected"))
    put_file(store, "decision", decision_body(X, status="accepted"))  # shadow-divergence
    other = decision_body("01Q", status="rejected")
    put_archive(
        store,
        "seg",
        [
            archive_line("decision", decision_body(X, status="rejected")),
            archive_line("decision", other),
        ],
    )
    p = plan_of(store)
    assert [w.record_id for w in p.inserts] == ["01Q"]  # packed
    assert "archive/seg.jsonl" in kept_paths(p)  # the segment stays for the skipped line


def test_community_entries_kept_verbatim(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    community = entity_body("01C", canonical_name="community:7", kind="abstract")
    put_file(store, "entity", community)
    entries = [{"entity_id": "01C", "tier": 1}, {"entity_id": "E2", "tier": 2}]
    put_file(store, "bindings", entries, record_id="01B")
    p = plan_of(store)
    by_id = {w.record_id: w for w in p.inserts}
    assert by_id["01C"].raw_line == line_of("entity", "01C", community)
    assert by_id["01B"].raw_line == line_of("bindings", "01B", entries)  # the derived entry stays
    assert {"entities/01C.json", "bindings/01B.json"} <= set(p.file_deletions)


# -- eligibility --------------------


def test_unsettled_untracked_dirty_and_unlogged_stay(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    for name in ("settled", "recent", "untracked", "unlogged"):
        put_file(store, "fact", fact_body(f"01{name.upper()}"))
    for name in ("eqrecent", "equntracked", "equnlogged"):  # a file equal to its shard line
        put_file(store, "fact", fact_body(f"01{name.upper()}"))
        put_shard_line(store, "fact", f"01{name.upper()}", fact_body(f"01{name.upper()}"))
    now = 100 * DAY
    landing = {
        ".sidegraph/facts/01EQRECENT.json": now - 1 * DAY,
        ".sidegraph/facts/01EQUNTRACKED.json": now - 30 * DAY,
        ".sidegraph/facts/01SETTLED.json": now - 8 * DAY,
        ".sidegraph/facts/01RECENT.json": now - 1 * DAY,
        ".sidegraph/facts/01UNTRACKED.json": now - 30 * DAY,
    }
    dirty = frozenset({".sidegraph/facts/01UNTRACKED.json", ".sidegraph/facts/01EQUNTRACKED.json"})
    p = plan_of(store, Eligibility(7, now, landing, dirty, STORE_REL))
    assert p.file_deletions == ["facts/01SETTLED.json"]
    assert kept_reason(p, "facts/01RECENT.json") == "not settled"
    assert kept_reason(p, "facts/01UNTRACKED.json") == "untracked or modified"
    assert kept_reason(p, "facts/01UNLOGGED.json") == "no commit in the log"
    for name in ("EQRECENT", "EQUNTRACKED", "EQUNLOGGED"):  # the normal pre-deletion state stays
        assert f"facts/01{name}.json" in kept_paths(p)
    assert [w.record_id for w in p.inserts] == ["01SETTLED"]


def test_domain_created_long_ago_dropped_yesterday_stays(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    rid = "01ARZ3NDEKTSV4RRFFQ69G5FAV"  # a ULID from 2016
    put_file(store, "domain", domain_body(rid, status="dropped"))
    now = 2_000_000_000
    landing = {f".sidegraph/domains/{rid}.json": now - DAY}  # the drop landed yesterday
    p = plan_of(store, Eligibility(7, now, landing, frozenset(), STORE_REL))
    assert not p.file_deletions and f"domains/{rid}.json" in kept_paths(p)


# -- encoding --------------------


def test_lone_surrogate_file_kept_and_reported(tmp_path: Path) -> None:  # §5.2/§6.2
    store = make_store(tmp_path)
    # the model rejects a surrogate in a field it knows, so the lone surrogate rides in a key a
    # newer client wrote: readable, and not encodable as UTF-8
    put_file(store, "fact", fact_body("01A", future_key="x"))
    path = store / "facts" / "01A.json"
    path.write_text(path.read_text("utf-8").replace('"x"', '"\\ud800"'), "utf-8")
    put_file(store, "fact", fact_body("01B", statement="ok"))
    p = plan_of(store)
    assert ("facts/01A.json", "unencodable") in p.kept
    assert "facts/01A.json" not in p.file_deletions
    assert "facts/01B.json" in p.file_deletions  # the run continues


# -- placement --------------------


def test_misplaced_line_moves_and_a_newer_file_replaces_in_the_bucket(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    old = decision_body("01D", status="proposed")
    put_shard_line(store, "decision", "01D", old, bucket=misplaced_name("01D"))
    put_file(store, "decision", decision_body("01D", status="accepted"))
    p = plan_of(store)
    assert p.moves == [
        LineMove(
            "decision",
            "01D",
            misplaced_name("01D"),
            bucket("01D", N),
            line_of("decision", "01D", old),
        )
    ]  # verbatim: the old body travels, not the newer one
    assert [(w.record_id, w.bucket, w.origin) for w in p.replacements] == [
        ("01D", bucket("01D", N), "decisions/01D.json")
    ]
    folded = line_of("decision", "01D", decision_body("01D", status="accepted"))
    assert p.target[bucket("01D", N)] == [("decision", "01D", folded)]  # the file's body, no other
    assert all(not rows for b, rows in p.target.items() if b != bucket("01D", N))


def test_equal_lines_in_one_bucket_collapse_in_the_target(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    body = fact_body("01A")
    put_shard_line(store, "fact", "01A", body)
    put_shard_line(store, "fact", "01A", body)
    p = plan_of(store)
    assert len(p.target[bucket("01A", N)]) == 1 and not p.moves


def test_every_bucket_is_in_the_target(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    assert sorted(plan_of(store).target) == list(range(N))


def test_equal_bindings_bodies_of_two_identities_stay_two_lines(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    put_shard_line(store, "bindings", "01B", bindings_body("E1"))
    put_shard_line(store, "bindings", "01C", bindings_body("E1"), bucket=misplaced_name("01C"))
    p = plan_of(store)
    rows = [row for rows in p.target.values() for row in rows]
    assert sorted(r[1] for r in rows) == ["01B", "01C"]


def test_self_healing_counts(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    put_shard_line(store, "fact", "01A", fact_body("01A"))
    put_shard_line(store, "fact", "01A", fact_body("01A"))  # an equal duplicate
    put_shard_line(store, "fact", "01B", fact_body("01B"), bucket=misplaced_name("01B"))
    put_shard_line(store, "fact", "01C", fact_body("01C"), bucket="stray.shard")
    assert self_healing_counts(inv_of(store), N) == {
        "misplaced": 1,
        "equal_duplicate": 1,
        "stray": 1,
    }


def test_terminal_statuses_match_verify() -> None:
    from sidegraph import maintenance
    from sidegraph.verify import _ARCHIVE_TERMINAL_STATUSES

    assert maintenance._TERMINAL_STATUSES == _ARCHIVE_TERMINAL_STATUSES


def test_plan_is_pure_and_repeatable(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    put_file(store, "fact", fact_body("01A"))
    put_file(store, "bindings", bindings_body("E1"), record_id="01B")
    put_shard_line(store, "decision", "01D", decision_body("01D"), bucket=misplaced_name("01D"))
    put_archive(store, "seg", [archive_line("decision", decision_body("01D", status="rejected"))])
    inv = inv_of(store)
    before = copy.deepcopy(inv)  # occurrences with their parsed bodies, images, stats, listing
    first = plan_maintenance(inv, ELIG_ALL, N)
    assert inv == before
    assert plan_maintenance(inv, ELIG_ALL, N) == first
    assert inv == before


# -- fix round 1 ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", ["proposed", "accepted"])
def test_archive_only_nonterminal_candidate_is_packed_and_its_segment_kept(
    tmp_path: Path, status: str
) -> None:
    store = make_store(tmp_path)
    body = decision_body("01G", status=status)
    put_archive(store, "seg", [archive_line("decision", body)])
    p = plan_of(store)
    assert [(w.record_id, w.raw_line) for w in p.inserts] == [
        ("01G", line_of("decision", "01G", body))
    ]
    assert p.target[bucket("01G", N)] == [("decision", "01G", line_of("decision", "01G", body))]
    assert "archive/seg.jsonl" not in p.segment_deletions  # the deletion guard stays
    assert kept_reason(p, "archive/seg.jsonl") == "nonterminal line"


def test_two_differing_archive_lines_each_compatible_with_the_shard_are_skipped(
    tmp_path: Path,
) -> None:
    store = make_store(tmp_path)
    put_shard_line(store, "decision", X, decision_body(X, status="rejected"))
    lines = [
        archive_line("decision", decision_body(X, status="rejected")),
        archive_line("decision", decision_body(X, status="rejected", valid_to=None)),
    ]
    put_archive(store, "seg", lines)
    assert Skip("decision", X, "duplicate-ulid") in plan_of(store).skipped


def _abstract_community(store: Path, eid: str, *, kind: str) -> None:
    put_file(store, "entity", entity_body(eid, canonical_name="community:7", kind=kind))


def test_derived_entry_missing_from_the_file_does_not_skip_the_bindings(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    _abstract_community(store, "01C", kind="abstract")
    put_shard_line(store, "bindings", "01B", bindings_body("01C", "E2"))
    put_file(store, "bindings", bindings_body("E2"), record_id="01B")
    p = plan_of(store)
    assert not [s for s in p.skipped if s.record_id == "01B"]
    assert "bindings/01B.json" in p.file_deletions


def test_concrete_entity_named_community_is_not_excluded(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    _abstract_community(store, "01C", kind="concrete")
    put_shard_line(store, "bindings", "01B", bindings_body("01C", "E2"))
    put_file(store, "bindings", bindings_body("E2"), record_id="01B")
    p = plan_of(store)
    assert Skip("bindings", "01B", "binding-entry-dropped") in p.skipped
    assert "bindings/01B.json" not in p.file_deletions


def test_unencodable_archive_candidate_is_reported_once(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    line = json.dumps(archive_line("decision", decision_body("01G", status="rejected", f="x")))
    put_archive(store, "seg", [line.replace('"x"', '"\\ud800"')])
    p = plan_of(store)
    assert [r for path, r in p.kept if path == "archive/seg.jsonl"] == ["unencodable"]
    assert not p.inserts and not p.segment_deletions


def test_communities_shrinking_in_a_file_keeps_the_file(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    put_shard_line(store, "domain", "01M", domain_body("01M", communities=["c1", "c2"]))
    put_file(store, "domain", domain_body("01M", communities=["c1"]))
    p = plan_of(store)
    assert kept_reason(p, "domains/01M.json") == "keys would be lost"
    assert not p.skipped and not p.file_deletions


def test_wrong_shard_count_is_refused(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    low = next(f"01L{k}" for k in range(99) if bucket(f"01L{k}", 64) < 16)  # fits 16 buckets too
    put_shard_line(store, "fact", low, fact_body(low))
    with pytest.raises(ValueError):
        plan_maintenance(inv_of(store), ELIG_ALL, 16)  # the marker says 64
    (store / "shards" / "3f.shard").write_bytes(b"")
    bare = inventory_from_disk(store, validate=validator, assume_major=2)
    bare.marker = None
    with pytest.raises(ValueError):
        plan_maintenance(bare, ELIG_ALL, 16)  # bucket 0x3f is out of range for 16


def test_self_healing_counts_leave_skipped_identities_out(tmp_path: Path) -> None:
    store = make_store(tmp_path)
    put_shard_line(
        store, "decision", X, decision_body(X, status="rejected"), bucket=misplaced_name(X)
    )
    put_shard_line(store, "decision", X, decision_body(X, status="rejected"), bucket="held.shard")
    put_file(store, "decision", decision_body(X, status="accepted"))  # shadow-divergence
    put_shard_line(store, "fact", "01A", fact_body("01A"), bucket=misplaced_name("01A"))
    put_shard_line(store, "fact", "01C", fact_body("01C"), bucket="free.shard")
    assert self_healing_counts(inv_of(store), N) == {
        "misplaced": 1,
        "equal_duplicate": 0,
        "stray": 1,  # held.shard keeps the skipped line and stays
    }
