"""``sidegraph-verify`` snapshot layer (design/superpowers/specs/
2026-07-11-ci-integrity-design.md ruling 2): every test here builds a genuinely valid store
through the ``Store`` API, then corrupts exactly ONE thing per test by editing a canonical
file directly (never through ``Store``, which would just reject the corruption at the
write-path invariant it enforces) -- and asserts that ``verify_snapshot`` reports exactly
the one violation code that corruption is supposed to trip, on a store that is otherwise
clean. A clean store always yields ``[]``.
"""

from __future__ import annotations

import errno
import json
import os
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from sidegraph.cli import doctor_main, verify_main
from sidegraph.schema import (
    AnchorBinding,
    Decision,
    DecisionKind,
    DecisionStatus,
    Descriptor,
    Domain,
    Entity,
    Fact,
    Provenance,
)
from sidegraph.store import Store, _archive_record_line
from sidegraph.store_layout import CANONICAL_SUBDIRS
from sidegraph.verify import (
    BAD_ARCHIVE_SEGMENT,
    BAD_VALIDITY_WINDOW,
    DANGLING_BINDING_ENTITY,
    DANGLING_FACT_SUPPORT,
    DANGLING_PARENT,
    DANGLING_SUPERSEDES,
    DUPLICATE_ULID,
    FILENAME_ID_MISMATCH,
    PARENT_CYCLE,
    PARSE_ERROR,
    SUPERSEDED_WITHOUT_SUCCESSOR,
    SYMLINKED_STORE_ENTRY,
    UNKNOWN_SCHEMA_VERSION,
    Violation,
    verify_snapshot,
)


def _decision(**overrides) -> Decision:
    base = dict(
        title="Use file-per-record JSON",
        kind=DecisionKind.ADR,
        context="A single committed SQLite file can't be merged by git.",
        choice="One JSON file per record, plus a derived, gitignored local index.",
        valid_from=datetime(2026, 1, 10, tzinfo=UTC),
        provenance=Provenance(source="manual"),
    )
    base.update(overrides)
    return Decision(**base)


def _fact(**overrides) -> Fact:
    base = dict(
        statement="httpx retries idempotent requests by default.",
        source="httpx docs",
        valid_from=datetime(2026, 1, 10, tzinfo=UTC),
        provenance=Provenance(source="manual"),
    )
    base.update(overrides)
    return Fact(**base)


@pytest.fixture
def store(tmp_path) -> Store:
    with Store(tmp_path / ".sidegraph") as s:
        yield s


def _read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _write(path, data):
    path.write_text(json.dumps(data), encoding="utf-8")


def _decision_path(store: Store, decision_id: str):
    return store.path / "decisions" / f"{decision_id}.json"


def _fact_path(store: Store, fact_id: str):
    return store.path / "facts" / f"{fact_id}.json"


def _codes(violations: list[Violation]) -> list[str]:
    return [v.code for v in violations]


# -- clean store --------------------------------------------------------------------------


def test_clean_store_has_no_violations(store: Store):
    d = store.add_decision(_decision())
    entity = store.upsert_entity(
        Entity(canonical_name="f_widget", descriptor=Descriptor(name="f_widget", file_path="a.py"))
    )
    store.add_binding(AnchorBinding(record_id=d.id, entity_id=entity.entity_id, tier=2))
    store.add_fact(_fact(supports=[d.id]))
    store.add_domain(
        Domain(
            slug="payments",
            title="Payments",
            summary="Order settlement.",
            provenance=Provenance(source="manual"),
        )
    )
    assert verify_snapshot(store.path) == []


def test_empty_fresh_store_is_clean(tmp_path):
    with Store(tmp_path / ".sidegraph") as s:
        assert verify_snapshot(s.path) == []


# -- parse-error ---------------------------------------------------------------------------


