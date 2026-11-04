"""A domain replaced by two live domains is a split: ratify and doctor make it visible.

Two live replacements of one domain are a supported layout (a deliberate re-scope into parts,
or two branches that each replaced the domain and merged). Ratify succeeds and says so,
naming every part, for any domain, covered by the domain snapshot or not. Doctor reports it as
an advisory that ``--check`` does not escalate, and verify has nothing to say.

see docs/concepts/data-model.md (supersession)"""

from __future__ import annotations

import json
from pathlib import Path

from sidegraph.cli import doctor_main, ratify_main
from sidegraph.doctor import curate
from sidegraph.domain_splits import domain_splits
from sidegraph.schema import Domain, DomainStatus, Provenance
from sidegraph.server import _ratify_impl
from sidegraph.store import Store
from sidegraph.verify import verify_snapshot


def _domain(slug: str, **overrides) -> Domain:
    base = dict(
        slug=slug,
        title=slug,
        summary="Area responsibility",
        provenance=Provenance(source="manual"),
    )
    base.update(overrides)
    return Domain(**base)


def _write_domain_file(store_path: Path, domain: Domain) -> None:
    """Hand-write a domain file, as a conflict-free git merge of two branches leaves it."""
    data = domain.model_dump(mode="json")
    data.pop("communities", None)
    path = store_path / "domains" / f"{domain.domain_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, sort_keys=True, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def _two_proposals(store_dir: Path) -> tuple[str, str, str]:
    """An accepted ``old-area`` and two proposed replacements of it with different slugs, the
    second one hand-written as a merge would leave it."""
    with Store(store_dir) as s:
        old = s.add_domain(_domain("old-area", status=DomainStatus.ACCEPTED))
        left = s.supersede_domain(old.domain_id, _domain("new-left", supersedes=old.domain_id))
    right = _domain("new-right", supersedes=old.domain_id)
    _write_domain_file(store_dir, right)
    return old.domain_id, left.domain_id, right.domain_id


def _split_store(store_dir: Path) -> tuple[str, str, str]:
    """Both replacements accepted: the split."""
    old_id, left_id, right_id = _two_proposals(store_dir)
    with Store(store_dir) as s:
        assert s.ratify_domains(accept=[left_id, right_id]) == {
            left_id: "accepted",
            right_id: "accepted",
        }
    return old_id, left_id, right_id


def _statuses(store: Store) -> dict[str, DomainStatus]:
    return {d.slug: d.status for d in store.iter_domains()}


# -- ratify ---------------------------------------------------------------------------------


def test_ratify_accepts_both_parts_and_names_them_across_calls(tmp_path) -> None:
    store_dir = tmp_path / "s"
    _old_id, left_id, right_id = _two_proposals(store_dir)
    with Store(store_dir) as s:
        first = _ratify_impl(s, accept=[left_id])[left_id]
        second = _ratify_impl(s, accept=[right_id])[right_id]
        assert _statuses(s)["new-left"] == DomainStatus.ACCEPTED
        assert _statuses(s)["new-right"] == DomainStatus.ACCEPTED
        assert _statuses(s)["old-area"] == DomainStatus.SUPERSEDED

    assert "split" not in first
    assert second.startswith("accepted")
    assert "split: old-area is now replaced by 2 live domains" in second
    assert f"new-left ({left_id})" in second
    assert f"new-right ({right_id})" in second


def test_ratify_names_both_parts_when_one_call_accepts_both(tmp_path) -> None:
    store_dir = tmp_path / "s"
    _old_id, left_id, right_id = _two_proposals(store_dir)
    with Store(store_dir) as s:
        out = _ratify_impl(s, accept=[left_id, right_id])
        assert _statuses(s)["new-left"] == DomainStatus.ACCEPTED
        assert _statuses(s)["new-right"] == DomainStatus.ACCEPTED

    last = max((left_id, right_id), key=lambda i: list(out).index(i))
    assert "new-left" in out[last] and "new-right" in out[last]
    assert "split: old-area" in out[last]


