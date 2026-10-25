"""A seed that is a real file in another checkout of the repository is not called malformed.

The decision MCP is rooted at one checkout and shared by subagents in sibling linked worktrees;
``get_task_context`` and ``retrieve_decisions(files=...)`` must tell a file that exists in
another checkout from a path that is nowhere, and must never guess it to be a same-named graph
file. Real repositories from ``tests.graph_repo_support``.
see design/superpowers/specs/2026-10-11-worktree-only-seeds-design.md (section 4)
"""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from sidegraph import seed_ladder, server, worktree_seeds
from sidegraph.engine.reader import GraphifyReader
from sidegraph.retrieval import Seed
from sidegraph.schema import (
    AnchorBinding,
    Decision,
    DecisionKind,
    DecisionStatus,
    Descriptor,
    Entity,
    Provenance,
    Scope,
)
from sidegraph.store import Store
from sidegraph.worktree_seeds import Worktree
from tests.graph_repo_support import (
    GRAPH,
    _block,
    _impl,
    add_worktree,
    commit,
    git,
    make_main,
    remember,
    serve,
)

NOT_IN_GRAPH = "## Not in the code graph"
NEAREST = "## Nearest anchored"
NEW = "def n():\n    pass\n"


def bind(
    store_path: Path,
    title: str,
    file_path: str,
    name: str,
    status: str = "orphaned",
    also: tuple[str, str, str] | None = None,
) -> str:
    """One accepted repo-scoped gotcha bound with ``status`` to ``name`` in ``file_path``, and,
    with ``also=(file_path, name, status)``, to a second entity."""
    store = Store(store_path)
    try:
        d = store.add_decision(
            Decision(
                title=title,
                kind=DecisionKind.GOTCHA,
                status=DecisionStatus.ACCEPTED,
                context="c",
                choice="ch",
                scope=Scope.REPO,
                valid_from=datetime.now(UTC),
                provenance=Provenance(source="manual"),
            )
        )
        for f, n, st in [(file_path, name, status), *([also] if also else [])]:
            e = store.upsert_entity(
                Entity(canonical_name=n, descriptor=Descriptor(name=n, file_path=f))
            )
            store.add_binding(
                AnchorBinding(record_id=d.id, entity_id=e.entity_id, tier=2, status=st)
            )
        return d.id
    finally:
        store.close()


def repo(tmp_path: Path, files=("pkg/m.py", "pkg/n.py")) -> Path:
    main = make_main(tmp_path, files=files)
    remember(main / ".sidegraph", "m.py keeps a retry budget", "pkg/m.py")
    commit(main, "record", {})
    return main


def feat_with(main: Path, files: dict[str, str]) -> Path:
    feat = add_worktree(main, "feat")
    commit(feat, "feat", files)
    return feat


def fake_worktree(main: Path, rel: str) -> Path:
    """A directory standing in for the linked worktree ``feat``: it holds ``rel``, and git is
    not asked, so the ladder is handed it by the caller."""
    feat = main / ".worktrees" / "feat"
    (feat / rel).parent.mkdir(parents=True)
    (feat / rel).write_text(NEW)
    return feat


def ask(monkeypatch, main: Path, files=None, entities=None, **kw) -> str:
    store = serve(monkeypatch, main)
    return _impl(store, GraphifyReader(main / GRAPH), files, entities, **kw)


def lines(out: str, heading: str = NOT_IN_GRAPH) -> list[str]:
    return _block(out, heading)


def test_1_worktree_only_file_names_the_worktree_and_branch(tmp_path, monkeypatch):
    main = repo(tmp_path)
    feat_with(main, {"pkg/new.py": NEW})

    out = ask(monkeypatch, main, ["pkg/new.py"])

    (line,) = lines(out)
    assert "exists only in another checkout of this repository" in line
    assert "pkg/new.py (worktree .worktrees/feat, branch wt-feat)" in line
    assert "retrieve_decisions" not in line  # the pointer comes with records, not before
    assert "repo-relative" not in line
    assert "`pkg/m.py`" in "\n".join(lines(out, NEAREST))  # the Nearest block still names m.py


