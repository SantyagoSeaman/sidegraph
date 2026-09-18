"""Regression test for the git-native store's invariant
(see docs/reference/store-format.md#the-sync-clean-invariant): a graph rebuild must never
produce a git diff. `sync()` performs every sanctioned volatile
mutation — entity engine-mapping upserts, leaf binding status flips, domain community
refreshes, and the TOC cache — and every one of them must land ONLY in the derived,
gitignored index.db, never touch a single byte (or even the mtime) of a committed
decisions/domains/entities/bindings file."""

from __future__ import annotations

import json
import subprocess

import pytest

from sidegraph.engine.reader import GraphifyReader
from sidegraph.retrieval import TOC_CACHE_KEY
from sidegraph.schema import Domain, DomainStatus, Provenance
from sidegraph.server import _propose_decisions_impl, _ratify_decisions_impl
from sidegraph.store import Store
from sidegraph.sync import sync

# foo() stays put (same file, same community) but its node id renumbers -- the ordinary,
# routine "rebuild reshuffled ids" case. bar()'s node vanishes outright (renamed away with
# no successor anywhere in c.py) -- drives a genuine live -> orphaned leaf status flip.
GRAPH_A = {
    "built_at_commit": "vA",
    "nodes": [
        {
            "id": "n1",
            "label": "foo()",
            "norm_label": "foo()",
            "file_type": "code",
            "source_file": "a.py",
            "community": 1,
        },
        {
            "id": "n2",
            "label": "bar()",
            "norm_label": "bar()",
            "file_type": "code",
            "source_file": "c.py",
            "community": 2,
        },
    ],
    "links": [],
}
GRAPH_B = {
    "built_at_commit": "vB",
    "nodes": [
        # same descriptor (foo @ a.py), same community, new node id -> "rebound"
        {
            "id": "n1b",
            "label": "foo()",
            "norm_label": "foo()",
            "file_type": "code",
            "source_file": "a.py",
            "community": 1,
        },
        # bar() gone, nothing left in c.py at all -> orphaned, community unresolvable
    ],
    "links": [],
}


def _write_graph(tmp_path, name, data):
    p = tmp_path / name
    p.write_text(json.dumps(data))
    return p


# The historical blind spot (see design/superpowers/specs/
# 2026-07-10-derived-community-bindings-design.md's root-cause section): GRAPH_A/GRAPH_B
# above never renumber a community -- both nodes that survive (foo, at n1/n1b) keep
# community 1 throughout, so `sync._repoint_communities` never actually fires under
# `test_sync_with_renumbered_graph_never_touches_canonical_files`. That's exactly how the
# live sync-clean violation (8 bindings files + 4 community entities rewritten on a real
# corpus) shipped despite this file's "never touches canonical" test passing. GRAPH_COMMUNITY_A
# -> GRAPH_COMMUNITY_B below differ ONLY in the resolved node's community ("1" -> "72"); the
# node id is unchanged, so `foo` resolves EXACTLY (never rebinds/orphans) and the repoint path
# is the ONLY thing exercised.
GRAPH_COMMUNITY_A = {
    "built_at_commit": "vCommA",
    "nodes": [
        {
            "id": "n1",
            "label": "foo()",
            "norm_label": "foo()",
            "file_type": "code",
            "source_file": "a.py",
            "community": 1,
        },
    ],
    "links": [],
}
GRAPH_COMMUNITY_B = {
    "built_at_commit": "vCommB",
    "nodes": [
        # SAME node id, SAME file/label -- Leiden renumbered ONLY the community this build.
        {
            "id": "n1",
            "label": "foo()",
            "norm_label": "foo()",
            "file_type": "code",
            "source_file": "a.py",
            "community": 72,
        },
    ],
    "links": [],
}


@pytest.fixture(autouse=True)
def _no_ambient_initiative(monkeypatch):
    monkeypatch.setattr("sidegraph.capture._derive_initiative", lambda: None)


def _snapshot_canonical_dir(store: Store) -> dict[str, tuple[bytes, int, int]]:
    """relpath -> (content, size, mtime_ns) for every canonical (git-committed) file --
    proves not just that content is byte-identical but that the file was never even
    rewritten (mtime unchanged), which is what actually keeps `git status` silent."""
    out: dict[str, tuple[bytes, int, int]] = {}
    for sub in ("decisions", "domains", "entities", "bindings", "initiatives"):
        d = store.path / sub
        if not d.is_dir():
            continue
        for f in d.iterdir():
            if f.suffix != ".json":
                continue
            st = f.stat()
            out[f"{sub}/{f.name}"] = (f.read_bytes(), st.st_size, st.st_mtime_ns)
    return out