def test_the_cli_ratify_prints_the_split(tmp_path, capsys) -> None:
    store_dir = tmp_path / "s"
    _old_id, left_id, right_id = _two_proposals(store_dir)
    assert ratify_main(["--db", str(store_dir), "--accept", left_id, right_id]) == 0
    out = capsys.readouterr().out
    assert "split: old-area is now replaced by 2 live domains" in out
    assert "new-left" in out and "new-right" in out


def test_revising_a_part_of_a_split_does_not_repeat_the_note(tmp_path) -> None:
    store_dir = tmp_path / "s"
    _old_id, left_id, _right_id = _split_store(store_dir)
    with Store(store_dir) as s:
        left2 = s.supersede_domain(left_id, _domain("new-left-2", supersedes=left_id))
        answer = _ratify_impl(s, accept=[left2.domain_id])[left2.domain_id]
    assert answer.startswith("accepted")
    assert "split" not in answer


def test_a_single_replacement_says_nothing_about_a_split(tmp_path) -> None:
    store_dir = tmp_path / "s"
    with Store(store_dir) as s:
        old = s.add_domain(_domain("old-area", status=DomainStatus.ACCEPTED))
        new = s.supersede_domain(old.domain_id, _domain("new-area", supersedes=old.domain_id))
        assert "split" not in _ratify_impl(s, accept=[new.domain_id])[new.domain_id]


def _revived_split(store_dir: Path) -> tuple[str, str, str]:
    """O -> left, right; two merged proposals replace ``right``; the first is accepted and then
    dropped, so only ``left`` is live. Returns ``(left, right, second proposal)``: accepting the
    second restores the split through a deeper branch."""
    _old_id, left_id, right_id = _split_store(store_dir)
    with Store(store_dir) as s:
        first = s.supersede_domain(right_id, _domain("deep-one", supersedes=right_id))
    second = _domain("deep-two", supersedes=right_id)
    _write_domain_file(store_dir, second)
    with Store(store_dir) as s:
        assert s.ratify_domains(accept=[first.domain_id]) == {first.domain_id: "accepted"}
        assert s.ratify_domains(drop=[first.domain_id]) == {first.domain_id: "dropped"}
        assert domain_splits({d.domain_id: d for d in s.iter_domains()}) == {}
    return left_id, right_id, second.domain_id


def test_a_revived_split_is_reported_on_the_mcp_path(tmp_path) -> None:
    store_dir = tmp_path / "s"
    left_id, _right_id, second_id = _revived_split(store_dir)
    with Store(store_dir) as s:
        answer = _ratify_impl(s, accept=[second_id])[second_id]
    assert "split: old-area is now replaced by 2 live domains" in answer
    assert f"new-left ({left_id})" in answer
    assert f"deep-two ({second_id})" in answer


def test_a_revived_split_is_reported_on_the_cli_path(tmp_path, capsys) -> None:
    store_dir = tmp_path / "s"
    left_id, _right_id, second_id = _revived_split(store_dir)
    assert ratify_main(["--db", str(store_dir), "--accept", second_id]) == 0
    out = capsys.readouterr().out
    assert "split: old-area is now replaced by 2 live domains" in out
    assert left_id in out and second_id in out


def test_revising_the_revived_part_is_silent_again(tmp_path) -> None:
    store_dir = tmp_path / "s"
    _left_id, _right_id, second_id = _revived_split(store_dir)
    with Store(store_dir) as s:
        assert "split:" in _ratify_impl(s, accept=[second_id])[second_id]
        third = s.supersede_domain(second_id, _domain("deep-three", supersedes=second_id))
        assert "split" not in _ratify_impl(s, accept=[third.domain_id])[third.domain_id]


# -- the rule: a part counts while its chain reaches an accepted head -------------------------


