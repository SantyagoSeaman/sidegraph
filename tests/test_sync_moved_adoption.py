"""The moved rung adopts only a real move.

An exact rebind miss makes the rung look the NAME up across the whole graph. Before this
fix it adopted any unique same-suffix hit once the old path was gone and committed history
showed "old absent from HEAD, new present in HEAD" -- which says nothing about whether the
new path was already there before. A deleted type whose name matched an old member in some
other file (``Priority`` vs ``.priority``) was rewritten onto that file.

Two rules close it (design/superpowers/specs/2026-10-01-moved-rung-false-adoption-design.md):

- D1: the hit's label must equal the descriptor's name, decoration stripped on both sides,
  case kept.
- D2: the new path must have been ADDED by the change that removed the old one, read from
  git and failing closed on any git answer that is not a clear yes.

Every test here builds a real git repository: the rules are about what git says.
"""

import json
import subprocess
from pathlib import Path

import pytest

import sidegraph.sync as sync_module
from sidegraph.engine.reader import GraphifyReader
from sidegraph.gitenv import git_env
from sidegraph.schema import Descriptor, Entity
from sidegraph.store import Store
from sidegraph.sync import PENDING_MOVES_KEY, rebind_entity, sync

_GIT_CONFIG = {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}


def _git(cwd: Path, *args: str, when: str | None = None) -> str:
    """``when`` pins author and committer dates, for tests that depend on commit order."""
    env = {**git_env(), **_GIT_CONFIG}
    if when is not None:
        env |= {"GIT_AUTHOR_DATE": when, "GIT_COMMITTER_DATE": when}
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, env=env)
    assert result.returncode == 0, f"git {args} failed: {result.stderr}"
    return result.stdout


def _configure(repo: Path) -> None:
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")


def _repo(root: Path, name: str = "repo") -> Path:
    """A fresh repository whose scratch directory ``.sg/`` (graph, store) is never tracked."""
    repo = root / name
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _configure(repo)
    (repo / ".git" / "info" / "exclude").write_text(".sg/\n")
    return repo


def _write(repo: Path, rel: str, text: str = "x\n") -> None:
    target = repo / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)


def _commit(repo: Path, message: str, when: str | None = None) -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message, when=when)


def _graph(repo: Path, nodes: list[tuple[str, str, str]]) -> GraphifyReader:
    """A graph of ``(node id, label, source file)`` rows, written into the repo's ``.sg/``."""
    rows = [
        {
            "id": node_id,
            "label": label,
            "norm_label": label,
            "file_type": "document" if file.endswith(".md") else "code",
            "source_file": file,
            "community": 1,
        }
        for node_id, label, file in nodes
    ]
    path = repo / ".sg" / "graph.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps({"built_at_commit": "vB", "nodes": rows, "links": []}))
    return GraphifyReader(path)


def _store(repo: Path, name: str, old_path: str) -> tuple[Store, Entity]:
    store = Store(repo / ".sg" / "t.db")
    entity = store.upsert_entity(
        Entity(
            canonical_name=name,
            descriptor=Descriptor(name=name, file_path=old_path),
            last_seen_node_id="old",
            last_seen_graph_version="vA",
        )
    )
    return store, entity


def _rebind(repo: Path, nodes, name: str, old_path: str, *, canonical_writes: bool = True):
    reader = _graph(repo, nodes)
    store, entity = _store(repo, name, old_path)
    outcome = rebind_entity(
        entity, store, reader, repo_root=repo, canonical_writes=canonical_writes
    )
    return outcome, store.get_entity(entity.entity_id)


def _assert_refused(outcome, kept, old_path: str) -> None:
    assert outcome.status == "orphaned"
    assert kept.descriptor.file_path == old_path  # the canonical descriptor is untouched
    assert kept.last_seen_node_id == "old"


# -- D1: a hit is a move only when its label IS the descriptor's name -------------------


def test_t1_a_deleted_type_is_not_adopted_as_a_move_to_an_older_member(tmp_path):
    """The field report: ``Priority`` (a type) is deleted with its file, and the only
    remaining node that canonicalizes to it is an old ``.priority`` member elsewhere."""
    repo = _repo(tmp_path)
    _write(repo, "A.swift", "enum Priority {}\n")
    _write(repo, "B.swift", "case priority\n")
    _commit(repo, "types")
    _git(repo, "rm", "-q", "A.swift")
    _commit(repo, "remove Priority")

    outcome, kept = _rebind(repo, [("n1", ".priority", "B.swift")], "Priority", "A.swift")

    _assert_refused(outcome, kept, "A.swift")


