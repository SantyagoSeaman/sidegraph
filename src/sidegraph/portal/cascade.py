"""Read-only batch cascade forecast for ratification (Portal design, D6)."""

from collections import defaultdict
from collections.abc import Iterable, Mapping

from sidegraph.schema import Decision, DecisionStatus, DomainStatus, Fact
from sidegraph.store import Store


def _successors(records: Iterable[Decision | Fact]) -> dict[str, list[Decision | Fact]]:
    children: dict[str, list[Decision | Fact]] = defaultdict(list)
    for record in records:
        if record.supersedes is not None:
            children[record.supersedes].append(record)
    return children


def _can_accept(
    record: Decision | Fact,
    records: Mapping[str, Decision | Fact],
    children: Mapping[str, list[Decision | Fact]],
    accepted: set[str],
    closed: set[str],
) -> bool:
    # Store._refuse_stale_predecessor evaluates the committed state. Use the same two
    # predicates with earlier successful forecasts applied to the status of each version.
    if record.supersedes is None or (previous := records.get(record.supersedes)) is None:
        return True
    if previous.status == DecisionStatus.PROPOSED and previous.id not in accepted:
        return False
    pending = [previous.id]
    seen = {previous.id}
    while pending:
        for successor in children.get(pending.pop(), []):
            if successor.id in seen:
                continue
            seen.add(successor.id)
            is_accepted = successor.id in accepted or successor.status == DecisionStatus.ACCEPTED
            if is_accepted and successor.id not in closed and successor.valid_to is None:
                return False
            if is_accepted or successor.status == DecisionStatus.SUPERSEDED:
                pending.append(successor.id)
    return True


def preview(store: Store, accept: list[str], reject: list[str]) -> dict[str, str]:
    """Predict verdicts and proposed evidence outcomes over the whole marked batch (D6)."""
    accepted = set(accept)
    rejected = set(reject) - accepted
    outcomes = dict.fromkeys([*accept, *reject], "stays")
    decisions = {d.id: d for d in store.iter_decisions()}
    proposed = {d.id for d in decisions.values() if d.status == DecisionStatus.PROPOSED}
    all_facts = {f.id: f for f in store.iter_facts()}
    facts = {f.id: f for f in all_facts.values() if f.status == DecisionStatus.PROPOSED}
    decision_children = _successors(decisions.values())
    fact_children = _successors(all_facts.values())
    evidence: dict[str, list[Fact]] = defaultdict(list)
    for fact in facts.values():
        for supporter in set(fact.supports):
            evidence[supporter].append(fact)

    # Apply runs marked decision accepts first, with all evidence checked before each accept.
    accepted_decisions: set[str] = set()
    accepted_facts: set[str] = set()
    closed_decisions: set[str] = set()
    closed_facts: set[str] = set()
    for record_id in dict.fromkeys(accept):
        if record_id not in proposed:
            continue
        decision = decisions[record_id]
        carried = [f for f in evidence[record_id] if f.id not in accepted_facts]
        fact_predecessors = [f.supersedes for f in carried if f.supersedes]
        targets = set(fact_predecessors)
        if len(targets) != len(fact_predecessors) or not _can_accept(
            decision, decisions, decision_children, accepted_decisions, closed_decisions
        ):
            continue
        if not all(
            _can_accept(fact, all_facts, fact_children, accepted_facts, closed_facts)
            for fact in carried
        ):
            continue
        outcomes[record_id] = "accepted"
        accepted_decisions.add(record_id)
        accepted_facts.update(f.id for f in carried)
        closed_facts.update(targets)
        if decision.supersedes:
            closed_decisions.add(decision.supersedes)

    # The second accept pass processes explicitly marked facts and domains in request order.
    domains = {d.domain_id: d for d in store.iter_domains()}
    accepted_domains: dict[str, set[str]] = defaultdict(set)
    for current_domain in domains.values():
        if current_domain.status == DomainStatus.ACCEPTED:
            accepted_domains[current_domain.slug].add(current_domain.domain_id)
    closed_domains: set[str] = set()
    for record_id in dict.fromkeys(accept):
        if record_id in decisions or record_id in accepted_facts:
            continue
        if record_id in facts and record_id not in closed_facts:
            fact = facts[record_id]
            if not _can_accept(fact, all_facts, fact_children, accepted_facts, closed_facts):
                continue
            accepted_facts.add(record_id)
            if fact.supersedes:
                closed_facts.add(fact.supersedes)
            continue
        domain = domains.get(record_id)
        if domain is not None and domain.status == DomainStatus.PROPOSED:
            if record_id in closed_domains or accepted_domains[domain.slug] - {domain.supersedes}:
                continue
            if domain.supersedes:
                closed_domains.add(domain.supersedes)
                predecessor = domains.get(domain.supersedes)
                if predecessor is not None:
                    accepted_domains[predecessor.slug].discard(predecessor.domain_id)
            accepted_domains[domain.slug].add(record_id)
            outcomes[record_id] = "accepted"

    # Drop uses Store._all_supporters_rejected against the decision statuses after the batch.
    reject_decisions = (proposed & rejected) - closed_decisions
    rejected_supporters = reject_decisions | {
        d.id for d in decisions.values() if d.status == DecisionStatus.REJECTED
    }
    outcomes.update(dict.fromkeys(reject_decisions, "rejected"))
    marked_decisions = decisions.keys() & (accepted | rejected)
    for fact in facts.values():
        if fact.id in accepted_facts:
            outcomes[fact.id] = "accepted"
        elif fact.id not in closed_facts:
            supporters = set(fact.supports)
            if fact.id in rejected or (
                reject_decisions and supporters and supporters <= rejected_supporters
            ):
                outcomes[fact.id] = "rejected"
            elif supporters & marked_decisions:
                outcomes[fact.id] = "stays"

    for domain in domains.values():
        if domain.status == DomainStatus.PROPOSED and domain.domain_id in rejected - closed_domains:
            outcomes[domain.domain_id] = "dropped"
    return outcomes
