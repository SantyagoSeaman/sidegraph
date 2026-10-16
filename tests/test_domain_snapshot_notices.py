"""Ratify and supersede answers, and doctor, say when a domain's membership is frozen or split.

see design/superpowers/specs/2026-10-14-graphify-upgrade-design.md (D5)"""

from __future__ import annotations

from sidegraph.cli import ratify_main
from sidegraph.doctor import curate
from sidegraph.schema import Domain, Provenance
from sidegraph.server import _ratify_impl, _supersede_domain_impl
from sidegraph.store import Store
from tests.snapshot_support import accept, standard_domains
from tests.test_sync_snapshot_membership import _snapshotted


def _successor(store, old, slug):
    return store.supersede_domain(
        old.domain_id,
        Domain(
            slug=slug,
            title=slug,
            summary="s",
            path_prefixes=["src"],
            supersedes=old.domain_id,
            provenance=Provenance(source="manual"),
        ),
    )


def test_ratifying_a_snapshot_domain_s_successor_says_its_membership_is_frozen(tmp_path):
    """Red against unfixed ratify: silence, and the new src/ rule looks applied."""
    store, domains, _l, mention = _snapshotted(tmp_path)
    succ = _successor(store, domains["payments"], "payments")
    answer = _ratify_impl(store, accept=[succ.domain_id], reader=mention)[succ.domain_id]
    assert "membership frozen by the domain snapshot" in answer
    assert "sidegraph-domains snapshot --refresh --domain payments" in answer


def test_ratifying_the_second_part_of_a_split_names_both_parts(tmp_path):
    store, domains, _l, mention = _snapshotted(tmp_path)
    accept(store, "billing-a", supersedes=domains["payments"])
    b = _successor(store, domains["payments"], "billing-b")
    answer = _ratify_impl(store, accept=[b.domain_id], reader=mention)[b.domain_id]
    assert "split from snapshot domain payments" in answer
    assert "--domain billing-a --domain billing-b" in answer


def test_the_cli_ratify_prints_the_same_note(tmp_path, capsys):
    store, domains, _l, _m = _snapshotted(tmp_path)
    succ = _successor(store, domains["payments"], "payments")
    store.close()
    assert (
        ratify_main(
            [
                "--db",
                str(tmp_path / ".sidegraph"),
                "--graph",
                str(tmp_path / "mention.json"),
                "--accept",
                succ.domain_id,
            ]
        )
        == 0
    )
    assert "membership frozen by the domain snapshot" in capsys.readouterr().out


def test_superseding_a_snapshot_domain_says_where_its_entries_go(tmp_path):
    store, _d, _l, mention = _snapshotted(tmp_path)
    out = _supersede_domain_impl(store, mention, "payments", "billing", "Billing", "Charges.")
    assert "carries over to billing" in out["snapshot_note"]


def test_without_a_snapshot_the_supersede_answer_has_no_note(tmp_path):
    """Guard."""
    store = Store(tmp_path / ".sidegraph")
    standard_domains(store)
    assert "snapshot_note" not in _supersede_domain_impl(
        store, None, "payments", "billing", "B", "x"
    )


def test_doctor_reports_a_split(tmp_path):
    """Red against a split that only the human ratify answers mention (the auto-ratify paths
    drop the activation note)."""
    store, domains, _l, _m = _snapshotted(tmp_path)
    accept(store, "billing-a", supersedes=domains["payments"])
    accept(store, "billing-b", supersedes=domains["payments"])
    store.close()
    found = [f for f in curate(tmp_path / ".sidegraph").findings if f.code == "snapshot-split"]
    assert len(found) == 1
    assert "--domain billing-a --domain billing-b" in found[0].detail