def test_parse_error_on_truncated_json(store: Store):
    d = store.add_decision(_decision())
    path = _decision_path(store, d.id)
    text = path.read_text(encoding="utf-8")
    path.write_text(text[: len(text) // 2], encoding="utf-8")

    violations = verify_snapshot(store.path)
    assert _codes(violations) == [PARSE_ERROR]
    assert violations[0].path == str(path)


def test_parse_error_on_missing_required_field(store: Store):
    d = store.add_decision(_decision())
    path = _decision_path(store, d.id)
    data = _read(path)
    del data["choice"]  # required field
    _write(path, data)

    violations = verify_snapshot(store.path)
    assert _codes(violations) == [PARSE_ERROR]


# -- unknown-schema-version ------------------------------------------------------------------


def test_unknown_schema_version_on_bad_marker(store: Store):
    marker = store.path / "format"
    marker.write_text("sidegraph-store 9.9.9\n", encoding="utf-8")

    violations = verify_snapshot(store.path)
    assert _codes(violations) == [UNKNOWN_SCHEMA_VERSION]
    assert violations[0].path == str(marker)


def test_unknown_schema_version_on_missing_marker(store: Store):
    marker = store.path / "format"
    marker.unlink()

    violations = verify_snapshot(store.path)
    assert _codes(violations) == [UNKNOWN_SCHEMA_VERSION]


# -- bad-validity-window ---------------------------------------------------------------------


def test_bad_validity_window_on_decision(store: Store):
    d = store.add_decision(_decision())
    path = _decision_path(store, d.id)
    data = _read(path)
    data["valid_to"] = "2020-01-01T00:00:00+00:00"  # before valid_from (2026-01-10)
    _write(path, data)

    violations = verify_snapshot(store.path)
    assert _codes(violations) == [BAD_VALIDITY_WINDOW]
    assert violations[0].path == str(path)


def test_bad_validity_window_on_fact(store: Store):
    d = store.add_decision(_decision())
    f = store.add_fact(_fact(supports=[d.id]))
    path = _fact_path(store, f.id)
    data = _read(path)
    data["valid_to"] = "2020-01-01T00:00:00+00:00"
    _write(path, data)

    violations = verify_snapshot(store.path)
    assert _codes(violations) == [BAD_VALIDITY_WINDOW]
    assert violations[0].path == str(path)


# -- superseded-without-successor -------------------------------------------------------------


def test_superseded_without_successor(store: Store):
    d = store.add_decision(_decision())
    path = _decision_path(store, d.id)
    data = _read(path)
    data["status"] = "superseded"
    data["valid_to"] = "2026-02-01T00:00:00+00:00"  # keep the validity window itself legal
    _write(path, data)

    violations = verify_snapshot(store.path)
    assert _codes(violations) == [SUPERSEDED_WITHOUT_SUCCESSOR]
    assert violations[0].path == str(path)


def test_superseded_domain_without_successor(store: Store):
    dom = store.add_domain(
        Domain(
            slug="payments",
            title="Payments",
            summary="Order settlement.",
            provenance=Provenance(source="manual"),
        )
    )
    path = store.path / "domains" / f"{dom.domain_id}.json"
    data = _read(path)
    data["status"] = "superseded"
    _write(path, data)

    violations = verify_snapshot(store.path)
    assert _codes(violations) == [SUPERSEDED_WITHOUT_SUCCESSOR]


def test_supersede_via_store_api_is_clean(store: Store):
    """The normal, non-corrupted path -- add_decision(supersedes=...) closes the
    predecessor and writes the successor atomically -- must NOT trip either supersedes
    check; pins that the checks mirror the real rule rather than something stricter."""
    old = store.add_decision(_decision())
    store.add_decision(
        _decision(title="v2", supersedes=old.id, valid_from=datetime(2026, 2, 1, tzinfo=UTC))
    )
    assert verify_snapshot(store.path) == []


# -- dangling-supersedes ---------------------------------------------------------------------


def test_dangling_supersedes_on_decision(store: Store):
    d = store.add_decision(_decision())
    path = _decision_path(store, d.id)
    data = _read(path)
    data["supersedes"] = "01JUNKNOWNDECISIONIDXXXXX"
    _write(path, data)

    violations = verify_snapshot(store.path)
    assert _codes(violations) == [DANGLING_SUPERSEDES]
    assert violations[0].path == str(path)


def test_dangling_supersedes_on_domain(store: Store):
    dom = store.add_domain(
        Domain(
            slug="payments",
            title="Payments",
            summary="Order settlement.",
            provenance=Provenance(source="manual"),
        )
    )
    path = store.path / "domains" / f"{dom.domain_id}.json"
    data = _read(path)
    data["supersedes"] = "01JUNKNOWNDOMAINIDXXXXXXX"
    _write(path, data)

    violations = verify_snapshot(store.path)
    assert _codes(violations) == [DANGLING_SUPERSEDES]


@pytest.mark.parametrize("bad", [["01JANOTHERDECISIONIDXXXXX"], {"id": "x"}], ids=["list", "dict"])
def test_non_string_supersedes_is_reported_not_a_crash(store: Store, capsys, bad):
    """Unfixed, the successor set hashed the raw value and ``verify_snapshot`` raised
    ``TypeError: unhashable type``. The model check already owns the field: parse-error."""
    d = store.add_decision(_decision())
    path = _decision_path(store, d.id)
    data = _read(path)
    data["supersedes"] = bad
    _write(path, data)

    violations = verify_snapshot(store.path)
    assert _codes(violations) == [PARSE_ERROR]
    assert violations[0].path == str(path)
    assert verify_main(["--db", str(store.path)]) == 2
    assert "1 violation(s)" in capsys.readouterr().out


@pytest.mark.parametrize(
    "bad",
    [[["x"]], [{"a": 1}], 5, True, "abc"],
    ids=["nested-list", "dict-item", "int", "true", "str"],
)
def test_malformed_fact_supports_is_reported_not_a_crash(store: Store, capsys, bad):
    """Unfixed, ``did not in decision_ids`` hashed each raw item (TypeError for a list/dict),
    iterating an int/bool raised TypeError, and a string yielded one dangling-support per
    character. Only a list of strings is checked; the model check owns the rest."""
    d = store.add_decision(_decision())
    fa = store.add_fact(_fact(supports=[d.id]))
    path = _fact_path(store, fa.id)
    data = _read(path)
    data["supports"] = bad
    _write(path, data)

    violations = verify_snapshot(store.path)
    assert _codes(violations) == [PARSE_ERROR]
    assert violations[0].path == str(path)
    assert verify_main(["--db", str(store.path)]) == 2
    assert "1 violation(s)" in capsys.readouterr().out


def test_non_string_supersedes_on_a_fact_and_a_domain_is_reported(store: Store):
    f = store.add_fact(_fact())
    fpath = _fact_path(store, f.id)
    data = _read(fpath)
    data["supersedes"] = ["x"]
    _write(fpath, data)
    dom = store.add_domain(
        Domain(
            slug="payments",
            title="Payments",
            summary="Order settlement.",
            provenance=Provenance(source="manual"),
        )
    )
    dpath = store.path / "domains" / f"{dom.domain_id}.json"
    data = _read(dpath)
    data["supersedes"] = {"a": 1}
    _write(dpath, data)

    violations = verify_snapshot(store.path)
    assert sorted((v.code, v.path) for v in violations) == sorted(
        [(PARSE_ERROR, str(fpath)), (PARSE_ERROR, str(dpath))]
    )


def test_non_string_supersedes_in_an_archive_line_is_a_bad_archive_segment(store: Store):
    line = json.loads(_archived_decision_line())
    line["supersedes"] = ["x"]
    seg = _segment(store, json.dumps(line))

    violations = verify_snapshot(store.path)
    assert [(v.code, v.path) for v in violations] == [(BAD_ARCHIVE_SEGMENT, str(seg))]


# -- dangling-binding-entity -----------------------------------------------------------------


def test_dangling_binding_entity(store: Store):
    d = store.add_decision(_decision())
    entity = store.upsert_entity(
        Entity(canonical_name="f_widget", descriptor=Descriptor(name="f_widget", file_path="a.py"))
    )
    store.add_binding(AnchorBinding(record_id=d.id, entity_id=entity.entity_id, tier=2))
    (store.path / "entities" / f"{entity.entity_id}.json").unlink()

    violations = verify_snapshot(store.path)
    assert _codes(violations) == [DANGLING_BINDING_ENTITY]
    assert violations[0].path == str(store.path / "bindings" / f"{d.id}.json")


# -- dangling-fact-support -------------------------------------------------------------------


def test_dangling_fact_support(store: Store):
    d = store.add_decision(_decision())
    f = store.add_fact(_fact(supports=[d.id]))
    path = _fact_path(store, f.id)
    data = _read(path)
    data["supports"] = [d.id, "01JUNKNOWNDECISIONIDXXXXX"]
    _write(path, data)

    violations = verify_snapshot(store.path)
    assert _codes(violations) == [DANGLING_FACT_SUPPORT]
    assert violations[0].path == str(path)


# -- duplicate-ulid --------------------------------------------------------------------------


def test_duplicate_ulid_hot_and_archive(store: Store):
    d = store.add_decision(_decision())
    store.drop(d.id)
    payload = _read(_decision_path(store, d.id))
    archive_dir = store.path / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True)
    line = json.dumps({"record_type": "decision", **payload})
    (archive_dir / "2026-07-11-1-deadbeefcafe.jsonl").write_text(line + "\n", encoding="utf-8")

    violations = verify_snapshot(store.path)
    assert _codes(violations) == [DUPLICATE_ULID]


def test_archive_archive_byte_identical_duplicate_is_exempt(store: Store):
    """Two segments both listing the same domain id with BYTE-IDENTICAL payloads (e.g.
    compacted independently on two branches, later merged) is the store's own sanctioned
    shape (design amendment, Task-2 review — see store._archived_records's "guaranteed
    byte-identical by design") -- verify must NOT fail a legal cross-branch compaction
    merge, even though this remains a hot file with no other copy."""
    dom = store.add_domain(
        Domain(
            slug="payments",
            title="Payments",
            summary="Order settlement.",
            provenance=Provenance(source="manual"),
        )
    )
    store.ratify_domains(drop=[dom.domain_id])
    path = store.path / "domains" / f"{dom.domain_id}.json"
    payload = _read(path)
    path.unlink()  # a compacted record has no hot file left
    archive_dir = store.path / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True)
    line = json.dumps({"record_type": "domain", **payload})
    (archive_dir / "2026-07-10-1-aaaaaaaaaaaa.jsonl").write_text(line + "\n", encoding="utf-8")
    (archive_dir / "2026-07-11-1-bbbbbbbbbbbb.jsonl").write_text(line + "\n", encoding="utf-8")

    assert verify_snapshot(store.path) == []


def test_archive_archive_byte_different_duplicate_is_flagged(store: Store):
    """Two segments listing the same domain id with DIFFERING payloads is corruption-shaped
    (a terminal record's archived content can never legitimately change), not a sanctioned
    merge -- still flagged despite both copies living only in the archive."""
    dom = store.add_domain(
        Domain(
            slug="payments",
            title="Payments",
            summary="Order settlement.",
            provenance=Provenance(source="manual"),
        )
    )
    store.ratify_domains(drop=[dom.domain_id])
    path = store.path / "domains" / f"{dom.domain_id}.json"
    payload = _read(path)
    path.unlink()
    archive_dir = store.path / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True)
    line_a = json.dumps({"record_type": "domain", **payload})
    payload_b = {**payload, "summary": "A DIFFERENT summary than the original."}
    line_b = json.dumps({"record_type": "domain", **payload_b})
    (archive_dir / "2026-07-10-1-aaaaaaaaaaaa.jsonl").write_text(line_a + "\n", encoding="utf-8")
    (archive_dir / "2026-07-11-1-bbbbbbbbbbbb.jsonl").write_text(line_b + "\n", encoding="utf-8")

    violations = verify_snapshot(store.path)
    assert _codes(violations) == [DUPLICATE_ULID]


def test_hot_hot_duplicate_from_a_copied_file(store: Store):
    """Two hot files carrying the same internal id (e.g. ``cp decisions/<id>.json
    decisions/<id>-copy.json``) -- the store's file-per-record model allows at most one hot
    file per id, so this is always flagged, regardless of content. By construction the copy
    can never ALSO be correctly named (only one file can be named ``<id>.json``), so
    filename-id-mismatch fires alongside duplicate-ulid on the copy -- both are real,
    independent findings about the same corruption, not a bug in isolating them."""
    d = store.add_decision(_decision())
    original = _decision_path(store, d.id)
    copy_path = original.with_name(f"{d.id}-copy.json")
    copy_path.write_text(original.read_text(encoding="utf-8"), encoding="utf-8")

    violations = verify_snapshot(store.path)
    assert sorted(_codes(violations)) == sorted([DUPLICATE_ULID, FILENAME_ID_MISMATCH])
    mismatch = next(v for v in violations if v.code == FILENAME_ID_MISMATCH)
    assert mismatch.path == str(copy_path)  # only the copy's name fails to match the id


def test_filename_id_mismatch_on_renamed_hot_file(store: Store):
    """A single hot file, renamed so its filename no longer matches its own internal id --
    no duplication (the original name is gone), so this is a PURE filename-id-mismatch case,
    isolated from duplicate-ulid."""
    d = store.add_decision(_decision())
    original = _decision_path(store, d.id)
    renamed = original.with_name("renamed.json")
    original.rename(renamed)

    violations = verify_snapshot(store.path)
    assert _codes(violations) == [FILENAME_ID_MISMATCH]
    assert violations[0].path == str(renamed)


# -- bad-archive-segment ---------------------------------------------------------------------


