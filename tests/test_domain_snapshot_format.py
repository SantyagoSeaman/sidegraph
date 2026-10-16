"""The domain-snapshot file: format, matching rules, lineage, round trip (D5.1, D5.2).

see design/superpowers/specs/2026-10-14-graphify-upgrade-design.md (D5)"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from sidegraph import domain_snapshot as ds
from sidegraph.engine.reader import NodeRef
from sidegraph.schema import Domain, DomainStatus, Provenance
from sidegraph.store_layout import snapshot_signature


def _n(nid, name, path, community, file_type="code"):
    return NodeRef(
        node_id=nid,
        name=name,
        norm_name=name.lower(),
        file_type=file_type,
        file_path=path,
        community=community,
    )


GUIDE = [
    _n("g", "guide.md", "docs/guide.md", "3", "document"),
    _n("g_single_1", "Single repo", "docs/guide.md", "3", "document"),
    _n("g_single_2", "Single repo", "docs/guide.md", "5", "document"),
]
CODE = [
    _n("m", "model.py", "src/model.py", "1"),
    _n("m_inv", "Invoice", "src/model.py", "1"),
    _n("m_rat_12", "Invoices never change once sent.", "src/model.py", "1", "rationale"),
    _n("m_tmp", ".tmp()", "src/model.py", "2"),
]
STUB = _n("stub", "BaseModel", "", "1")
NODES = [*GUIDE, *CODE, STUB]
MEMBERS = {
    "planning": {"g", "g_single_1"},
    "billing": {"m", "m_inv", "m_tmp", "stub"},
    "audit": {"m_rat_12", "m_tmp"},
}
IDS = {"audit": "01E", "billing": "01A", "planning": "01C"}


def _snap(nodes=NODES, members=MEMBERS, ids=IDS):
    return ds.build_snapshot(
        nodes,
        members,
        ids,
        taken_at=datetime(2026, 10, 14, tzinfo=UTC),
        engine_version=None,
        engine_generation="legacy",
        graph_version="content:abc",
        store_commit=None,
    )


def test_reading_a_snapshot_back_over_its_own_graph_loses_nothing():
    """Every node, path-less ones included. Red against nothing existing (ImportError), and
    against a (file, name)-only key (a discarded design, declared): the two 'Single repo'
    headings share one key, and the rationale node could carry no entry."""
    rec = ds.recorded_sets(NODES, ds.parse_snapshot(ds.snapshot_text(_snap())))
    for n in NODES:
        assert rec.sets[n.node_id] == {s for s, ids in MEMBERS.items() if n.node_id in ids}


def test_a_path_less_node_keeps_its_recorded_set_wherever_its_community_goes():
    """Red against the vote for path-less nodes: in the guide's community 3 the stub would
    take planning. A recorded stub is not a voter."""
    moved = [n.model_copy(update={"community": "3"}) if n.node_id == "stub" else n for n in NODES]
    rec = ds.recorded_sets(moved, _snap())
    assert rec.sets["stub"] == {"billing"} and "stub" not in rec.listed


def test_a_stub_that_had_no_domain_stays_domainless():
    """Red against the vote: community 1's listed nodes are mostly billing's, so the stub would
    join it. The empty list records that it existed with no domain."""
    nodes = [*CODE, STUB]
    snap = _snap(nodes, {"billing": {"m", "m_inv"}}, {"billing": "01A"})
    assert [(e.name, e.file_type, e.domains) for e in snap.stubs] == [("basemodel", "code", [])]
    assert ds.recorded_sets(nodes, snap).sets["stub"] == frozenset()


def test_stubs_of_one_name_with_different_sets_are_kept_per_node():
    """Two BaseModel stubs, one in billing and one in no domain, get an entry each; a third
    stub, new since the snapshot, votes (community 1: billing holds 2 of its 3 listed nodes)."""
    nodes = [*CODE, STUB, _n("stub_2", "BaseModel", "", "2")]
    snap = _snap(nodes, {"billing": {"m", "m_inv", "stub"}}, {"billing": "01A"})
    (entry,) = snap.stubs
    assert entry.domains is None
    assert [(x.id, x.domains) for x in entry.nodes] == [("stub", ["billing"]), ("stub_2", [])]
    rec = ds.recorded_sets([*nodes, _n("stub_3", "BaseModel", "", "1")], snap).sets
    assert (rec["stub"], rec["stub_2"], rec["stub_3"]) == ({"billing"}, frozenset(), {"billing"})


def test_a_renumbered_node_is_found_by_its_unique_name_but_a_rationale_is_not():
    renamed = [n.model_copy(update={"node_id": n.node_id + "_v2"}) for n in CODE]
    rec = ds.recorded_sets(renamed, _snap())
    assert rec.sets["m_tmp_v2"] == {"billing", "audit"}
    assert rec.sets["m_rat_12_v2"] == {"billing"}  # declared: a rationale matches by id only


def test_one_entry_matched_by_id_and_another_by_its_unique_name():
    nodes = [
        _n("f", "f.py", "src/f.py", "1"),
        _n("a", "alpha", "src/f.py", "1"),
        _n("b", "beta", "src/f.py", "1"),
    ]
    snap = _snap(nodes, {"x": {"a"}, "y": {"b"}}, {"x": "01X", "y": "01Y"})
    rec = ds.recorded_sets([nodes[0], nodes[1], _n("b2", "beta", "src/f.py", "1")], snap).sets
    assert rec["a"] == {"x"} and rec["b2"] == {"y"}


def test_a_name_two_entries_share_never_matches_by_name():
    """Red against counting names among unmatched entries only: x keeps its id under a new
    label, so z would take y's set."""
    nodes = [
        _n("f", "f.py", "src/f.py", "1"),
        _n("x", "foo", "src/f.py", "1"),
        _n("y", "foo", "src/f.py", "2"),
    ]
    snap = _snap(nodes, {"a": {"x"}, "b": {"y"}}, {"a": "01X", "b": "01Y"})
    now = [nodes[0], _n("x", "fooz", "src/f.py", "1"), _n("z", "foo", "src/f.py", "2")]
    rec = ds.recorded_sets(now, snap).sets
    assert rec["x"] == {"a"} and rec["z"] == frozenset()


