import json
import subprocess

import pytest

import sidegraph.sync as sync_module
from sidegraph.engine.reader import GraphifyReader
from sidegraph.gitenv import git_env
from sidegraph.schema import AnchorBinding, Descriptor, Entity
from sidegraph.store import Store
from sidegraph.sync import rebind_entity, report_as_dict, report_has_findings, sync

GRAPH_B = {
    "built_at_commit": "vB",
    "nodes": [
        # stable: same id, same file  -> unchanged
        {
            "id": "s1",
            "label": "f_stable()",
            "norm_label": "f_stable()",
            "file_type": "code",
            "source_file": "a.py",
            "community": 1,
        },
        # old_fn renamed away         -> orphaned
        {
            "id": "r2",
            "label": "new_fn()",
            "norm_label": "new_fn()",
            "file_type": "code",
            "source_file": "b.py",
            "community": 1,
        },
        # mover: moved c.py -> d.py, new id -> moved
        {
            "id": "m2",
            "label": "mover_fn()",
            "norm_label": "mover_fn()",
            "file_type": "code",
            "source_file": "d.py",
            "community": 2,
        },
        # dup_fn now exists twice     -> ambiguous
        {
            "id": "d2",
            "label": "dup_fn()",
            "norm_label": "dup_fn()",
            "file_type": "code",
            "source_file": "e.py",
            "community": 2,
        },
        {
            "id": "d3",
            "label": "dup_fn()",
            "norm_label": "dup_fn()",
            "file_type": "code",
            "source_file": "f.py",
            "community": 3,
        },
    ],
    "links": [],
}

# Doc-corpus support: loose rung must not cross-adopt a code symbol into a doc heading
# (or vice versa) even on a unique name-only hit.
GRAPH_DOC = {
    "built_at_commit": "vDoc",
    "nodes": [
        # unique doc heading colliding (by canonical name) with a vanished code symbol
        # -> the loose rung must NOT adopt this cross-type hit.
        {
            "id": "doc1",
            "label": "gate",
            "norm_label": "gate",
            "file_type": "document",
            "source_file": "ADR-001.md",
            "community": 5,
        },
        # a doc heading that itself moved file -> unique name-only hit, same suffix -> moved.
        {
            "id": "doc2",
            "label": "context",
            "norm_label": "context",
            "file_type": "document",
            "source_file": "ADR-002.md",
            "community": 6,
        },
    ],
    "links": [],
}


def _write_graph(tmp_path, name, data):
    import json

    p = tmp_path / name
    p.write_text(json.dumps(data))
    return p


def _entity(store, name, file, node_id, version="vA"):
    e = Entity(
        canonical_name=name,
        descriptor=Descriptor(name=name, file_path=file),
        last_seen_node_id=node_id,
        last_seen_graph_version=version,
    )
    return store.upsert_entity(e)


def _leaf(store, entity_id, status="live"):
    from datetime import UTC, datetime

    from sidegraph.schema import Decision, DecisionKind, Provenance

    d = store.add_decision(
        Decision(
            title=f"about {entity_id}",
            kind=DecisionKind.LESSON,
            context="c",
            choice="ch",
            valid_from=datetime.now(UTC),
            provenance=Provenance(source="manual"),
        )
    )
    store.add_binding(AnchorBinding(record_id=d.id, entity_id=entity_id, tier=2, status=status))
    return d


def test_unchanged_refreshes_version(tmp_path):
    reader = GraphifyReader(_write_graph(tmp_path, "b.json", GRAPH_B))
    s = Store(tmp_path / "t.db")
    e = _entity(s, "f_stable", "a.py", "s1")
    out = rebind_entity(e, s, reader)
    assert out.status == "unchanged" and out.node_id == "s1"
    assert s.get_entity(e.entity_id).last_seen_graph_version == reader.graph_version()
    assert s.get_entity(e.entity_id).last_seen_graph_version.startswith("vB:")


def test_rebound_heals_orphaned_leaf(tmp_path):
    reader = GraphifyReader(_write_graph(tmp_path, "b.json", GRAPH_B))
    s = Store(tmp_path / "t.db")
    e = _entity(s, "mover_fn", "d.py", "m1")  # file already right, id stale
    d = _leaf(s, e.entity_id, status="orphaned")
    out = rebind_entity(e, s, reader)
    assert out.status == "rebound" and out.node_id == "m2"
    binds = s.bindings_for_record(d.id)
    assert binds[0].status == "live"  # healed


def test_moved_updates_descriptor_and_heals(tmp_path):
    """The moved rung requires repo_root, the old path's confirmed absence there, AND that
    absence/presence confirmed by COMMITTED git history (dirty-tree guard fixed
    2026-09-18 -- see test_moved_rung_fails_closed_on_uncommitted_delete for the negative
    case this rung now also has to reject): c.py never existed, d.py is committed, so
    HEAD itself proves the move genuinely happened, the same signal the live fix is built
    on."""
    _init_repo(tmp_path)
    (tmp_path / "d.py").write_text("def mover_fn(): pass\n")
    _git(["add", "-A"], tmp_path)
    _git(["commit", "-q", "-m", "d.py lives here now"], tmp_path)
    reader = GraphifyReader(_write_graph(tmp_path, "b.json", GRAPH_B))
    s = Store(tmp_path / "t.db")
    e = _entity(s, "mover_fn", "c.py", "m1")  # old file -> exact miss, name-only hit
    d = _leaf(s, e.entity_id, status="live")
    out = rebind_entity(e, s, reader, repo_root=tmp_path)  # c.py confirmed absent
    assert out.status == "moved" and out.node_id == "m2" and out.detail == "c.py -> d.py"
    kept = s.get_entity(e.entity_id)
    assert kept.descriptor.file_path == "d.py"  # descriptor follows the move
    assert s.bindings_for_record(d.id)[0].status == "live"


def test_ambiguous_degrades_and_keeps_mapping(tmp_path):
    reader = GraphifyReader(_write_graph(tmp_path, "b.json", GRAPH_B))
    s = Store(tmp_path / "t.db")
    e = _entity(s, "dup_fn", None, "d1")  # no file -> two candidates in B
    d = _leaf(s, e.entity_id, status="live")
    out = rebind_entity(e, s, reader)
    assert out.status == "ambiguous" and "2" in out.detail
    assert s.get_entity(e.entity_id).last_seen_node_id == "d1"  # mapping untouched
    assert s.bindings_for_record(d.id)[0].status == "degraded"


def test_name_only_ambiguous_degrades(tmp_path):
    reader = GraphifyReader(_write_graph(tmp_path, "b.json", GRAPH_B))
    s = Store(tmp_path / "t.db")
    e = _entity(s, "dup_fn", "g.py", "d1")  # exact miss (no dup_fn in g.py), loose -> d2+d3
    d = _leaf(s, e.entity_id, status="live")
    out = rebind_entity(e, s, reader)
    assert out.status == "ambiguous" and "2" in out.detail
    assert s.get_entity(e.entity_id).descriptor.file_path == "g.py"  # descriptor untouched
    assert s.bindings_for_record(d.id)[0].status == "degraded"