def test_bad_archive_segment(store: Store):
    archive_dir = store.path / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True)
    (archive_dir / "2026-07-11-1-deadbeefcafe.jsonl").write_text(
        "{not valid json\n", encoding="utf-8"
    )

    violations = verify_snapshot(store.path)
    assert _codes(violations) == [BAD_ARCHIVE_SEGMENT]


# -- operational error ------------------------------------------------------------------------


def test_verify_snapshot_raises_on_missing_store_dir(tmp_path):
    with pytest.raises(NotADirectoryError):
        verify_snapshot(tmp_path / "does-not-exist")


# -- CLI: sidegraph-verify --------------------------------------------------------------------


def test_cli_exit_0_on_clean_store(tmp_path, capsys):
    with Store(tmp_path / ".sidegraph") as s:
        s.add_decision(_decision())
    rc = verify_main(["--db", str(tmp_path / ".sidegraph")])
    out = capsys.readouterr().out
    assert rc == 0
    assert out.strip() == "clean"


def test_cli_exit_2_and_human_output_on_violation(tmp_path, capsys):
    store_dir = tmp_path / ".sidegraph"
    with Store(store_dir) as s:
        d = s.add_decision(_decision())
        path = _decision_path(s, d.id)
    data = _read(path)
    data["supersedes"] = "01JUNKNOWNDECISIONIDXXXXX"
    _write(path, data)

    rc = verify_main(["--db", str(store_dir)])
    out = capsys.readouterr().out
    lines = out.strip().splitlines()
    assert rc == 2
    assert (
        lines[0]
        == f"{DANGLING_SUPERSEDES}  {path}  supersedes unknown record '01JUNKNOWNDECISIONIDXXXXX'"
    )
    assert lines[-1] == "1 violation(s)"


def test_cli_json_flag_prints_pure_json(tmp_path, capsys):
    store_dir = tmp_path / ".sidegraph"
    with Store(store_dir) as s:
        d = s.add_decision(_decision())
        path = _decision_path(s, d.id)
    data = _read(path)
    data["supersedes"] = "01JUNKNOWNDECISIONIDXXXXX"
    _write(path, data)

    rc = verify_main(["--db", str(store_dir), "--json"])
    out = capsys.readouterr().out
    parsed = json.loads(out)  # pure JSON -- nothing else on stdout
    assert rc == 2
    assert parsed["clean"] is False
    assert parsed["violations"] == [
        {
            "code": DANGLING_SUPERSEDES,
            "path": str(path),
            "detail": "supersedes unknown record '01JUNKNOWNDECISIONIDXXXXX'",
        }
    ]


def test_cli_json_clean_shape(tmp_path, capsys):
    store_dir = tmp_path / ".sidegraph"
    with Store(store_dir) as s:
        s.add_decision(_decision())

    rc = verify_main(["--db", str(store_dir), "--json"])
    out = capsys.readouterr().out
    parsed = json.loads(out)
    assert rc == 0
    assert parsed == {"clean": True, "violations": []}


def test_cli_exit_1_on_unreadable_store_dir(tmp_path, capsys):
    rc = verify_main(["--db", str(tmp_path / "does-not-exist")])
    out = capsys.readouterr().out
    assert rc == 1
    assert "not readable" in out


# -- unsafe-record-id (design/superpowers/specs/2026-09-29-record-identity-design.md D7) ---

UNSAFE_CODE = "unsafe-record-id"  # == verify.UNSAFE_RECORD_ID


def _unsafe_paths(store: Store) -> set[str]:
    return {v.path for v in verify_snapshot(store.path) if v.code == UNSAFE_CODE}


def test_verify_reports_unsafe_record_id_in_hot_file(store: Store):
    d = store.add_decision(_decision())
    stem = "01JCRAFTEDIDXXXXXXXXXXXXXX"
    _write(_decision_path(store, stem), {**_read(_decision_path(store, d.id)), "id": "../../x"})
    assert str(_decision_path(store, stem)) in _unsafe_paths(store)


def test_verify_reports_unsafe_hot_stem(store: Store):
    d = store.add_decision(_decision())
    path = _decision_path(store, ".hidden")
    _write(path, {**_read(_decision_path(store, d.id)), "id": ".hidden"})
    assert str(path) in _unsafe_paths(store)


def test_verify_reports_unsafe_bindings_stem(store: Store):
    path = store.path / "bindings" / ".x.json"
    _write(path, [])
    assert str(path) in _unsafe_paths(store)


def _store_with_a_domain(store: Store) -> tuple[dict[str, Path], dict[str, dict]]:
    """One real record in each of decisions/, facts/ and domains/: their paths and payloads."""
    d = store.add_decision(_decision())
    fact = store.add_fact(_fact(supports=[d.id]))
    domain = store.add_domain(
        Domain(slug="core", title="Core", summary="s", provenance=Provenance(source="manual"))
    )
    paths = {
        "decisions": _decision_path(store, d.id),
        "facts": _fact_path(store, fact.id),
        "domains": store.path / "domains" / f"{domain.domain_id}.json",
    }
    return paths, {kind: _read(path) for kind, path in paths.items()}


def _pairs(store: Store) -> list[tuple[str, str]]:
    return [(v.code, v.path) for v in verify_snapshot(store.path)]


@pytest.mark.parametrize("kind", ["decisions", "facts", "domains"])
def test_an_unsafe_filename_that_copies_a_real_record_reports_only_unsafe_record_id(
    store: Store, kind
):
    """The reload skips a file whose stem is unsafe, so ``filename-id-mismatch`` and
    ``duplicate-ulid`` would describe a file that never reaches the index."""
    paths, raws = _store_with_a_domain(store)
    copy = store.path / kind / ".x.json"
    _write(copy, raws[kind])
    assert _pairs(store) == [(UNSAFE_CODE, str(copy))]


@pytest.mark.parametrize("kind", ["decisions", "facts", "domains"])
def test_a_safe_stem_with_an_unsafe_id_reports_only_unsafe_record_id(store: Store, kind):
    """The reload skips this file too (an unsafe id), so no mismatch or parse-error follows."""
    paths, raws = _store_with_a_domain(store)
    id_field = "domain_id" if kind == "domains" else "id"
    crafted = store.path / kind / "01JCRAFTEDIDXXXXXXXXXXXXXX.json"
    _write(crafted, {**raws[kind], id_field: "../x"})
    assert _pairs(store) == [(UNSAFE_CODE, str(crafted))]


@pytest.mark.parametrize("kind", ["decisions", "facts", "domains", "bindings"])
@pytest.mark.parametrize("content", [b"not json", b"\xff\xfe", b"[1]", b"{}"])
def test_an_unsafe_filename_is_reported_whether_or_not_it_parses(store: Store, kind, content):
    """Parsing first turned a non-JSON, non-UTF-8 or wrong-shaped file into a bare
    ``parse-error``; the filename is the primary finding."""
    _store_with_a_domain(store)
    path = store.path / kind / ".y.json"
    path.write_bytes(content)
    assert _pairs(store) == [(UNSAFE_CODE, str(path))]


def test_a_safe_stem_that_does_not_parse_is_still_a_parse_error(store: Store):
    _store_with_a_domain(store)
    path = store.path / "decisions" / "01JBROKENXXXXXXXXXXXXXXXXX.json"
    path.write_text("not json", encoding="utf-8")
    assert _pairs(store) == [("parse-error", str(path))]


def test_a_reference_to_a_stem_skipped_file_is_not_dangling(store: Store):
    """The heal for a file skipped for its name is to rename it back, so a fact that supports
    its id raises nothing at the fact."""
    paths, raws = _store_with_a_domain(store)
    skipped = store.path / "decisions" / ".x.json"
    _write(skipped, {**raws["decisions"], "id": "01JSKIPPEDXXXXXXXXXXXXXXXX"})
    fact = store.path / "facts" / "01JSUPPORTSXXXXXXXXXXXXXXX.json"
    _write(fact, {**raws["facts"], "id": fact.stem, "supports": ["01JSKIPPEDXXXXXXXXXXXXXXXX"]})
    assert _pairs(store) == [(UNSAFE_CODE, str(skipped))]


@pytest.mark.parametrize("skipped_name", ["01JCRAFTEDIDXXXXXXXXXXXXXX.json", ".x.json"])
def test_a_reference_to_an_unsafe_id_still_dangles(store: Store, skipped_name):
    """An unsafe id is nobody's legitimate target, so the file is not offered as one, whether
    the file is skipped for its id alone or for its name too."""
    paths, raws = _store_with_a_domain(store)
    crafted = store.path / "decisions" / skipped_name
    _write(crafted, {**raws["decisions"], "id": "../x"})
    fact = store.path / "facts" / "01JSUPPORTSXXXXXXXXXXXXXXX.json"
    _write(fact, {**raws["facts"], "id": fact.stem, "supports": ["../x"]})
    assert sorted(_pairs(store)) == sorted(
        [("dangling-fact-support", str(fact)), (UNSAFE_CODE, str(crafted))]
    )