@pytest.mark.parametrize(
    ("stored_name", "label"),
    [(".record()", ".record()"), ("record()", ".record()"), ("record", ".record()")],
)
def test_t1b_a_real_move_of_a_decorated_symbol_is_adopted(tmp_path, stored_name, label):
    """Graphify renders methods ``.m()`` and the store keeps them as the graph spelled
    them, so D1 must strip BOTH sides: stripping the label only refuses ``.record()``."""
    repo = _repo(tmp_path)
    _write(repo, "A.swift", "func record() {}\n")
    _commit(repo, "initial")
    _git(repo, "mv", "A.swift", "C.swift")
    _commit(repo, "move A.swift to C.swift")

    outcome, kept = _rebind(repo, [("n1", label, "C.swift")], stored_name, "A.swift")

    assert outcome.status == "moved"
    assert outcome.detail == "A.swift -> C.swift"
    assert kept.descriptor.file_path == "C.swift"


def test_t4_a_case_mismatch_is_not_a_move_even_when_git_shows_a_move(tmp_path):
    """The file really moved in one commit, but the hit is ``foo`` for a stored ``Foo``:
    a different symbol, so D1 alone refuses it (D2 would pass)."""
    repo = _repo(tmp_path)
    _write(repo, "A.swift", "struct Foo {}\n")
    _commit(repo, "initial")
    _git(repo, "mv", "A.swift", "C.swift")
    _write(repo, "C.swift", "let foo = 1\n")
    _commit(repo, "move A.swift to C.swift, now holding foo")

    outcome, kept = _rebind(repo, [("n1", "foo", "C.swift")], "Foo", "A.swift")

    _assert_refused(outcome, kept, "A.swift")


def test_t9_an_uncommitted_case_mismatch_records_no_pending_move(tmp_path):
    """D1 runs BEFORE the ``moved_uncommitted`` return: a symbol that only loosely matches
    must not be remembered as a pending move, to be adopted once someone commits. B.swift is
    new in the working tree, so only D1 (not the already-in-HEAD fall-through) stops it."""
    repo = _repo(tmp_path)
    _write(repo, "A.swift", "enum Priority {}\n")
    _commit(repo, "type")
    (repo / "A.swift").unlink()  # uncommitted: HEAD still has A.swift
    _write(repo, "B.swift", "case priority\n")  # uncommitted: not in HEAD
    reader = _graph(repo, [("n1", ".priority", "B.swift")])
    store, entity = _store(repo, "Priority", "A.swift")

    report = sync(store, reader)

    assert [o.status for o in report.outcomes] == ["orphaned"]
    assert store.get_meta(PENDING_MOVES_KEY) is None
    assert store.get_entity(entity.entity_id).descriptor.file_path == "A.swift"


# -- D2: the new path must have been added by the change that removed the old one ---------


def test_t2_a_real_git_mv_is_adopted(tmp_path):
    repo = _repo(tmp_path)
    _write(repo, "A.swift", "struct Foo {}\n")
    _commit(repo, "initial")
    _git(repo, "mv", "A.swift", "C.swift")
    _commit(repo, "move A.swift to C.swift")

    outcome, kept = _rebind(repo, [("n1", "Foo", "C.swift")], "Foo", "A.swift")

    assert outcome.status == "moved" and outcome.detail == "A.swift -> C.swift"
    assert kept.descriptor.file_path == "C.swift"


def test_t3_a_type_deleted_while_an_existing_file_holds_the_same_name_is_not_a_move(tmp_path):
    """Exact case, so D1 passes: ``Priority`` now appears in a B.swift that was already
    there before A.swift was deleted. A collision, not a move."""
    repo = _repo(tmp_path)
    _write(repo, "A.swift", "enum Priority {}\n")
    _write(repo, "B.swift", "typealias Priority = Int\n")
    _commit(repo, "initial")
    _git(repo, "rm", "-q", "A.swift")
    _commit(repo, "remove A.swift")

    outcome, kept = _rebind(repo, [("n1", "Priority", "B.swift")], "Priority", "A.swift")

    _assert_refused(outcome, kept, "A.swift")