def test_orphaned_when_name_gone(tmp_path):
    reader = GraphifyReader(_write_graph(tmp_path, "b.json", GRAPH_B))
    s = Store(tmp_path / "t.db")
    e = _entity(s, "old_fn", "b.py", "r1")
    d = _leaf(s, e.entity_id, status="live")
    out = rebind_entity(e, s, reader)
    assert out.status == "orphaned"
    assert s.bindings_for_record(d.id)[0].status == "orphaned"


def test_loose_rung_rejects_cross_type_collision(tmp_path):
    """A vanished code symbol whose canonical name uniquely collides with a doc heading
    must NOT be adopted cross-type — the loose rung requires a matching file suffix."""
    reader = GraphifyReader(_write_graph(tmp_path, "doc.json", GRAPH_DOC))
    s = Store(tmp_path / "t.db")
    e = _entity(s, "gate", "risk/gate.py", "old-code-id")
    d = _leaf(s, e.entity_id, status="live")
    out = rebind_entity(e, s, reader)
    assert out.status == "orphaned"  # NOT "moved"
    kept = s.get_entity(e.entity_id)
    assert kept.last_seen_node_id == "old-code-id"  # node mapping untouched
    assert kept.descriptor.file_path == "risk/gate.py"  # descriptor untouched
    assert s.bindings_for_record(d.id)[0].status == "orphaned"


def test_loose_rung_adopts_doc_to_doc_move(tmp_path):
    """A doc heading that moved file (same suffix) still rebinds via the loose rung, once
    repo_root confirms ADR-001.md (a bare string here, never a real file) is genuinely
    gone from disk AND ADR-002.md's committed HEAD confirms the move -- see
    test_moved_updates_descriptor_and_heals for why both are required."""
    _init_repo(tmp_path)
    (tmp_path / "ADR-002.md").write_text("# context\n")
    _git(["add", "-A"], tmp_path)
    _git(["commit", "-q", "-m", "ADR-002.md lives here now"], tmp_path)
    reader = GraphifyReader(_write_graph(tmp_path, "doc.json", GRAPH_DOC))
    s = Store(tmp_path / "t.db")
    e = _entity(s, "context", "ADR-001.md", "old-doc-id")
    d = _leaf(s, e.entity_id, status="live")
    out = rebind_entity(e, s, reader, repo_root=tmp_path)  # ADR-001.md confirmed absent
    assert out.status == "moved" and out.node_id == "doc2"
    kept = s.get_entity(e.entity_id)
    assert kept.descriptor.file_path == "ADR-002.md"  # descriptor follows the move
    assert s.bindings_for_record(d.id)[0].status == "live"


def test_moved_rung_rejects_out_of_scope_same_name_file(tmp_path):
    """The live bug (observed on this repo, twice): an exact miss whose OLD path is simply
    outside the graph's scope -- never indexed, not moved -- must not be mistaken for a
    move merely because a same-suffix name-only hit happens to be unique elsewhere in the
    graph. The graph alone cannot tell these two causes of an exact miss apart (there are
    no nodes at the old path either way); when repo_root is known, the filesystem can --
    c.py is still sitting right there, so nothing moved.
    """
    reader = GraphifyReader(_write_graph(tmp_path, "b.json", GRAPH_B))
    s = Store(tmp_path / "t.db")
    (tmp_path / "c.py").write_text("# out-of-scope file that never moved\n")
    e = _entity(s, "mover_fn", "c.py", "m1")
    d = _leaf(s, e.entity_id, status="live")
    out = rebind_entity(e, s, reader, repo_root=tmp_path)
    assert out.status == "orphaned"  # NOT "moved" -- c.py is still on disk
    kept = s.get_entity(e.entity_id)
    assert kept.descriptor.file_path == "c.py"  # descriptor unchanged
    assert kept.last_seen_node_id == "m1"  # node mapping untouched
    assert s.bindings_for_record(d.id)[0].status == "orphaned"


def test_moved_rung_fails_closed_when_repo_root_unknown(tmp_path):
    """Without repo_root the moved rung cannot verify the old path is gone -- so it fails
    CLOSED rather than falling back to the old unguarded adopt-on-unique-hit behavior, even
    for what would (if checked) turn out to be a genuine move. Conflating "can't verify"
    with "verified gone" is exactly the class of bug this rung exists to fix, one level up:
    an unadopted move degrades to orphaned (visible, heal-anchors-repairable); a wrong
    adoption is silent and permanent. This is the path a non-git corpus takes on every
    sync (graph_version's own content-hash fallback treats "no .git" as an ordinary,
    supported case -- see engine/reader.py), not just a misconfigured checkout."""
    reader = GraphifyReader(_write_graph(tmp_path, "b.json", GRAPH_B))
    s = Store(tmp_path / "t.db")
    e = _entity(s, "mover_fn", "c.py", "m1")
    d = _leaf(s, e.entity_id, status="live")
    out = rebind_entity(e, s, reader)  # no repo_root -> can't verify -> don't adopt
    assert out.status == "orphaned"  # NOT "moved"
    kept = s.get_entity(e.entity_id)
    assert kept.descriptor.file_path == "c.py"  # descriptor unchanged
    assert kept.last_seen_node_id == "m1"  # node mapping untouched
    assert s.bindings_for_record(d.id)[0].status == "orphaned"


def _git(args, cwd):
    # git_env(), not the autouse delenv: a test that calls monkeypatch.undo() restores those.
    env = {**git_env(), "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, env=env)
    assert result.returncode == 0, f"git {args} failed: {result.stderr}"


def _init_repo(root):
    _git(["init", "-q"], root)
    _git(["config", "user.email", "test@example.com"], root)
    _git(["config", "user.name", "Test"], root)


def test_moved_rung_fails_closed_on_uncommitted_delete(tmp_path):
    """Defect 1 (dirty-tree corruption): the pre-fix rung trusted the WORKING TREE alone
    -- 'old path missing from disk' + 'unique same-suffix hit' -- and one person's
    uncommitted `rm c.py` was enough to make it rewrite the canonical, repo-committed
    descriptor for the whole team. Here c.py is genuinely COMMITTED (HEAD still has it)
    but locally deleted from disk without `git rm`/a commit -- a dirty tree, not a real
    move. The rung must NOT adopt: it has no committed evidence the file is gone, only a
    local, unshared change. Leaves the entity's mapping exactly as it was and reports a
    'moved_uncommitted' outcome instead of silently guessing.
    """
    _init_repo(tmp_path)
    (tmp_path / "c.py").write_text("def mover_fn(): pass\n")
    _git(["add", "-A"], tmp_path)
    _git(["commit", "-q", "-m", "initial"], tmp_path)
    (tmp_path / "c.py").unlink()  # dirty, uncommitted delete -- never staged, never committed

    reader = GraphifyReader(_write_graph(tmp_path, "b.json", GRAPH_B))
    s = Store(tmp_path / "t.db")
    e = _entity(s, "mover_fn", "c.py", "m1")
    d = _leaf(s, e.entity_id, status="live")

    out = rebind_entity(e, s, reader, repo_root=tmp_path)

    assert out.status == "moved_uncommitted"  # NOT "moved" -- evidence isn't committed
    kept = s.get_entity(e.entity_id)
    assert kept.descriptor.file_path == "c.py"  # descriptor UNCHANGED
    assert kept.last_seen_node_id == "m1"  # node mapping UNCHANGED
    assert s.bindings_for_record(d.id)[0].status == "live"  # binding left exactly as it was


# -- a pending (uncommitted) move is remembered and re-verified once HEAD moves ------------------
#
# Committing the rename does not change graph.json, so the graph version can stay the same
# and the gate would skip forever. The pass records the entity ids and HEAD in the
# ``pending_uncommitted_moves`` meta key; a gated call re-checks them once HEAD has moved.

_PENDING_KEY = "pending_uncommitted_moves"


def _head(root):
    r = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, check=True
    )
    return r.stdout.strip()


