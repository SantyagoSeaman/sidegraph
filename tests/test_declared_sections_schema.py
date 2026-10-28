"""Capsule shape of declared sections on a DriftCheck (md1 scheme)."""

import json

import pytest
from pydantic import ValidationError
from ulid import ULID

from sidegraph.schema import DeclaredSection, DeclaredSectionFile, DriftCheck

SHA = "a" * 40
H64 = "b" * 64
T64 = "c" * 64


def _section(role: str = "declared", heading: str = H64, text: str = T64) -> dict:
    return {"heading_hash": heading, "role": role, "text_hash": text}


def _entry(path: str = "docs/spec.md", **overrides) -> dict:
    entry = {
        "path": path,
        "scheme": "md1",
        "refs_hash": H64,
        "sections": [_section()],
    }
    entry.update(overrides)
    return entry


def _check(declared: list | None = None, paths: tuple[str, ...] = ("docs/spec.md",)) -> dict:
    kwargs = {
        "schema_version": 1,
        "decision_id": str(ULID()),
        "checked_at": "2026-10-27T10:00:00Z",
        "checked_commit": SHA,
        "provenance": {"source": "agent", "commit": SHA},
        "rationale": "Read the section; it still holds.",
        "required_files": [paths[0]],
        "checked_files": [{"path": p, "blob_id": SHA, "mode": "100644"} for p in paths],
        "subject_digest": "d" * 64,
    }
    if declared is not None:
        kwargs["declared_sections"] = declared
    return kwargs


def test_new_shape_roundtrips() -> None:
    declared = [_entry(sections=[_section(), _section("watch", "e" * 64, "f" * 64)])]
    check = DriftCheck.model_validate(_check(declared))
    dumped = json.loads(check.model_dump_json())
    assert dumped["declared_sections"] == declared
    assert DriftCheck.model_validate(dumped) == check


def test_plain_capsule_dumps_empty_declared_sections() -> None:
    check = DriftCheck.model_validate(_check())
    assert json.loads(check.model_dump_json())["declared_sections"] == []


def test_model_config_unchanged() -> None:
    # Older readers ignore the new key only while no model forbids extras.
    for model in (DriftCheck, DeclaredSectionFile, DeclaredSection):
        assert "extra" not in model.model_config


def test_path_must_be_in_checked_files() -> None:
    with pytest.raises(ValidationError):
        DriftCheck.model_validate(_check([_entry("docs/other.md")]))


def test_duplicate_paths_rejected() -> None:
    with pytest.raises(ValidationError):
        DriftCheck.model_validate(_check([_entry(), _entry()]))


def test_over_1024_sections_total_rejected() -> None:
    paths = tuple(f"docs/s{i}.md" for i in range(5))
    sections = [_section(heading=f"{n:064x}") for n in range(205)]
    declared = [_entry(p, sections=sections) for p in paths]  # 5 * 205 = 1025
    with pytest.raises(ValidationError):
        DriftCheck.model_validate(_check(declared, paths))
    ok = [_entry(p, sections=sections[:204]) for p in paths[:5]]  # 1020
    DriftCheck.model_validate(_check(ok, paths))


def test_over_64_files_rejected() -> None:
    paths = tuple(f"docs/s{i}.md" for i in range(65))
    declared = [_entry(p) for p in paths]
    with pytest.raises(ValidationError):
        DriftCheck.model_validate(_check(declared, paths))


def test_md1_requires_md_extension() -> None:
    with pytest.raises(ValidationError):
        DriftCheck.model_validate(_check([_entry("docs/spec.txt")], ("docs/spec.txt",)))
    DriftCheck.model_validate(_check([_entry("docs/spec.markdown")], ("docs/spec.markdown",)))


@pytest.mark.parametrize(
    "entry",
    [
        _entry(refs_hash=None),
        _entry(refs_hash="b" * 63),
        _entry(sections=[_section(heading="b" * 63)]),
        _entry(sections=[_section(text="c" * 65)]),
        _entry(sections=[_section("watch")]),
    ],
)
def test_md1_known_roles_need_64_hex_hashes_refs_hash_and_a_declared_role(entry: dict) -> None:
    with pytest.raises(ValidationError):
        DriftCheck.model_validate(_check([entry]))


def test_unknown_scheme_with_region_role_and_extra_keys_validates() -> None:
    entry = {
        "path": "src/App.swift",
        "scheme": "swift1",
        "future_key": {"a": 1},
        "sections": [{"heading_hash": "ab12", "role": "region", "text_hash": "cd", "x": 1}],
    }
    DriftCheck.model_validate(_check([entry], ("src/App.swift",)))


def test_md1_entry_with_only_unknown_role_validates() -> None:
    entry = _entry(refs_hash=None, sections=[_section("region", "ab", "cd")])
    DriftCheck.model_validate(_check([entry]))


def test_md1_unknown_role_beside_watch_only_validates() -> None:
    entry = _entry(sections=[_section("watch"), _section("region", "ab", "cd")])
    DriftCheck.model_validate(_check([entry]))


def test_no_heading_text_field() -> None:
    assert set(DeclaredSection.model_fields) == {"heading_hash", "role", "text_hash"}