def test_a_same_named_file_added_long_after_the_deletion_is_not_a_move(tmp_path):
    """The new path must exist at the deleting change itself, not merely at HEAD."""
    repo = _repo(tmp_path)
    _write(repo, "A.swift", "struct Foo {}\n")
    _commit(repo, "initial")
    _git(repo, "rm", "-q", "A.swift")
    _commit(repo, "remove A.swift")
    _write(repo, "C.swift", "struct Foo {}\n")
    _commit(repo, "months later: an unrelated Foo")

    outcome, kept = _rebind(repo, [("n1", "Foo", "C.swift")], "Foo", "A.swift")

    _assert_refused(outcome, kept, "A.swift")


# A failure of any one of D2's git questions refuses the move. The wrapper lets every other
# git call (the committed-evidence check, HEAD) through, so ONLY D2's calls fail.


def _is_deletion_search(args: list[str]) -> bool:
    return args[0] == "log"


def _is_parent_probe(args: list[str]) -> bool:
    return args[0] == "rev-parse" and args[-1].endswith("^1")


def _is_ls_tree_of_parent(args: list[str]) -> bool:
    return args[0] == "ls-tree" and args[1].endswith("^1")


def _is_ls_tree_of_change(args: list[str]) -> bool:
    return args[0] == "ls-tree" and not args[1].endswith("^1") and args[1] != "HEAD"


@pytest.mark.parametrize(
    "target",
    [_is_deletion_search, _is_parent_probe, _is_ls_tree_of_parent, _is_ls_tree_of_change],
    ids=["log", "parent-probe", "ls-tree-parent", "ls-tree-change"],
)
@pytest.mark.parametrize("failure", ["raises", "exits-nonzero"])
def test_t5_when_only_d2s_git_calls_fail_the_move_is_refused(
    tmp_path, monkeypatch, target, failure
):
    repo = _repo(tmp_path)
    _write(repo, "A.swift", "struct Foo {}\n")
    _commit(repo, "initial")
    _git(repo, "mv", "A.swift", "C.swift")
    _commit(repo, "move A.swift to C.swift")
    real = sync_module._run_git
    hit = []

    def flaky(args, cwd, *, timeout=None):
        if not target(args):
            return real(args, cwd, timeout=timeout)
        hit.append(args)
        if failure == "raises":
            raise ValueError(f"could not run git ({' '.join(args)}): boom")
        return subprocess.CompletedProcess(["git", *args], 128, stdout="", stderr="fatal: boom")

    monkeypatch.setattr(sync_module, "_run_git", flaky)

    outcome, kept = _rebind(repo, [("n1", "Foo", "C.swift")], "Foo", "A.swift")

    _assert_refused(outcome, kept, "A.swift")
    assert hit, "the wrapper never saw the D2 call it was meant to break"


def test_t5_control_the_same_repo_adopts_with_a_wrapper_that_breaks_nothing(tmp_path, monkeypatch):
    """Without it, T5 could be red for a reason of its own (a broken wrapper)."""
    repo = _repo(tmp_path)
    _write(repo, "A.swift", "struct Foo {}\n")
    _commit(repo, "initial")
    _git(repo, "mv", "A.swift", "C.swift")
    _commit(repo, "move A.swift to C.swift")
    real = sync_module._run_git
    monkeypatch.setattr(
        sync_module, "_run_git", lambda args, cwd, *, timeout=None: real(args, cwd, timeout=timeout)
    )

    outcome, _ = _rebind(repo, [("n1", "Foo", "C.swift")], "Foo", "A.swift")

    assert outcome.status == "moved"


def _shallow_clone(origin: Path, dest: Path, depth: int = 1) -> Path:
    _git(dest.parent, "clone", "-q", f"--depth={depth}", f"file://{origin}", dest.name)
    _configure(dest)
    (dest / ".git" / "info" / "exclude").write_text(".sg/\n")
    return dest