@pytest.mark.parametrize("direction", ["successor-renamed", "predecessor-renamed"])
def test_renaming_one_side_of_a_supersede_chain_reports_only_the_renamed_file(
    store: Store, direction
):
    """A accepted, superseded by B, fact F supports A. Renaming one file to ``.<name>`` must not
    fan out into findings at the untouched files, which only the rename can heal."""
    a = store.add_decision(_decision(title="a"))
    store.ratify(a.id)
    b = store.add_decision(_decision(title="b", supersedes=a.id))
    store.add_fact(_fact(supports=[a.id]))
    assert verify_snapshot(store.path) == []
    moved = _decision_path(store, a.id if direction == "predecessor-renamed" else b.id)
    renamed = moved.with_name(f".{moved.name}")
    moved.rename(renamed)
    assert _pairs(store) == [(UNSAFE_CODE, str(renamed))]


@pytest.mark.parametrize("renamed", ["predecessor", "successor"])
@pytest.mark.parametrize("kind", ["facts", "domains"])
def test_renaming_one_side_of_a_fact_or_domain_chain_reports_only_the_renamed_file(
    store: Store, kind, renamed
):
    if kind == "facts":
        old = store.add_fact(_fact(statement="p"))
        new = store.add_fact(_fact(statement="q", supersedes=old.id))
        ids = (old.id, new.id)
    else:
        dom = Domain(slug="core", title="Core", summary="s", provenance=Provenance(source="m"))
        old = store.add_domain(dom)
        new = store.supersede_domain(
            old.domain_id,
            Domain(
                slug="core",
                title="Core 2",
                summary="s",
                supersedes=old.domain_id,
                provenance=Provenance(source="m"),
            ),
        )
        ids = (old.domain_id, new.domain_id)
    assert verify_snapshot(store.path) == []
    moved = store.path / kind / f"{ids[0] if renamed == 'predecessor' else ids[1]}.json"
    target = moved.with_name(f".{moved.name}")
    moved.rename(target)
    assert _pairs(store) == [(UNSAFE_CODE, str(target))]


def test_renaming_an_entity_file_reports_only_the_renamed_file(store: Store):
    d = store.add_decision(_decision())
    entity = store.upsert_entity(
        Entity(canonical_name="f_widget", descriptor=Descriptor(name="f_widget", file_path="a.py"))
    )
    store.add_binding(AnchorBinding(record_id=d.id, entity_id=entity.entity_id, tier=2))
    assert verify_snapshot(store.path) == []
    moved = store.path / "entities" / f"{entity.entity_id}.json"
    target = moved.with_name(f".{moved.name}")
    moved.rename(target)
    assert _pairs(store) == [(UNSAFE_CODE, str(target))]


def test_verify_reports_unsafe_archive_line(store: Store):
    d = store.add_decision(_decision())
    archive_dir = store.path / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True)
    line = json.dumps(
        {"record_type": "decision", **_read(_decision_path(store, d.id)), "id": "../x"}
    )
    seg = archive_dir / "2026-07-11-1-deadbeefcafe.jsonl"
    seg.write_text(line + "\n", encoding="utf-8")
    assert str(seg) in _unsafe_paths(store)


def test_verify_tolerates_list_id(store: Store):
    d = store.add_decision(_decision())
    raw = _read(_decision_path(store, d.id))
    hot = _decision_path(store, "01JLISTIDXXXXXXXXXXXXXXXXX")
    _write(hot, {**raw, "id": ["a"]})
    facts = store.path / "domains" / "01JLISTDOMXXXXXXXXXXXXXXXX.json"
    _write(facts, {"domain_id": {"a": 1}})
    archive_dir = store.path / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True)
    seg = archive_dir / "2026-07-11-1-deadbeefcafe.jsonl"
    seg.write_text(
        json.dumps({"record_type": "decision", **raw, "id": ["a"]}) + "\n", encoding="utf-8"
    )
    found = _unsafe_paths(store)  # must not raise TypeError: unhashable type
    assert {str(hot), str(facts), str(seg)} <= found


@pytest.mark.parametrize("record_type", [["decision"], {"a": 1}, 7, None])
def test_verify_tolerates_a_non_string_record_type(store: Store, record_type):
    d = store.add_decision(_decision())
    archive_dir = store.path / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True)
    seg = archive_dir / "2026-07-11-1-deadbeefcafe.jsonl"
    seg.write_text(
        json.dumps({**_read(_decision_path(store, d.id)), "record_type": record_type}) + "\n",
        encoding="utf-8",
    )
    found = verify_snapshot(store.path)  # must not raise TypeError: unhashable type
    assert not [v for v in found if v.path == str(seg)]


@pytest.mark.parametrize("shape", ["missing", "non-string"])
def test_verify_reports_an_archive_line_with_a_missing_or_non_string_id(store: Store, shape):
    d = store.add_decision(_decision())
    raw = _read(_decision_path(store, d.id))
    if shape == "missing":
        del raw["id"]
    else:
        raw["id"] = 7
    archive_dir = store.path / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True)
    seg = archive_dir / "2026-07-11-1-deadbeefcafe.jsonl"
    seg.write_text(json.dumps({"record_type": "decision", **raw}) + "\n", encoding="utf-8")
    found = [v for v in verify_snapshot(store.path) if v.path == str(seg)]
    assert [v.code for v in found] == [UNSAFE_CODE]


def test_verify_reports_symlinked_store_entry(store: Store, tmp_path):
    """Every symlinked store-owned entry is one violation (not just the first): a committed
    link fails the CI gate at PR time, though ``Store()`` itself would refuse to open."""
    store.add_decision(_decision())
    outside = tmp_path / "outside"
    outside.mkdir()
    (store.path / "decisions").rename(outside / "decisions")
    (store.path / "decisions").symlink_to(outside / "decisions", target_is_directory=True)
    (store.path / "index.db").rename(outside / "index.db")
    (store.path / "index.db").symlink_to(outside / "index.db")

    violations = verify_snapshot(store.path)

    linked = [v for v in violations if v.code == SYMLINKED_STORE_ENTRY]
    assert sorted(v.path for v in linked) == [
        str(store.path / "decisions"),
        str(store.path / "index.db"),
    ]


def _git_init(repo: Path) -> None:
    for args in (
        ["init", "-q"],
        ["config", "user.email", "t@example.com"],
        ["config", "user.name", "T"],
        ["add", "-A"],
        ["commit", "-q", "--allow-empty", "-m", "initial"],
    ):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


def _crossing_store(store: Store) -> None:
    """Records that reference each other across directories: a compacted d1 (archive) superseded
    by d2, a binding of d2 onto an entity, and a fact supporting d2. Cutting any one pool off
    would leave a cross-reference dangling, which is the noise a link must not cause."""
    d1 = store.add_decision(_decision())
    store.ratify(d1.id)
    d2 = store.add_decision(_decision(title="v2", supersedes=d1.id))
    entity = store.upsert_entity(
        Entity(canonical_name="f_widget", descriptor=Descriptor(name="f_widget", file_path="a.py"))
    )
    store.add_binding(AnchorBinding(record_id=d2.id, entity_id=entity.entity_id, tier=2))
    store.add_fact(_fact(supports=[d2.id]))
    store.compact()
    assert verify_snapshot(store.path) == []


def _link(store: Store, name: str, target: Path) -> Path:
    link = store.path / name
    if target.exists():  # onto another store dir: the original is dropped
        shutil.rmtree(link)
    else:  # out of the store: the original moves to the target
        link.rename(target)
    link.symlink_to(target, target_is_directory=True)
    return link


def _link_cases(store: Store, tmp_path: Path, case: str) -> Path:
    """Link one store dir out of the store (moved there) or onto another store dir."""
    if case == "decisions->facts":
        return _link(store, "decisions", store.path / "facts")
    outside = tmp_path / "outside"
    outside.mkdir()
    return _link(store, case, outside / case)


LINK_CASES = ["entities", "decisions", "archive", "facts", "bindings", "decisions->facts"]
STOP_LINE = (
    "remaining checks skipped (the store has symlinked entries — remove the links and rerun)"
)