def _pending_repo(tmp_path):
    """A repo whose c.py -> d.py rename is staged but NOT committed, and the graph built on
    that dirty tree. The graph's version is fixed by its content, so committing the rename
    leaves it unchanged."""
    _init_repo(tmp_path)
    (tmp_path / "c.py").write_text("def mover_fn(): pass\n")
    _git(["add", "c.py"], tmp_path)
    _git(["commit", "-q", "-m", "initial"], tmp_path)
    _git(["mv", "c.py", "d.py"], tmp_path)  # staged, not committed
    return GraphifyReader(_write_graph(tmp_path, "b.json", GRAPH_B))


def _pending_store(tmp_path):
    s = Store(tmp_path / "t.db")
    e = _entity(s, "mover_fn", "c.py", "m1")
    d = _leaf(s, e.entity_id, status="live")
    return s, e, d


def _commit_the_move(tmp_path):
    # plain commit, no -A: the store and graph files sit in the repo root, untracked
    _git(["commit", "-q", "-m", "rename c.py to d.py"], tmp_path)


def test_committed_move_heals_without_force(tmp_path):
    reader = _pending_repo(tmp_path)
    s, e, d = _pending_store(tmp_path)

    r1 = sync(s, reader)
    assert [o.status for o in r1.outcomes] == ["moved_uncommitted"]
    assert s.get_entity(e.entity_id).descriptor.file_path == "c.py"

    _commit_the_move(tmp_path)
    r2 = sync(s, reader)  # same graph version, no force

    assert not r2.skipped
    assert [o.status for o in r2.outcomes] == ["moved"]
    kept = s.get_entity(e.entity_id)
    assert kept.descriptor.file_path == "d.py"
    assert kept.last_seen_node_id == "m2"
    assert s.bindings_for_record(d.id)[0].status == "live"


def test_pending_move_not_rechecked_while_head_unchanged(tmp_path, monkeypatch):
    reader = _pending_repo(tmp_path)
    s, e, d = _pending_store(tmp_path)
    sync(s, reader)
    before = s.get_meta(_PENDING_KEY)
    assert before is not None  # precondition: pass 1 remembered the move

    def _boom(*a, **k):
        raise AssertionError("rebind_entity must not run while HEAD is unchanged")

    monkeypatch.setattr("sidegraph.sync.rebind_entity", _boom)
    r = sync(s, reader)

    assert r.skipped
    assert s.get_meta(_PENDING_KEY) == before  # a gated call with HEAD unchanged keeps the key


def test_unresolvable_head_keeps_the_pending_move(tmp_path, monkeypatch):
    reader = _pending_repo(tmp_path)
    s, e, d = _pending_store(tmp_path)
    sync(s, reader)
    _commit_the_move(tmp_path)
    before = s.get_meta(_PENDING_KEY)
    assert before is not None  # precondition: pass 1 remembered the move

    def _no_git(*a, **k):
        raise ValueError("git unavailable")

    monkeypatch.setattr("sidegraph.sync._run_git", _no_git)
    r = sync(s, reader)

    assert r.skipped
    assert s.bindings_for_record(d.id)[0].status == "live"
    assert s.get_meta(_PENDING_KEY) == before  # byte-identical


def test_pending_key_recorded_then_cleared(tmp_path):
    reader = _pending_repo(tmp_path)
    s, e, d = _pending_store(tmp_path)
    sync(s, reader)

    import json

    raw = s.get_meta(_PENDING_KEY)
    assert raw is not None  # precondition: pass 1 remembered the move
    data = json.loads(raw)
    assert data["entity_ids"] == [e.entity_id]
    assert data["head"] == _head(tmp_path)

    _commit_the_move(tmp_path)
    sync(s, reader)
    assert s.get_meta(_PENDING_KEY) is None


def test_pending_entity_gone_is_dropped(tmp_path):
    import json

    reader = _pending_repo(tmp_path)
    s, e, d = _pending_store(tmp_path)
    sync(s, reader)
    # a stale head (the narrow pass must run) and an id the store no longer resolves
    s.set_meta(
        _PENDING_KEY, json.dumps({"head": "0" * 40, "entity_ids": ["01GONEGONEGONEGONEGONEGONE"]})
    )

    r = sync(s, reader)

    assert not r.skipped
    assert r.outcomes == []
    assert s.get_meta(_PENDING_KEY) is None


def test_narrow_report_carries_slug_conflicts(tmp_path):
    import json

    from sidegraph.retrieval import TOC_CACHE_KEY
    from sidegraph.schema import Domain, DomainStatus, Provenance

    reader = _pending_repo(tmp_path)
    s, e, d = _pending_store(tmp_path)
    s.close()
    for i, domain_id in enumerate(("01SLUGPENDINGBRANCHA0001", "01SLUGPENDINGBRANCHB0002")):
        dom = Domain(
            domain_id=domain_id,
            slug="payments",
            title=f"Payments {i}",
            summary="Handles order settlement and refunds.",
            status=DomainStatus.ACCEPTED,
            provenance=Provenance(source="manual"),
        )
        data = dom.model_dump(mode="json")
        data.pop("communities", None)
        path = tmp_path / "t.db" / "domains" / f"{domain_id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    s = Store(tmp_path / "t.db")  # the external files force a reload

    r1 = sync(s, reader)  # full pass: remembers the pending move
    assert [o.status for o in r1.outcomes if o.status == "moved_uncommitted"]
    s.set_meta(TOC_CACHE_KEY, "sentinel")
    _commit_the_move(tmp_path)
    r2 = sync(s, reader)  # narrow pass

    assert not r2.skipped
    assert [o.status for o in r2.outcomes] == ["moved"]
    assert r2.slug_conflicts != []
    assert s.get_meta(TOC_CACHE_KEY) != "sentinel"


def _assert_abandoned_move_left_alone(s, e, d, r, tmp_path):
    assert not r.skipped
    assert [o.status for o in r.outcomes] == ["unchanged"]
    assert r.stale_decisions == []
    # still watched, re-stamped with the current HEAD: the rename may come back
    assert json.loads(s.get_meta(_PENDING_KEY)) == {
        "head": _head(tmp_path),
        "entity_ids": [e.entity_id],
    }
    kept = s.get_entity(e.entity_id)
    assert kept.descriptor.file_path == "c.py"
    assert kept.last_seen_node_id == "m1"
    assert s.bindings_for_record(d.id)[0].status == "live"