def _models(store_dir: Path) -> dict[str, Domain]:
    with Store(store_dir) as s:
        return {d.domain_id: d for d in s.iter_domains()}


def test_domain_splits_names_the_live_parts(tmp_path) -> None:
    store_dir = tmp_path / "s"
    old_id, left_id, right_id = _split_store(store_dir)
    assert domain_splits(_models(store_dir)) == {old_id: sorted([left_id, right_id])}


def test_a_part_that_moved_on_counts_as_its_live_head(tmp_path) -> None:
    store_dir = tmp_path / "s"
    old_id, left_id, right_id = _split_store(store_dir)
    with Store(store_dir) as s:
        right2 = s.supersede_domain(right_id, _domain("new-right-2", supersedes=right_id))
        s.ratify_domains(accept=[right2.domain_id])
    assert domain_splits(_models(store_dir)) == {old_id: sorted([left_id, right2.domain_id])}


def test_a_part_whose_chain_ended_in_a_drop_no_longer_counts(tmp_path) -> None:
    store_dir = tmp_path / "s"
    _old_id, left_id, right_id = _split_store(store_dir)
    with Store(store_dir) as s:
        right2 = s.supersede_domain(right_id, _domain("new-right-2", supersedes=right_id))
        s.ratify_domains(accept=[right2.domain_id])
        s.ratify_domains(drop=[right2.domain_id])  # right is superseded, its head is dropped
    assert domain_splits(_models(store_dir)) == {}
    assert left_id in _models(store_dir)


def test_a_part_behind_a_long_chain_still_counts(tmp_path) -> None:
    store_dir = tmp_path / "s"
    old_id, left_id, right_id = _split_store(store_dir)
    with Store(store_dir) as s:
        tail = right_id
        for n in range(70):  # past any fixed walk limit
            nxt = s.supersede_domain(tail, _domain(f"chain-{n}", supersedes=tail))
            s.ratify_domains(accept=[nxt.domain_id])
            tail = nxt.domain_id
    models = _models(store_dir)
    assert domain_splits(models) == {old_id: sorted([left_id, tail])}
    assert len(_split_findings(store_dir)) == 1


def test_a_dropped_part_no_longer_counts(tmp_path) -> None:
    store_dir = tmp_path / "s"
    _old_id, _left_id, right_id = _split_store(store_dir)
    with Store(store_dir) as s:
        s.ratify_domains(drop=[right_id])
    assert domain_splits(_models(store_dir)) == {}


def test_a_pending_replacement_is_not_a_part(tmp_path) -> None:
    store_dir = tmp_path / "s"
    _old_id, left_id, _right_id = _two_proposals(store_dir)
    with Store(store_dir) as s:
        s.ratify_domains(accept=[left_id])
    assert domain_splits(_models(store_dir)) == {}


# -- doctor and verify ------------------------------------------------------------------------


def _split_findings(store_dir: Path):
    return [f for f in curate(store_dir).findings if f.code == "domain-split"]


def test_doctor_reports_a_split_of_an_uncovered_domain(tmp_path) -> None:
    store_dir = tmp_path / "s"
    old_id, left_id, right_id = _split_store(store_dir)

    found = _split_findings(store_dir)

    assert len(found) == 1
    assert found[0].record_id == old_id
    assert "old-area is replaced by 2 live domains" in found[0].detail
    assert f"new-left ({left_id})" in found[0].detail
    assert f"new-right ({right_id})" in found[0].detail


def test_doctor_check_does_not_escalate_a_split(tmp_path, capsys) -> None:
    store_dir = tmp_path / "s"
    _split_store(store_dir)
    assert doctor_main(["--db", str(store_dir)]) == 0
    assert doctor_main(["--db", str(store_dir), "--check"]) == 0
    assert "domain-split" in capsys.readouterr().out