@pytest.mark.parametrize("case", LINK_CASES)
def test_a_symlinked_store_dir_stops_verify_at_the_link(store: Store, tmp_path, capsys, case):
    """A store with a symlinked internal is one ``Store()`` refuses to open: verify reports each
    link and stops. No record is linted through it and no cross-reference is checked against a
    cut-off pool (no dangling-binding-entity, superseded-without-successor, ...)."""
    _crossing_store(store)
    link = _link_cases(store, tmp_path, case)
    expected = [(SYMLINKED_STORE_ENTRY, str(link))]

    assert [(v.code, v.path) for v in verify_snapshot(store.path)] == expected

    assert verify_main(["--db", str(store.path)]) == 2
    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith(f"{SYMLINKED_STORE_ENTRY}  {link}  ")
    assert out[1:] == [STOP_LINE, "1 violation(s)"]

    assert doctor_main(["--db", str(store.path)]) == 2
    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith(f"{SYMLINKED_STORE_ENTRY}  {link}  ")
    assert out[1:] == [STOP_LINE, "1 violation(s), 0 finding(s)"]


def test_symlinked_store_dir_json_output_keeps_its_shape(store: Store, tmp_path, capsys):
    _crossing_store(store)
    link = _link_cases(store, tmp_path, "entities")

    assert verify_main(["--db", str(store.path), "--json"]) == 2
    report = json.loads(capsys.readouterr().out)
    assert set(report) == {"clean", "violations"}
    assert [v["path"] for v in report["violations"]] == [str(link)]

    assert doctor_main(["--db", str(store.path), "--json"]) == 2
    report = json.loads(capsys.readouterr().out)
    assert report["clean"] is False
    assert [v["path"] for v in report["violations"]] == [str(link)]
    assert report["findings"] == []
    assert report["skipped"] == ["advisory-checks"]


@pytest.mark.parametrize("name", ["decisions", "facts", "domains", "entities", "archive"])
def test_a_symlinked_store_dir_with_foreign_files_is_not_linted(store: Store, tmp_path, name):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "junk.json").write_text("not json", encoding="utf-8")
    (outside / ".x.json").write_text("[]", encoding="utf-8")
    (outside / "junk.jsonl").write_text("not json\n", encoding="utf-8")
    link = store.path / name
    if link.is_dir() and not link.is_symlink():
        link.rmdir()
    link.symlink_to(outside, target_is_directory=True)

    assert [(v.code, v.path) for v in verify_snapshot(store.path)] == [
        (SYMLINKED_STORE_ENTRY, str(link))
    ]


def test_a_symlinked_store_root_is_still_checked(store: Store, tmp_path, capsys):
    """Only a store-owned entry inside the store stops the run: the root may be a link."""
    d = store.add_decision(_decision())
    root_link = tmp_path / "link-to-store"
    root_link.symlink_to(store.path, target_is_directory=True)
    path = _decision_path(store, d.id)
    _write(path, {**_read(path), "id": "01SOMEOTHERID"})  # a real violation, found via the link

    found = [(v.code, v.path) for v in verify_snapshot(root_link)]

    assert found == [(FILENAME_ID_MISMATCH, str(root_link / "decisions" / path.name))]
    assert verify_main(["--db", str(root_link)]) == 2
    assert STOP_LINE not in capsys.readouterr().out


def test_a_store_reached_through_a_symlinked_parent_is_fully_linted(tmp_path, capsys):
    real = tmp_path / "real"
    with Store(real / ".sidegraph") as s:
        d = s.add_decision(_decision())
        path = _decision_path(s, d.id)
    _write(path, {**_read(path), "id": "01SOMEOTHERID"})
    parent_link = tmp_path / "parent-link"
    parent_link.symlink_to(real, target_is_directory=True)

    found = [(v.code, v.path) for v in verify_snapshot(parent_link / ".sidegraph")]

    assert found == [
        (FILENAME_ID_MISMATCH, str(parent_link / ".sidegraph" / "decisions" / path.name))
    ]
    assert verify_main(["--db", str(parent_link / ".sidegraph")]) == 2
    assert STOP_LINE not in capsys.readouterr().out


def test_a_committed_dir_link_that_dangles_stops_at_the_link(store: Store, tmp_path, capsys):
    """A link committed to the repository dangles in a fresh clone: only the link is reported."""
    _crossing_store(store)
    shutil.rmtree(store.path / "entities")
    link = store.path / "entities"
    link.symlink_to(tmp_path / "nowhere", target_is_directory=True)

    assert [(v.code, v.path) for v in verify_snapshot(store.path)] == [
        (SYMLINKED_STORE_ENTRY, str(link))
    ]
    assert verify_main(["--db", str(store.path)]) == 2
    out = capsys.readouterr().out.splitlines()
    assert out[1:] == [STOP_LINE, "1 violation(s)"]


def test_a_symlinked_format_marker_is_never_read(store: Store, tmp_path, capsys):
    """The marker's text goes into a violation detail; a link to any readable file would print
    that file in a CI log. The link is the finding, and its target is not opened."""
    secret = tmp_path / "secret.txt"
    secret.write_text("line1 TOKEN=abc123\nline2 more\n", encoding="utf-8")
    marker = store.path / "format"
    marker.unlink()
    marker.symlink_to(secret)

    assert [(v.code, v.path) for v in verify_snapshot(store.path)] == [
        (SYMLINKED_STORE_ENTRY, str(marker))
    ]
    for main in (verify_main, doctor_main):
        assert main(["--db", str(store.path)]) == 2
        out = capsys.readouterr().out
        assert "TOKEN=abc123" not in out
        assert STOP_LINE in out
        assert main(["--db", str(store.path), "--json"]) == 2
        assert "TOKEN=abc123" not in capsys.readouterr().out


@pytest.mark.parametrize("main", [verify_main, doctor_main])
def test_a_bad_ref_with_a_symlinked_store_dir_is_still_an_operational_error(
    store: Store, tmp_path, capsys, main
):
    """An unresolvable ``--against`` ref is exit 1 whatever the store holds; the links must not
    turn it into the violation exit."""
    _link_cases(store, tmp_path, "entities")
    _git_init(store.path.parent)

    assert main(["--db", str(store.path), "--against", "no-such-ref"]) == 1
    assert "no-such-ref" in capsys.readouterr().out


@pytest.mark.parametrize("main", [verify_main, doctor_main])
def test_against_outside_git_with_a_symlinked_store_dir_is_an_operational_error(
    store: Store, tmp_path, capsys, main
):
    _link_cases(store, tmp_path, "entities")

    assert main(["--db", str(store.path), "--against", "HEAD"]) == 1
    capsys.readouterr()


def test_an_unsafe_bindings_stem_adds_no_dangling_entity_finding(store: Store, capsys):
    """The reload skips a bindings file with an unsafe stem entirely, so its items are not
    checked against the entities: the unsafe stem is the one finding."""
    path = store.path / "bindings" / ".x.json"
    _write(path, [{"entity_id": "01NOSUCHENTITY", "tier": 2}])

    violations = verify_snapshot(store.path)

    assert [(v.code, v.path) for v in violations] == [(UNSAFE_CODE, str(path))]
    assert verify_main(["--db", str(store.path)]) == 2
    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith(f"{UNSAFE_CODE}  {path}  ")
    assert out[1:] == ["1 violation(s)"]


def test_a_record_file_of_invalid_utf8_is_a_parse_error_naming_its_path(tmp_path, capsys):
    """Unfixed, ``_load_raw_json`` let the ``UnicodeDecodeError`` through: ``verify_snapshot``
    raised, and the CLI exited 1 with "store not readable" and no file name.
    see design/superpowers/specs/2026-10-03-store-survives-a-bad-file-design.md (D2, T14)"""
    store_dir = tmp_path / ".sidegraph"
    with Store(store_dir) as s:
        d = s.add_decision(_decision())
        path = _decision_path(s, d.id)
    path.write_bytes(b'{"id": "\xff\xfe"}')

    violations = verify_snapshot(store_dir)
    assert [(v.code, v.path) for v in violations] == [(PARSE_ERROR, str(path))]

    rc = verify_main(["--db", str(store_dir)])
    out = capsys.readouterr().out
    assert rc == 2
    assert str(path) in out
    assert "not readable" not in out


# -- archive segments: verify lists what the reload skips -----------------------------------
# The store's reload skips an archive line it cannot read and tells the human to run verify, so
# verify must name the same lines (design/superpowers/specs/2026-10-03-store-survives-a-bad-
# file-design.md D4).

SEGMENT_NAME = "2026-07-11-1-deadbeefcafe.jsonl"


def _archived_decision_line(**overrides) -> str:
    """One archive line for a terminal decision that has no hot file."""
    decision = _decision(
        status=DecisionStatus.REJECTED, valid_to=datetime(2026, 1, 11, tzinfo=UTC), **overrides
    )
    return _archive_record_line("decision", decision.model_dump(mode="json"))


def _segment(store: Store, *lines: str | bytes) -> Path:
    archive_dir = store.path / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True)
    path = archive_dir / SEGMENT_NAME
    path.write_bytes(
        b"\n".join(x if isinstance(x, bytes) else x.encode("utf-8") for x in lines) + b"\n"
    )
    return path


