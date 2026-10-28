"""The strict input model and error vocabulary of record_drift_check's ``sections``."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from sidegraph import server, validation_errors
from sidegraph.input_limits import InputLimitError, preflight_direct
from sidegraph.validation_errors import SafeWriteError, format_validation_error

CODES = [
    "section_file_not_in_files",
    "section_file_not_markdown",
    "section_file_too_large",
    "section_scope_too_large",
    "section_text_unavailable",
    "section_file_unsupported",
    "unknown_section",
    "ambiguous_section",
    "duplicate_section",
    "duplicate_section_file",
    "too_many_sections",
]


def item(**overrides) -> dict:
    base = {"file": "docs/spec.md", "headings": [["55.2 Rules"]], "also_watch": []}
    base.update(overrides)
    return base


def render(payload) -> str:
    with pytest.raises(ValidationError) as caught:
        server.SectionsInput(sections=payload)
    return format_validation_error(caught.value)


def test_valid_payload_validates() -> None:
    parsed = server.SectionsInput(
        sections=[item(also_watch=[["56. Echo", "56.4 Amendments"]]), item(file="docs/b.md")]
    )
    assert parsed.sections[0].also_watch == [["56. Echo", "56.4 Amendments"]]
    assert server.SectionsInput(sections=[{"file": "a.md", "headings": [["x"]]}])


@pytest.mark.parametrize(
    "payload, expected",
    [
        ([item(also_watched=[["x"]])], "sections.[0].field: extra_forbidden"),
        ([item(headings=["55.2 Rules"])], "sections.[0].headings.[0]: list_type"),
        ([item(headings="55.2 Rules")], "sections.[0].headings: list_type"),
        ([item(headings=[[""]])], "sections.[0].headings.[0].[0]: string_too_short"),
        ([item(headings=[["x" * 201]])], "sections.[0].headings.[0].[0]: string_too_long"),
        ([item(headings=[["x"] * 7])], "sections.[0].headings.[0]: too_long"),
        ([item(headings=[[1]])], "sections.[0].headings.[0].[0]: string_type"),
        ([item(headings=[["x"]] * 33)], "sections.[0].headings: too_long"),
        ([item(headings=[])], "sections.[0].headings: too_short"),
        ([item(file="")], "sections.[0].file: string_too_short"),
        ([item(file="x" * 1025)], "sections.[0].file: string_too_long"),
        ([item(file=f"f{n}.md") for n in range(17)], "sections: too_long"),
    ],
)
def test_input_table(payload, expected: str) -> None:
    text = render(payload)
    assert expected in text
    assert "invalid_value" not in text


def test_oversized_payload_is_refused_by_the_preflight() -> None:
    huge = [item(headings=[["x" * 200]] * 32, also_watch=[["y" * 200]] * 32)] * 16
    huge = huge * 2000
    with pytest.raises(InputLimitError):
        preflight_direct("id", "commit", ["a.md"], "why", huge)


@pytest.mark.parametrize("code", CODES)
def test_every_new_safe_write_code_has_a_message(code: str) -> None:
    assert code in validation_errors._CODES
    text = str(SafeWriteError("sections", code))
    assert text.startswith(f"invalid draft: sections: {code}")
    assert len(text) < validation_errors.MAX_CHARACTERS