def test_doctor_is_silent_once_a_chain_no_longer_reaches_an_accepted_head(tmp_path) -> None:
    store_dir = tmp_path / "s"
    _old_id, _left_id, right_id = _split_store(store_dir)
    with Store(store_dir) as s:
        s.ratify_domains(drop=[right_id])
    assert _split_findings(store_dir) == []


def test_doctor_is_silent_without_a_split(tmp_path) -> None:
    store_dir = tmp_path / "s"
    with Store(store_dir) as s:
        old = s.add_domain(_domain("old-area", status=DomainStatus.ACCEPTED))
        new = s.supersede_domain(old.domain_id, _domain("new-area", supersedes=old.domain_id))
        s.ratify_domains(accept=[new.domain_id])
    assert _split_findings(store_dir) == []


def test_verify_has_nothing_to_say_about_a_split(tmp_path) -> None:
    store_dir = tmp_path / "s"
    _split_store(store_dir)
    assert verify_snapshot(store_dir) == []


# -- a split of a domain the snapshot covers keeps its snapshot wording -----------------------


def test_a_covered_split_is_the_snapshots_not_a_second_finding(tmp_path) -> None:
    from tests.snapshot_support import accept
    from tests.test_sync_snapshot_membership import _snapshotted

    store, domains, _l, _mention = _snapshotted(tmp_path)
    accept(store, "billing-a", supersedes=domains["payments"])
    accept(store, "billing-b", supersedes=domains["payments"])
    store.close()

    codes = [f.code for f in curate(tmp_path / ".sidegraph").findings]
    assert "snapshot-split" in codes
    assert "domain-split" not in codes


# -- every successor branch counts (nested splits, dead branches) ------------------------------


def _node(name: str, supersedes: str | None, status: DomainStatus) -> Domain:
    return _domain(
        f"d-{name.lower()}",
        domain_id=f"01SPLITNODE{name:0>15}",
        supersedes=f"01SPLITNODE{supersedes:0>15}" if supersedes else None,
        status=status,
    )


def _graph_store(store_dir: Path, nodes: list[tuple[str, str | None, DomainStatus]]) -> None:
    """A store of hand-written domain files: ``(name, supersedes name, status)`` each."""
    Store(store_dir).close()
    for name, sup, status in nodes:
        _write_domain_file(store_dir, _node(name, sup, status))


def _id(name: str) -> str:
    return f"01SPLITNODE{name:0>15}"


_S, _A, _D = DomainStatus.SUPERSEDED, DomainStatus.ACCEPTED, DomainStatus.DROPPED
# O is replaced by L (itself replaced by A -> dropped D, and by B -> accepted C) and by R.
_DEAD_BEFORE_LIVE = [
    ("O", None, _S),
    ("L", "O", _S),
    ("A", "L", _S),
    ("D", "A", _D),
    ("B", "L", _S),
    ("C", "B", _A),
    ("R", "O", _A),
]


def test_a_dead_branch_before_a_live_one_does_not_hide_the_split(tmp_path) -> None:
    store_dir = tmp_path / "s"
    _graph_store(store_dir, _DEAD_BEFORE_LIVE)
    assert domain_splits(_models(store_dir)) == {_id("O"): sorted([_id("C"), _id("R")])}
    assert len(_split_findings(store_dir)) == 1


def test_a_nested_fork_is_a_split_at_each_level(tmp_path) -> None:
    store_dir = tmp_path / "s"
    _graph_store(
        store_dir,
        [("O", None, _S), ("L", "O", _S), ("A", "L", _A), ("B", "L", _A), ("R", "O", _A)],
    )
    assert domain_splits(_models(store_dir)) == {
        _id("L"): [_id("A"), _id("B")],
        _id("O"): [_id("A"), _id("B"), _id("R")],
    }


