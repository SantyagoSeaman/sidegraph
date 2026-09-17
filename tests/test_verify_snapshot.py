"""``sidegraph-verify`` snapshot layer (design/superpowers/specs/
2026-07-11-ci-integrity-design.md ruling 2): every test here builds a genuinely valid store
through the ``Store`` API, then corrupts exactly ONE thing per test by editing a canonical
file directly (never through ``Store``, which would just reject the corruption at the
write-path invariant it enforces) -- and asserts that ``verify_snapshot`` reports exactly
the one violation code that corruption is supposed to trip, on a store that is otherwise
clean. A clean store always yields ``[]``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from sidegraph.cli import verify_main
from sidegraph.schema import (
    AnchorBinding,
    Decision,
    DecisionKind,
    Descriptor,
    Domain,
    Entity,
    Fact,
    Provenance,
)
from sidegraph.store import Store
from sidegraph.verify import (
    BAD_ARCHIVE_SEGMENT,
    BAD_VALIDITY_WINDOW,
    DANGLING_BINDING_ENTITY,
    DANGLING_FACT_SUPPORT,
    DANGLING_SUPERSEDES,
    DUPLICATE_ULID,
    FILENAME_ID_MISMATCH,
    PARSE_ERROR,
    SUPERSEDED_WITHOUT_SUCCESSOR,
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
