"""Volatile-state healing after a canonical reload (design/superpowers/specs/
2026-07-25-domain-membership-and-guard-feedback-design.md).

A reload resets domain communities, entity last_seen_* and binding statuses to cold
defaults but preserves the meta table, so `graph_version` alone cannot see that the
index went cold. These tests pin the flag that can."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from sidegraph import sync as sync_mod
from sidegraph.engine.reader import GraphifyReader
from sidegraph.schema import (
    AnchorBinding,
    Decision,
    DecisionKind,
    Descriptor,
    Domain,
    DomainStatus,
    Entity,
    Provenance,
)
from sidegraph.store import VOLATILE_STALE_KEY, Store
from sidegraph.sync import sync

GRAPH = {
    "built_at_commit": "v1",
    "nodes": [
        {
            "id": "n1",
            "label": "foo()",
            "source_file": "a.py",
            "source_location": "L1",
            "file_type": "code",
            "community": "1",
        },
        {
            "id": "n2",
            "label": "bar()",
            "source_file": "b.py",
            "source_location": "L1",
            "file_type": "code",
            "community": "2",
        },
    ],
    "links": [],
}


def _store_and_reader(tmp_path, *, domains=("d1",)):
    """A store with one accepted, seed-anchored domain per slug, plus a matching reader."""
    graph = tmp_path / "graph.json"
    graph.write_text(json.dumps(GRAPH), encoding="utf-8")
    store = Store(tmp_path / "store")
    for slug in domains:
        store.add_domain(
            Domain(
                slug=slug,
                title=slug,
                summary=f"{slug} summary",
                status=DomainStatus.ACCEPTED,
                seed_anchors=[{"name": "foo()", "file_path": "a.py"}],
                provenance=Provenance(source="manual"),
            )
        )
    return store, GraphifyReader(graph)


def test_a_failing_toc_build_does_not_leave_the_heal_flag_set(tmp_path, monkeypatch):
    """The flag must be cleared where volatile state is actually rebuilt, not after
    build_toc. Otherwise a persistent build_toc failure leaves 'version stamped but flag
    set' -- and once the gate reads the flag, every retrieval call re-runs the full ladder
    forever, for a failure in a phase that has nothing to do with volatile state."""
    store, reader = _store_and_reader(tmp_path)

    def boom(*_a, **_k):
        raise RuntimeError("toc build failed")

    monkeypatch.setattr(sync_mod, "build_toc", boom)
    with pytest.raises(RuntimeError):
        sync(store, reader)
    assert store.get_meta(VOLATILE_STALE_KEY) == "0"


def test_one_broken_domain_does_not_stop_the_others_healing(tmp_path, monkeypatch):
    """A single malformed seed descriptor must not cost fourteen healthy domains their
    heal. Without per-domain isolation the raise propagates out of _refresh_domains and
    aborts the entire pass."""
    store, reader = _store_and_reader(tmp_path, domains=("good", "bad"))
    real_resolve = reader.resolve

    def selective_boom(desc):
        if desc.file_path == "poison.py":
            raise RuntimeError("malformed descriptor")
        return real_resolve(desc)

    monkeypatch.setattr(reader, "resolve", selective_boom)
    bad = store.find_domain_by_slug("bad")
    store.supersede_domain(
        bad.domain_id,
        Domain(
            slug="bad",
            title="bad",
            summary="bad summary",
            status=DomainStatus.ACCEPTED,
            # REQUIRED: Store.supersede_domain raises ValueError unless the successor's
            # `supersedes` equals old_id (store.py:1665). Omitting it makes this test die
            # before it ever reaches sync -- a red test that indicts innocent code.
            supersedes=bad.domain_id,
            seed_anchors=[{"name": "nope()", "file_path": "poison.py"}],
            provenance=Provenance(source="manual"),
        ),
    )

    report = sync(store, reader, force=True)

    assert [f["slug"] for f in report.domain_failures] == ["bad"]
    assert "malformed descriptor" in report.domain_failures[0]["error"]
    assert store.find_domain_by_slug("good").communities == ["1"]


def test_a_domain_failure_is_reported_not_swallowed(tmp_path, monkeypatch):
    """Both lazy-sync callers suppress exceptions, so a failure that is not in the report
    is invisible: the store silently never heals and the only symptom is latency."""
    store, reader = _store_and_reader(tmp_path)
    monkeypatch.setattr(
        reader, "resolve", lambda _d: (_ for _ in ()).throw(RuntimeError("graph unreadable"))
    )
    report = sync(store, reader, force=True)
    assert report.domain_failures
    assert sync_mod.report_as_dict(report)["domain_failures"] == report.domain_failures


def test_a_broken_write_does_not_stop_the_others_healing(tmp_path, monkeypatch):
    """The try in _refresh_domains must enclose the WRITE as well as the recompute.
    store.refresh_domain_communities can raise too -- ValueError on a domain that
    vanished mid-pass (concurrent supersede; see store.py:1825) -- and if only the
    recompute were wrapped, that raise would escape and abort the pass exactly like an
    unwrapped reader.resolve raise would. Both tests above only exercise a resolve-side
    (recompute-phase) raise; this one pins the write-phase half of the same guarantee,
    including that a second, healthy domain in the same pass still refreshes."""
    store, reader = _store_and_reader(tmp_path, domains=("good", "bad"))
    bad = store.find_domain_by_slug("bad")
    real_refresh = store.refresh_domain_communities

    def selective_boom(domain_id, communities):
        if domain_id == bad.domain_id:
            raise ValueError("domain vanished mid-pass")
        return real_refresh(domain_id, communities)

    monkeypatch.setattr(store, "refresh_domain_communities", selective_boom)

    report = sync(store, reader, force=True)

    assert [f["slug"] for f in report.domain_failures] == ["bad"]
    assert "domain vanished mid-pass" in report.domain_failures[0]["error"]
    assert store.find_domain_by_slug("good").communities == ["1"]


def _bust_the_digest(store_dir):
    """What a git pull/merge/branch-switch does: canonical bytes look new to the store, so
    the next open reloads the index. The digest hashes (relpath, size, mtime_ns), so a bare
    touch is enough -- no content change required."""
    import os
    import time

    target = next((store_dir / "domains").glob("*.json"))
    stamp = time.time() + 10
    os.utime(target, (stamp, stamp))


def test_a_reload_heals_on_the_next_ORDINARY_sync(tmp_path):
    """The whole point. After a reload the graph has not changed, so the version clause
    holds and the pass used to be skipped -- leaving every domain at zero members while the
    TOC still rendered healthy."""
    store, reader = _store_and_reader(tmp_path)
    sync(store, reader, force=True)
    assert store.find_domain_by_slug("d1").communities == ["1"]

    store_dir = store.path
    store.close()
    _bust_the_digest(store_dir)
    reopened = Store(store_dir)
    assert reopened.get_meta(VOLATILE_STALE_KEY) == "1"
    assert reopened.find_domain_by_slug("d1").communities == []

    report = sync(reopened, reader)  # NO force

    assert report.skipped is False
    assert reopened.find_domain_by_slug("d1").communities == ["1"]
    assert reopened.get_meta(VOLATILE_STALE_KEY) == "0"


def test_the_pass_after_a_heal_is_gated_again(tmp_path):
    """It heals once per reload, not on every call: the completed pass clears the flag."""
    store, reader = _store_and_reader(tmp_path)
    sync(store, reader, force=True)
    store_dir = store.path
    store.close()
    _bust_the_digest(store_dir)
    reopened = Store(store_dir)
    assert sync(reopened, reader).skipped is False
    assert sync(reopened, reader).skipped is True


def test_a_reload_heals_the_other_two_slices_too(tmp_path):
    """Spec §5 test 3 -- never written across the branch's original 532 added test lines
    (final-review.md Important-3): assert the other two cold slices heal in the same pass
    as domain membership -- Entity.last_seen_* repopulated for a still-live entity, and a
    binding that should read 'orphaned' no longer reads 'live' for one whose code is
    actually gone. This is the slice the spec (and the whole-branch review) calls the
    worst: it fails TOWARD trust -- a stale 'live' binding after a reload looks exactly
    like nothing broke, so a regression here would produce no visible symptom at all."""
    store, reader = _store_and_reader(tmp_path)

    kept = store.upsert_entity(
        Entity(canonical_name="foo()", descriptor=Descriptor(name="foo()", file_path="a.py"))
    )
    gone = store.upsert_entity(
        Entity(canonical_name="bar()", descriptor=Descriptor(name="bar()", file_path="b.py"))
    )
    d_kept = store.add_decision(
        Decision(
            title="about foo",
            kind=DecisionKind.LESSON,
            context="c",
            choice="ch",
            valid_from=datetime.now(UTC),
            provenance=Provenance(source="manual"),
        )
    )
    d_gone = store.add_decision(
        Decision(
            title="about bar",
            kind=DecisionKind.LESSON,
            context="c",
            choice="ch",
            valid_from=datetime.now(UTC),
            provenance=Provenance(source="manual"),
        )
    )
    store.add_binding(
        AnchorBinding(record_id=d_kept.id, entity_id=kept.entity_id, tier=2, status="live")
    )
    store.add_binding(
        AnchorBinding(record_id=d_gone.id, entity_id=gone.entity_id, tier=2, status="live")
    )

    # graph_v2: bar()'s node is gone entirely -- orphans `gone`; foo() (`kept`) still
    # resolves, at the SAME node id, so a heal must repopulate it exactly.
    graph_v2 = tmp_path / "graph_v2.json"
    graph_v2.write_text(
        json.dumps(
            {
                "built_at_commit": "v2",
                "nodes": [
                    {
                        "id": "n1",
                        "label": "foo()",
                        "source_file": "a.py",
                        "source_location": "L1",
                        "file_type": "code",
                        "community": "1",
                    }
                ],
                "links": [],
            }
        )
    )
    reader_v2 = GraphifyReader(graph_v2)

    sync(store, reader_v2, force=True)
    assert store.get_entity(kept.entity_id).last_seen_node_id == "n1"
    gone_leaf = [b for b in store.bindings_for_record(d_gone.id) if b.tier == 2][0]
    assert gone_leaf.status == "orphaned"

    store_dir = store.path
    store.close()
    _bust_the_digest(store_dir)
    reopened = Store(store_dir)

    # Cold defaults confirm the reload actually happened (design §3 / store.py's
    # _reload_index_from_canonical): last_seen_* -> None, binding status -> "live".
    assert reopened.get_entity(kept.entity_id).last_seen_node_id is None
    cold_gone_leaf = [b for b in reopened.bindings_for_record(d_gone.id) if b.tier == 2][0]
    assert cold_gone_leaf.status == "live"
    assert reopened.get_meta(VOLATILE_STALE_KEY) == "1"

    report = sync(reopened, reader_v2)  # NO force -- the one-shot heal
    assert report.skipped is False

    # Slice: Entity.last_seen_* repopulated for the still-live entity.
    assert reopened.get_entity(kept.entity_id).last_seen_node_id == "n1"

    # Slice: the binding that should be orphaned no longer reads live -- this is the one
    # that "fails toward trust" if it regresses.
    healed_gone_leaf = [b for b in reopened.bindings_for_record(d_gone.id) if b.tier == 2][0]
    assert healed_gone_leaf.status == "orphaned"
    assert [d["id"] for d in report.stale_decisions] == [d_gone.id]