def test_3_record_bound_to_the_worktree_only_path(tmp_path, monkeypatch):
    main = repo(tmp_path)
    feat_with(main, {"pkg/new.py": NEW})
    bind(main / ".sidegraph", "new.py must stay pure", "pkg/new.py", "n")

    out = ask(monkeypatch, main, ["pkg/new.py"])

    # Direct file retrieval replaces the old listing-only behavior with explicit file applicability.
    assert "[file-bound] new.py must stay pure" in out
    assert "the graph cannot verify its symbol anchors" in out
    assert "more record(s)" not in out and NEAREST not in out
    assert "No current record is anchored to `pkg/new.py`" not in out


def test_3b_only_the_orphan_bound_record_in_the_store(tmp_path, monkeypatch):
    main = make_main(tmp_path, files=("pkg/m.py",))
    bind(main / ".sidegraph", "new.py must stay pure", "pkg/new.py", "n")
    commit(main, "record", {})
    feat_with(main, {"pkg/new.py": NEW})

    out = ask(monkeypatch, main, ["pkg/new.py"])

    assert "[file-bound] new.py must stay pure" in out
    assert "## Why this is empty" not in out and "Nearest anchored:" not in out


def test_3c_an_orphan_on_a_file_the_graph_holds_keeps_todays_line(tmp_path, monkeypatch):
    main = repo(tmp_path)
    bind(main / ".sidegraph", "n.py batch rule", "pkg/n.py", "gone_symbol")

    out = ask(monkeypatch, main, ["pkg/n.py"])

    near = "\n".join(lines(out, NEAREST))
    assert "No current record is anchored to `pkg/n.py`" in near
    assert "record(s) bound to it" not in out


def test_3d_a_record_the_answer_delivers_is_not_called_undelivered(tmp_path, monkeypatch):
    """One record bound to the worktree-only file (orphaned) and to a graph file (live)."""
    main = repo(tmp_path)
    feat_with(main, {"pkg/new.py": NEW})
    bind(
        main / ".sidegraph",
        "new.py and n.py share a lock",
        "pkg/new.py",
        "n",
        also=("pkg/n.py", "fn_1()", "live"),
    )

    both = ask(monkeypatch, main, ["pkg/new.py", "pkg/n.py"])
    assert "new.py and n.py share a lock" in both  # the main answer delivers it
    assert "record(s) bound to it" not in both and "this answer does not" not in both
    assert "more record(s)" not in both


def test_4_branch_only_file_absent_everywhere_is_not_here(tmp_path, monkeypatch):
    main = repo(tmp_path)
    feat_with(main, {"pkg/new.py": NEW})

    out = ask(monkeypatch, main, ["pkg/nope.py"])

    (line,) = lines(out)
    assert line.startswith("1 of 1 seed path is not a file in this checkout (")
    assert " or in any other checkout of this repository: pkg/nope.py." in line
    assert "may be on a branch that is not checked out anywhere" in line
    assert "repo-relative" not in line


def test_4b_not_here_omits_the_other_checkout_clause_without_worktrees(tmp_path, monkeypatch):
    main = repo(tmp_path)

    out = ask(monkeypatch, main, ["pkg/nope.py"])

    (line,) = lines(out)
    assert "other checkout" not in line
    assert "): pkg/nope.py. The path is well-formed as read" in line


def test_5_one_same_named_graph_file_does_not_capture_a_worktree_only_file(tmp_path, monkeypatch):
    main = repo(tmp_path, files=("pkg/m.py", "lib/new.py"))
    feat_with(main, {"pkg/new.py": NEW})

    out = ask(monkeypatch, main, ["pkg/new.py"])

    assert "guessed" not in out and "read as `lib/new.py`" not in out
    (line,) = lines(out)
    assert "exists only in another checkout" in line