def test_t6_a_depth_one_clone_cannot_prove_a_move_so_it_refuses(tmp_path):
    """In a depth-1 clone the boundary commit looks like a root: git shows no deletion at
    all. ``b/README.md`` pre-dates the deletion of ``a/README.md``; with no history to say
    so, the move is refused instead of adopted."""
    origin = _repo(tmp_path, "origin")
    _write(origin, "a/README.md", "# a\n")
    _write(origin, "b/README.md", "# b\n")
    _commit(origin, "initial")
    _git(origin, "rm", "-q", "a/README.md")
    _commit(origin, "drop a/README.md")
    clone = _shallow_clone(origin, tmp_path / "clone")

    outcome, kept = _rebind(clone, [("n1", "README.md", "b/README.md")], "README.md", "a/README.md")

    _assert_refused(outcome, kept, "a/README.md")


def test_t6_a_deeper_shallow_clone_still_adopts_a_move_it_can_see_whole(tmp_path):
    """The shallow boundary refuses only what it cannot see: depth 2 holds the deleting
    commit and its parent."""
    origin = _repo(tmp_path, "origin")
    _write(origin, "A.swift", "struct Foo {}\n")
    _commit(origin, "initial")
    _git(origin, "mv", "A.swift", "C.swift")
    _commit(origin, "move A.swift to C.swift")
    clone = _shallow_clone(origin, tmp_path / "clone", depth=2)

    outcome, _ = _rebind(clone, [("n1", "Foo", "C.swift")], "Foo", "A.swift")

    assert outcome.status == "moved"


def test_t6b_an_old_path_that_was_never_committed_is_not_a_move(tmp_path):
    """The old path (a gitignored directory, say) was never in git, so no commit deleted
    it and git cannot say the new path appeared with it."""
    repo = _repo(tmp_path)
    _write(repo, "b/README.md", "# b\n")
    _commit(repo, "initial")

    outcome, kept = _rebind(repo, [("n1", "README.md", "b/README.md")], "README.md", "a/README.md")

    _assert_refused(outcome, kept, "a/README.md")


def test_t10_a_branch_that_adds_then_deletes_in_two_commits_merged_no_ff_is_one_move(tmp_path):
    """Judged on the first-parent line, the merge is one change: C.swift appears and
    A.swift goes in the same step. Looking at the branch's own deleting commit instead
    would find C.swift already there and refuse."""
    repo = _repo(tmp_path)
    _write(repo, "A.swift", "struct Foo {}\n")
    _commit(repo, "initial")
    _git(repo, "switch", "-q", "-c", "feature")
    _write(repo, "C.swift", "struct Foo {}\n")
    _commit(repo, "add C.swift")
    _git(repo, "rm", "-q", "A.swift")
    _commit(repo, "remove A.swift")
    _git(repo, "switch", "-q", "main")
    _git(repo, "merge", "-q", "--no-ff", "-m", "merge feature", "feature")

    outcome, kept = _rebind(repo, [("n1", "Foo", "C.swift")], "Foo", "A.swift")

    assert outcome.status == "moved"
    assert kept.descriptor.file_path == "C.swift"


# -- the trust flag: an uncommitted move still has the old path in HEAD --------------------


def test_t7_trusted_dirty_tree_adopts_an_uncommitted_git_mv(tmp_path, monkeypatch):
    """With the flag on, the old path is still in HEAD, so D2 asks the other question: is
    the new path new to the working tree (absent from HEAD)? It is, so the move stands."""
    monkeypatch.setenv("SIDEGRAPH_TRUST_DIRTY_TREE", "on")
    repo = _repo(tmp_path)
    _write(repo, "A.swift", "struct Foo {}\n")
    _commit(repo, "initial")
    _git(repo, "mv", "A.swift", "C.swift")  # staged, never committed

    outcome, kept = _rebind(repo, [("n1", "Foo", "C.swift")], "Foo", "A.swift")

    assert outcome.status == "moved"
    assert kept.descriptor.file_path == "C.swift"