def test_two_current_nodes_with_one_name_and_new_ids_take_the_file_default():
    renamed = [n.model_copy(update={"node_id": n.node_id + "_v2"}) for n in GUIDE]
    assert ds.recorded_sets(renamed, _snap()).sets["g_single_2_v2"] == {"planning"}  # declared


def test_a_label_that_gained_call_decoration_still_matches():
    nodes = [_n("f", "f.py", "src/f.py", "1"), _n("p", "PredictBatch", "src/f.py", "1")]
    snap = _snap(nodes, {"a": {"f"}, "b": {"p"}}, {"a": "01X", "b": "01Y"})
    moved = [nodes[0], _n("p2", ".PredictBatch()", "src/f.py", "1")]
    assert ds.recorded_sets(moved, snap).sets["p2"] == {"b"}


def test_a_file_new_since_the_snapshot_votes_and_no_voters_means_no_domain():
    new = [_n("n1", "new.py", "src/new.py", "1"), _n("n2", "lonely.py", "src/lonely.py", "9")]
    rec = ds.recorded_sets([*CODE, *new], _snap())
    assert rec.sets["n1"] == {"billing"} and rec.sets["n2"] == frozenset()
    assert {"n1", "n2"}.isdisjoint(rec.listed)


def test_a_file_with_no_domain_is_still_listed():
    snap = _snap([_n("x", "x.py", "src/x.py", "4")], {"a": set()}, {"a": "01X"})
    assert snap.files["src/x.py"].domains == [] and snap.covered_slugs() == ["a"]


