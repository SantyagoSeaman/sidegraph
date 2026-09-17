"""Cross-branch domain-slug race detection (see
docs/reference/store-format.md#merge-semantics-git-resolves-it-not-the-store): two branches
independently propose/accept a domain with the same slug -- different `domain_id`s, so the
files themselves merge in cleanly with no git conflict,
and nothing at write time (`_validate_domain_write`, single-process only) ever catches it
after the fact. The INDEX detects this at load (cold open / digest mismatch) and flags it,
matching the spec's promise: "the INDEX detects duplicate live slugs at load and flags them
in the sync report ('slug conflict — supersede one')"."""

from __future__ import annotations

import json
from pathlib import Path

from sidegraph.engine.reader import GraphifyReader
from sidegraph.schema import Domain, DomainStatus, Provenance
from sidegraph.store import Store
from sidegraph.sync import sync


def _domain(**overrides) -> Domain:
    base = dict(
        slug="payments",
        title="Payments",
        summary="Handles order settlement and refunds.",
        provenance=Provenance(source="manual"),
    )
    base.update(overrides)
    return Domain(**base)


def _write_domain_file(store_path: Path, domain: Domain) -> None:
    """Hand-write a domain's canonical file directly (bypassing the Store API entirely) --
    exactly what a `git merge` landing two branches' independently-created domain files
    would leave on disk. Same shape _write_domain_canonical produces: full Domain minus
    the volatile `communities` field."""
    data = domain.model_dump(mode="json")
    data.pop("communities", None)
    path = store_path / "domains" / f"{domain.domain_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, sort_keys=True, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def _write_graph(tmp_path, name, data):
    p = tmp_path / name
    p.write_text(json.dumps(data))
    return p


_EMPTY_GRAPH = {"built_at_commit": "v1", "nodes": [], "links": []}


# -- detection at cold load ------------------------------------------------------------


def test_two_accepted_domains_same_slug_flagged_as_conflict(tmp_path, capsys) -> None:
    store_dir = tmp_path / "s"
    Store(store_dir).close()  # bootstrap the canonical layout, nothing in it yet

    d1 = _domain(
        domain_id="01SLUGCONFLICTBRANCHA0001",
        slug="payments",
        title="Payments (branch A)",
        status=DomainStatus.ACCEPTED,
    )
    d2 = _domain(
        domain_id="01SLUGCONFLICTBRANCHB0002",
        slug="payments",
        title="Payments (branch B)",
        status=DomainStatus.ACCEPTED,
    )
    _write_domain_file(store_dir, d1)
    _write_domain_file(store_dir, d2)

    store = Store(store_dir)  # digest mismatch (files appeared externally) -> full reload
    try:
        conflicts = store.domain_slug_conflicts()
        assert conflicts == [
            {"slug": "payments", "domain_ids": sorted([d1.domain_id, d2.domain_id])}
        ]
        err = capsys.readouterr().err
        assert "WARNING" in err
        assert "payments" in err
        assert d1.domain_id in err and d2.domain_id in err

        # retrieval keeps working deterministically despite the conflict
        found = store.find_domain_by_slug("payments")
        assert found is not None  # both are accepted; newest (higher ULID) wins the tie
        assert found.domain_id == max(d1.domain_id, d2.domain_id)
    finally:
        store.close()


def test_proposed_and_accepted_same_slug_still_flagged(tmp_path, capsys) -> None:
    """Both proposed and accepted count as "live" for conflict purposes -- same status set
    _validate_domain_write's own write-time uniqueness query uses (proposed | accepted);
    only superseded/dropped free a slug back up. Verified directly against
    find_domain_by_slug's own status handling (excludes only SUPERSEDED) before pinning:
    a dropped domain is still visible to find_domain_by_slug but must NOT count as part of
    a slug conflict (see the no-false-positive test below)."""
    store_dir = tmp_path / "s"
    Store(store_dir).close()

    accepted = _domain(
        domain_id="01SLUGMIXEDACCEPTED00001",
        slug="payments",
        title="Payments accepted",
        status=DomainStatus.ACCEPTED,
    )
    proposed = _domain(
        domain_id="01SLUGMIXEDPROPOSED00002",
        slug="payments",
        title="Payments proposed",
        status=DomainStatus.PROPOSED,
    )
    _write_domain_file(store_dir, accepted)
    _write_domain_file(store_dir, proposed)

    store = Store(store_dir)
    try:
        conflicts = store.domain_slug_conflicts()
        assert conflicts == [
            {"slug": "payments", "domain_ids": sorted([accepted.domain_id, proposed.domain_id])}
        ]
        assert "WARNING" in capsys.readouterr().err
    finally:
        store.close()


# -- no false positives ------------------------------------------------------------------


def test_unique_slug_no_conflict(tmp_path) -> None:
    store = Store(tmp_path / "s")
    try:
        store.add_domain(_domain(slug="payments"))
        assert store.domain_slug_conflicts() == []
    finally:
        store.close()


def test_dropped_domain_never_counted_toward_a_conflict(tmp_path, capsys) -> None:
    """A dropped domain intentionally frees its slug back up (see
    test_store_domains.test_slug_reusable_once_dropped) -- it must never itself trigger a
    conflict against a live domain sharing the same slug."""
    store_dir = tmp_path / "s"
    Store(store_dir).close()

    live = _domain(
        domain_id="01SLUGDROPPEDLIVE000001",
        slug="payments",
        title="Payments (kept)",
        status=DomainStatus.ACCEPTED,
    )
    dropped = _domain(
        domain_id="01SLUGDROPPEDGONE000002",
        slug="payments",
        title="Payments (dropped)",
        status=DomainStatus.DROPPED,
    )
    _write_domain_file(store_dir, live)
    _write_domain_file(store_dir, dropped)

    store = Store(store_dir)
    try:
        assert store.domain_slug_conflicts() == []
        assert "WARNING" not in capsys.readouterr().err
    finally:
        store.close()


