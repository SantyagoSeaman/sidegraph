"""The format-2 line, bucket, marker and shard-listing grammar (T-F1, T-F2, T-F3).

see design/superpowers/specs/2026-10-20-packed-store-format-design.md (§5.2, §5.3, §5.5)
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sidegraph.store_format import (
    UnencodableBody,
    archive_body,
    bucket,
    bucket_name,
    conflict_regions,
    decode_line,
    encode_body,
    encode_line,
    has_shard_files,
    list_shard_files,
    marker_text,
    parse_marker,
    render_shard,
    split_lines,
)

P = Path("/store/format")


class E(Exception):
    """Stand-in for the caller's ``malformed`` refusal."""


def _malformed(marker: Path, detail: str) -> Exception:
    return E(f"malformed store format marker at {marker}: {detail}")


def test_line_round_trip_keeps_raw_body():  # T-F1
    body = {"id": "01A", "z": None, "n": {"e": [], "o": {}}, "t": "a b", "unknown": 1}
    line = encode_line("decision", "01A", body)
    assert line.startswith(b'{"type":"decision","id":"01A","body":')
    assert decode_line(line).body == body
    assert decode_line(line + b"\r").body == body  # one \r stripped by split_lines
    assert split_lines(line + b"\r\n") == [(1, line)]
    assert b"\n" not in line


def test_archive_body_packs_without_record_type():  # T-F1, red: keep record_type
    archived = {"record_type": "decision", "id": "01A", "status": "superseded"}
    assert b"record_type" not in encode_line("decision", "01A", archive_body(archived))
    assert "record_type" in archived  # the input is copied, not mutated


def test_lone_surrogate_is_reported_not_packed():  # T-F1
    with pytest.raises(UnencodableBody):
        encode_body({"id": "01A", "t": "\ud800"})


def test_u2028_is_data_not_a_line_break():  # T-F1
    data = encode_line("fact", "01B", {"id": "01B", "t": "x y"}) + b"\n"
    assert len(split_lines(data)) == 1
    assert decode_line(split_lines(data)[0][1]).body == {"id": "01B", "t": "x y"}


IDS = ("01KX8NNGY546QH4W6EXWXDQ0Z2", "a", "é", "01M5DE0M5WHDTNWNJ5XKSAA50Z")


@pytest.mark.parametrize(
    "n,expected",
    [(16, (8, 12, 4, 13)), (64, (35, 50, 18, 55)), (256, (142, 202, 74, 223))],
)
def test_bucket_vectors(n, expected):  # T-F2: pins the function
    for rid, b in zip(IDS, expected, strict=True):
        assert bucket(rid, n) == b
    assert bucket_name(10) == "0a.shard"


def test_marker_major2_exact_grammar():  # T-F3
    m = parse_marker(
        "sidegraph-store 2.0.0\nrequires-sidegraph >= 0.17.0\nshards 64\n", P, malformed=_malformed
    )
    assert (m.major, m.requirement, m.shards) == (2, "0.17.0", 64)


@pytest.mark.parametrize(
    "text",
    [
        "sidegraph-store 2.0.0\n",
        "sidegraph-store 2.0.0\nrequires-sidegraph >= 0.17.0\n",  # mutant: default shard count
        "sidegraph-store 2.0.0\nshards 64\nrequires-sidegraph >= 0.17.0\n",
        "sidegraph-store 2.0.0\nrequires-sidegraph >= 0.17.0\nshards 32\n",
        "sidegraph-store 2.0.0\nrequires-sidegraph >= 0.17.0\nshards 64\nextra\n",
    ],
)
def test_marker_major2_malformed(text):
    with pytest.raises(E, match="malformed"):
        parse_marker(text, P, malformed=_malformed)


def test_marker_majors_0_and_1_and_text_round_trip():
    zero = parse_marker("sidegraph-store 0.1.0\n", P, malformed=_malformed)
    assert (zero.major, zero.requirement, zero.shards) == (0, None, None)
    one = parse_marker(
        "sidegraph-store 1.0.0\nrequires-sidegraph >= 0.14.0\n", P, malformed=_malformed
    )
    assert (one.major, one.requirement, one.shards) == (1, "0.14.0", None)
    two = marker_text(2, "0.17.0", 64)
    assert two == "sidegraph-store 2.0.0\nrequires-sidegraph >= 0.17.0\nshards 64\n"
    assert parse_marker(two, P, malformed=_malformed).shards == 64
    assert marker_text(1, "0.14.0", None) == "sidegraph-store 1.0.0\nrequires-sidegraph >= 0.14.0\n"
    assert marker_text(0, None, None) == "sidegraph-store 0.1.0\n"


def test_listing_classifies_by_lower_case(tmp_path):  # §5.3
    for name in ("00.shard", "0A.SHARD", "x.shard", "01.shard.abc.tmp", "notes.txt"):
        (tmp_path / name).write_bytes(b"")
    lst = list_shard_files(tmp_path, 64)
    assert set(lst.buckets) == {0}
    assert [p.name for p in lst.strays] == ["0A.SHARD", "x.shard"]  # red: case variant invisible
    assert lst.case_collisions == ["0A.SHARD"]


