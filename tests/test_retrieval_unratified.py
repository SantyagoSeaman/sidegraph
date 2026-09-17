from datetime import UTC, datetime

from sidegraph.retrieval import _fmt_decision
from sidegraph.schema import Decision, DecisionKind, DecisionStatus, Provenance


def _dec(status):
    return Decision(
        title="T",
        kind=DecisionKind.GOTCHA,
        status=status,
        context="c",
        choice="ch",
        valid_from=datetime.now(UTC),
        provenance=Provenance(source="agent"),
    )


def test_proposed_renders_unratified_tag():
    line = _fmt_decision(_dec(DecisionStatus.PROPOSED))
    assert "[unratified]" in line


def test_accepted_renders_untagged():
    line = _fmt_decision(_dec(DecisionStatus.ACCEPTED))
    assert "[unratified]" not in line


def _dec_with(title, choice):
    return Decision(
        title=title,
        kind=DecisionKind.ADR,
        status=DecisionStatus.ACCEPTED,
        context="c",
        choice=choice,
        valid_from=datetime.now(UTC),
        provenance=Provenance(source="import"),
    )


def test_choice_matching_title_renders_title_only():
    # imported decisions: title == choice's first line (here, the whole single-line choice) ->
    # "- [adr] X: X" would be pure duplication; render "- [adr] X" instead.
    d = _dec_with("single-line rationale", "single-line rationale")
    line = _fmt_decision(d)
    assert line == "- [adr] single-line rationale"
    assert ": single-line rationale" not in line


def test_choice_starting_with_title_renders_title_only():
    # choice is the fuller text the title was truncated from -- still a duplication of the
    # title text at the front, so still collapse to title-only.
    d = _dec_with("single-line rationale", "single-line rationale, with more detail after")
    line = _fmt_decision(d)
    assert line == "- [adr] single-line rationale"


def test_choice_differing_from_title_keeps_both():
    d = _dec_with("short title", "an unrelated, fuller choice")
    line = _fmt_decision(d)
    assert line == "- [adr] short title: an unrelated, fuller choice"


def test_title_choice_dedup_ignores_surrounding_whitespace():
    d = _dec_with("  padded title  ", "padded title has more text")
    line = _fmt_decision(d)
    assert line == "- [adr]   padded title  "