def test_abandoned_move_reverted_leaves_the_leaf_live(tmp_path):
    reader = _pending_repo(tmp_path)
    s, e, d = _pending_store(tmp_path)
    assert [o.status for o in sync(s, reader).outcomes] == ["moved_uncommitted"]

    _git(["mv", "d.py", "c.py"], tmp_path)  # the rename is reverted ...
    (tmp_path / "x.txt").write_text("x\n")
    _git(["add", "x.txt"], tmp_path)
    _git(["commit", "-q", "-m", "unrelated"], tmp_path)  # ... and HEAD moves without a rebuild
    r = sync(s, reader)

    _assert_abandoned_move_left_alone(s, e, d, r, tmp_path)


def test_abandoned_move_stashed_leaves_the_leaf_live(tmp_path):
    reader = _pending_repo(tmp_path)
    s, e, d = _pending_store(tmp_path)
    assert [o.status for o in sync(s, reader).outcomes] == ["moved_uncommitted"]

    _git(["branch", "feature"], tmp_path)
    _git(["stash", "-q"], tmp_path)  # the staged rename is stashed away
    _git(["checkout", "-q", "feature"], tmp_path)
    (tmp_path / "y.txt").write_text("y\n")
    _git(["add", "y.txt"], tmp_path)
    _git(["commit", "-q", "-m", "unrelated"], tmp_path)
    r = sync(s, reader)

    _assert_abandoned_move_left_alone(s, e, d, r, tmp_path)


GRAPH_TWO_MOVERS = {
    "built_at_commit": "vTwo",
    "nodes": [
        {
            "id": "m2",
            "label": "mover_fn()",
            "norm_label": "mover_fn()",
            "file_type": "code",
            "source_file": "d.py",
            "community": 2,
        },
        {
            "id": "n2",
            "label": "other_fn()",
            "norm_label": "other_fn()",
            "file_type": "code",
            "source_file": "d2.py",
            "community": 2,
        },
    ],
    "links": [],
}


def _two_pending(tmp_path):
    """Two leaves, c.py -> d.py and c2.py -> d2.py, both renamed on the dirty tree only."""
    _init_repo(tmp_path)
    (tmp_path / "c.py").write_text("def mover_fn(): pass\n")
    (tmp_path / "c2.py").write_text("def other_fn(): pass\n")
    _git(["add", "c.py", "c2.py"], tmp_path)
    _git(["commit", "-q", "-m", "initial"], tmp_path)
    _git(["mv", "c.py", "d.py"], tmp_path)
    _git(["mv", "c2.py", "d2.py"], tmp_path)
    reader = GraphifyReader(_write_graph(tmp_path, "two.json", GRAPH_TWO_MOVERS))
    s = Store(tmp_path / "t.db")
    e1 = _entity(s, "mover_fn", "c.py", "m1")
    e2 = _entity(s, "other_fn", "c2.py", "n1")
    d1 = _leaf(s, e1.entity_id)
    d2 = _leaf(s, e2.entity_id)
    return reader, s, (e1, e2), (d1, d2)


def test_narrow_pass_isolates_one_entitys_error_and_drops_its_id(tmp_path, monkeypatch):
    reader, s, (e1, e2), (d1, d2) = _two_pending(tmp_path)
    assert [o.status for o in sync(s, reader).outcomes] == ["moved_uncommitted"] * 2
    _commit_the_move(tmp_path)

    real = rebind_entity

    def _flaky(entity, *a, **k):
        if entity.entity_id == e1.entity_id:
            raise RuntimeError("boom")
        return real(entity, *a, **k)

    monkeypatch.setattr("sidegraph.sync.rebind_entity", _flaky)
    r = sync(s, reader)  # must not raise

    by_id = {o.entity_id: o for o in r.outcomes}
    assert by_id[e1.entity_id].status == "error"
    assert "boom" in by_id[e1.entity_id].detail
    assert by_id[e2.entity_id].status == "moved"  # the second entity was still processed
    assert s.get_entity(e2.entity_id).descriptor.file_path == "d2.py"
    assert s.get_meta(_PENDING_KEY) is None  # the errored id is dropped, not kept pending


def test_a_stashed_rename_that_comes_back_still_heals(tmp_path):
    """Codex's sequence: an abandoned move must keep being watched, or the rename that is
    committed later is never noticed (the gate skips, the descriptor stays at c.py)."""
    reader = _pending_repo(tmp_path)
    s, e, d = _pending_store(tmp_path)
    assert [o.status for o in sync(s, reader).outcomes] == ["moved_uncommitted"]

    _git(["stash", "-q"], tmp_path)  # the staged rename is stashed away
    (tmp_path / "x.txt").write_text("x\n")
    _git(["add", "x.txt"], tmp_path)
    _git(["commit", "-q", "-m", "unrelated"], tmp_path)
    r1 = sync(s, reader)
    assert [o.status for o in r1.outcomes] == ["unchanged"]
    assert json.loads(s.get_meta(_PENDING_KEY))["entity_ids"] == [e.entity_id]

    _git(["stash", "pop", "-q", "--index"], tmp_path)  # the rename is back ...
    _commit_the_move(tmp_path)  # ... and committed, with no graph rebuild
    r2 = sync(s, reader)

    assert not r2.skipped
    assert [o.status for o in r2.outcomes] == ["moved"]
    assert s.get_entity(e.entity_id).descriptor.file_path == "d.py"
    assert s.get_meta(_PENDING_KEY) is None


GRAPH_A = {
    "built_at_commit": "vA",
    "nodes": [
        {
            "id": "m1",
            "label": "mover_fn()",
            "norm_label": "mover_fn()",
            "file_type": "code",
            "source_file": "c.py",
            "community": 2,
        }
    ],
    "links": [],
}


def test_a_full_pass_after_a_rebuild_clears_a_watched_abandoned_move(tmp_path):
    """The narrow pass keeps an abandoned move under watch; the next FULL pass replaces the
    key from its own outcomes, so the entity goes once it is no longer moved_uncommitted."""
    reader = _pending_repo(tmp_path)
    s, e, d = _pending_store(tmp_path)
    sync(s, reader)
    _git(["mv", "d.py", "c.py"], tmp_path)
    (tmp_path / "x.txt").write_text("x\n")
    _git(["add", "x.txt"], tmp_path)
    _git(["commit", "-q", "-m", "unrelated"], tmp_path)
    sync(s, reader)
    assert json.loads(s.get_meta(_PENDING_KEY))["entity_ids"] == [e.entity_id]  # precondition

    rebuilt = GraphifyReader(_write_graph(tmp_path, "a.json", GRAPH_A))
    r = sync(s, rebuilt)  # a new graph version: a full pass

    assert not r.skipped
    assert "moved_uncommitted" not in [o.status for o in r.outcomes]
    assert s.get_meta(_PENDING_KEY) is None


def _commit_after_the_ladder(monkeypatch, tmp_path):
    """A commit lands right after the ladder has run, before the pass records its HEAD."""
    real = rebind_entity
    fired = []

    def wrapped(*a, **k):
        out = real(*a, **k)
        if not fired:
            fired.append(True)
            _commit_the_move(tmp_path)
        return out

    monkeypatch.setattr("sidegraph.sync.rebind_entity", wrapped)