def test_an_archive_line_of_invalid_utf8_is_a_bad_archive_segment_naming_its_line(
    store: Store, capsys
):
    """Unfixed, the whole-file ``read_text`` raised ``UnicodeDecodeError``: ``verify_snapshot``
    raised, and the CLI exited 1 with "store not readable"."""
    seg = _segment(
        store,
        _archived_decision_line(),
        b'{"record_type": "decision", "title": "\xff\xfe"}',
    )

    violations = verify_snapshot(store.path)
    assert [(v.code, v.path) for v in violations] == [(BAD_ARCHIVE_SEGMENT, str(seg))]
    assert "line 2" in violations[0].detail

    assert verify_main(["--db", str(store.path)]) == 2
    out = capsys.readouterr().out
    assert str(seg) in out
    assert "not readable" not in out


def test_an_archive_line_nested_past_the_recursion_limit_is_a_bad_archive_segment(store: Store):
    seg = _segment(store, _archived_decision_line(), "[" * 200_000 + "]" * 200_000)

    violations = verify_snapshot(store.path)
    assert [(v.code, v.path) for v in violations] == [(BAD_ARCHIVE_SEGMENT, str(seg))]
    assert "line 2" in violations[0].detail


@pytest.mark.parametrize("fault", ["invalid status", "lone surrogate"])
def test_an_archive_line_whose_decision_does_not_validate_is_a_bad_archive_segment(
    store: Store, fault
):
    """The store lists such a segment (its reload runs the model on every archive line);
    unfixed, ``verify_snapshot`` returned ``[]`` for it."""
    payload = json.loads(_archived_decision_line())
    if fault == "invalid status":
        payload["status"] = "bogus"
    else:
        payload["title"] = "\ud800"  # parses and validates, then cannot be serialised
    seg = _segment(store, _archived_decision_line(), json.dumps(payload))

    violations = verify_snapshot(store.path)
    assert [(v.code, v.path) for v in violations] == [(BAD_ARCHIVE_SEGMENT, str(seg))]
    assert "line 2" in violations[0].detail


@pytest.mark.parametrize("char", ["\u2028", "\u0085"])
def test_an_archive_line_holding_a_unicode_line_separator_is_not_a_bad_segment(store: Store, char):
    """``str.splitlines`` splits a title that holds U+2028 or U+0085 in two, so unfixed,
    verify reported ``bad-archive-segment`` for a segment the store reads fine."""
    _segment(store, _archived_decision_line(title=f"before{char}after"))

    assert verify_snapshot(store.path) == []


# -- hot files: what the reload skips as ``parse-error`` ------------------------------------


def test_a_record_file_nested_past_the_recursion_limit_is_a_parse_error_naming_its_path(
    store: Store, capsys
):
    """Unfixed, ``json.loads`` raised ``RecursionError`` through ``_load_raw_json``: the CLI
    exited 1 with "maximum recursion depth" and no file name."""
    d = store.add_decision(_decision())
    path = _decision_path(store, d.id)
    path.write_text("[" * 200_000 + "]" * 200_000, encoding="utf-8")

    violations = verify_snapshot(store.path)
    assert [(v.code, v.path) for v in violations] == [(PARSE_ERROR, str(path))]

    assert verify_main(["--db", str(store.path)]) == 2
    out = capsys.readouterr().out
    assert str(path) in out
    assert "not readable" not in out


@pytest.mark.parametrize("kind", ["decision", "entity", "bindings"])
def test_a_record_that_validates_but_cannot_be_serialised_is_a_parse_error(store: Store, kind):
    """A lone surrogate passes the model and fails ``model_dump_json``, which the store's
    reload runs on every record and skips the file for; unfixed, verify reported it clean."""
    d = store.add_decision(_decision())
    entity = store.upsert_entity(
        Entity(canonical_name="f_widget", descriptor=Descriptor(name="f_widget", file_path="a.py"))
    )
    store.add_binding(AnchorBinding(record_id=d.id, entity_id=entity.entity_id, tier=2))
    if kind == "decision":
        path = _decision_path(store, d.id)
        _write(path, {**_read(path), "title": "\ud800"})
    elif kind == "entity":
        path = store.path / "entities" / f"{entity.entity_id}.json"
        _write(path, {**_read(path), "canonical_name": "\ud800"})
    else:
        path = store.path / "bindings" / f"{d.id}.json"
        _write(path, [{**item, "status": "\ud800"} for item in _read(path)])

    violations = verify_snapshot(store.path)
    assert [v.code for v in violations] == [PARSE_ERROR]
    assert violations[0].path.startswith(str(path))


def test_against_reader_reports_invalid_utf8_and_deep_nesting_as_unparsable(tmp_path):
    """``--against`` reads the working-tree side through ``_read_json_object``. A record file of
    invalid UTF-8, or JSON nested past the recursion limit, is unparsable content to report,
    not a crash that takes verify down with exit 1.
    see design/superpowers/specs/2026-10-03-store-survives-a-bad-file-design.md D2"""
    from sidegraph.verify import _read_json_object, _UnparsableContent

    bad_utf8 = tmp_path / "a.json"
    bad_utf8.write_bytes(b'{"id": "\xff"}')
    deep = tmp_path / "b.json"
    deep.write_text("[" * 100_000 + "]" * 100_000, encoding="utf-8")
    for path in (bad_utf8, deep):
        with pytest.raises(_UnparsableContent):
            _read_json_object(path)


# -- an unreadable store directory reads as unreadable on every Python ----------------------
# Python 3.13 re-raises EACCES from Path.is_dir/is_file/is_symlink; 3.14 returns False. Unfixed,
# 3.14 took the "format marker missing" (or "store directory not found") path and reported
# unknown-schema-version, exit 2, for a store whose directory has no search bit.


def _as_3_14_for(monkeypatch, *, stat_of: Path | None = None, lstat_of: Path | None = None):
    """Make every Python and uid behave like 3.14 on an unsearchable path: the pathlib
    predicates say False for anything at or under it, and ``os.stat``/``os.lstat`` of the
    named path is EACCES (the shape of mode 0600: readable, not searchable)."""
    blocked = [os.path.abspath(x) for x in (stat_of, lstat_of) if x is not None]

    def _hit(p) -> bool:
        a = os.path.abspath(p)
        return any(a == b or a.startswith(b + os.sep) for b in blocked)

    for name in ("is_file", "is_symlink", "exists", "is_dir"):
        real = getattr(Path, name)

        def wrapper(self, *a, _real=real, **k):
            return False if _hit(self) else _real(self, *a, **k)

        monkeypatch.setattr(Path, name, wrapper)
    for name, target in (("stat", stat_of), ("lstat", lstat_of)):
        if target is None:
            continue
        real_fn = getattr(os, name)
        abs_target = os.path.abspath(target)

        def probe(path, *a, _real=real_fn, _t=abs_target, **k):
            if os.path.abspath(path) == _t:
                raise PermissionError(errno.EACCES, "Permission denied", str(path))
            return _real(path, *a, **k)

        monkeypatch.setattr(os, name, probe)


@pytest.mark.parametrize("cmd", ["verify", "doctor"])
@pytest.mark.parametrize("what", ["marker", "store-dir"])
def test_an_unsearchable_store_is_not_readable_not_a_missing_marker(
    cmd, what, tmp_path, monkeypatch, capsys
):
    store_dir = tmp_path / ".sidegraph"
    Store(store_dir).close()
    if what == "marker":  # mode 0600 on the store dir
        _as_3_14_for(monkeypatch, lstat_of=store_dir / "format")
    else:  # an unsearchable parent: the store dir itself cannot be stat'ed
        _as_3_14_for(monkeypatch, stat_of=store_dir)

    main = verify_main if cmd == "verify" else doctor_main
    assert main(["--db", str(store_dir)]) == 1
    out = capsys.readouterr().out
    assert out.splitlines()[0] == (
        f"store not readable ({store_dir}): [Errno 13] Permission denied: '{store_dir}'"
    )
    assert "unknown-schema-version" not in out
    assert "not found" not in out


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores modes")
@pytest.mark.parametrize("cmd", ["verify", "doctor"])
@pytest.mark.parametrize("mode", [0o000, 0o600])
def test_a_store_dir_without_the_search_bit_is_not_readable_end_to_end(cmd, mode, tmp_path, capsys):
    store_dir = tmp_path / ".sidegraph"
    Store(store_dir).close()
    store_dir.chmod(mode)
    try:
        main = verify_main if cmd == "verify" else doctor_main
        assert main(["--db", str(store_dir)]) == 1
        out = capsys.readouterr().out
    finally:
        store_dir.chmod(0o755)
    # names the directory to chmod, not the marker (whose own mode is fine)
    assert out.splitlines()[0] == (
        f"store not readable ({store_dir}): [Errno 13] Permission denied: '{store_dir}'"
    )
    assert "unknown-schema-version" not in out


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores modes")
@pytest.mark.parametrize("cmd", ["verify", "doctor"])
def test_an_unsearchable_parent_is_not_readable_not_a_missing_directory(cmd, tmp_path, capsys):
    parent = tmp_path / "parent"
    store_dir = parent / ".sidegraph"
    Store(store_dir).close()
    parent.chmod(0o600)
    try:
        main = verify_main if cmd == "verify" else doctor_main
        assert main(["--db", str(store_dir)]) == 1
        out = capsys.readouterr().out
    finally:
        parent.chmod(0o755)
    assert out.splitlines()[0].startswith(f"store not readable ({store_dir}): ")
    assert "not found" not in out


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores modes")
@pytest.mark.parametrize("cmd", ["verify", "doctor"])
def test_a_searchable_unlistable_store_dir_is_checked_normally(cmd, tmp_path, capsys):
    """Mode 0300: ``Store()`` works on it, so the lint must not call it unreadable."""
    store_dir = tmp_path / ".sidegraph"
    Store(store_dir).close()
    store_dir.chmod(0o300)
    try:
        main = verify_main if cmd == "verify" else doctor_main
        rc = main(["--db", str(store_dir)])
        out = capsys.readouterr().out
    finally:
        store_dir.chmod(0o755)
    assert rc == 0
    assert "store not readable" not in out
    assert "unknown-schema-version" not in out


