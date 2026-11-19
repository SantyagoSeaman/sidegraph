"""User-facing verdict refusals for the Portal (D6)."""


def portal_message(raw: str) -> tuple[str, str]:
    """Map agent-facing ratification text to a reason and display message.

    See design/superpowers/specs/2026-11-17-portal-design.md (D6).
    """
    if raw.startswith("error: domain was accepted in the meantime"):
        return "domain_accepted_meanwhile", "This domain was accepted in the meantime."
    if raw.startswith("error: already "):
        status = raw.split()[2].rstrip(";")
        if status in {"accepted", "rejected", "dropped"}:
            return "opposite_verdict", f"This item was already {status}."
    if "ratify cannot change it" in raw:
        return "not_proposed", "This item is no longer proposed."
    if raw.startswith("error: unknown id"):
        return "unknown_id", "This item is no longer available."
    for phrase, reason in (
        ("cannot be ratified: its evidence facts", "evidence_conflict"),
        ("already has a pending successor", "pending_successor"),
        ("pending proposal", "pending_proposal"),
        ("is still proposed; ratify or drop it first", "pending_proposal"),
        ("supersede its live successor", "not_current"),
        ("supersede that one instead", "not_current"),
        ("can no longer be superseded", "not_current"),
        ("is no longer current", "not_current"),
    ):
        if phrase in raw:
            return (
                reason,
                "The version or its evidence changed. Review the record before trying again.",
            )
    return "ratification_failed", "Reopen the queue and check this item's status before retrying."
