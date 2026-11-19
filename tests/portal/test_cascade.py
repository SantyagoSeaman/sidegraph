"""The read-only ratification forecast uses the same batch cascade as Apply (D6)."""

import pytest

from sidegraph.portal.contract import PreviewResult
from sidegraph.server import _ratify_impl
from sidegraph.store import Store
from tests.store_record_support import _decision, _domain, _fact


@pytest.mark.parametrize(
    ("accept_first", "reject_second", "exclusive_outcome", "shared_outcome"),
    [
        (False, False, "rejected", "stays"),
        (False, True, "rejected", "rejected"),
        (True, True, "accepted", "accepted"),
    ],
)
def test_cascade_preview_whole_batch(
    tmp_path, accept_first, reject_second, exclusive_outcome, shared_outcome
) -> None:
    from sidegraph.portal.cascade import preview

    with Store(tmp_path / ".sidegraph") as store:
        first = store.add_decision(_decision("First", status="proposed"))
        second = store.add_decision(_decision("Second", status="proposed"))
        exclusive = store.add_fact(_fact(status="proposed", supports=[first.id]))
        shared = store.add_fact(_fact(status="proposed", supports=[first.id, second.id]))
        accept = [first.id] if accept_first else []
        reject = ([] if accept_first else [first.id]) + ([second.id] if reject_second else [])
        accepted_domain = store.add_domain(_domain("accepted-domain"))
        dropped_domain = store.add_domain(_domain("dropped-domain"))
        accept.append(accepted_domain.domain_id)
        reject.append(dropped_domain.domain_id)
        outcomes = PreviewResult.model_validate(
            {
                "outcomes": {
                    key: {"outcome": val} for key, val in preview(store, accept, reject).items()
                }
            }
        ).outcomes
        assert store.get_fact(exclusive.id).status == "proposed"
        assert store.get_fact(shared.id).status == "proposed"
        assert outcomes[first.id].outcome == ("accepted" if accept_first else "rejected")
        assert outcomes[exclusive.id].outcome == exclusive_outcome
        assert outcomes[shared.id].outcome == shared_outcome
        _ratify_impl(store, accept, reject, drop_needs_verdict=True)
        for domain, expected in [(accepted_domain, "accepted"), (dropped_domain, "dropped")]:
            assert outcomes[domain.domain_id].outcome == expected
            assert store.get_domain(domain.domain_id).status == expected
        for fact in (exclusive, shared):
            outcome = outcomes[fact.id].outcome
            assert store.get_fact(fact.id).status == ("proposed" if outcome == "stays" else outcome)


def test_preview_stays_when_apply_would_refuse(tmp_path) -> None:
    from sidegraph.portal.cascade import preview
    from tests.store_format2_support import put_file

    store_path = tmp_path / ".sidegraph"
    with Store(store_path) as store:
        old = store.add_decision(_decision("Predecessor"))
        store.add_decision(_decision("Accepted successor", supersedes=old.id))
        domain = store.add_domain(_domain(status="accepted"))
        merged = _decision("Merged successor", status="proposed", supersedes=old.id)
        put_file(store_path, "decision", merged.model_dump(mode="json"))
        store.refresh_if_stale()
        evidence = store.add_fact(_fact(status="proposed", supports=[merged.id]))
        outcomes = PreviewResult.model_validate(
            {
                "outcomes": {
                    key: {"outcome": val}
                    for key, val in preview(store, [merged.id], [domain.domain_id]).items()
                }
            }
        ).outcomes
        assert outcomes[merged.id].outcome == "stays"
        assert outcomes[domain.domain_id].outcome == "stays"
        assert evidence.id not in outcomes or outcomes[evidence.id].outcome == "stays"
        applied = _ratify_impl(store, [merged.id], [domain.domain_id], drop_needs_verdict=True)
        assert applied[merged.id].startswith("error:")
        assert applied[domain.domain_id].startswith("error:")
        assert store.get_fact(evidence.id).status == "proposed"


def test_preview_forecasts_conflicts_created_by_an_earlier_accept(tmp_path) -> None:
    from sidegraph.portal.cascade import preview
    from tests.store_format2_support import put_file

    store_path = tmp_path / ".sidegraph"
    with Store(store_path) as store:
        original = store.add_decision(_decision("Original"))
        intermediate = store.add_decision(_decision("Intermediate", supersedes=original.id))
        retired = store.add_decision(_decision("Retired", supersedes=intermediate.id))
        store.retire(retired.id, "obsolete")
        first = _decision("First draft", supersedes=intermediate.id, status="proposed")
        second = _decision("Second draft", supersedes=original.id, status="proposed")
        for draft in (first, second):
            put_file(store_path, "decision", draft.model_dump(mode="json"))
        store.refresh_if_stale()
        evidence = store.add_fact(_fact(status="proposed", supports=[second.id]))
        accept = [first.id, second.id]
        outcomes = PreviewResult.model_validate(
            {"outcomes": {key: {"outcome": val} for key, val in preview(store, accept, []).items()}}
        ).outcomes
        assert outcomes[first.id].outcome == "accepted"
        assert outcomes[second.id].outcome == "stays"
        assert outcomes[evidence.id].outcome == "stays"
        applied = _ratify_impl(store, accept)
        assert applied[first.id] == "accepted"
        assert applied[second.id].startswith("error:")
        assert store.get_fact(evidence.id).status == "proposed"


def test_preview_includes_untouched_evidence_of_a_settled_mark(tmp_path) -> None:
    from sidegraph.portal.cascade import preview

    with Store(tmp_path / ".sidegraph") as store:
        settled = store.add_decision(_decision())
        fact = store.add_fact(_fact(status="proposed", supports=[settled.id]))
        outcomes = PreviewResult.model_validate(
            {
                "outcomes": {
                    key: {"outcome": val} for key, val in preview(store, [settled.id], []).items()
                }
            }
        ).outcomes
        assert outcomes[settled.id].outcome == outcomes[fact.id].outcome == "stays"
