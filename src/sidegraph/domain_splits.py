"""Domain splits: one domain replaced by two or more domains that are live at once.

A split is legal and supported (a deliberate 1-to-3 re-scope, or two branches that each
replaced the same domain and merged). No write path creates a second live replacement
(``supersede_domain`` only replaces the live head), so a split arrives with a merge or by hand.
Ratify and doctor make it visible rather than forbid it.

Pure functions over domain records, so the store, ratify and doctor (which reads files, not
the store) share one rule: the one ``Store._live_head_after`` uses on the write side.

see docs/concepts/data-model.md (supersession)"""

from __future__ import annotations

from collections.abc import Mapping

from .schema import Domain, DomainStatus


def domain_parts(domains: Mapping[str, Domain]) -> dict[str, list[str]]:
    """``predecessor id -> ids of its live parts`` for every domain with at least one.

    A successor is a part only while its chain still reaches an ``accepted`` head: an
    ``accepted`` successor is its own head, a ``superseded`` one is followed through its own
    ``accepted``/``superseded`` successors on every branch. Proposed, dropped and rejected
    records are not part of a chain, so a branch that ended in a drop stops counting. Ids
    within a list are sorted."""
    children: dict[str, list[Domain]] = {}
    for d in domains.values():
        if d.supersedes and d.status in (DomainStatus.ACCEPTED, DomainStatus.SUPERSEDED):
            children.setdefault(d.supersedes, []).append(d)

    def heads(start: Domain) -> set[str]:
        """Every ``accepted`` domain reachable from ``start`` through ``superseded`` ones,
        over all successor branches (breadth-first). A ``superseded`` record that ends in a
        drop yields nothing, so a dead branch never hides a live one beside it. No depth
        cutoff, which would read a long chain as headless; the visited set ends a cycle in
        hand-edited data."""
        found: set[str] = set()
        seen = {start.domain_id}
        queue = [start]
        while queue:
            current = queue.pop(0)
            if current.status == DomainStatus.ACCEPTED:
                found.add(current.domain_id)
                continue
            for c in children.get(current.domain_id, []):
                if c.domain_id not in seen:
                    seen.add(c.domain_id)
                    queue.append(c)
        return found

    return {
        pred_id: parts
        for pred_id, successors in children.items()
        if (parts := sorted(set().union(*(heads(c) for c in successors))))
    }


def domain_splits(domains: Mapping[str, Domain]) -> dict[str, list[str]]:
    """:func:`domain_parts` for the domains with two or more parts: the splits."""
    return {pred_id: parts for pred_id, parts in domain_parts(domains).items() if len(parts) > 1}


def split_added_by(
    before: Mapping[str, Domain], after: Mapping[str, Domain], domain_id: str
) -> list[str]:
    """The predecessors whose split ``domain_id``'s accept created or grew: those with two or
    more live parts afterwards, ``domain_id`` among them, and more parts than before. A plain
    revision (a part replaced by its successor) keeps the count and so is not one."""
    parts_before = domain_parts(before)
    return [
        pred_id
        for pred_id, parts in sorted(domain_parts(after).items())
        if len(parts) > 1 and domain_id in parts and len(parts) > len(parts_before.get(pred_id, []))
    ]
