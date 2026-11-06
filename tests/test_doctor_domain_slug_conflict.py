"""doctor's ``domain-not-closed`` and ``domain-slug-conflict``.

An older client (0.23) accepts a proposed domain revision without closing the accepted domain
it replaces, so both stay ``accepted`` (``domain-not-closed``, whatever the slugs). Two branches
can also each add a domain under one slug and merge (``domain-slug-conflict``). ``sidegraph-sync``
lists same-slug pairs as ``slug_conflicts``; doctor must report both, so ``--check`` fails.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from sidegraph.cli import doctor_main
from sidegraph.doctor import curate
from sidegraph.schema import Domain, DomainStatus, Provenance
from sidegraph.store import Store, live_slug_conflicts
from tests.store_format2_support import domain_body, format1_store, make_store, put_shard_line

CODE = "domain-slug-conflict"
NOT_CLOSED = "domain-not-closed"


def _domain(slug: str, **overrides) -> Domain:
    base = dict(slug=slug, title=slug, summary="Area", provenance=Provenance(source="manual"))
    base.update(overrides)
    return Domain(**base)


def _write_domain_file(store_path: Path, domain: Domain) -> None:
    """Hand-write a domain file, as an older client or a git merge leaves it."""
    data = domain.model_dump(mode="json")
    data.pop("communities", None)
    path = store_path / "domains" / f"{domain.domain_id}.json"
    path.write_text(
        json.dumps(data, sort_keys=True, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def _unclosed_predecessor(store_dir: Path, new_slug: str = "pay") -> tuple[str, str]:
    """An accepted ``pay`` and its revision, accepted the way a 0.23 client did it: the
    revision's file flips to accepted and the predecessor is never closed."""
    with Store(store_dir) as s:
        old = s.add_domain(_domain("pay", status=DomainStatus.ACCEPTED))
        new = s.supersede_domain(old.domain_id, _domain(new_slug, supersedes=old.domain_id))
    accepted = new.model_copy(
        update={"status": DomainStatus.ACCEPTED, "ratified_at": datetime.now(UTC)}
    )
    _write_domain_file(store_dir, accepted)
    return old.domain_id, new.domain_id


def _findings(store_dir: Path, code: str = CODE):
    return [f for f in curate(store_dir).findings if f.code == code]


def test_doctor_reports_a_predecessor_left_open_by_an_older_client(tmp_path) -> None:
    # Red against unfixed code: doctor had no check for it at all.
    store_dir = tmp_path / "s"
    old_id, new_id = _unclosed_predecessor(store_dir)

    found = _findings(store_dir, NOT_CLOSED)

    assert len(found) == 1
    assert found[0].record_id == old_id
    assert found[0].record_type == "domain"
    assert found[0].path == str(store_dir / "domains" / f"{old_id}.json")
    assert f"pay ({old_id}) is still accepted, but pay ({new_id}) replaced it" in found[0].detail
    assert f"`sidegraph-ratify --drop {old_id}`" in found[0].detail
    # Fully explained by the unclosed predecessor: no second, generic finding for the slug.
    assert _findings(store_dir) == []


def test_doctor_reports_a_renamed_replacement_left_open(tmp_path) -> None:
    # Red against a check keyed on slugs alone: the replacement took another slug, so no slug
    # is shared, yet two accepted domains still claim the area.
    store_dir = tmp_path / "s"
    old_id, new_id = _unclosed_predecessor(store_dir, new_slug="billing")
    found = _findings(store_dir, NOT_CLOSED)
    assert [f.record_id for f in found] == [old_id]
    assert f"but billing ({new_id}) replaced it" in found[0].detail


def test_doctor_names_every_open_link_of_a_chain(tmp_path) -> None:
    # A -> B -> C all accepted (an older client accepted twice): both A and B need closing.
    store_dir = tmp_path / "s"
    a_id, b_id = _unclosed_predecessor(store_dir)
    c = _domain("pay", supersedes=b_id, status=DomainStatus.ACCEPTED)
    _write_domain_file(store_dir, c)
    assert sorted(f.record_id for f in _findings(store_dir, NOT_CLOSED)) == sorted([a_id, b_id])
    assert _findings(store_dir) == []