def _store_and_reader_for_clean(tmp_path):
    """Store with at least one anchored, ratified decision, plus a ratified domain, plus a
    GraphifyReader over GRAPH_A -- shared setup for the sync-clean tests below. The domain
    is required so a `domains/*.json` file exists for `_bust_the_digest` to touch."""
    reader_a = GraphifyReader(_write_graph(tmp_path, "a.json", GRAPH_A))
    store = Store(tmp_path / "s")
    results = _propose_decisions_impl(
        store,
        reader_a,
        [
            {
                "title": "foo matters",
                "kind": "gotcha",
                "context": "c",
                "choice": "keep",
                "anchors": [{"name": "foo", "file_path": "a.py"}],
            },
        ],
        session_id="s-clean-2",
    )
    _ratify_decisions_impl(store, accept=[r["decision_id"] for r in results])

    domain = store.add_domain(
        Domain(
            slug="core",
            title="Core",
            summary="s.",
            communities=["1"],
            provenance=Provenance(source="manual"),
        )
    )
    store.ratify_domains(accept=[domain.domain_id])

    return store, reader_a


def test_sync_with_renumbered_graph_never_touches_canonical_files(tmp_path):
    reader_a = GraphifyReader(_write_graph(tmp_path, "a.json", GRAPH_A))
    store = Store(tmp_path / "s")

    results = _propose_decisions_impl(
        store,
        reader_a,
        [
            {
                "title": "foo matters",
                "kind": "gotcha",
                "context": "c",
                "choice": "keep",
                "anchors": [{"name": "foo", "file_path": "a.py"}],
            },
            {
                "title": "bar matters",
                "kind": "gotcha",
                "context": "c",
                "choice": "keep",
                "anchors": [{"name": "bar", "file_path": "c.py"}],
            },
        ],
        session_id="s-clean",
    )
    assert [r["status"] for r in results] == ["written", "written"]
    _ratify_decisions_impl(store, accept=[r["decision_id"] for r in results])

    # An accepted domain with a deliberately STALE `communities` mapping + a path_prefixes
    # stabilizer rule that DOES match the live graph -- guarantees refresh_domain_communities
    # actually has something to change (a no-op refresh wouldn't exercise the write path).
    domain = store.add_domain(
        Domain(
            slug="core",
            title="Core",
            summary="s.",
            communities=["99"],
            path_prefixes=["a.py"],
            provenance=Provenance(source="manual"),
        )
    )
    store.ratify_domains(accept=[domain.domain_id])

    before = _snapshot_canonical_dir(store)
    assert before  # sanity: there IS something to protect

    reader_b = GraphifyReader(_write_graph(tmp_path, "b.json", GRAPH_B))
    report = sync(store, reader_b)

    # -- the mutations actually happened (not a vacuous pass) --------------------------
    by_name = {o.canonical_name: o for o in report.outcomes}
    assert by_name["foo"].status == "rebound"
    assert by_name["bar"].status == "orphaned"
    assert store.get_domain(domain.domain_id).communities == ["1"]
    assert store.get_meta(TOC_CACHE_KEY) is not None
    assert store.get_domain(domain.domain_id).status == DomainStatus.ACCEPTED  # untouched

    bar_entity = store.find_entities_by_name("bar")[0]
    bar_leaf = [b for b in store.bindings_for_entity(bar_entity.entity_id) if b.tier == 2][0]
    assert bar_leaf.status == "orphaned"

    foo_entity = store.find_entities_by_name("foo")[0]
    assert foo_entity.last_seen_node_id == "n1b"  # entity mapping upsert landed

    # -- and NOT ONE byte (or mtime) of a committed file moved --------------------------
    after = _snapshot_canonical_dir(store)
    assert after == before


def test_second_identical_sync_is_a_pure_index_noop_too(tmp_path):
    """Belt and braces: a second, idempotent sync pass over the SAME (already-synced)
    graph must also leave the canonical dir untouched."""
    store, reader_a = _store_and_reader_for_clean(tmp_path)

    sync(store, reader_a)
    before = _snapshot_canonical_dir(store)

    report = sync(store, reader_a, force=True)
    assert report.skipped is False  # force=True actually re-ran the pass

    after = _snapshot_canonical_dir(store)
    assert after == before


