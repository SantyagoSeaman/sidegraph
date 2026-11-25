"""When to ask project memory and what to record.

See design/superpowers/specs/2026-11-20-sessionstart-briefing-design.md (B8).
"""

from ..context import BriefingContext
from ..lines import size

_PROTOCOL = (
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
)


def render(ctx: BriefingContext, budget: int) -> list[str]:
    """Render the protocol as one unit if it fits the budget."""
    return list(_PROTOCOL) if size(_PROTOCOL) <= budget else []