def test_6_two_same_named_graph_files_are_not_ambiguous(tmp_path, monkeypatch):
    main = repo(tmp_path, files=("pkg/m.py", "a/README.md", "b/README.md"))
    feat_with(main, {"docs/x/README.md": "# x\n"})

    out = ask(monkeypatch, main, ["docs/x/README.md"])

    assert "ambiguous" not in out
    (line,) = lines(out)
    assert "exists only in another checkout" in line


def test_7_entity_seed_with_a_worktree_only_file_is_not_hunted_across_files(tmp_path, monkeypatch):
    main = repo(tmp_path, files=("pkg/m.py", "lib/new.py"))
    feat_with(main, {"pkg/new.py": NEW})

    # `fn_1()` is the name `lib/new.py` holds: a guess across files would be accepted.
    out = ask(monkeypatch, main, None, [{"name": "fn_1()", "file_path": "pkg/new.py"}])

    assert "lib/new.py" not in out
    (line,) = lines(out)
    assert "pkg/new.py (worktree .worktrees/feat, branch wt-feat)" in line


@pytest.mark.parametrize("spelling", ["absolute", "relative"])
def test_8_nested_worktree_spellings_are_read_as_the_repo_relative_path(
    tmp_path, monkeypatch, spelling
):
    main = repo(tmp_path)
    feat = feat_with(main, {"pkg/new.py": NEW})
    seed = str(feat / "pkg" / "new.py") if spelling == "absolute" else ".worktrees/feat/pkg/new.py"

    out = ask(monkeypatch, main, [seed])

    (line,) = lines(out)
    assert "pkg/new.py (worktree .worktrees/feat, branch wt-feat)" in line
    assert ".worktrees/feat/pkg/new.py" not in line
    assert "rebuild" not in line
    assert "read as `pkg/new.py` (normalised)" in out


def test_9_absolute_path_in_a_sibling_directory_worktree(tmp_path, monkeypatch):
    main = repo(tmp_path)
    sibling = tmp_path / "R-feat"
    git(main, "worktree", "add", "-q", str(sibling), "-b", "sib")
    commit(sibling, "sib", {"pkg/new.py": NEW})

    out = ask(monkeypatch, main, [str(sibling / "pkg" / "new.py")])

    (line,) = lines(out)
    assert "pkg/new.py (worktree " in line and "branch sib" in line
    assert "repo-relative" not in line


def test_9c_a_sibling_worktree_reached_through_a_symlinked_parent(tmp_path, monkeypatch):
    main = repo(tmp_path)
    sibling = tmp_path / "R-feat"
    git(main, "worktree", "add", "-q", str(sibling), "-b", "sib")
    commit(sibling, "sib", {"pkg/sib.py": NEW})
    alias = tmp_path / "alias"
    alias.symlink_to(tmp_path, target_is_directory=True)

    out = ask(monkeypatch, main, [str(alias / "R-feat" / "pkg" / "sib.py")])

    (line,) = lines(out)
    assert "exists only in another checkout" in line and "branch sib" in line


def test_9d_a_nested_spelling_of_a_graph_file_is_read_as_that_file(tmp_path, monkeypatch):
    main = repo(tmp_path)
    feat_with(main, {"pkg/new.py": NEW})

    out = ask(monkeypatch, main, [".worktrees/feat/pkg/m.py"])

    assert NOT_IN_GRAPH not in out
    assert "read as `pkg/m.py` (normalised)" in out


def test_9e_an_entity_seed_with_a_nested_path_gets_the_normalised_note(tmp_path, monkeypatch):
    main = repo(tmp_path)
    feat = feat_with(main, {"pkg/new.py": NEW})

    out = ask(monkeypatch, main, None, [{"name": "n", "file_path": str(feat / "pkg" / "new.py")}])

    assert "`n` in `pkg/new.py` (normalised)" in out


