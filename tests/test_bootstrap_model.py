from pathlib import Path

import pytest
from pydantic import ValidationError

from sidegraph.bootstrap.model import BootstrapCandidate, ProfileDetection, WarningCode
from sidegraph.profiles import detect_profile
from sidegraph.schema import DecisionKind, DecisionStatus


@pytest.fixture
def candidate_kwargs():
    return {
        "file_path": "docs/adr/001.md",
        "ref": "docs/adr/001.md",
        "source_hash": "a" * 64,
        "title": "Use SQLite",
        "context": "The project needs local state.",
        "choice": "Use SQLite.",
        "rejected": "Do not add a hosted database.",
        "consequences": "One local file.",
        "kind": DecisionKind.ADR,
        "default_status": DecisionStatus.PROPOSED,
        "redacted_anchor_text": "Use SQLite.",
        "anchor_intents": (),
        "anchors": (),
        "warnings": (),
    }


def test_candidate_key_is_stable_and_model_is_frozen(candidate_kwargs):
    first = BootstrapCandidate.from_fields(**candidate_kwargs)
    second = BootstrapCandidate.from_fields(**candidate_kwargs)
    assert first.key == second.key
    assert first.model_dump_json() == second.model_dump_json()
    with pytest.raises(ValidationError):
        first.title = "changed"


def test_warning_codes_are_public_stable_strings():
    assert WarningCode.MISSING_CHOICE == "missing-choice"
    assert WarningCode.MISSING_REJECTED == "missing-rejected-alternatives"
    assert WarningCode.AMBIGUOUS_ANCHOR == "ambiguous-anchor"
    assert WarningCode.CURRENT_STATE == "likely-current-state-summary"
    assert WarningCode.DUPLICATE_PLAN == "duplicate-within-plan"
    assert WarningCode.DUPLICATE_CANONICAL == "duplicate-canonical-memory"


def test_explicit_profile_always_wins(tmp_path: Path):
    (tmp_path / ".specify").mkdir()
    (tmp_path / "_bmad").mkdir()
    result = detect_profile(tmp_path, "generic-adr")
    assert result == ProfileDetection(selected="generic-adr", matches=("generic-adr",))


def test_multiple_specific_markers_are_ambiguous(tmp_path: Path):
    (tmp_path / ".specify").mkdir()
    (tmp_path / "_bmad").mkdir()
    result = detect_profile(tmp_path, None)
    assert result.selected is None
    assert result.matches == ("bmad", "spec-kit")


def test_generic_requires_its_own_matching_glob(tmp_path: Path):
    assert detect_profile(tmp_path, None).matches == ()
    adr = tmp_path / "docs" / "adr" / "001.md"
    adr.parent.mkdir(parents=True)
    adr.write_text("# A\n")
    assert detect_profile(tmp_path, None).selected == "generic-adr"
