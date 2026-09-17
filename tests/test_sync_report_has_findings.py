"""Direct unit coverage for ``sync.report_has_findings`` -- pins each attention-finding
trigger independently on hand-built ``report_as_dict``-shaped dicts, and pins that the
informational-only fields (including, since the live-findings refinement, ``orphaned``/
``ambiguous`` outcomes) never trigger it. See design/superpowers/specs/
2026-07-11-ci-live-findings-design.md ruling 1. Complements tests/test_cli_sync_check.py
(end-to-end CLI coverage) with isolated, single-trigger-at-a-time cases that an end-to-end
fixture can't easily pin on its own."""

from __future__ import annotations

from sidegraph.sync import report_has_findings


def _report(**overrides) -> dict:
    base = {
        "synced": True,
        "from_version": "v1",
        "to_version": "v2",
        "counts": "",
        "repointed": 0,
        "outcomes": [],
        "stale_decisions": [],
        "empty_domains": [],
        "overbroad_domains": [],
        "slug_conflicts": [],
        "domains_refreshed": 0,
        "domain_failures": [],
    }
    base.update(overrides)
    return base


def test_clean_report_has_no_findings():
    assert report_has_findings(_report()) is False


def test_orphaned_outcome_alone_does_not_trigger():
    # Informational since the live-findings refinement (ruling 1): a legitimate rename+heal
    # leaves the renamed-away entity's leaf orphaned for good (no retirement path in an
    # append-only store) -- that residue must not red-flag the check forever. When an
    # orphaned anchor actually costs reachability, the decision goes stale and
    # ``stale_decisions`` already fires (see test_stale_decisions_alone_triggers below).
    d = _report(outcomes=[{"status": "orphaned", "canonical_name": "x", "detail": None}])
    assert report_has_findings(d) is False


def test_ambiguous_outcome_alone_does_not_trigger():
    # Same ruling as orphaned above -- ambiguous is a heads-up, not a hard failure.
    d = _report(outcomes=[{"status": "ambiguous", "canonical_name": "x", "detail": "2 candidates"}])
    assert report_has_findings(d) is False


def test_error_outcome_alone_triggers():
    d = _report(outcomes=[{"status": "error", "canonical_name": "x", "detail": "boom"}])
    assert report_has_findings(d) is True


def test_stale_decisions_alone_triggers():
    d = _report(stale_decisions=[{"id": "d1", "title": "t"}])
    assert report_has_findings(d) is True


def test_slug_conflicts_alone_triggers():
    d = _report(slug_conflicts=[{"slug": "payments", "domain_ids": ["a", "b"]}])
    assert report_has_findings(d) is True


def test_moved_outcome_alone_does_not_trigger():
    # "moved" is noteworthy (surfaced in "outcomes") but not itself a finding -- the
    # mapping healed itself, nothing needs a human's hand.
    d = _report(outcomes=[{"status": "moved", "canonical_name": "x", "detail": "a.py -> b.py"}])
    assert report_has_findings(d) is False


def test_empty_and_overbroad_domains_alone_do_not_trigger():
    # Informational only, per ruling 1 -- never fail the check on their own.
    d = _report(
        empty_domains=[{"slug": "ghost", "title": "Ghost"}],
        overbroad_domains=[{"slug": "wide", "title": "Wide", "matched": 5, "total": 10}],
    )
    assert report_has_findings(d) is False


def test_domain_failure_is_an_attention_finding():
    """A domain that cannot heal is an error class, like an `error` outcome -- not
    informational like empty_domains/overbroad_domains. CI must exit 2 on it."""
    from sidegraph.sync import report_has_findings

    clean = {"outcomes": [], "stale_decisions": [], "slug_conflicts": [], "domain_failures": []}
    assert not report_has_findings(clean)
    failure = {"slug": "d", "title": "d", "error": "boom"}
    assert report_has_findings({**clean, "domain_failures": [failure]})