def test_t8_trusted_dirty_tree_refuses_a_new_path_that_already_exists_in_head(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("SIDEGRAPH_TRUST_DIRTY_TREE", "on")
    repo = _repo(tmp_path)
    _write(repo, "a/README.md", "# a\n")
    _write(repo, "b/README.md", "# b\n")
    _commit(repo, "initial")
    (repo / "a" / "README.md").unlink()  # uncommitted delete

    outcome, kept = _rebind(repo, [("n1", "README.md", "b/README.md")], "README.md", "a/README.md")

    _assert_refused(outcome, kept, "a/README.md")


def test_the_trust_flag_still_fails_closed_when_the_head_lookup_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("SIDEGRAPH_TRUST_DIRTY_TREE", "on")
    repo = _repo(tmp_path)
    _write(repo, "A.swift", "struct Foo {}\n")
    _commit(repo, "initial")
    _git(repo, "mv", "A.swift", "C.swift")
    real = sync_module._run_git

    def flaky(args, cwd, *, timeout=None):
        if args[0] == "ls-tree":
            return subprocess.CompletedProcess(["git", *args], 128, stdout="", stderr="fatal")
        return real(args, cwd, timeout=timeout)

    monkeypatch.setattr(sync_module, "_run_git", flaky)

    outcome, kept = _rebind(repo, [("n1", "Foo", "C.swift")], "Foo", "A.swift")

    _assert_refused(outcome, kept, "A.swift")


# -- index-only mode (a borrowed graph) never reaches D1 or D2 -----------------------------


def test_an_index_only_pass_never_asks_git_about_a_move(tmp_path, monkeypatch):
    """The moved rung abstains in an index-only pass (a borrowed graph): neither the
    committed-evidence check nor D2's history questions are asked, because they would read
    another checkout's history to decide a write that pass is not allowed to make."""
    repo = _repo(tmp_path)
    _write(repo, "A.swift", "struct Foo {}\n")
    _commit(repo, "initial")
    _git(repo, "mv", "A.swift", "C.swift")
    _commit(repo, "move A.swift to C.swift")
    calls: list[list[str]] = []
    real = sync_module._run_git

    def spy(args, cwd, *, timeout=None):
        calls.append(args)
        return real(args, cwd, timeout=timeout)

    monkeypatch.setattr(sync_module, "_run_git", spy)

    outcome, kept = _rebind(
        repo, [("n1", "Foo", "C.swift")], "Foo", "A.swift", canonical_writes=False
    )

    _assert_refused(outcome, kept, "A.swift")
    assert calls == []


def test_an_index_only_sync_never_reads_history_for_a_move(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    _write(repo, "A.swift", "struct Foo {}\n")
    _commit(repo, "initial")
    _git(repo, "mv", "A.swift", "C.swift")
    _commit(repo, "move A.swift to C.swift")
    reader = _graph(repo, [("n1", "Foo", "C.swift")])
    store, entity = _store(repo, "Foo", "A.swift")
    calls: list[list[str]] = []
    real = sync_module._run_git

    def spy(args, cwd, *, timeout=None):
        calls.append(args)
        return real(args, cwd, timeout=timeout)

    monkeypatch.setattr(sync_module, "_run_git", spy)

    sync(store, reader, canonical_writes=False)

    assert not [c for c in calls if c[0] in ("log", "ls-tree") or c[-1].endswith("^1")]
    assert store.get_entity(entity.entity_id).descriptor.file_path == "A.swift"


# -- glob characters in a path: D2's deletion search matches the old path literally ----------
#
# ``git log -- <path>`` reads ``[``, ``*`` and ``?`` as a glob; ``git ls-tree`` does not. A path
# like ``app/[id]/view.tsx`` (a Next.js route) would match ``app/d/view.tsx`` and find that file's
# deleting commit instead.


def _rename(repo: Path, old: str, new: str) -> None:
    """Move a file on disk only (``git mv`` would read a glob character as a pattern); the next
    commit's ``add -A`` records it."""
    (repo / new).parent.mkdir(parents=True, exist_ok=True)
    (repo / old).rename(repo / new)


def test_a_glob_in_the_old_path_does_not_borrow_another_files_deletion(tmp_path):
    """``[id]`` matches the ``d`` of ``app/d/view.tsx``. That file's move, with an unrelated
    ``Foo`` added beside it, is NOT the deletion of ``app/[id]/view.tsx``."""
    repo = _repo(tmp_path)
    _write(repo, "app/[id]/view.tsx", "export const Foo = 1\n")
    _write(repo, "app/d/view.tsx", "export const Bar = 'a long unrelated body of text'\n")
    _commit(repo, "initial")
    (repo / "app" / "[id]" / "view.tsx").unlink()
    _commit(repo, "remove the [id] route")
    _rename(repo, "app/d/view.tsx", "app/e/view.tsx")
    _write(repo, "app/e/view.tsx", "export const Bar = 'a long unrelated body of text'\nFoo\n")
    _commit(repo, "move the d route to e, and add an unrelated Foo")

    outcome, kept = _rebind(repo, [("n1", "Foo", "app/e/view.tsx")], "Foo", "app/[id]/view.tsx")

    _assert_refused(outcome, kept, "app/[id]/view.tsx")


@pytest.mark.parametrize(
    ("old", "new"),
    [
        # the new path matches the old path read as a glob, so a glob search pairs the two as a
        # rename and reports no deletion at all
        ("app/[slug]/page.tsx", "app/s/page.tsx"),
        ("a/x*.py", "a/xy.py"),
        ("a/what?.py", "a/whatx.py"),
        # control: nothing for a glob to pair, so these already work
        ("app/[slug]/page.tsx", "app/blog/page.tsx"),
        ("app/blog/page.tsx", "app/[slug]/page.tsx"),  # ls-tree is literal: no change needed
    ],
)
def test_a_real_move_of_a_path_with_glob_characters_is_adopted(tmp_path, old, new):
    repo = _repo(tmp_path)
    _write(repo, old, "export const Foo = 1\n")
    _commit(repo, "initial")
    _rename(repo, old, new)
    _commit(repo, "move it")

    outcome, kept = _rebind(repo, [("n1", "Foo", new)], "Foo", old)

    assert outcome.status == "moved"
    assert kept.descriptor.file_path == new


# -- a signed history: log.showSignature must not leak into the commit id ----------------------


def _sign_commits_with_ssh(repo: Path) -> None:
    """Sign every commit with a throwaway ssh key and make ``git log`` print the signature
    check. Skips the test when this machine cannot sign."""
    key = repo.parent / "signing_key"
    made = subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "test", "-f", str(key)],
        capture_output=True,
    )
    if made.returncode != 0:
        pytest.skip("ssh-keygen cannot make a signing key here")
    signers = repo.parent / "allowed_signers"
    signers.write_text(f"test@example.com {key.with_suffix('.pub').read_text()}")
    for name, value in [
        ("gpg.format", "ssh"),
        ("user.signingkey", str(key)),
        ("gpg.ssh.allowedSignersFile", str(signers)),
        ("commit.gpgsign", "true"),
        ("log.showSignature", "true"),
    ]:
        _git(repo, "config", name, value)
    probe = subprocess.run(
        ["git", "commit", "-q", "--allow-empty", "-m", "probe"],
        cwd=repo,
        capture_output=True,
        env={**git_env(), **_GIT_CONFIG},
    )
    if probe.returncode != 0:
        pytest.skip("git cannot sign a commit with ssh here")


