"""Declared-section drift checks are documented where the contract lives."""

from pathlib import Path

from sidegraph import doctor, validation_errors

ROOT = Path(__file__).resolve().parent.parent


def read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_every_reason_is_documented() -> None:
    docs = read("docs/reference/store-format.md") + read("docs/reference/mcp-tools.md")
    missing = [text for _, text in doctor._REASONS if text not in docs]
    assert not missing


def test_every_section_error_code_is_in_the_tool_reference() -> None:
    codes = [
        key
        for key, text in validation_errors._MESSAGES.items()
        if text.startswith("invalid draft: sections:")
    ]
    assert len(codes) == 11
    tools = read("docs/reference/mcp-tools.md")
    assert [code for code in codes if f"`{code}`" not in tools] == []


def test_markdown_it_py_is_a_documented_requirement() -> None:
    assert "markdown-it-py" in read("docs/reference/mcp-tools.md")
    assert "markdown-it-py" in read("docs/getting-started/installation.md")


def test_capsule_field_is_in_store_format_and_changelog() -> None:
    assert "declared_sections" in read("docs/reference/store-format.md")
    assert "declared_sections" in read("CHANGELOG.md")


def test_skills_teach_scoping() -> None:
    assert "Scoping a check on a document" in read("plugin/sidegraph/skills/triage-drift/SKILL.md")
    for skill in ("record-decision", "record-fact"):
        text = read(f"plugin/sidegraph/skills/{skill}/SKILL.md")
        assert "section scope is declared when the record is checked" in text


def test_pre_change_rearm_wording_is_gone() -> None:
    skill = read("plugin/sidegraph/skills/triage-drift/SKILL.md")
    assert "Changed checked files or required scope re-arm review." not in skill
    retrieval = read("docs/guides/retrieval-in-sessions.md")
    assert (
        "Changed inspected files or scope re-arm the warning; unrelated commits do" not in retrieval
    )