def test_10_graph_seeds_make_no_git_call_and_a_missing_seed_lists_the_worktrees_once(
    tmp_path, monkeypatch
):
    main = repo(tmp_path)
    feat_with(main, {"pkg/new.py": NEW})
    calls: list[Path] = []
    real = worktree_seeds.linked_worktrees
    monkeypatch.setattr(
        worktree_seeds, "linked_worktrees", lambda root: calls.append(root) or real(root)
    )

    ask(monkeypatch, main, [str(main / "pkg" / "m.py"), "pkg/m.py"])
    assert calls == []

    ask(monkeypatch, main, ["pkg/new.py", "pkg/nope.py", "pkg/x.py"])
    assert len(calls) == 1


def test_11_malformed_seeds_keep_their_sentence(tmp_path, monkeypatch):
    main = repo(tmp_path)
    seeds = ["../x", "/elsewhere/x.py", ".", "C:\\elsewhere\\x.py"]

    out = ask(monkeypatch, main, seeds)

    (line,) = lines(out)
    assert line.startswith(
        "4 of 4 seed paths are not repo-relative paths to files in this repository: "
        "../x, /elsewhere/x.py, ., C:\\elsewhere\\x.py."
    )
    assert "Check the path" in line

    # A dotted path is normalised, then not here.
    (line,) = lines(ask(monkeypatch, main, ["a/../b.py"]))
    assert "): b.py. The path is well-formed as read" in line

    # A relative backslash path is a filename on posix.
    (line,) = lines(ask(monkeypatch, main, ["pkg\\new.py"]))
    assert "not a file in this checkout" in line and "pkg\\new.py" in line


def test_12_git_dir_in_the_environment_does_not_redirect_the_worktree_list(tmp_path, monkeypatch):
    main = repo(tmp_path)
    feat = feat_with(main, {"pkg/new.py": NEW})
    other = tmp_path / "other"
    other.mkdir()
    subprocess.run(["git", "init", "-q", str(other)], check=True)
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))

    got = worktree_seeds.linked_worktrees(main)

    assert [w.path.name for w in got] == [feat.name]
    assert got[0].branch == "wt-feat"


def test_13_linked_worktrees_degrades_to_empty(tmp_path, monkeypatch):
    main = repo(tmp_path)
    feat = feat_with(main, {"pkg/new.py": NEW})
    gone = add_worktree(main, "gone")
    assert [w.path.name for w in worktree_seeds.linked_worktrees(main)] == ["feat", "gone"]
    # A vanished directory is skipped.
    import shutil

    shutil.rmtree(gone)
    assert [w.path for w in worktree_seeds.linked_worktrees(main)] == [feat]
    # A repository with no other checkout, and the root itself, are never listed.
    assert worktree_seeds.linked_worktrees(feat) != [] and all(
        w.path != feat for w in worktree_seeds.linked_worktrees(feat)
    )

    # Timeout.
    def timeout(*a, **k):
        raise subprocess.TimeoutExpired("git", 5)

    monkeypatch.setattr(subprocess, "run", timeout)
    assert worktree_seeds.linked_worktrees(main) == []
    monkeypatch.undo()
    # No git on PATH.
    monkeypatch.setenv("PATH", "")
    assert worktree_seeds.linked_worktrees(main) == []


def test_13a_git_is_run_with_a_timeout_and_no_stdin(tmp_path, monkeypatch):
    seen: dict = {}

    def fake(*a, **k):
        seen.update(k)
        return subprocess.CompletedProcess([], 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake)
    worktree_seeds.linked_worktrees(tmp_path)

    assert isinstance(seen["timeout"], int) and seen["timeout"] > 0
    assert seen["stdin"] is subprocess.DEVNULL


