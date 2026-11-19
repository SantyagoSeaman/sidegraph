"""Display words for binding statuses (Portal design, D5)."""

from typing import Literal


def word_for(
    status: str, *, index_cold: bool
) -> Literal["found", "ambiguous", "missing", "unchecked"]:
    """Translate the binding status, including the cold-index placeholder (D5)."""
    if index_cold:
        return "unchecked"
    match status:
        case "live":
            return "found"
        case "degraded":
            return "ambiguous"
        case "orphaned":
            return "missing"
        case _:
            return "unchecked"