def test_doctor_reports_a_predecessor_whose_replacement_was_replaced_since(tmp_path) -> None:
    # Red against a check that needs the replacement still accepted (review round 2, Opus):
    # A -> B accepted by an older client, then B -> C ratified by this version closes B and
    # leaves A accepted beside C under another slug.
    store_dir = tmp_path / "s"
    a_id, b_id = _unclosed_predecessor(store_dir, new_slug="billing")
    with Store(store_dir) as s:
        c = s.supersede_domain(b_id, _domain("ledger", supersedes=b_id))
        s.ratify_domains(accept=[c.domain_id])
        assert s.get_domain(b_id).status == DomainStatus.SUPERSEDED
    found = _findings(store_dir, NOT_CLOSED)
    assert [f.record_id for f in found] == [a_id]
    assert f"but billing ({b_id}) replaced it, and the live replacement is now ledger " in (
        found[0].detail
    )


def test_a_replacement_chain_that_ended_in_a_drop_is_not_reported(tmp_path) -> None:
    # Red against counting any superseded replacement (review round 3, Opus): with C dropped,
    # A is the only live domain of the area, and the drop doctor advised would empty it.
    store_dir = tmp_path / "s"
    a_id, b_id = _unclosed_predecessor(store_dir, new_slug="billing")
    with Store(store_dir) as s:
        c = s.supersede_domain(b_id, _domain("ledger", supersedes=b_id))
        s.ratify_domains(accept=[c.domain_id])
        s.ratify_domains(drop=[c.domain_id])
    assert _findings(store_dir, NOT_CLOSED) == []


def test_a_recovered_dropped_proposal_is_not_reported(tmp_path) -> None:
    # Red against counting any superseded replacement (review round 3, Sol): B was proposed
    # and dropped, never accepted; C recovering it marks B superseded.
    store_dir = tmp_path / "s"
    with Store(store_dir) as s:
        a = s.add_domain(_domain("pay", status=DomainStatus.ACCEPTED))
        b = s.supersede_domain(a.domain_id, _domain("billing", supersedes=a.domain_id))
        s.ratify_domains(drop=[b.domain_id])
        c = s.supersede_domain(b.domain_id, _domain("ledger", supersedes=b.domain_id))
        assert s.get_domain(b.domain_id).status == DomainStatus.SUPERSEDED
        assert _findings(store_dir, NOT_CLOSED) == []
        s.ratify_domains(accept=[c.domain_id])
    assert _findings(store_dir, NOT_CLOSED) == []


def test_doctor_reports_two_unrelated_domains_on_one_slug(tmp_path) -> None:
    # Red against unfixed code. The merge-race shape: neither replaces the other.
    store_dir = tmp_path / "s"
    with Store(store_dir) as s:
        first = s.add_domain(_domain("pay", status=DomainStatus.ACCEPTED))
    second = _domain("pay", status=DomainStatus.ACCEPTED)
    _write_domain_file(store_dir, second)

    found = _findings(store_dir)

    assert len(found) == 1
    ids = sorted([first.domain_id, second.domain_id])
    assert found[0].record_id is None
    assert found[0].path == str(store_dir)
    assert f"slug pay is held by 2 live domains: {ids[0]}, {ids[1]}" in found[0].detail
    assert "`sidegraph-ratify --drop <id>`" in found[0].detail
    assert "replaces" not in found[0].detail


def test_doctor_check_fails_on_a_slug_conflict(tmp_path, capsys) -> None:
    # Red against unfixed code (exit 0), and against adding the code to the exempt set.
    store_dir = tmp_path / "s"
    _unclosed_predecessor(store_dir, new_slug="billing")
    assert doctor_main(["--db", str(store_dir), "--check"]) == 2
    assert NOT_CLOSED in capsys.readouterr().out


def test_doctor_check_fails_on_a_merged_slug_conflict(tmp_path, capsys) -> None:
    store_dir = tmp_path / "s"
    with Store(store_dir) as s:
        s.add_domain(_domain("pay", status=DomainStatus.ACCEPTED))
    _write_domain_file(store_dir, _domain("pay", status=DomainStatus.ACCEPTED))
    assert doctor_main(["--db", str(store_dir), "--check"]) == 2
    assert CODE in capsys.readouterr().out