def test_superseded_domain_never_counted_toward_a_conflict(tmp_path) -> None:
    store_dir = tmp_path / "s"
    Store(store_dir).close()

    live = _domain(
        domain_id="01SLUGSUPERSEDEDLIVE0001",
        slug="payments",
        title="Payments (kept)",
        status=DomainStatus.ACCEPTED,
    )
    superseded = _domain(
        domain_id="01SLUGSUPERSEDEDOLD00002",
        slug="payments",
        title="Payments (old)",
        status=DomainStatus.SUPERSEDED,
        supersedes=None,
    )
    _write_domain_file(store_dir, live)
    _write_domain_file(store_dir, superseded)

    store = Store(store_dir)
    try:
        assert store.domain_slug_conflicts() == []
    finally:
        store.close()


# -- computed live: reflects the CURRENT state on every call, no caching -----------------
#
# Review round 3 fix: an earlier version cached the conflict list in index meta at reload
# time only. A probe proved that wrong for the common single-clone workflow: resolve the
# conflict in-process, reopen in a brand-new process (or even just call sync(force=True))
# against the SAME already-fresh store -> nothing ever busts the digest to force a fresh
# reload, so the resolved conflict kept reporting as unresolved forever.
# domain_slug_conflicts() now recomputes from the index on every call instead.


def test_domain_slug_conflicts_empty_when_none_exist(tmp_path) -> None:
    store = Store(tmp_path / "s")
    try:
        assert store.domain_slug_conflicts() == []
    finally:
        store.close()


def test_conflict_clears_immediately_in_process_once_a_duplicate_is_retired(
    tmp_path, capsys
) -> None:
    store_dir = tmp_path / "s"
    Store(store_dir).close()

    keeper = _domain(
        domain_id="01SLUGCLEARKEEP0000001",
        slug="payments",
        title="Payments (keep)",
        status=DomainStatus.ACCEPTED,
    )
    duplicate = _domain(
        domain_id="01SLUGCLEARDROP0000002",
        slug="payments",
        title="Payments (duplicate)",
        status=DomainStatus.PROPOSED,
    )
    _write_domain_file(store_dir, keeper)
    _write_domain_file(store_dir, duplicate)

    store = Store(store_dir)  # cold reload: conflict detected + warned
    try:
        assert len(store.domain_slug_conflicts()) == 1
        capsys.readouterr()  # drain the reload-time warning

        # Resolve it: retire the duplicate (a PROPOSED domain -> plain ratify-drop,
        # sidesteps supersede_domain's own slug-uniqueness check entirely, which is a
        # separate, orthogonal concern from this detection feature).
        store.ratify_domains(drop=[duplicate.domain_id])

        # No reopen, no forced reload -- domain_slug_conflicts() is computed live, so this
        # SAME open Store already sees it cleared.
        assert store.domain_slug_conflicts() == []
        assert store.get_domain(keeper.domain_id).status == DomainStatus.ACCEPTED
    finally:
        store.close()


def test_conflict_clears_across_a_reopen_too(tmp_path) -> None:
    """Also true from a brand-new process/Store instance against the same, already-fresh
    (digest-matching) store -- the exact shape a probe caught the old cached version
    failing on."""
    store_dir = tmp_path / "s"
    Store(store_dir).close()

    keeper = _domain(
        domain_id="01SLUGREOPENKEEP000001",
        slug="payments",
        title="Payments (keep)",
        status=DomainStatus.ACCEPTED,
    )
    duplicate = _domain(
        domain_id="01SLUGREOPENDROP000002",
        slug="payments",
        title="Payments (duplicate)",
        status=DomainStatus.PROPOSED,
    )
    _write_domain_file(store_dir, keeper)
    _write_domain_file(store_dir, duplicate)

    store = Store(store_dir)
    store.ratify_domains(drop=[duplicate.domain_id])
    store.close()  # digest is self-consistent now -- a reopen is a fast, no-reload path

    reopened = Store(store_dir)
    try:
        assert reopened.domain_slug_conflicts() == []
    finally:
        reopened.close()


# -- surfaced in the sidegraph-sync report ------------------------------------------------


def test_sync_report_surfaces_slug_conflicts(tmp_path) -> None:
    store_dir = tmp_path / "s"
    Store(store_dir).close()

    d1 = _domain(
        domain_id="01SLUGSYNCBRANCHA00001",
        slug="payments",
        title="Payments (A)",
        status=DomainStatus.ACCEPTED,
    )
    d2 = _domain(
        domain_id="01SLUGSYNCBRANCHB00002",
        slug="payments",
        title="Payments (B)",
        status=DomainStatus.ACCEPTED,
    )
    _write_domain_file(store_dir, d1)
    _write_domain_file(store_dir, d2)

    store = Store(store_dir)
    try:
        reader = GraphifyReader(_write_graph(tmp_path, "g.json", _EMPTY_GRAPH))
        report = sync(store, reader)
        assert report.slug_conflicts == [
            {"slug": "payments", "domain_ids": sorted([d1.domain_id, d2.domain_id])}
        ]
    finally:
        store.close()


def test_sync_report_no_slug_conflicts_field_when_none_exist(tmp_path) -> None:
    store = Store(tmp_path / "s")
    try:
        store.add_domain(_domain(slug="payments"))
        reader = GraphifyReader(_write_graph(tmp_path, "g.json", _EMPTY_GRAPH))
        report = sync(store, reader)
        assert report.slug_conflicts == []
    finally:
        store.close()