def test_a_signed_history_with_log_show_signature_still_adopts_a_real_move(tmp_path):
    """With ``log.showSignature`` on, ``git log`` prints a ``Good "git" signature`` line before
    the commit id, which would make the answer more than one token and refuse every move."""
    repo = _repo(tmp_path)
    _sign_commits_with_ssh(repo)
    _write(repo, "A.swift", "struct Foo {}\n")
    _commit(repo, "initial")
    _git(repo, "mv", "A.swift", "C.swift")
    _commit(repo, "move A.swift to C.swift")
    shown = _git(repo, "log", "-1", "--format=%H")
    assert len(shown.split()) > 1, "the signature check did not reach git log's output"

    outcome, kept = _rebind(repo, [("n1", "Foo", "C.swift")], "Foo", "A.swift")

    assert outcome.status == "moved"
    assert kept.descriptor.file_path == "C.swift"


def test_d2s_deletion_search_is_literal_mainline_and_signature_free(tmp_path, monkeypatch):
    """Portable form of the three tests above: the command itself, where they need git
    features this machine may lack."""
    repo = _repo(tmp_path)
    _write(repo, "A.swift", "struct Foo {}\n")
    _commit(repo, "initial")
    _git(repo, "mv", "A.swift", "C.swift")
    _commit(repo, "move A.swift to C.swift")
    searches: list[list[str]] = []
    real = sync_module._run_git

    def spy(args, cwd, *, timeout=None):
        if args[0] == "log":
            searches.append(args)
        return real(args, cwd, timeout=timeout)

    monkeypatch.setattr(sync_module, "_run_git", spy)

    outcome, _ = _rebind(repo, [("n1", "Foo", "C.swift")], "Foo", "A.swift")

    assert outcome.status == "moved"
    (args,) = searches
    assert {"--first-parent", "-m", "--no-show-signature"} <= set(args)
    assert args[-2:] == ["--", ":(literal)A.swift"]