@pytest.mark.parametrize("pass_kind", ["full", "narrow"])
def test_head_is_read_before_the_ladder(tmp_path, monkeypatch, pass_kind):
    reader = _pending_repo(tmp_path)
    s, e, d = _pending_store(tmp_path)
    if pass_kind == "narrow":
        assert [o.status for o in sync(s, reader).outcomes] == ["moved_uncommitted"]
        # a stale recorded head sends the next gated call down the narrow path
        s.set_meta(_PENDING_KEY, json.dumps({"head": "0" * 40, "entity_ids": [e.entity_id]}))
    before = _head(tmp_path)
    _commit_after_the_ladder(monkeypatch, tmp_path)

    r1 = sync(s, reader)
    assert not r1.skipped
    assert [o.status for o in r1.outcomes] == ["moved_uncommitted"]
    assert json.loads(s.get_meta(_PENDING_KEY))["head"] == before  # the pre-ladder HEAD
    monkeypatch.undo()

    r2 = sync(s, reader)  # HEAD differs from the recorded one: the narrow pass heals

    assert not r2.skipped
    assert [o.status for o in r2.outcomes] == ["moved"]
    assert s.get_entity(e.entity_id).descriptor.file_path == "d.py"
    assert s.get_meta(_PENDING_KEY) is None


def test_lookup_failure_is_an_error_outcome_inside_the_guard(tmp_path, monkeypatch):
    reader, s, (e1, e2), (d1, d2) = _two_pending(tmp_path)
    assert [o.status for o in sync(s, reader).outcomes] == ["moved_uncommitted"] * 2
    _commit_the_move(tmp_path)
    real_get = Store.get_entity

    def flaky(self, entity_id, *a, **k):
        if entity_id == e1.entity_id:
            raise RuntimeError("lookup failed")
        return real_get(self, entity_id, *a, **k)

    monkeypatch.setattr(Store, "get_entity", flaky)
    r = sync(s, reader)  # must not raise
    monkeypatch.undo()

    by_id = {o.entity_id: o for o in r.outcomes}
    assert [o.status for o in r.outcomes].count("error") == 1
    assert by_id[e1.entity_id].status == "error"
    assert "lookup failed" in by_id[e1.entity_id].detail
    assert by_id[e2.entity_id].status == "moved"
    assert s.get_meta(_PENDING_KEY) is None  # the errored id is dropped, not kept pending


def test_a_recorded_null_head_runs_the_narrow_pass_once_a_commit_exists(tmp_path):
    """A record made in an empty repo has head null; the first commit resolves HEAD, which
    differs from null, so the entity is re-checked."""
    reader = _pending_repo(tmp_path)
    s, e, d = _pending_store(tmp_path)
    sync(s, reader)
    _commit_the_move(tmp_path)
    s.set_meta(_PENDING_KEY, json.dumps({"head": None, "entity_ids": [e.entity_id]}))

    r = sync(s, reader)

    assert not r.skipped
    assert [o.status for o in r.outcomes] == ["moved"]


def test_a_recorded_null_head_with_no_commit_yet_skips_and_keeps_the_key(tmp_path):
    _init_repo(tmp_path)  # no commit: HEAD cannot be resolved
    reader = GraphifyReader(_write_graph(tmp_path, "b.json", GRAPH_B))
    s, e, d = _pending_store(tmp_path)
    sync(s, reader)  # stamps the graph version
    raw = json.dumps({"head": None, "entity_ids": [e.entity_id]})
    s.set_meta(_PENDING_KEY, raw)

    r = sync(s, reader)

    assert r.skipped
    assert s.get_meta(_PENDING_KEY) == raw


def test_narrow_report_dict_shape(tmp_path):
    from sidegraph.sync import report_as_dict

    reader = _pending_repo(tmp_path)
    s, e, d = _pending_store(tmp_path)
    sync(s, reader)
    _commit_the_move(tmp_path)

    d_ = report_as_dict(sync(s, reader))

    assert d_["synced"]
    assert d_["from_version"] == d_["to_version"]
    assert d_["counts"] == str({"moved": 1})
    assert [o["status"] for o in d_["outcomes"]] == ["moved"]
    assert d_["domains_refreshed"] == 0
    assert d_["empty_domains"] == [] and d_["overbroad_domains"] == []
    assert d_["domain_failures"] == []


GRAPH_COMMUNITY_MOVE = {
    "built_at_commit": "vCom",
    "nodes": [
        {
            "id": "m2",
            "label": "mover_fn()",
            "norm_label": "mover_fn()",
            "file_type": "code",
            "source_file": "d.py",
            "community": 7,
        },
        {
            "id": "g1",
            "label": "g_fn()",
            "norm_label": "g_fn()",
            "file_type": "code",
            "source_file": "g.py",
            "community": 2,
        },
    ],
    "links": [],
}


def _community2_status(tmp_path, with_sibling):
    """A committed move repoints the leaf community 2 -> 7 in the narrow pass. Returns the
    status of the record's ``community:2`` Tier-1 binding afterwards."""
    reader, s, d, c2 = _community_move_setup(tmp_path, with_sibling)
    _commit_the_move(tmp_path)
    r = sync(s, reader)  # narrow pass
    assert not r.skipped
    assert "moved" in [o.status for o in r.outcomes]
    return _status_of(s, d, c2)


def _community_move_setup(tmp_path, with_sibling):
    """Everything up to the committed move: one full pass has left the leaf
    ``moved_uncommitted``. Returns ``(reader, store, decision, community:2 entity)``."""
    _init_repo(tmp_path)
    (tmp_path / "c.py").write_text("def mover_fn(): pass\n")
    (tmp_path / "g.py").write_text("def g_fn(): pass\n")
    _git(["add", "c.py", "g.py"], tmp_path)
    _git(["commit", "-q", "-m", "initial"], tmp_path)
    _git(["mv", "c.py", "d.py"], tmp_path)
    reader = GraphifyReader(_write_graph(tmp_path, "com.json", GRAPH_COMMUNITY_MOVE))
    s = Store(tmp_path / "t.db")
    mover = s.upsert_entity(
        Entity(
            canonical_name="mover_fn",
            descriptor=Descriptor(name="mover_fn", file_path="c.py"),
            last_seen_node_id="m1",
            last_seen_community="2",
        )
    )
    d = _leaf(s, mover.entity_id)
    c2 = s.get_or_create_abstract_entity("community:2")
    s.add_binding(AnchorBinding(record_id=d.id, entity_id=c2.entity_id, tier=1, status="live"))
    if with_sibling:
        g = s.upsert_entity(
            Entity(
                canonical_name="g_fn",
                descriptor=Descriptor(name="g_fn", file_path="g.py"),
                last_seen_node_id="g1",
                last_seen_community="2",
            )
        )
        s.add_binding(AnchorBinding(record_id=d.id, entity_id=g.entity_id, tier=2, status="live"))
    assert "moved_uncommitted" in [o.status for o in sync(s, reader).outcomes]
    return reader, s, d, c2


def _status_of(s, d, c2):
    tier1 = [b for b in s.bindings_for_record(d.id) if b.entity_id == c2.entity_id]
    return tier1[0].status