def test_a_replacement_accepted_by_this_version_is_not_reported(tmp_path) -> None:
    # Red against a check that flags any accepted successor without looking at whether the
    # predecessor is still accepted: ratify closes it as superseded.
    store_dir = tmp_path / "s"
    with Store(store_dir) as s:
        old = s.add_domain(_domain("pay", status=DomainStatus.ACCEPTED))
        new = s.supersede_domain(old.domain_id, _domain("billing", supersedes=old.domain_id))
        s.ratify_domains(accept=[new.domain_id])
    assert _findings(store_dir, NOT_CLOSED) == []
    assert _findings(store_dir) == []


def test_a_single_pending_revision_is_not_a_conflict(tmp_path) -> None:
    # Red against a check that counts every live holder without the pending-revision exemption.
    store_dir = tmp_path / "s"
    with Store(store_dir) as s:
        old = s.add_domain(_domain("pay", status=DomainStatus.ACCEPTED))
        s.supersede_domain(old.domain_id, _domain("pay", supersedes=old.domain_id))
    assert _findings(store_dir) == []


def test_dropping_the_left_open_predecessor_clears_the_finding(tmp_path) -> None:
    # Red against a check that counts dropped domains as live: the documented fix must work.
    store_dir = tmp_path / "s"
    old_id, _new_id = _unclosed_predecessor(store_dir)
    with Store(store_dir) as s:
        s.ratify_domains(drop=[old_id])
    assert _findings(store_dir) == []
    assert _findings(store_dir, NOT_CLOSED) == []


def test_doctor_and_the_store_agree(tmp_path) -> None:
    # Guard: on a merge race, doctor names exactly the holders sync's report names.
    store_dir = tmp_path / "s"
    with Store(store_dir) as s:
        s.add_domain(_domain("pay", status=DomainStatus.ACCEPTED))
    _write_domain_file(store_dir, _domain("pay", status=DomainStatus.ACCEPTED))
    with Store(store_dir) as s:
        (conflict,) = s.domain_slug_conflicts()
    (found,) = _findings(store_dir)
    assert f"held by 2 live domains: {', '.join(conflict['domain_ids'])}" in found.detail


def test_a_malformed_domain_does_not_crash_doctor(tmp_path) -> None:
    # Red against building conflict rows from unvalidated payloads: a list `supersedes` is
    # unhashable and raised TypeError out of curate(). A format-1 store hands doctor the raw
    # files; format 2 already drops a record that fails the model check.
    store_dir = format1_store(tmp_path / "s")
    old_id, new_id = _unclosed_predecessor(store_dir)
    path = store_dir / "domains" / f"{new_id}.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["supersedes"] = [old_id]
    payload["status"] = "proposed"
    path.write_text(json.dumps(payload), encoding="utf-8")
    curate(store_dir)  # no exception; verify reports the malformed record


def test_a_packed_domain_is_reported_where_it_is_stored(tmp_path) -> None:
    # Red against a check that builds `domains/<id>.json`: a packed record has no such file.
    store_dir = make_store(tmp_path)
    old_id, new_id = "01M6S0000000000000000000A1", "01M6S0000000000000000000B2"
    shard = put_shard_line(
        store_dir, "domain", old_id, domain_body(old_id, slug="pay", status="accepted")
    )
    put_shard_line(
        store_dir,
        "domain",
        new_id,
        domain_body(new_id, slug="pay", status="accepted", supersedes=old_id),
    )
    (found,) = _findings(store_dir, NOT_CLOSED)
    assert found.record_id == old_id
    assert found.path == str(shard)


def test_live_slug_conflicts_keeps_two_revisions_of_one_holder_as_rivals() -> None:
    # Guard on the shared function: two pending revisions of one accepted holder still conflict.
    rows = [
        {"domain_id": "A", "slug": "pay", "status": "accepted", "supersedes": None},
        {"domain_id": "B", "slug": "pay", "status": "proposed", "supersedes": "A"},
        {"domain_id": "C", "slug": "pay", "status": "proposed", "supersedes": "A"},
        {"domain_id": "D", "slug": "pay", "status": "dropped", "supersedes": None},
        {"domain_id": "E", "slug": "ops", "status": "accepted", "supersedes": None},
    ]
    assert live_slug_conflicts(rows) == [{"slug": "pay", "domain_ids": ["A", "B", "C"]}]
