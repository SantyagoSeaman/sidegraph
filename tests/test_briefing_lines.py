"""Briefing line formats and whole-entry budgets (D7–D8)."""

import pytest

from sidegraph.host import hooks
from sidegraph.host.briefing.lines import decision_line, fit_lines, size, title_line, units
from sidegraph.hot_index import Record


@pytest.fixture
def row() -> dict:
    return {"id": "0" * 26, "kind": "gotcha", "title": "Title", "choice": "Keep it."}


def test_units_counts_an_astral_character_twice() -> None:
    assert units("a") == 1 and units("\U0001f600") == 2
    assert size([]) == 0
    assert size(["a", "\U0001f600"]) == 4


def test_decision_line_cuts_title_and_choice_as_the_hook_line_does(row: dict) -> None:
    row.update(title="a" * 200, choice="x" * 140 + ". Another sentence. A third one.")
    assert decision_line(row, frozenset()) == hooks._record_line(Record(row, False))


def test_the_drift_tag_follows_the_title(row: dict) -> None:
    assert "Title [drifted] — " in decision_line(row, frozenset({row["id"]}))


def test_rejected_is_taken_from_its_first_character(row: dict) -> None:
    row["rejected"] = "The predecessor's rule: gone. A second sentence."
    assert " Rejected: The predecessor's rule: gone. A second sentence. (id " in decision_line(
        row, frozenset(), rejected=120
    )


@pytest.mark.parametrize("text", [None, "   "])
def test_a_blank_rejected_adds_nothing(row: dict, text: str | None) -> None:
    row["rejected"] = text
    assert decision_line(row, frozenset(), rejected=120) == decision_line(row, frozenset())


def test_a_line_stays_one_line(row: dict) -> None:
    row.update(title="a\nb\tc   d", rejected="x\ny")
    assert "\n" not in decision_line(row, frozenset(), rejected=120)
    assert "a b c d" in title_line(row, frozenset())


def test_title_line_without_kind(row: dict) -> None:
    assert title_line(row, frozenset(), kind=False).startswith("- " + row["title"][:10])


def test_fit_lines_keeps_the_longest_prefix_with_its_pointer() -> None:
    def more(n: int) -> str:
        return f"{n} more"

    entries = ["e1", "e2", "entry-three"]
    assert fit_lines("H", entries, 19, more) == ["H", *entries]
    assert fit_lines("H", entries, 18, more) == ["H", "e1", "e2", "1 more"]
    assert fit_lines("H", entries, 13, more) == ["H", "e1", "2 more"]
    assert fit_lines("H", entries, 10, more) == []
    assert fit_lines("H", [], 1000, more) == []


def test_a_malformed_rejected_value_does_not_hide_the_decisions(row: dict) -> None:
    damaged = row | {"title": "Damaged reason", "rejected": 17}
    good = row | {"title": "Valid reason", "rejected": "It broke."}
    lines = [decision_line(record, frozenset(), rejected=120) for record in (damaged, good)]
    assert lines[0] == decision_line(damaged, frozenset())
    assert " Rejected: It broke. (id " in lines[1]