_TIER1_META = "pending_tier1_reconcile"


def _fail_the_reconcile_once(monkeypatch, times=1):
    """Make ``_reconcile_tier1_communities`` raise for its first ``times`` calls."""
    real = sync_module._reconcile_tier1_communities
    calls = []

    def flaky(record_id, vacated, store):
        calls.append(record_id)
        if len(calls) <= times:
            raise RuntimeError("injected reconcile failure")
        return real(record_id, vacated, store)

    monkeypatch.setattr(sync_module, "_reconcile_tier1_communities", flaky)
    return calls


def test_a_failed_reconcile_in_a_full_pass_is_retried_by_the_next_plain_sync(tmp_path, monkeypatch):
    reader, s, d, c2 = _community_move_setup(tmp_path, with_sibling=False)
    _commit_the_move(tmp_path)
    _fail_the_reconcile_once(monkeypatch)

    r = sync(s, reader, force=True)

    assert s.get_meta(_PENDING_KEY) is None  # the adopt completed
    assert [o.status for o in r.outcomes if o.status == "error"] == ["error"]
    assert s.get_meta("last_synced_graph_version") == reader.graph_version()
    assert json.loads(s.get_meta(_TIER1_META)) == {d.id: ["2"]}
    assert _status_of(s, d, c2) == "live"  # the stale row survived the failed pass

    r2 = sync(s, reader)  # same version, same HEAD: the gate would skip, the retry repairs

    assert not r2.skipped and [o.status for o in r2.outcomes] == ["moved"]
    assert s.get_meta(_TIER1_META) is None
    assert _status_of(s, d, c2) == "orphaned"


def test_a_failed_reconcile_in_a_narrow_pass_is_retried_by_the_next_plain_sync(
    tmp_path, monkeypatch
):
    reader, s, d, c2 = _community_move_setup(tmp_path, with_sibling=False)
    _commit_the_move(tmp_path)
    _fail_the_reconcile_once(monkeypatch)

    r = sync(s, reader)  # narrow pass

    assert not r.skipped and "error" in [o.status for o in r.outcomes]
    assert json.loads(s.get_meta(_TIER1_META)) == {d.id: ["2"]}
    assert _status_of(s, d, c2) == "live"

    assert not sync(s, reader).skipped  # the retry repairs, so the call is not a skip
    assert s.get_meta(_TIER1_META) is None
    assert _status_of(s, d, c2) == "orphaned"


def test_a_retry_that_fails_again_keeps_the_key_and_reports_an_error(tmp_path, monkeypatch):
    reader, s, d, c2 = _community_move_setup(tmp_path, with_sibling=False)
    _commit_the_move(tmp_path)
    _fail_the_reconcile_once(monkeypatch, times=2)
    sync(s, reader)  # narrow pass: the first failure

    r = sync(s, reader)  # the retry fails as well

    assert not r.skipped
    assert [o.status for o in r.outcomes] == ["error"]
    assert json.loads(s.get_meta(_TIER1_META)) == {d.id: ["2"]}
    assert _status_of(s, d, c2) == "live"

    assert not sync(s, reader).skipped  # the third call's retry succeeds
    assert s.get_meta(_TIER1_META) is None
    assert _status_of(s, d, c2) == "orphaned"


def test_a_successful_retry_on_a_gated_call_reports_synced_with_the_repaired_record(
    tmp_path, monkeypatch
):
    reader, s, d, c2 = _community_move_setup(tmp_path, with_sibling=False)
    _commit_the_move(tmp_path)
    _fail_the_reconcile_once(monkeypatch)
    sync(s, reader)  # narrow pass: the reconcile fails and is remembered
    assert json.loads(s.get_meta(_TIER1_META)) == {d.id: ["2"]}

    r = sync(s, reader)  # same version, same HEAD: the gate would skip, the retry repairs

    assert _status_of(s, d, c2) == "orphaned"
    assert not r.skipped
    d_report = report_as_dict(r)
    assert d_report["synced"] is True
    assert [(o["status"], o["detail"]) for o in d_report["outcomes"]] == [
        ("moved", "tier-1 community reconcile retried")
    ]
    assert d_report["outcomes"][0]["canonical_name"] == f"community rows of record {d.id}"
    assert not report_has_findings(d_report)
    assert sync(s, reader).skipped  # nothing left to retry: the next call skips again


def _abstaining_pending(tmp_path, monkeypatch):
    """A pending ``{record: ['2']}`` whose retry must abstain: the narrow pass's reconcile
    raised once, then a second live Tier-2 leaf with NO community baseline is bound."""
    reader, s, d, c2 = _community_move_setup(tmp_path, with_sibling=False)
    _commit_the_move(tmp_path)
    _fail_the_reconcile_once(monkeypatch)
    sync(s, reader)  # narrow pass: the reconcile raises and is remembered
    assert json.loads(s.get_meta(_TIER1_META)) == {d.id: ["2"]}
    monkeypatch.undo()  # the real reconcile from here on
    nb = s.upsert_entity(
        Entity(
            canonical_name="nb_fn",
            descriptor=Descriptor(name="nb_fn", file_path="g.py"),
            last_seen_node_id="nb1",
            last_seen_community=None,
        )
    )
    s.add_binding(AnchorBinding(record_id=d.id, entity_id=nb.entity_id, tier=2, status="live"))
    return reader, s, d, c2


def test_a_retry_that_abstains_keeps_the_pair(tmp_path, monkeypatch):
    reader, s, d, c2 = _abstaining_pending(tmp_path, monkeypatch)

    sync(s, reader)

    assert s.get_meta(_TIER1_META) is not None  # the key survived the abstain
    assert json.loads(s.get_meta(_TIER1_META)) == {d.id: ["2"]}
    assert _status_of(s, d, c2) == "live"  # nothing was orphaned: the abstain is real


def test_a_retry_that_abstains_reports_nothing_and_the_gate_skips(tmp_path, monkeypatch):
    reader, s, d, c2 = _abstaining_pending(tmp_path, monkeypatch)

    r = sync(s, reader)

    assert [o.status for o in r.outcomes] == []
    assert r.skipped
    assert report_as_dict(r)["synced"] is False
    assert not report_has_findings(report_as_dict(r))


def test_an_abstained_pair_settles_once_the_leaf_has_a_baseline(tmp_path, monkeypatch):
    reader, s, d, c2 = _abstaining_pending(tmp_path, monkeypatch)
    sync(s, reader)  # the retry abstains
    nb = next(e for e in s.iter_concrete_entities() if e.canonical_name == "nb_fn")
    nb.last_seen_community = "7"  # where the mover now lives
    s.upsert_entity(nb)

    r = sync(s, reader)

    assert [(o.status, o.detail) for o in r.outcomes] == [
        ("moved", "tier-1 community reconcile retried")
    ]
    assert s.get_meta(_TIER1_META) is None
    assert _status_of(s, d, c2) == "orphaned"


def test_narrow_pass_keeps_a_shared_community_live_while_a_sibling_holds_it(tmp_path):
    assert _community2_status(tmp_path, with_sibling=True) == "live"


def test_narrow_pass_orphans_a_community_no_leaf_holds_any_more(tmp_path):
    assert _community2_status(tmp_path, with_sibling=False) == "orphaned"