def test_sync_renumbers_community_without_touching_canonical(tmp_path):
    """Closes the blind spot documented above GRAPH_COMMUNITY_A/GRAPH_COMMUNITY_B: the
    SAME entity, at the SAME node id, resolves to a genuinely renumbered community --
    the one case that actually drives `_repoint_communities` -- and the full canonical
    snapshot (content AND mtime) must still be identical across `sync()`, while the index
    shows the re-pointed Tier-1 binding."""
    reader_a = GraphifyReader(_write_graph(tmp_path, "ca.json", GRAPH_COMMUNITY_A))
    store = Store(tmp_path / "s2")

    results = _propose_decisions_impl(
        store,
        reader_a,
        [
            {
                "title": "foo matters",
                "kind": "gotcha",
                "context": "c",
                "choice": "keep",
                "anchors": [{"name": "foo", "file_path": "a.py"}],
            },
        ],
        session_id="s-renumber",
    )
    assert results[0]["status"] == "written"
    decision_id = results[0]["decision_id"]
    _ratify_decisions_impl(store, accept=[decision_id])

    # captured at proposal time (index-only per Task 1): a live Tier-1 community:1 binding.
    tier1_before = [b for b in store.bindings_for_record(decision_id) if b.tier == 1]
    assert len(tier1_before) == 1
    assert store.get_entity(tier1_before[0].entity_id).canonical_name == "community:1"
    assert tier1_before[0].status == "live"

    before = _snapshot_canonical_dir(store)
    assert before  # sanity: there IS something to protect

    reader_b = GraphifyReader(_write_graph(tmp_path, "cb.json", GRAPH_COMMUNITY_B))
    report = sync(store, reader_b)

    # -- the repoint actually happened (not a vacuous pass) ------------------------------
    by_name = {o.canonical_name: o for o in report.outcomes}
    assert by_name["foo"].status == "unchanged"  # same node id -> never rebinds/orphans
    assert by_name["foo"].repointed == 1

    bindings = store.bindings_for_record(decision_id)
    tier1 = [b for b in bindings if b.tier == 1]
    live_tier1 = [b for b in tier1 if b.status == "live"]
    assert len(live_tier1) == 1
    assert store.get_entity(live_tier1[0].entity_id).canonical_name == "community:72"

    orphaned_tier1 = [b for b in tier1 if b.status == "orphaned"]
    assert len(orphaned_tier1) == 1
    assert store.get_entity(orphaned_tier1[0].entity_id).canonical_name == "community:1"

    # -- and NOT ONE byte (or mtime) of a committed file moved --------------------------
    after = _snapshot_canonical_dir(store)
    assert after == before


def _bust_the_digest(store_dir):
    """What a git pull/branch switch does. The canonical digest hashes
    (relpath, size, mtime_ns), so a bare touch is enough — no content change needed."""
    import os
    import time

    target = next((store_dir / "domains").glob("*.json"))
    stamp = time.time() + 10
    os.utime(target, (stamp, stamp))


def test_a_healing_pass_after_a_reload_is_sync_clean(tmp_path):
    """The reason communities/last_seen_*/binding statuses are derived at all: rebuilding
    them must never produce a git diff. Extends the invariant to the new healing path."""
    store, reader = _store_and_reader_for_clean(tmp_path)
    sync(store, reader, force=True)
    store_dir = store.path
    del store

    # Bust FIRST, snapshot SECOND. The bust is itself an mtime write on a canonical file,
    # and _snapshot_canonical_dir records mtime_ns — snapshotting before it would compare
    # the touched file against its own pre-touch mtime and fail against a PERFECT
    # implementation, indicting the healing pass for the test's own mutation. Reopening
    # touches nothing canonical (the format marker and .gitignore are written only when
    # missing), so the post-bust snapshot is the honest baseline.
    _bust_the_digest(store_dir)
    reopened = Store(store_dir)
    before = _snapshot_canonical_dir(reopened)

    assert sync(reopened, reader).skipped is False
    assert _snapshot_canonical_dir(reopened) == before


def test_a_moved_entity_DOES_rewrite_its_canonical_file(tmp_path):
    """The converse, pinned deliberately (spec D4): identity is canonical state, not
    derived. A 'moved' adoption rewrites entity.descriptor and that diff is CORRECT --
    someone reading only the test above would otherwise 'fix' it."""
    # The moved rung fails closed without a resolvable repo_root AND committed evidence
    # (dirty-tree guard, sync.py's _committed_evidence_confirms_move) -- git-init tmp_path
    # so it can confirm a.py is genuinely gone once the graph moves it, same as the live
    # checkout the fix targets.
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    store, reader = _store_and_reader_for_clean(tmp_path)
    sync(store, reader, force=True)
    before = _snapshot_canonical_dir(store)

    # Same symbol, same community, different file — the "moved" rung of rebind_entity.
    # Commit the new file's real presence at HEAD so the dirty-tree guard's committed-
    # evidence check confirms the move (a.py was never committed, so its absence is
    # already confirmed).
    moved_file = tmp_path / "moved" / "c.py"
    moved_file.parent.mkdir(parents=True, exist_ok=True)
    moved_file.write_text("def foo(): pass\n")
    subprocess.run(["git", "add", "moved/c.py"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "move to moved/c.py"], cwd=tmp_path, check=True)

    moved = dict(GRAPH_A)
    moved["nodes"] = [{**n, "source_file": "moved/c.py"} for n in GRAPH_A["nodes"]]
    moved_graph = tmp_path / "graph_moved.json"
    moved_graph.write_text(json.dumps(moved), encoding="utf-8")

    sync(store, GraphifyReader(moved_graph), force=True)

    after = _snapshot_canonical_dir(store)
    changed = {path for path in after if after[path] != before.get(path)}
    assert any(path.startswith("entities/") for path in changed), changed
    assert not any(path.startswith(("decisions/", "domains/", "bindings/")) for path in changed), (
        changed
    )
