import subprocess

from sidegraph.engine.reader import GraphifyReader
from sidegraph.schema import AnchorBinding, Descriptor, Entity
from sidegraph.store import Store
from sidegraph.sync import rebind_entity, sync

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
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
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