def test_unparseable_pending_key_is_dropped(tmp_path):
    reader = _pending_repo(tmp_path)
    s, e, d = _pending_store(tmp_path)
    sync(s, reader)
    s.set_meta(_PENDING_KEY, "{not json")

    r = sync(s, reader)  # must not raise

    assert r.skipped
    assert s.get_meta(_PENDING_KEY) is None


def test_moved_rung_fails_closed_when_old_path_still_committed_at_head(tmp_path):
    """Isolates the OTHER half of the committed-evidence check: the new path (d.py) IS
    genuinely committed, but the old path (c.py) is ALSO still committed at HEAD -- only
    removed from disk by an uncommitted delete. A move needs BOTH halves confirmed;
    a same-suffix hit whose OLD file git still tracks is not a confirmed move, no matter
    how convincingly the new file is committed."""
    _init_repo(tmp_path)
    (tmp_path / "c.py").write_text("def mover_fn(): pass\n")
    (tmp_path / "d.py").write_text("def mover_fn(): pass\n")
    _git(["add", "-A"], tmp_path)
    _git(["commit", "-q", "-m", "both c.py and d.py committed"], tmp_path)
    (tmp_path / "c.py").unlink()  # dirty, uncommitted delete -- c.py is still at HEAD

    reader = GraphifyReader(_write_graph(tmp_path, "b.json", GRAPH_B))
    s = Store(tmp_path / "t.db")
    e = _entity(s, "mover_fn", "c.py", "m1")
    d = _leaf(s, e.entity_id, status="live")

    out = rebind_entity(e, s, reader, repo_root=tmp_path)

    assert out.status == "moved_uncommitted"  # NOT "moved" -- c.py is still at HEAD
    kept = s.get_entity(e.entity_id)
    assert kept.descriptor.file_path == "c.py"
    assert kept.last_seen_node_id == "m1"
    assert s.bindings_for_record(d.id)[0].status == "live"


def test_moved_rung_fails_closed_when_new_path_never_committed(tmp_path):
    """The mirror-image isolation: the OLD path (c.py) is genuinely, committedly gone
    from HEAD (a real `git rm` + commit), but the graph's proposed new home (d.py) was
    never committed at all -- e.g. it's a fresh, un-added file, or doesn't exist on disk
    either. Confirming only half of the move is not confirming the move."""
    _init_repo(tmp_path)
    (tmp_path / "c.py").write_text("def mover_fn(): pass\n")
    _git(["add", "-A"], tmp_path)
    _git(["commit", "-q", "-m", "initial"], tmp_path)
    _git(["rm", "-q", "c.py"], tmp_path)
    _git(["commit", "-q", "-m", "remove c.py"], tmp_path)
    # d.py is never created or committed anywhere.

    reader = GraphifyReader(_write_graph(tmp_path, "b.json", GRAPH_B))
    s = Store(tmp_path / "t.db")
    e = _entity(s, "mover_fn", "c.py", "m1")
    d = _leaf(s, e.entity_id, status="live")

    out = rebind_entity(e, s, reader, repo_root=tmp_path)

    assert out.status == "moved_uncommitted"  # NOT "moved" -- d.py isn't committed
    kept = s.get_entity(e.entity_id)
    assert kept.descriptor.file_path == "c.py"
    assert kept.last_seen_node_id == "m1"
    assert s.bindings_for_record(d.id)[0].status == "live"


def test_moved_rung_adopts_when_the_move_is_actually_committed(tmp_path):
    """Positive control for the same guard: once the move is genuinely committed (old
    path really gone from HEAD, new path really tracked at HEAD), the rung still adopts
    -- the fix must not turn into a guard that never fires."""
    _init_repo(tmp_path)
    (tmp_path / "c.py").write_text("def mover_fn(): pass\n")
    _git(["add", "-A"], tmp_path)
    _git(["commit", "-q", "-m", "initial"], tmp_path)
    _git(["mv", "c.py", "d.py"], tmp_path)
    _git(["commit", "-q", "-m", "move c.py -> d.py"], tmp_path)

    reader = GraphifyReader(_write_graph(tmp_path, "b.json", GRAPH_B))
    s = Store(tmp_path / "t.db")
    e = _entity(s, "mover_fn", "c.py", "m1")
    d = _leaf(s, e.entity_id, status="live")

    out = rebind_entity(e, s, reader, repo_root=tmp_path)

    assert out.status == "moved" and out.node_id == "m2" and out.detail == "c.py -> d.py"
    kept = s.get_entity(e.entity_id)
    assert kept.descriptor.file_path == "d.py"
    assert s.bindings_for_record(d.id)[0].status == "live"


def test_moved_rung_escape_hatch_trusts_dirty_tree(tmp_path, monkeypatch):
    """SIDEGRAPH_TRUST_DIRTY_TREE=on -- the documented, off-by-default override for
    someone who has verified their own working tree and wants the old disk-only
    behavior back. Same dirty-tree setup as
    test_moved_rung_fails_closed_on_uncommitted_delete, but with the escape hatch set."""
    monkeypatch.setenv("SIDEGRAPH_TRUST_DIRTY_TREE", "on")
    _init_repo(tmp_path)
    (tmp_path / "c.py").write_text("def mover_fn(): pass\n")
    _git(["add", "-A"], tmp_path)
    _git(["commit", "-q", "-m", "initial"], tmp_path)
    (tmp_path / "c.py").unlink()

    reader = GraphifyReader(_write_graph(tmp_path, "b.json", GRAPH_B))
    s = Store(tmp_path / "t.db")
    e = _entity(s, "mover_fn", "c.py", "m1")
    d = _leaf(s, e.entity_id, status="live")

    out = rebind_entity(e, s, reader, repo_root=tmp_path)

    assert out.status == "moved" and out.node_id == "m2"
    assert s.get_entity(e.entity_id).descriptor.file_path == "d.py"
    assert s.bindings_for_record(d.id)[0].status == "live"


