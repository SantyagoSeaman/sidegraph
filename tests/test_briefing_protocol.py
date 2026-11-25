"""The SessionStart protocol text and its all-or-nothing budget."""

from sidegraph.host.briefing.blocks.protocol import render
from sidegraph.host.briefing.lines import size
from tests.briefing_fixtures import context


def test_the_text_is_the_spec_text() -> None:
    assert render(context(), 900) == [
        "## Working with this project's memory",
        "Ask memory (retrieve_decisions for a topic, get_task_context for files or symbols):",
        "- before you settle on a plan or propose an approach: "
        "it may already have been tried and abandoned;",
        "- before you delete or rewrite code that looks legacy, odd or wrong: "
        "the reason it is there may be recorded;",
        "- when a record and the code disagree: say so, check the code, "
        "then record_drift_check or supersede_decision.",
        "Record what you learn (propose_decisions; supersede_decision when a record became false):",
        "- a choice made between alternatives, with what was rejected and why;",
        "- an approach tried and abandoned;",
        "- a trap that cost time to find.",
    ]


def test_it_is_668_units_and_all_or_nothing() -> None:
    ctx = context()
    full_text = render(ctx, 900)
    assert size(full_text) == 668
    assert render(ctx, 667) == []
    assert render(ctx, 668) == full_text


def test_it_repeats_no_call_syntax() -> None:
    text = "\n".join(render(context(), 900))
    assert "retrieve_decisions" in text
    assert "retrieve_decisions(query=" not in text
    assert "get_task_context(files=" not in text


def test_it_names_the_four_situations_and_the_three_things_to_record() -> None:
    text = "\n".join(render(context(), 900))
    assert "settle on a plan" in text
    assert "propose an approach" in text
    assert "delete or rewrite" in text
    assert "disagree" in text
    assert "a choice made" in text
    assert "an approach tried and abandoned" in text
    assert "a trap" in text