def test_every_live_head_is_listed_when_there_are_three(tmp_path) -> None:
    store_dir = tmp_path / "s"
    _graph_store(
        store_dir,
        [
            ("O", None, _S),
            ("L", "O", _S),
            ("A", "L", _S),
            ("A1", "A", _A),
            ("B", "L", _S),
            ("C", "B", _A),
            ("R", "O", _A),
        ],
    )
    splits = domain_splits(_models(store_dir))
    assert splits[_id("O")] == sorted([_id("A1"), _id("C"), _id("R")])
    assert splits[_id("L")] == sorted([_id("A1"), _id("C")])


def test_ratify_names_every_live_part_of_a_nested_split(tmp_path) -> None:
    store_dir = tmp_path / "s"
    _graph_store(store_dir, [n for n in _DEAD_BEFORE_LIVE if n[0] != "R"])
    _write_domain_file(store_dir, _node("R", "O", DomainStatus.PROPOSED))
    with Store(store_dir) as s:
        answer = _ratify_impl(s, accept=[_id("R")])[_id("R")]
    assert "split: d-o is now replaced by 2 live domains" in answer
    assert "d-c" in answer and "d-r" in answer


# -- doctor survives a split whose predecessor cannot be read -----------------------------------


def _doctor_json(store_dir: Path, capsys) -> tuple[int, dict]:
    code = doctor_main(["--db", str(store_dir), "--json"])
    return code, json.loads(capsys.readouterr().out)


def test_doctor_keeps_its_strict_findings_when_the_predecessor_is_missing(tmp_path, capsys) -> None:
    store_dir = tmp_path / "s"
    old_id, _l, _r = _split_store(store_dir)
    (store_dir / "domains" / f"{old_id}.json").unlink()

    code, report = _doctor_json(store_dir, capsys)

    assert code == 2
    assert [v["code"] for v in report["violations"]].count("dangling-supersedes") == 2
    assert all(f["code"] != "domain-split" for f in report["findings"])


def test_doctor_keeps_its_strict_findings_when_the_predecessor_is_malformed(
    tmp_path, capsys
) -> None:
    store_dir = tmp_path / "s"
    old_id, _l, _r = _split_store(store_dir)
    path = store_dir / "domains" / f"{old_id}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["status"] = "no-such-status"
    path.write_text(json.dumps(data), encoding="utf-8")

    code, report = _doctor_json(store_dir, capsys)

    assert code == 2
    assert "parse-error" in [v["code"] for v in report["violations"]]
    assert all(f["code"] != "domain-split" for f in report["findings"])


# -- a covered split keeps the snapshot advice without a usable graph ---------------------------


def _covered_split_second_part(tmp_path):
    from tests.snapshot_support import accept
    from tests.test_domain_snapshot_notices import _successor
    from tests.test_sync_snapshot_membership import _snapshotted

    store, domains, _l, mention = _snapshotted(tmp_path)
    accept(store, "billing-a", supersedes=domains["payments"])
    b = _successor(store, domains["payments"], "billing-b")
    return store, b, mention


def test_a_covered_split_keeps_the_snapshot_advice_without_a_reader(tmp_path) -> None:
    store, b, _mention = _covered_split_second_part(tmp_path)
    answer = _ratify_impl(store, accept=[b.domain_id], reader=None)[b.domain_id]
    assert "split from snapshot domain payments" in answer
    assert "--domain billing-a --domain billing-b" in answer


def test_a_covered_split_keeps_the_snapshot_advice_when_the_refresh_fails(
    tmp_path, monkeypatch
) -> None:
    import sidegraph.sync as sync_module

    def boom(*_a, **_k):
        raise RuntimeError("graph unreadable")

    store, b, mention = _covered_split_second_part(tmp_path)
    monkeypatch.setattr(sync_module, "refresh_domain_communities_now", boom)
    answer = _ratify_impl(store, accept=[b.domain_id], reader=mention)[b.domain_id]
    assert "split from snapshot domain payments" in answer
    assert "--domain billing-a --domain billing-b" in answer