def test_listing_bucket_out_of_range_is_stray_and_dirs_ignored(tmp_path):
    (tmp_path / "ff.shard").write_bytes(b"")
    (tmp_path / "03.shard").mkdir()
    lst = list_shard_files(tmp_path, 16)
    assert lst.buckets == {}
    assert [p.name for p in lst.strays] == ["ff.shard"]
    assert has_shard_files(tmp_path.parent) is False
    (tmp_path.parent / "shards").mkdir()
    assert has_shard_files(tmp_path.parent) is False
    (tmp_path.parent / "shards" / "00.shard").write_bytes(b"")
    assert has_shard_files(tmp_path.parent) is True


def test_blank_and_unterminated_lines():  # Review Focus 1
    a = encode_line("fact", "01A", {"id": "01A"})
    b = encode_line("fact", "01B", {"id": "01B"})
    assert [ln for _, ln in split_lines(a + b"\n\n  \r\n" + b)] == [a, b]


def test_decode_recovers_identity_from_a_damaged_body():
    good = encode_line("decision", "01A", {"id": "01A", "t": "x"})
    damaged = good[:-3]
    out = decode_line(damaged)
    assert (out.record_type, out.record_id) == ("decision", "01A")
    assert out.body is None and out.error and out.raw == damaged
    junk = decode_line(b"not json")
    assert (junk.record_type, junk.record_id, junk.body) == (None, None, None)
    assert junk.error


def test_conflict_regions_and_render_shard():
    data = b"a\n<<<<<<< HEAD\nb\n=======\nc\n>>>>>>> x\nd\n"
    assert conflict_regions(data) == [(2, 6)]
    x = encode_line("fact", "01B", {"id": "01B"})
    y = encode_line("decision", "01Z", {"id": "01Z"})
    z = encode_line("decision", "01A", {"id": "01A"})
    out = render_shard([("fact", "01B", x), ("decision", "01Z", y), ("decision", "01A", z)])
    assert out == z + b"\n" + y + b"\n" + x + b"\n"
    assert render_shard([]) == b""


def test_unlistable_shards_dir_propagates(tmp_path):
    import os

    shards = tmp_path / "shards"
    shards.mkdir()
    (shards / "00.shard").write_bytes(b"")
    shards.chmod(0)
    try:
        if os.access(shards, os.R_OK):
            pytest.skip("permissions not enforced (root)")
        with pytest.raises(PermissionError):
            list_shard_files(shards, 64)
        with pytest.raises(PermissionError):
            has_shard_files(tmp_path)
    finally:
        shards.chmod(0o755)


def test_entry_stat_failure_propagates_but_a_vanished_entry_is_ignored(tmp_path, monkeypatch):
    (tmp_path / "00.shard").write_bytes(b"")
    (tmp_path / "01.shard").write_bytes(b"")
    real = Path.lstat

    def fake(self, *a, **k):
        if self.name == "00.shard":
            raise PermissionError("denied")
        if self.name == "01.shard":
            raise FileNotFoundError("gone")
        return real(self, *a, **k)

    monkeypatch.setattr(Path, "lstat", fake)
    with pytest.raises(PermissionError):
        list_shard_files(tmp_path, 64)
    (tmp_path / "00.shard").unlink()
    monkeypatch.setattr(Path, "lstat", fake)
    assert list_shard_files(tmp_path, 64).buckets == {}


def test_decode_is_strict_utf8_without_bom():
    good = encode_line("fact", "01A", {"id": "01A", "t": "x"})
    utf16 = good.decode("utf-8").encode("utf-16")
    assert decode_line(utf16).error and decode_line(utf16).body is None
    bom = b"\xef\xbb\xbf" + good
    out = decode_line(bom)
    assert out.error and out.body is None and out.raw == bom
    assert (out.record_type, out.record_id) == ("fact", "01A")
    bad = encode_line("fact", "01A", {"id": "01A", "t": "x"}).replace(b'"x"', b'"\xff"')
    out = decode_line(bad)
    assert out.error and out.body is None
    assert (out.record_type, out.record_id) == ("fact", "01A")


def test_duplicate_envelope_key_is_damaged():
    raw = b'{"type":"fact","id":"01A","body":{},"body":{"x":1}}'
    out = decode_line(raw)
    assert out.error and out.body is None
    assert (out.record_type, out.record_id) == ("fact", "01A")


def test_canonical_bytes_are_pinned():
    body = {"z": {"b": 1, "a": [1, {"d": None, "c": "é"}]}, "a": "ü x"}
    assert (
        encode_line("decision", "01A", body)
        == (
            '{"type":"decision","id":"01A","body":'
            '{"a":"ü x","z":{"a":[1,{"c":"é","d":null}],"b":1}}}'
        ).encode()
    )


@pytest.mark.parametrize(
    "text",
    [
        "sidegraph-store 2.0.0\nrequires-sidegraph >= 0.17.0\nshards 064\n",
        "sidegraph-store 2.0.0\nrequires-sidegraph >= 0.17.0\nshards ６４\n",
        "sidegraph-store 2.0.0\nrequires-sidegraph >= ０.17.0\nshards 64\n",
        "sidegraph-store ２.0.0\nrequires-sidegraph >= 0.17.0\nshards 64\n",
    ],
)
def test_marker_digits_are_ascii_and_shard_count_exact(text):
    with pytest.raises(E, match="malformed"):
        parse_marker(text, P, malformed=_malformed)


def test_unclosed_conflict_region_ends_at_the_last_line():
    assert conflict_regions(b"<<<<<<< a\nx\n") == [(1, 2)]
    assert conflict_regions(b"<<<<<<< a\nx") == [(1, 2)]


def test_lone_surrogate_id_is_unencodable():
    with pytest.raises(UnencodableBody):
        bucket("\ud800", 64)