def test_13c_linked_worktrees_caps_the_listing(tmp_path, monkeypatch):
    monkeypatch.setattr(worktree_seeds, "_CAP", 3)
    dirs = []
    for i in range(5):
        d = tmp_path / f"w{i}"
        d.mkdir()
        dirs.append(d)
    text = "worktree /r\nHEAD a\nbranch refs/heads/main\n\n" + "".join(
        f"worktree {d}\nHEAD a\nbranch refs/heads/b{i}\n\n" for i, d in enumerate(dirs)
    )
    done = subprocess.CompletedProcess([], 0, stdout=text, stderr="")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: done)
    assert len(worktree_seeds.linked_worktrees(tmp_path)) == 3


@pytest.fixture
def borrowing(tmp_path, monkeypatch):
    main = repo(tmp_path)
    w = add_worktree(main, "w")
    x = add_worktree(main, "x")
    commit(x, "x", {"pkg/xonly.py": NEW})
    commit(main, "late", {"pkg/late.py": NEW})
    store = serve(monkeypatch, w)
    return main, w, store


def test_14_borrowed_mode_names_the_graph_checkout_separately(borrowing):
    main, w, store = borrowing
    reader = GraphifyReader(main / GRAPH)

    out = _impl(store, reader, ["pkg/xonly.py"], borrowed_from=main)

    (line,) = lines(out)
    assert "exists only in another checkout" in line
    assert f"not in the one this server reads ({w})" in line
    assert f"(built from {main})" in line
    assert "branch wt-x" in line


def test_14b_a_file_only_on_main_after_the_build_gets_the_freshness_sentence(borrowing):
    main, _w, store = borrowing
    reader = GraphifyReader(main / GRAPH)

    out = _impl(store, reader, ["pkg/late.py"], borrowed_from=main)

    (line,) = lines(out)
    assert "exists but is not in the code graph: pkg/late.py." in line
    assert "another checkout" not in line


def test_17_retrieve_decisions_files_reads_a_worktree_only_path_exactly(tmp_path, monkeypatch):
    main = repo(tmp_path, files=("pkg/m.py", "lib/new.py"))
    remember(main / ".sidegraph", "lib new is legacy", "lib/new.py", name="fn_2()")
    bind(main / ".sidegraph", "new.py must stay pure", "pkg/new.py", "n")
    commit(main, "records", {})
    feat_with(main, {"pkg/new.py": NEW})
    store = serve(monkeypatch, main)
    reader = GraphifyReader(main / GRAPH)

    listing = json.loads(server._decision_listing(store, reader, files=["pkg/new.py"]))

    titles = [d["title"] for d in listing["decisions"]]
    assert titles == ["new.py must stay pure"]


def test_18_a_remapped_but_unresolved_seed_has_a_normalised_note(tmp_path):
    main = make_main(tmp_path)
    feat = fake_worktree(main, "pkg/new.py")

    ladder = seed_ladder.tolerate(
        [Seed(file_path=str(feat / "pkg" / "new.py"))],
        GraphifyReader(main / GRAPH),
        main,
        lambda: [Worktree(feat, "wt-feat")],
    )

    assert ladder.unresolved == ["pkg/new.py"]
    assert ladder.guessed == []
    (note,) = ladder.notes
    assert (note.outcome, note.read_as) == ("normalised", ("pkg/new.py",))


@pytest.mark.parametrize("spelling", ["absolute", "nested"])
@pytest.mark.parametrize("kind", ["file", "entity"])
def test_19_a_remapped_worktree_seed_is_journaled_as_the_repo_relative_path(
    tmp_path, monkeypatch, spelling, kind
):
    """The seed the answer shows as ``pkg/new.py`` is the key telemetry stores, so it joins the
    descriptor paths of the records anchored there (batch panel fix C3)."""
    main = repo(tmp_path)
    feat = feat_with(main, {"pkg/new.py": NEW})
    given = str(feat / "pkg" / "new.py") if spelling == "absolute" else ".worktrees/feat/pkg/new.py"
    store = serve(monkeypatch, main)
    monkeypatch.setattr(server, "_session_key", lambda _s: "sess-1")
    files = [given] if kind == "file" else None
    entities = [{"name": "n", "file_path": given}] if kind == "entity" else None

    out = _impl(store, GraphifyReader(main / GRAPH), files, entities)

    assert "pkg/new.py" in out and "normalised" in out  # precondition: shown normalised
    assert set(store.retrieval_seed_queries()) == {"pkg/new.py"}
    journal = [r["key"] for r in store.retrieval_events() if r["kind"] == "seed"]
    assert journal == ["pkg/new.py"]
    store.close()