# -- an unreadable record directory is a permission problem, not missing records ----------


def _store_in_git(tmp_path: Path) -> Store:
    """A committed store whose facts support its decisions and whose bindings name entities, so
    an emptied decisions pool would dangle the facts and read as deleted against HEAD."""
    with Store(tmp_path / ".sidegraph") as s:
        for n in range(2):
            d = s.add_decision(_decision(title=f"d{n}"))
            e = s.upsert_entity(
                Entity(
                    canonical_name=f"f{n}",
                    descriptor=Descriptor(name=f"f{n}", file_path="a.py"),
                )
            )
            s.add_binding(AnchorBinding(record_id=d.id, entity_id=e.entity_id, tier=2))
            s.add_fact(_fact(supports=[d.id]))
    _git_init(tmp_path)
    return s


def _chmod_or_skip(path: Path, mode: int) -> None:
    """chmod ``path``, skipping the test where the mode changes nothing (root, odd filesystems)."""
    path.chmod(mode)
    try:
        names = os.listdir(path)
        if names:
            os.lstat(path / names[0])
    except PermissionError:
        return
    path.chmod(0o755)
    pytest.skip("chmod has no effect here")


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores modes")
@pytest.mark.parametrize("subdir", ["decisions", "facts", "bindings"])
@pytest.mark.parametrize("mode", [0o000, 0o100, 0o400, 0o600])  # none, search only, read only, rw
@pytest.mark.parametrize("cmd", ["verify", "verify-against", "doctor"])
def test_an_unreadable_record_directory_is_reported_once_not_as_missing_records(
    cmd, mode, subdir, tmp_path, capsys
):
    _assert_unreadable_dir_reported_once(cmd, mode, subdir, tmp_path, capsys)


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores modes")
@pytest.mark.parametrize("subdir", CANONICAL_SUBDIRS)
@pytest.mark.parametrize("mode", [0o000, 0o400])
def test_every_canonical_record_directory_is_probed(mode, subdir, tmp_path, capsys):
    _assert_unreadable_dir_reported_once("verify", mode, subdir, tmp_path, capsys)


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores modes")
@pytest.mark.parametrize("subdir", CANONICAL_SUBDIRS)
@pytest.mark.parametrize("cmd", ["verify", "doctor"])
def test_an_empty_record_directory_without_the_search_bit_is_unreadable(
    cmd, subdir, tmp_path, capsys
):
    """No entry to lstat, so the search bit has to be probed on the directory itself."""
    store = _store_in_git(tmp_path)
    record_dir = store.path / subdir
    record_dir.mkdir(exist_ok=True)
    for entry in list(record_dir.iterdir()):
        entry.unlink()
    record_dir.chmod(0o400)
    try:
        rc = (doctor_main if cmd == "doctor" else verify_main)(["--db", str(store.path)])
        out = capsys.readouterr().out
    finally:
        record_dir.chmod(0o755)
    assert rc == 1
    assert out.splitlines()[0].startswith(f"store not readable ({store.path}): ")
    assert out.rstrip().endswith(f"'{record_dir}'")


def _assert_unreadable_dir_reported_once(cmd, mode, subdir, tmp_path, capsys):
    store = _store_in_git(tmp_path)
    (store.path / subdir).mkdir(exist_ok=True)
    if not any((store.path / subdir).iterdir()):  # the search bit is only probed through an entry
        (store.path / subdir / "x.json").write_text("{}\n")
    record_dir = store.path / subdir
    _chmod_or_skip(record_dir, mode)
    try:
        argv = ["--db", str(store.path)]
        if cmd == "verify-against":
            argv += ["--against", "HEAD"]
        rc = (doctor_main if cmd == "doctor" else verify_main)(argv)
        out = capsys.readouterr().out
    finally:
        record_dir.chmod(0o755)
    assert rc == 1
    assert out.splitlines()[0].startswith(f"store not readable ({store.path}): ")
    # the message ends with the directory itself, not with an entry inside it
    assert out.rstrip().endswith(f"'{record_dir}'")
    assert len(out.splitlines()) == 1  # one line, not one per record
    for noise in ("dangling-", "illegal-deletion", "parse-error", "violation(s)"):
        assert noise not in out


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores modes")
def test_verify_against_alone_refuses_an_unreadable_record_directory(tmp_path):
    from sidegraph.verify import verify_against

    store = _store_in_git(tmp_path)
    _chmod_or_skip(store.path / "decisions", 0o000)
    try:
        with pytest.raises(PermissionError, match="decisions"):
            verify_against(store.path, "HEAD")
    finally:
        (store.path / "decisions").chmod(0o755)


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores modes")
def test_an_unreadable_archive_directory_is_not_readable_either(tmp_path, capsys):
    store = _store_in_git(tmp_path)
    archive = store.path / "archive"
    archive.mkdir()
    (archive / "2026-01-01-1-000000000000.jsonl").write_text("")
    _chmod_or_skip(archive, 0o000)
    try:
        rc = verify_main(["--db", str(store.path)])
        out = capsys.readouterr().out
    finally:
        archive.chmod(0o755)
    assert rc == 1
    assert str(archive) in out.splitlines()[0]


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores modes")
def test_a_symlinked_record_directory_still_stops_at_the_link(tmp_path, capsys):
    """The link short-circuit runs before the readability probe: an unreadable link TARGET is not
    the store's problem, the link is."""
    store = _store_in_git(tmp_path)
    target = tmp_path / "elsewhere"
    shutil.move(str(store.path / "facts"), target)
    (store.path / "facts").symlink_to(target)
    _chmod_or_skip(target, 0o000)
    try:
        rc = verify_main(["--db", str(store.path)])
        out = capsys.readouterr().out
    finally:
        target.chmod(0o755)
    assert rc == 2
    assert "symlinked-store-entry" in out
    assert "store not readable" not in out


# -- domain parent_id: dangling and cyclic ------------------------------------------------------
#
# ``Store._check_domain_parent_acyclic`` refuses a domain whose ``parent_id`` names no domain or
# whose parent chain loops back to it; ``parent_id`` is immutable afterwards, so only a hand edit
# can produce either shape, and verify must report it.


def _domain(slug: str, **overrides) -> Domain:
    return Domain(
        slug=slug,
        title=slug.title(),
        summary="s",
        provenance=Provenance(source="manual"),
        **overrides,
    )


def _domain_path(store: Store, domain_id: str) -> Path:
    return store.path / "domains" / f"{domain_id}.json"


def _set_parent(store: Store, domain_id: str, parent) -> Path:
    path = _domain_path(store, domain_id)
    _write(path, {**_read(path), "parent_id": parent})
    return path


def _cli_pairs(store: Store, capsys) -> tuple[int, list[tuple[str, str]]]:
    rc = verify_main(["--db", str(store.path), "--json"])
    found = json.loads(capsys.readouterr().out)["violations"]
    return rc, [(v["code"], v["path"]) for v in found]


def test_a_hot_domain_whose_parent_id_names_no_domain_is_dangling(store: Store, capsys):
    store.add_domain(_domain("root"))
    child = store.add_domain(_domain("child"))
    path = _set_parent(store, child.domain_id, "not-a-domain")
    assert _cli_pairs(store, capsys) == (2, [(DANGLING_PARENT, str(path))])