def test_sync_resolves_repo_root_from_the_graph_not_the_store(tmp_path):
    """Pins the round-2 mistake THIS fix already made once: a first cut of
    _resolve_repo_root took `store`, not `reader` -- and every other sync/cli test in
    this suite co-locates the store and the graph.json under the same tmp_path, so a
    store-keyed lookup and a reader-keyed lookup resolve to the identical answer there.
    That leaves store-keying entirely unpinned: it would silently pass all 37 other
    sync/cli tests, and even the live verification proof can't tell the difference
    either, since fail-closed degrades a store-keyed miss to "orphaned" rather than
    corrupting anything -- so nothing except a docstring stood between this codebase and
    that exact regression.

    Here the store lives OUTSIDE any git working tree (its own bare tmp subdir) and the
    graph lives INSIDE a real one -- the two lookups genuinely disagree: a store-keyed
    resolve reports "not a git repo" (repo_root=None) and the moved rung fails closed to
    orphaned; a reader-keyed resolve finds the real repo root, confirms c.py is
    genuinely absent there, and the move is adopted. Must go red if sync.py's
    _resolve_repo_root (or its call site inside sync()) is ever re-keyed off the store.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    (repo / "d.py").write_text("def mover_fn(): pass\n")
    _git(["add", "-A"], repo)
    _git(["commit", "-q", "-m", "d.py lives here now"], repo)
    reader = GraphifyReader(_write_graph(repo, "b.json", GRAPH_B))  # c.py absent in repo

    store_dir = tmp_path / "elsewhere" / "not-a-git-repo"
    store_dir.mkdir(parents=True)
    s = Store(store_dir)
    e = _entity(s, "mover_fn", "c.py", "m1")
    d = _leaf(s, e.entity_id, status="live")

    report = sync(s, reader)
    by_name = {o.canonical_name: o for o in report.outcomes}
    assert by_name["mover_fn"].status == "moved"  # NOT "orphaned"
    kept = s.get_entity(e.entity_id)
    assert kept.descriptor.file_path == "d.py"  # descriptor follows the move
    assert s.bindings_for_record(d.id)[0].status == "live"


# -- a reconcile failure after adoption cannot leave the pending key stale -------------------


def _other_repo(tmp_path):
    """Repository B: ``d.py`` committed, no ``c.py``. Under B's tree the moved rung's
    committed-evidence check would pass for ``c.py -> d.py``."""
    b = tmp_path / "other"
    b.mkdir()
    _init_repo(b)
    (b / "d.py").write_text("x = 1\n")
    _git(["add", "-A"], b)
    _git(["commit", "-q", "-m", "b"], b)
    return b


def _point_git_at(monkeypatch, repo):
    monkeypatch.setenv("GIT_DIR", str(repo / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(repo))


def _committed_repo(a, name="f"):
    a.mkdir()
    _init_repo(a)
    (a / name).write_text("1")
    _git(["add", "-A"], a)
    _git(["commit", "-q", "-m", "a"], a)
    return a


def test_current_head_ignores_git_dir(tmp_path, monkeypatch):
    a = _committed_repo(tmp_path / "a")
    a_head = _head(a)  # before the env is set: the helper would read B's HEAD after
    _point_git_at(monkeypatch, _other_repo(tmp_path))

    assert sync_module._current_head(a) == a_head


def test_resolve_repo_root_ignores_git_dir(tmp_path, monkeypatch):
    a = _committed_repo(tmp_path / "a")
    reader = GraphifyReader(_write_graph(a, "b.json", GRAPH_B))
    _point_git_at(monkeypatch, _other_repo(tmp_path))

    assert sync_module._resolve_repo_root(reader) == a.resolve()


def test_committed_evidence_ignores_git_dir(tmp_path, monkeypatch):
    a = _committed_repo(tmp_path / "a", name="c.py")  # A's HEAD: c.py, no d.py
    _point_git_at(monkeypatch, _other_repo(tmp_path))  # B's HEAD: d.py, no c.py

    assert sync_module._committed_evidence_confirms_move(a, "c.py", "d.py") is False


def test_an_uncommitted_move_is_not_adopted_under_a_foreign_git_dir(tmp_path, monkeypatch):
    a = tmp_path / "a"
    a.mkdir()
    reader = _pending_repo(a)  # A's rename is staged, not committed
    s = Store(a / "t.db")
    e = _entity(s, "mover_fn", "c.py", "m1")
    _leaf(s, e.entity_id, status="live")
    assert [o.status for o in sync(s, reader).outcomes] == ["moved_uncommitted"]
    _point_git_at(monkeypatch, _other_repo(tmp_path))

    r = sync(s, reader)

    assert s.get_entity(e.entity_id).descriptor.file_path == "c.py"  # A never committed it
    assert r.skipped  # A's HEAD is unchanged, so the gate skips


def test_a_committed_move_heals_when_git_dir_does_not_resolve_from_the_repo(tmp_path, monkeypatch):
    a = tmp_path / "a"
    a.mkdir()
    reader = _pending_repo(a)
    s = Store(a / "t.db")
    e = _entity(s, "mover_fn", "c.py", "m1")
    _leaf(s, e.entity_id, status="live")
    sync(s, reader)
    _commit_the_move(a)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GIT_DIR", "a/.git")  # valid from the process cwd, not from a/

    r = sync(s, reader)

    assert [o.status for o in r.outcomes] == ["moved"]


def _reconcile_that_cannot_read_entities(monkeypatch, key_seen):
    """Make ``store.get_entity`` raise only while ``_reconcile_tier1_communities`` runs, and
    note what the pending key held when the reconcile started."""
    real = sync_module._reconcile_tier1_communities

    def wrapper(record_id, gone, store):
        key_seen.append(store.get_meta(_PENDING_KEY))

        def boom(*a, **k):
            raise RuntimeError("injected: reconcile cannot read an entity")

        with monkeypatch.context() as m:
            m.setattr(store, "get_entity", boom)
            real(record_id, gone, store)

    monkeypatch.setattr(sync_module, "_reconcile_tier1_communities", wrapper)


def test_a_reconcile_failure_after_adoption_keeps_the_pending_key_true(tmp_path, monkeypatch):
    reader, s, (e1, e2), (d1, d2) = _two_pending(tmp_path)
    e1.last_seen_community = "9"  # a community the move vacates, so the reconcile runs
    s.upsert_entity(e1)
    s.add_binding(AnchorBinding(record_id=d1.id, entity_id=e2.entity_id, tier=2, status="live"))
    assert [o.status for o in sync(s, reader).outcomes] == ["moved_uncommitted"] * 2
    _commit_the_move(tmp_path)

    key_seen: list = []
    _reconcile_that_cannot_read_entities(monkeypatch, key_seen)
    r = sync(s, reader)  # the narrow pass adopts both, then the reconcile raises

    assert key_seen == [None]  # the key was rewritten from the adopts BEFORE the reconcile
    assert sorted(o.status for o in r.outcomes) == ["error", "moved", "moved"]
    assert s.get_meta(_PENDING_KEY) is None  # the adopted ids are not pending any more
    monkeypatch.undo()
    (tmp_path / "x.txt").write_text("x\n")
    _git(["add", "x.txt"], tmp_path)
    _git(["commit", "-q", "-m", "unrelated"], tmp_path)
    r2 = sync(s, reader)  # HEAD moved again: a stale key would report them abandoned
    assert [o.status for o in r2.outcomes] == ["moved"]  # only the reconcile retry, no abandoned


def test_a_stale_key_holding_an_adopted_entity_is_dropped_not_abandoned(tmp_path):
    reader = _pending_repo(tmp_path)
    s, e, d = _pending_store(tmp_path)
    sync(s, reader)
    _commit_the_move(tmp_path)
    assert [o.status for o in sync(s, reader).outcomes] == ["moved"]
    assert s.get_entity(e.entity_id).descriptor.file_path == "d.py"
    # the state a failed reconcile used to leave: the adopted id still remembered
    s.set_meta(_PENDING_KEY, json.dumps({"head": "0" * 40, "entity_ids": [e.entity_id]}))

    r = sync(s, reader)

    assert not r.skipped
    assert [o.status for o in r.outcomes] == ["unchanged"]
    assert "abandoned" not in (r.outcomes[0].detail or "")
    assert s.get_meta(_PENDING_KEY) is None