def test_19b_a_remapped_seed_for_a_file_no_checkout_holds_is_journaled_normalised(
    tmp_path, monkeypatch
):
    main = repo(tmp_path)
    feat = feat_with(main, {"pkg/new.py": NEW})
    store = serve(monkeypatch, main)
    monkeypatch.setattr(server, "_session_key", lambda _s: "sess-1")
    given = str(feat / "pkg" / "ghost.py")

    out = _impl(store, GraphifyReader(main / GRAPH), None, [{"name": "n", "file_path": given}])

    assert "pkg/ghost.py" in out and given not in out  # precondition: shown normalised
    assert set(store.retrieval_seed_queries()) == {"pkg/ghost.py"}
    store.close()


def test_20_direct_file_delivery_supersedes_the_old_nearest_count(tmp_path, monkeypatch):
    """Retrieval reaches the seed itself; partial-count floors have separate budget regressions."""
    main = make_main(tmp_path, files=("pkg/m.py", "pkg/n.py"))
    bind(main / ".sidegraph", "orphaned note on new.py", "pkg/new.py", "n")
    bind(main / ".sidegraph", "a live note on n.py", "pkg/n.py", "fn_1()", "live")
    commit(main, "records", {})
    feat_with(main, {"pkg/new.py": NEW})

    out = ask(monkeypatch, main, ["pkg/new.py"])

    assert "[file-bound] orphaned note on new.py" in out
    assert "a live note on n.py" not in out
    assert "## Nearest anchored records" not in out and "more record(s)" not in out


def test_20b_direct_file_records_are_not_claimed_as_remaining(tmp_path, monkeypatch):
    main = make_main(tmp_path, files=("pkg/m.py", "pkg/n.py"))
    bind(main / ".sidegraph", "orphaned note on new.py", "pkg/new.py", "n")
    bind(
        main / ".sidegraph",
        "shared lock",
        "pkg/new.py",
        "n",
        also=("pkg/n.py", "fn_1()", "live"),
    )
    commit(main, "records", {})
    feat_with(main, {"pkg/new.py": NEW})

    out = ask(monkeypatch, main, ["pkg/new.py"])

    assert "[file-bound] shared lock" in out and "orphaned note on new.py" in out
    assert "more record(s)" not in out and "No current record is anchored" not in out
    assert "## Nearest anchored records" not in out and "this answer does not" not in out


def test_19c_an_ambiguous_entity_seed_with_a_remapped_path_is_journaled_normalised(
    tmp_path, monkeypatch
):
    main = tmp_path
    feat = fake_worktree(main, "pkg/new.py")
    graph = main / "dup.json"
    graph.write_text(
        json.dumps(
            {
                "built_at_commit": "v1",
                "nodes": [
                    {
                        "id": f"d{i}",
                        "label": "dup()",
                        "norm_label": "dup()",
                        "file_type": "code",
                        "source_file": f"pkg/{name}.py",
                        "source_location": "L1",
                        "community": 1,
                    }
                    for i, name in enumerate(("m", "n"))
                ],
                "links": [],
            }
        )
    )
    ladder = seed_ladder.tolerate(
        [Seed(name="dup", file_path=str(feat / "pkg" / "ghost.py"))],
        GraphifyReader(graph),
        main,
        lambda: [Worktree(feat, "wt-feat")],
    )
    assert ladder.seed_paths == ["pkg/ghost.py"]