def test_an_empty_parent_id_is_dangling_like_the_write_path_treats_it(store: Store, capsys):
    """The write path tests ``is not None``, so ``""`` is looked up and refused as unknown."""
    child = store.add_domain(_domain("child"))
    path = _set_parent(store, child.domain_id, "")
    assert _cli_pairs(store, capsys) == (2, [(DANGLING_PARENT, str(path))])


def test_an_archived_domain_whose_parent_id_names_no_domain_is_dangling(store: Store, capsys):
    parent = store.add_domain(_domain("root"))
    child = store.add_domain(_domain("child", parent_id=parent.domain_id))
    store.supersede_domain(child.domain_id, _domain("child", supersedes=child.domain_id))
    assert store.compact().domains_compacted == 1
    seg = next((store.path / "archive").glob("*.jsonl"))
    line = json.loads(seg.read_text(encoding="utf-8").splitlines()[0])
    assert line["domain_id"] == child.domain_id
    seg.write_text(json.dumps({**line, "parent_id": "gone"}) + "\n", encoding="utf-8")
    assert _cli_pairs(store, capsys) == (2, [(DANGLING_PARENT, str(seg))])


@pytest.mark.parametrize("bad", [["a"], {"id": "a"}], ids=["list", "dict"])
def test_a_non_string_parent_id_is_a_parse_error_not_a_crash(store: Store, capsys, bad):
    store.add_domain(_domain("root"))
    child = store.add_domain(_domain("child"))
    path = _set_parent(store, child.domain_id, bad)
    assert _cli_pairs(store, capsys) == (2, [(PARSE_ERROR, str(path))])


def test_a_parent_held_by_a_file_skipped_for_its_name_is_not_dangling(store: Store):
    """Renaming the parent file back is the one heal, so the child raises nothing."""
    parent = store.add_domain(_domain("root"))
    child = store.add_domain(_domain("child", parent_id=parent.domain_id))
    skipped = store.path / "domains" / ".root.json"
    _domain_path(store, parent.domain_id).rename(skipped)
    assert _pairs(store) == [(UNSAFE_CODE, str(skipped))]
    assert _read(_domain_path(store, child.domain_id))["parent_id"] == parent.domain_id


def test_a_history_written_through_the_api_never_trips_the_parent_checks(store: Store):
    """Parents that are superseded, dropped or compacted away still exist for the write path
    (``get_domain`` returns them whatever their status), so their children stay valid."""
    root = store.add_domain(_domain("root"))
    mid = store.add_domain(_domain("mid", parent_id=root.domain_id))
    leaf = store.add_domain(_domain("leaf", parent_id=mid.domain_id))
    store.add_domain(_domain("other", parent_id=root.domain_id))
    store.supersede_domain(mid.domain_id, _domain("mid", supersedes=mid.domain_id))
    store.supersede_domain(root.domain_id, _domain("root", supersedes=root.domain_id))
    assert _read(_domain_path(store, leaf.domain_id))["parent_id"] == mid.domain_id
    assert verify_snapshot(store.path) == []
    assert store.compact().domains_compacted == 2  # root and mid leave domains/ for the archive
    assert verify_snapshot(store.path) == []


def test_a_parent_cycle_is_reported_once_at_its_smallest_id(store: Store, capsys):
    """A -> B -> C -> A, plus D hanging off A and a self-parented E: one finding per cycle, at the
    cycle's smallest id, none for D (it is not on the cycle) and none per member."""
    a, b, c, d, e = (store.add_domain(_domain(s)).domain_id for s in "abcde")
    _set_parent(store, a, b)
    _set_parent(store, b, c)
    _set_parent(store, c, a)
    _set_parent(store, d, a)
    _set_parent(store, e, e)
    rc, found = _cli_pairs(store, capsys)
    assert rc == 2
    assert sorted(found) == sorted(
        [
            (PARENT_CYCLE, str(_domain_path(store, min(a, b, c)))),
            (PARENT_CYCLE, str(_domain_path(store, e))),
        ]
    )


def test_a_parent_cycle_through_an_archived_domain_is_reported_once(store: Store, capsys):
    a = store.add_domain(_domain("a")).domain_id
    b = store.add_domain(_domain("b", parent_id=a))
    store.supersede_domain(b.domain_id, _domain("b2", supersedes=b.domain_id))
    store.compact()
    seg = next((store.path / "archive").glob("*.jsonl"))
    line = json.loads(seg.read_text(encoding="utf-8").splitlines()[0])
    _set_parent(store, a, b.domain_id)
    rc, found = _cli_pairs(store, capsys)
    assert rc == 2
    assert found == [(PARENT_CYCLE, str(_domain_path(store, a) if a < b.domain_id else seg))]
    assert line["domain_id"] == b.domain_id


def test_a_long_parent_chain_is_clean_and_a_long_cycle_is_reported_once(store: Store, capsys):
    """Iterative, so a chain far past the recursion limit neither crashes nor loops."""
    template = _read(_domain_path(store, store.add_domain(_domain("seed")).domain_id))
    _domain_path(store, template["domain_id"]).unlink()
    ids = [f"D{i:05d}" for i in range(1500)]
    for i, did in enumerate(ids):
        parent = ids[i - 1] if i else None
        payload = {**template, "domain_id": did, "slug": did.lower(), "parent_id": parent}
        _write(_domain_path(store, did), payload)
    assert verify_snapshot(store.path) == []
    path = _set_parent(store, ids[0], ids[-1])
    assert _cli_pairs(store, capsys) == (2, [(PARENT_CYCLE, str(path))])


def _hand_domains(store: Store, parents: dict[str, str | None]) -> dict[str, Path]:
    """Replace the store's domains with hand-named ones (``{id: parent_id}``), so the ids sort the
    way a test needs; minted ULIDs sort by creation time, which hides ordering bugs."""
    template = _read(_domain_path(store, store.add_domain(_domain("seed")).domain_id))
    _domain_path(store, template["domain_id"]).unlink()
    paths = {}
    for did, parent in parents.items():
        paths[did] = _domain_path(store, did)
        _write(paths[did], {**template, "domain_id": did, "slug": did.lower(), "parent_id": parent})
    return paths


def test_a_tail_with_a_smaller_id_than_the_cycle_is_not_where_it_is_reported(store: Store, capsys):
    """A0000 hangs off the cycle and sorts before every member: the finding belongs to the
    cycle's smallest id, C0001, and its detail runs from there and closes on it."""
    paths = _hand_domains(
        store, {"A0000": "C0002", "C0001": "C0002", "C0002": "C0003", "C0003": "C0001"}
    )
    rc = verify_main(["--db", str(store.path), "--json"])
    found = json.loads(capsys.readouterr().out)["violations"]
    assert rc == 2
    assert [(v["code"], v["path"], v["detail"]) for v in found] == [
        (
            PARENT_CYCLE,
            str(paths["C0001"]),
            "parent_id chain loops back on itself: C0001 -> C0002 -> C0003 -> C0001",
        )
    ]


def test_an_archived_domain_holding_the_smallest_id_gets_the_cycle_finding(store: Store):
    paths = _hand_domains(store, {"M0002": "M0001"})
    seg = store.path / "archive" / "2026-01-01-1-000000000000.jsonl"
    seg.parent.mkdir(exist_ok=True)
    payload = {**_read(paths["M0002"]), "domain_id": "M0001", "slug": "m0001", "status": "dropped"}
    seg.write_text(
        json.dumps({"record_type": "domain", **payload, "parent_id": "M0002"}) + "\n",
        encoding="utf-8",
    )
    found = verify_snapshot(store.path)
    assert [(v.code, v.path, v.detail) for v in found] == [
        (PARENT_CYCLE, str(seg), "parent_id chain loops back on itself: M0001 -> M0002 -> M0001")
    ]


def test_a_huge_cycle_gets_a_short_detail(store: Store):
    n = 300
    ids = [f"D{i:05d}" for i in range(n)]
    _hand_domains(store, {did: ids[(i + 1) % n] for i, did in enumerate(ids)})
    (found,) = verify_snapshot(store.path)
    assert found.code == PARENT_CYCLE
    assert found.detail == (
        "parent_id chain loops back on itself: "
        + " -> ".join(ids[:10])
        + f" -> ... ({n} domains) -> D00000"
    )


@pytest.mark.parametrize("kind", ["decision", "domain"])
@pytest.mark.parametrize("status", ["proposed", "accepted"])
def test_nonterminal_archive_record_is_a_bad_segment(store: Store, kind, status):
    record = (
        _decision(status=status)
        if kind == "decision"
        else Domain(
            slug="payments",
            title="Payments",
            summary="Settlement",
            status=status,
            provenance=Provenance(source="manual"),
        )
    )
    segment = _segment(store, _archive_record_line(kind, record.model_dump(mode="json")))

    findings = verify_snapshot(store.path)
    assert _codes(findings) == [BAD_ARCHIVE_SEGMENT]
    assert findings[0].path == str(segment)