# -- the mainline, not a side branch, owns the deletion --------------------------------------


def test_a_side_branch_deleting_the_file_later_does_not_displace_the_mainline_move(tmp_path):
    """main moves A to C; a side branch, forked before the move, deletes A in a LATER commit
    and is merged ``--no-ff`` (resolved to main's tree). Plain ``log -m`` picks the side
    branch's commit, whose parent never held C; ``--first-parent`` picks main's move."""
    repo = _repo(tmp_path)
    _write(repo, "A.swift", "struct Foo {}\n")
    _commit(repo, "initial", when="2026-01-01T00:00:00")
    _git(repo, "switch", "-q", "-c", "side")
    (repo / "A.swift").unlink()
    _commit(repo, "side deletes A", when="2026-01-03T00:00:00")
    _git(repo, "switch", "-q", "main")
    _git(repo, "mv", "A.swift", "C.swift")
    _commit(repo, "main moves A to C", when="2026-01-02T00:00:00")
    _git(
        repo,
        "merge",
        "-q",
        "--no-ff",
        "-s",
        "ours",
        "-m",
        "merge side",
        "side",
        when="2026-01-04T00:00:00",
    )

    outcome, kept = _rebind(repo, [("n1", "Foo", "C.swift")], "Foo", "A.swift")

    assert outcome.status == "moved"
    assert kept.descriptor.file_path == "C.swift"


# -- an uncommitted collision is not a pending move --------------------------------------------


def test_an_uncommitted_delete_onto_a_path_already_in_head_is_orphaned_not_pending(tmp_path):
    """``a/README.md`` is deleted in the working tree while ``b/README.md`` is already in HEAD.
    Committing the delete could never make that a move (``b`` pre-dates it), so it must not be
    reported as one waiting for a commit."""
    repo = _repo(tmp_path)
    _write(repo, "a/README.md", "# a\n")
    _write(repo, "b/README.md", "# b\n")
    _commit(repo, "initial")
    (repo / "a" / "README.md").unlink()  # uncommitted
    reader = _graph(repo, [("n1", "README.md", "b/README.md")])
    store, entity = _store(repo, "README.md", "a/README.md")

    report = sync(store, reader)

    assert [o.status for o in report.outcomes] == ["orphaned"]
    assert store.get_meta(PENDING_MOVES_KEY) is None
    assert store.get_entity(entity.entity_id).descriptor.file_path == "a/README.md"


def _stage_a_move(repo: Path) -> None:
    _write(repo, "A.swift", "struct Foo {}\n")
    _commit(repo, "initial")
    _git(repo, "mv", "A.swift", "C.swift")  # staged, never committed


def test_a_genuinely_pending_move_is_still_reported_as_one(tmp_path):
    """Guard for the fall-through above: the new path is NOT in HEAD, so the move may yet be
    committed."""
    repo = _repo(tmp_path)
    _stage_a_move(repo)

    outcome, kept = _rebind(repo, [("n1", "Foo", "C.swift")], "Foo", "A.swift")

    assert outcome.status == "moved_uncommitted"
    assert kept.descriptor.file_path == "A.swift"


def test_a_pending_move_whose_head_lookup_fails_is_orphaned(tmp_path, monkeypatch):
    """Fail closed: when git cannot say whether the new path is in HEAD, nothing is remembered."""
    repo = _repo(tmp_path)
    _stage_a_move(repo)
    real = sync_module._run_git

    def flaky(args, cwd, *, timeout=None):
        if args[0] == "ls-tree":
            return subprocess.CompletedProcess(["git", *args], 128, stdout="", stderr="fatal")
        return real(args, cwd, timeout=timeout)

    monkeypatch.setattr(sync_module, "_run_git", flaky)

    outcome, kept = _rebind(repo, [("n1", "Foo", "C.swift")], "Foo", "A.swift")

    _assert_refused(outcome, kept, "A.swift")