@pytest.mark.parametrize(
    "mutate, message",
    [
        (lambda d: d["files"]["src/model.py"].update(domains=["ghost"]), "'ghost' is not one of"),
        (lambda d: d.update(format=2), "format"),
        (lambda d: d.update(format=True), "format"),
        (lambda d: d.update(format=1.0), "format"),
        (
            lambda d: d["files"]["src/model.py"]["nodes"].append(
                dict(d["files"]["src/model.py"]["nodes"][0])
            ),
            "an entry id repeats within a file",
        ),
        (lambda d: d["domains"].append(dict(d["domains"][0], slug="zz")), "a domain id repeats"),
        (lambda d: d.update(extra=1), "extra"),
        (
            lambda d: d["stubs"][0].update(nodes=[{"id": "stub", "domains": []}]),
            "either domains or nodes",
        ),
        (lambda d: d["stubs"][0].update(domains=["ghost"]), "'ghost' is not one of"),
        (lambda d: d["stubs"].append(dict(d["stubs"][0])), "stubs must be sorted"),
    ],
)
def test_an_invalid_snapshot_is_refused_with_its_first_problem(mutate, message):
    data = json.loads(ds.snapshot_text(_snap()))
    mutate(data)
    with pytest.raises(ds.SnapshotError, match=message):
        ds.parse_snapshot(json.dumps(data))


def test_text_that_is_not_json_is_refused():
    with pytest.raises(ds.SnapshotError):
        ds.parse_snapshot("{not json")


def test_reading_returns_the_signature_of_the_bytes_it_parsed(tmp_path):
    """A membership pass certifies this signature: it must name the bytes parsed, also for a
    file that fails, so the failure is recorded under it."""
    assert ds.read_snapshot(tmp_path) == (None, "absent")
    ds.write_snapshot(tmp_path, _snap())
    snap, signature = ds.read_snapshot(tmp_path)
    assert snap == _snap() and signature == snapshot_signature(tmp_path)
    ds.snapshot_path(tmp_path).write_text("{}", encoding="utf-8")
    with pytest.raises(ds.SnapshotError) as e:
        ds.read_snapshot(tmp_path)
    assert e.value.signature == snapshot_signature(tmp_path) != signature


def test_write_then_load_round_trips_and_reports_the_size(tmp_path):
    size = ds.write_snapshot(tmp_path, _snap())
    assert len(ds.snapshot_path(tmp_path).read_bytes()) == size
    assert ds.load_snapshot(tmp_path) == _snap()
    assert list(tmp_path.glob("*.tmp")) == []
    assert ds.load_snapshot(tmp_path / "none") is None


def _d(did, slug, supersedes=None, status=DomainStatus.ACCEPTED):
    return Domain(
        domain_id=did,
        slug=slug,
        title=slug,
        summary="s",
        supersedes=supersedes,
        status=status,
        provenance=Provenance(source="manual"),
    )


def test_heirs_follow_lineage_and_refuse_a_split_or_a_stranger():
    """Red against slug-keyed heirs: 01G reuses the dropped slug billing with no lineage."""
    old_billing = _d("01A", "billing", status=DomainStatus.SUPERSEDED)
    old_audit = _d("01E", "audit", status=DomainStatus.SUPERSEDED)
    accepted = [
        _d("01B", "invoicing", "01A"),
        _d("01C", "planning"),
        _d("01D", "audit-a", "01E"),
        _d("01F", "audit-b", "01E"),
        _d("01G", "billing"),
    ]
    by_id = {d.domain_id: d for d in (old_billing, old_audit)}
    heirs, splits = ds.snapshot_heirs(_snap(), accepted, by_id.get)
    assert heirs == {"01B": "billing", "01C": "planning"}
    assert splits == {"audit": ["01D", "01F"]}


def test_file_counts():
    assert ds.file_counts(_snap()) == {"audit": 1, "billing": 1, "planning": 1}
