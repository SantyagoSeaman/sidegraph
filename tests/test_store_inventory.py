"""The occurrence inventory and the one effective-record function (T-L1, T-L2, T-L3).

# see design/superpowers/specs/2026-10-20-packed-store-format-design.md (§5.3)
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from sidegraph import store_format
from sidegraph.store_format import bucket as bucket_of
from sidegraph.store_format import (
    bucket_name,
    decode_line,
    encode_line,
    inventory_from_disk,
    inventory_from_git_tree,
)
from sidegraph.store_rules import archive_candidate, effective, effective_all
from tests import store_format2_support
from tests.store_format2_support import (
    bindings_body,
    commit_all,
    decision_body,
    domain_body,
    entity_body,
    fact_body,
    make_store,
    per_record_file,
    put_archive,
    put_file,
    put_shard_line,
    validator,
)

git_repo = store_format2_support.git_repo  # the pytest fixture, bound by name for pytest
SRC = str(Path(__file__).resolve().parent.parent / "src")
BUILDERS = {
    "decision": decision_body,
    "fact": fact_body,
    "entity": entity_body,
    "domain": domain_body,
}
ID = "01HZZZZZZZZZZZZZZZZZZZZZZA"


def _inv(store):
    return inventory_from_disk(store, validate=validator)


def _body(t: str, rid: str, **over):
    return (
        bindings_body(*over.get("entities", ("E1",)))
        if t == "bindings"
        else BUILDERS[t](rid, **over)
    )


def _variant(t: str, rid: str, tag: str):
    """A body of type ``t`` that differs from ``_body(t, rid)`` in one value."""
    if t == "bindings":
        return bindings_body("E-" + tag)
    key = {
        "decision": "title",
        "fact": "statement",
        "entity": "canonical_name",
        "domain": "title",
    }[t]
    return BUILDERS[t](rid, **{key: tag})


def _archive_line(t: str, body: dict) -> dict:
    return {"record_type": t, **body}


@pytest.mark.parametrize("t", ["decision", "fact", "entity", "domain", "bindings"])
def test_file_beats_shard_beats_archive(tmp_path, t):  # T-L1
    store = make_store(tmp_path)
    f, s, a = (_variant(t, ID, tag) for tag in ("file", "shard", "archive"))
    put_file(store, t, f, record_id=ID)
    put_shard_line(store, t, ID, s)
    if t in ("decision", "domain"):
        put_archive(store, "seg1", [_archive_line(t, a)])
    assert effective(_inv(store), t, ID).raw == f
    per_record_file(store, t, ID).unlink()
    assert effective(_inv(store), t, ID).raw == s


@pytest.mark.parametrize("t", ["decision", "domain"])
def test_archive_is_last_resort(tmp_path, t):  # T-L1
    store = make_store(tmp_path)
    a = _variant(t, ID, "archive")
    put_archive(store, "seg1", [_archive_line(t, a)])
    assert effective(_inv(store), t, ID).raw == a


def test_bindings_file_is_whole_list(tmp_path):  # T-L1, red: union of file and shard
    store = make_store(tmp_path)
    put_file(store, "bindings", [{"entity_id": "E2", "tier": 2}], record_id="R")
    put_shard_line(store, "bindings", "R", [{"entity_id": "E1", "tier": 2}])
    assert effective(_inv(store), "bindings", "R").raw == [{"entity_id": "E2", "tier": 2}]


def test_superseding_archive_beats_shard_never_file(tmp_path):  # T-L1
    store = make_store(tmp_path)
    proposed = decision_body(ID, status="proposed")
    closed = decision_body(ID, status="superseded", valid_to="2026-02-01T00:00:00Z")
    put_shard_line(store, "decision", ID, proposed)
    put_archive(store, "seg1", [_archive_line("decision", closed)])
    assert effective(_inv(store), "decision", ID).raw == closed
    # the same archive line is a legal successor of a kept-hot file too, yet the file is a
    # deliberate local state and is served
    put_file(store, "decision", proposed)
    eff = effective(_inv(store), "decision", ID)
    assert eff.raw == proposed and eff.source.kind == "file"


def test_compatible_archive_does_not_beat_shard(tmp_path):  # T-L1
    # the archive line carries ``rejected: null`` the shard body lacks: ct(S, A) is clean and the
    # pair is compatible, so the archive is not superseding (mutant: no `not compatible`)
    store = make_store(tmp_path)
    shard = decision_body(ID)
    archived = decision_body(ID, rejected=None)
    assert "rejected" not in shard
    put_shard_line(store, "decision", ID, shard)
    put_archive(store, "seg1", [_archive_line("decision", archived)])
    eff = effective(_inv(store), "decision", ID)
    assert eff.raw == shard and eff.source.kind == "shard"


def test_revived_domain_shard_beats_dropped_archive(tmp_path):  # T-L1
    # an archived ``dropped`` domain, later superseded: the shard holds the successor state
    store = make_store(tmp_path)
    dropped = domain_body(ID, status="dropped")
    successor = domain_body(ID, status="superseded")
    put_shard_line(store, "domain", ID, successor)
    put_archive(store, "seg1", [_archive_line("domain", dropped)])
    eff = effective(_inv(store), "domain", ID)
    assert eff.raw == successor and eff.source.kind == "shard"
    assert eff.source.path == f"shards/{bucket_name(bucket_of(ID, 64))}"


def test_two_archive_lines_first_segment_no_superseding(tmp_path):  # T-L1, §5.3 rule 3
    store = make_store(tmp_path)
    shard = decision_body(ID, status="proposed")
    first = decision_body(ID, status="superseded", valid_to="2026-02-01T00:00:00Z")
    second = decision_body(ID, status="rejected", valid_to="2026-03-01T00:00:00Z")
    put_shard_line(store, "decision", ID, shard)
    put_archive(store, "b-seg", [_archive_line("decision", second)])
    put_archive(store, "a-seg", [_archive_line("decision", first)])
    inv = _inv(store)
    # the archive lines differ: no superseding test is run, the shard line is served
    assert effective(inv, "decision", ID).raw == shard
    store2 = make_store(tmp_path / "two")
    put_archive(store2, "b-seg", [_archive_line("decision", second)])
    put_archive(store2, "a-seg", [_archive_line("decision", first)])
    assert effective(_inv(store2), "decision", ID).raw == first  # first segment wins


def test_unreadable_file_of_packed_identity_has_no_record(tmp_path):  # T-L1, red: fall-through
    store = make_store(tmp_path)
    put_shard_line(store, "decision", ID, decision_body(ID))
    (store / "decisions").mkdir()
    (store / "decisions" / f"{ID}.json").write_text("{not json")
    eff = effective(_inv(store), "decision", ID)
    assert eff.raw is None and eff.reason == "unreadable"


def test_conflicted_file_has_no_record(tmp_path):  # T-L1
    store = make_store(tmp_path)
    put_shard_line(store, "decision", ID, decision_body(ID))
    (store / "decisions").mkdir()
    (store / "decisions" / f"{ID}.json").write_text(
        "<<<<<<< ours\n{}\n=======\n{}\n>>>>>>> theirs\n"
    )
    eff = effective(_inv(store), "decision", ID)
    assert eff.raw is None and eff.reason == "conflict"


def test_two_shard_lines_differ_none_equal_one(tmp_path):  # T-L1
    store = make_store(tmp_path)
    put_shard_line(store, "fact", ID, fact_body(ID), bucket="00.shard")
    put_shard_line(store, "fact", ID, fact_body(ID), bucket="01.shard")
    assert effective(_inv(store), "fact", ID).raw == fact_body(ID)
    put_shard_line(store, "fact", ID, fact_body(ID, statement="other"), bucket="02.shard")
    eff = effective(_inv(store), "fact", ID)
    assert eff.raw is None and eff.reason == "duplicate-packed-record"


def test_bad_archive_line_costs_nothing(tmp_path):  # T-L1, red: unreadable archive quarantines
    store = make_store(tmp_path)
    put_file(store, "decision", decision_body(ID))
    put_shard_line(store, "fact", ID, fact_body(ID))
    put_archive(
        store,
        "seg1",
        [
            b"{not json",
            {"record_type": "future-kind", "id": ID, "x": 1},
            {"record_type": "decision", "id": ID, "title": 3},  # known type, fails the model
            {"record_type": "decision", "id": "../x"},  # unsafe id
        ],
    )
    inv = _inv(store)
    assert effective(inv, "decision", ID).raw == decision_body(ID)
    assert effective(inv, "fact", ID).raw == fact_body(ID)
    kinds = [
        (o.error is None, o.archive_type_known) for o in inv.occurrences if o.kind == "archive"
    ]
    assert kinds == [(False, False), (True, False), (False, True), (False, True)]
    assert inv.unrecoverable == []


def test_bad_line_costs_only_its_identity(tmp_path):  # T-L2, red: skip the whole shard
    store = make_store(tmp_path)
    ids = ["01A", "01B", "01C"]
    path = store / "shards" / "00.shard"
    path.parent.mkdir()
    lines = [
        encode_line("fact", "01A", fact_body("01A")),
        encode_line("fact", "01B", {"id": "01B", "statement": 3}),  # damaged body
        encode_line("fact", "01C", fact_body("01C")),
    ]
    path.write_bytes(b"\n".join(lines) + b"\n")
    inv = _inv(store)
    assert effective(inv, "fact", "01A").raw == fact_body("01A")
    assert effective(inv, "fact", "01C").raw == fact_body("01C")
    bad = effective(inv, "fact", "01B")
    assert bad.raw is None and bad.reason == "unreadable"
    assert ("fact", "01B") in inv.by_identity()
    assert ids  # all three identities are inventoried
    assert set(inv.by_identity()) == {("fact", i) for i in ids}


def test_conflict_region_quarantines_named_identities(tmp_path):  # T-L2
    store = make_store(tmp_path)
    path = store / "shards" / "00.shard"
    path.parent.mkdir()
    path.write_bytes(
        encode_line("fact", "01A", fact_body("01A")) + b"\n"
        + b"<<<<<<< ours\n"
        + encode_line("fact", "01B", fact_body("01B")) + b"\n"
        + b"=======\n"
        + encode_line("fact", "01B", fact_body("01B", statement="theirs")) + b"\n"
        + b">>>>>>> theirs\n"
        + encode_line("fact", "01C", fact_body("01C")) + b"\n"
    )  # fmt: skip
    inv = _inv(store)
    assert effective(inv, "fact", "01B").reason == "conflict"
    assert effective(inv, "fact", "01A").raw == fact_body("01A")
    assert effective(inv, "fact", "01C").raw == fact_body("01C")
    assert all(o.in_conflict for o in inv.unrecoverable)


def test_damaged_body_still_yields_identity():  # T-L2, red: sort_keys on the envelope
    line = encode_line(
        "fact", "01F", {"id": "01F", "t": "x"}
    )  # the encoder's own output, then damaged
    assert decode_line(line[: line.index(b'"body":') + 9]).record_id == "01F"


def test_unrecoverable_line_quarantines_nothing(tmp_path):  # §5.3 rule 1
    store = make_store(tmp_path)
    path = store / "shards" / "00.shard"
    path.parent.mkdir()
    path.write_bytes(
        encode_line("fact", "01A", fact_body("01A")) + b"\n" + b"garbage that names nothing\n"
    )
    inv = _inv(store)
    assert inv.unrecoverable and effective(inv, "fact", "01A").raw is not None


def test_case_variant_shard_lines_are_occurrences(tmp_path):  # §5.3, red: case variant invisible
    store = make_store(tmp_path)
    put_shard_line(store, "fact", "01A", fact_body("01A"), bucket="0A.SHARD")
    inv = _inv(store)
    assert [o.path for o in inv.by_identity()[("fact", "01A")]] == ["shards/0A.SHARD"]


def test_decision_and_fact_share_an_id(tmp_path):  # T-L3, red: key by id only
    store = make_store(tmp_path)
    put_file(store, "decision", decision_body("X"))
    put_file(store, "fact", fact_body("X"))
    inv = _inv(store)
    assert effective(inv, "decision", "X").raw == decision_body("X")
    assert effective(inv, "fact", "X").raw == fact_body("X")
    assert set(effective_all(inv)) == {("decision", "X"), ("fact", "X")}


def test_invalid_utf8_line_costs_one_line(tmp_path):  # Review Focus 2
    store = make_store(tmp_path)
    path = store / "shards" / "00.shard"
    path.parent.mkdir()
    path.write_bytes(
        encode_line("fact", "01A", fact_body("01A")) + b"\n"
        + b'{"type":"fact","id":"01B","body":{"t":"\xff"}}\n'
        + b"\xef\xbb\xbf" + encode_line("fact", "01C", fact_body("01C")) + b"\n"
    )  # fmt: skip
    inv = _inv(store)
    assert effective(inv, "fact", "01A").raw == fact_body("01A")
    b = effective(inv, "fact", "01B")
    assert b.raw is None and b.reason == "unreadable" and ("fact", "01B") in inv.by_identity()
    c = effective(inv, "fact", "01C")
    assert c.raw is None and c.reason == "unreadable" and ("fact", "01C") in inv.by_identity()


def _signature(inv):
    return [
        (o.record_type, o.record_id, o.kind, o.path, o.k, o.line_no, o.raw, o.raw_bytes, o.error,
         o.parseable, o.in_conflict, o.archive_type_known)
        for o in inv.occurrences
    ]  # fmt: skip


def test_git_tree_inventory_equals_disk(git_repo):  # red: cat-file --batch with stdin=DEVNULL
    repo = git_repo
    store = make_store(repo)
    put_file(store, "decision", decision_body(ID))
    put_file(store, "bindings", bindings_body("E1"), record_id=ID)
    put_shard_line(store, "fact", "01A", fact_body("01A"))
    put_shard_line(store, "fact", "01B", fact_body("01B"), bucket="0A.SHARD")
    put_archive(store, "seg1", [_archive_line("decision", decision_body("01D", status="rejected"))])
    (store / "facts").mkdir()
    (store / "facts" / "bad.json").write_text("{")
    # non-shard files in shards/ are not occurrences, in either view
    old = encode_line("fact", "01A", fact_body("01A", statement="older")) + b"\n"
    (store / "shards" / "README.md").write_text("not a shard\n")
    (store / "shards" / "00.shard.tmp").write_bytes(old)
    (store / "shards" / "x.tmp").write_bytes(old)
    commit_all(repo, "store", date="2026-01-02T00:00:00Z")
    disk = _inv(store)
    tree = inventory_from_git_tree(repo, "HEAD", ".sidegraph", validate=validator)
    assert _signature(tree) == _signature(disk) and len(disk.occurrences) == 6
    assert set(tree.images) == set(disk.images) and not tree.unrecoverable
    assert tree.images == disk.images and tree.stats == {} and tree.marker == disk.marker


def test_inventory_keeps_bytes_and_prior_stats(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    put_shard_line(store, "fact", "01A", fact_body("01A"), bucket="00.shard")
    path = store / "shards" / "00.shard"
    data = path.read_bytes()
    real_stat = store_format.stat_entry
    newer = encode_line("fact", "01A", fact_body("01A", statement="newer")) + b"\n"

    def stat_then_rewrite(p):
        st = real_stat(p)
        if Path(p).name == "00.shard":
            path.write_bytes(newer)  # a writer lands between the stat and the read
            monkeypatch.setattr(store_format, "stat_entry", real_stat)
        return st

    monkeypatch.setattr(store_format, "stat_entry", stat_then_rewrite)
    inv = _inv(store)
    assert inv.stats["shards/00.shard"][1] == len(data)  # the stat taken before the read
    assert inv.images["shards/00.shard"] == newer  # the bytes parsed are the bytes imaged
    assert len(newer) != len(data)  # so the pre-read stat cannot certify them
    (occ,) = [o for o in inv.occurrences if o.kind == "shard"]
    assert occ.raw_bytes == newer.rstrip(b"\n")


def test_raw_bytes_of_a_file_and_a_crlf_line(tmp_path):
    store = make_store(tmp_path)
    file = put_file(store, "fact", fact_body("01F"))
    path = store / "shards" / "00.shard"
    path.parent.mkdir()
    line = encode_line("fact", "01A", fact_body("01A"))
    path.write_bytes(line + b"\r\n")
    by = {o.kind: o for o in _inv(store).occurrences}
    assert by["file"].raw_bytes == file.read_bytes() and by["shard"].raw_bytes == line
    assert by["shard"].k == 0 and by["shard"].line_no == 1


def test_major1_tree_does_not_inventory_shards(tmp_path):  # red: read shards whenever present
    store = make_store(tmp_path, fmt=1)
    (store / "shards").mkdir()
    (store / "shards" / "00.shard").write_bytes(
        encode_line("fact", "01A", fact_body("01A")) + b"\n"
    )
    inv = _inv(store)
    assert inv.occurrences == [] and inv.listing is None and "shards/00.shard" not in inv.images
    assumed = inventory_from_disk(store, validate=validator, assume_major=2)
    assert [o.kind for o in assumed.occurrences] == ["shard"] and assumed.listing is not None


def test_archive_segments_read_in_sorted_path_order(tmp_path):
    store = make_store(tmp_path)
    put_archive(store, "b", [_archive_line("decision", decision_body("01B", status="rejected"))])
    put_archive(store, "a", [_archive_line("decision", decision_body("01A", status="rejected"))])
    assert [o.path for o in _inv(store).occurrences] == ["archive/a.jsonl", "archive/b.jsonl"]


def test_inventory_modules_stay_pydantic_free():
    code = (
        "import sys\n"
        "import sidegraph.store_format, sidegraph.store_rules\n"
        "assert not [m for m in sys.modules if m.split('.')[0] in ('pydantic', 'ulid')"
        " or m in ('sidegraph.schema', 'sidegraph.store')], sorted(sys.modules)\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", code],
        env={"PYTHONPATH": SRC, "PATH": ""},
        capture_output=True,
        text=True,
    )
    assert done.returncode == 0, done.stderr


def test_symlinked_record_file_is_followed_on_disk_and_reported_in_git(git_repo):
    # disk follows a live symlink (store-format.md); a git tree holds only the link, so the git
    # view reports the occurrence with reason "symlink" and never reads it as a record
    repo = git_repo
    store = make_store(repo)
    real = put_file(store, "fact", fact_body("01L"))
    kept = real.read_bytes()
    (store / "elsewhere").mkdir()
    real.rename(store / "elsewhere" / "01L.json")
    real.symlink_to(Path("..") / "elsewhere" / "01L.json")
    commit_all(repo, "link", date="2026-01-02T00:00:00Z")
    disk = _inv(store)
    (occ,) = [o for o in disk.occurrences if o.kind == "file"]
    assert occ.error is None and occ.raw_bytes == kept
    tree = inventory_from_git_tree(repo, "HEAD", ".sidegraph", validate=validator)
    (linked,) = [o for o in tree.occurrences if o.kind == "file"]
    assert (linked.record_type, linked.record_id) == ("fact", "01L")
    assert linked.error == store_format.SYMLINK_REASON and linked.raw is None
    assert effective(tree, "fact", "01L").reason == "unreadable"


def test_unlistable_record_dir_is_an_error_not_an_empty_pool(tmp_path):
    store = make_store(tmp_path)
    put_file(store, "decision", decision_body(ID))
    decisions = store / "decisions"
    if not hasattr(os, "geteuid") or os.geteuid() == 0:
        pytest.skip("needs POSIX permissions; root bypasses them")
    decisions.chmod(0)
    try:
        with pytest.raises(PermissionError):
            _inv(store)
    finally:
        decisions.chmod(0o755)


def test_marker_is_stat_before_read(tmp_path):
    store = make_store(tmp_path)
    inv = _inv(store)
    st = (store / "format").stat()
    assert inv.stats["format"] == (st.st_ino, st.st_size, st.st_mtime_ns)


def test_equal_bodies_with_an_escaped_non_ascii_character_are_one_occurrence(tmp_path):
    store = make_store(tmp_path)
    body = fact_body("01A", zz="\u00e9")  # escaped on disk as \u00e9, equal either way
    one = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("ascii")
    two = json.dumps(body, sort_keys=True).encode("ascii")  # differs only in whitespace
    assert b"\\u00e9" in one and one != two
    for name, text in (("00.shard", one), ("01.shard", two)):
        path = store / "shards" / name
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(b'{"type":"fact","id":"01A","body":' + text + b"}\n")
    eff = effective(_inv(store), "fact", "01A")
    assert eff.raw is not None and eff.reason is None


def test_bodies_differing_only_in_json_type_are_different(tmp_path):
    for a, b in ((1, 1.0), (1, True), (0, False), (1.0, True)):
        store = make_store(tmp_path / f"{a!r}-{b!r}")
        for name, value in (("00.shard", a), ("01.shard", b)):
            put_shard_line(store, "fact", "01A", fact_body("01A", n=value), bucket=name)
        assert effective(_inv(store), "fact", "01A").reason == "duplicate-packed-record", (a, b)


def test_k_counts_an_identitys_lines_in_one_file(tmp_path):
    store = make_store(tmp_path)
    put_shard_line(store, "fact", "01A", fact_body("01A"), bucket="00.shard")
    put_shard_line(store, "fact", "01A", fact_body("01A", statement="two"), bucket="00.shard")
    put_shard_line(store, "fact", "01B", fact_body("01B"), bucket="00.shard")
    ks = [(o.record_id, o.k, o.line_no) for o in _inv(store).occurrences]
    assert ks == [("01A", 0, 1), ("01A", 1, 2), ("01B", 0, 3)]


def test_archive_candidate_skips_unreadable_lines(tmp_path):
    store = make_store(tmp_path)
    good = decision_body(ID, status="rejected")
    put_archive(store, "a", [_archive_line("decision", {**good, "title": 3})])  # fails the model
    put_archive(store, "b", [_archive_line("decision", good)])
    inv = _inv(store)
    occs = inv.by_identity()[("decision", ID)]
    assert archive_candidate(occs).path == "archive/b.jsonl"
    assert effective(inv, "decision", ID).raw == good


def test_archive_raw_bytes_are_the_line_as_read(tmp_path):
    store = make_store(tmp_path)
    line = json.dumps(_archive_line("decision", decision_body(ID, status="rejected")))
    path = store / "archive" / "a.jsonl"
    path.parent.mkdir()
    path.write_bytes(b"  " + line.encode() + b" \t\r\n")
    (occ,) = _inv(store).occurrences
    assert occ.raw_bytes == b"  " + line.encode() + b" \t"


def test_bindings_items_are_validated(tmp_path):
    store = make_store(tmp_path)
    put_shard_line(store, "bindings", "R", [{"entity_id": "E", "tier": 9}])
    eff = effective(_inv(store), "bindings", "R")
    assert eff.raw is None and eff.reason == "unreadable"
