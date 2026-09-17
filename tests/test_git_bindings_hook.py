"""П1 -- ``sidegraph-prepare-commit-msg`` (design/superpowers/specs/
2026-08-07-git-bindings-design.md §1). Ledger rows G1-G4 + the hook side of G3/G9.

Fixtures are REAL temp git repos built by the tests (tempfile + subprocess git
init/config/commit), same convention as ``test_capture_propose.py``/
``test_doctor_code_drift.py``'s git-backed groups -- never a mocked git.

Every test ``monkeypatch.chdir``s into the fixture repo before invoking
``prepare_commit_msg_main`` -- the hook (like the real git hook it becomes) resolves
its store/git context from the process cwd, matching how git actually invokes
``prepare-commit-msg`` (cwd = the working tree's top level).
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from sidegraph import gitio
from sidegraph.cli import prepare_commit_msg_main
from sidegraph.schema import (
    AnchorBinding,
    Decision,
    DecisionKind,
    DecisionStatus,
    Descriptor,
    Entity,
    Provenance,
)
from sidegraph.store import Store


def _git(args: list[str], cwd) -> subprocess.CompletedProcess:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    assert result.returncode == 0, f"git {args} failed: {result.stderr}"
    return result


def _init_repo(root) -> object:
    repo = root / "repo"
    repo.mkdir()
    _git(["init", "-q"], repo)
    _git(["config", "user.email", "t@example.com"], repo)
    _git(["config", "user.name", "T"], repo)
    return repo


def _head(repo) -> str:
    return _git(["rev-parse", "HEAD"], repo).stdout.strip()


def _commit_file(repo, name: str, content: str, message: str = "commit") -> str:
    (repo / name).write_text(content)
    _git(["add", "-A"], repo)
    _git(["commit", "-q", "-m", message], repo)
    return _head(repo)


def _stage_file(repo, name: str, content: str) -> None:
    (repo / name).write_text(content)
    _git(["add", name], repo)


def _decision(store: Store, title: str, *, commit: str | None = None) -> Decision:
    d = Decision(
        title=title,
        kind=DecisionKind.ADR,
        status=DecisionStatus.ACCEPTED,
        context="c",
        choice="ch",
        valid_from=datetime.now(UTC),
        provenance=Provenance(source="human", commit=commit),
    )
    store.add_decision(d)
    return d


def _entity_with_path(store: Store, name: str, path: str) -> Entity:
    return store.get_or_create_entity(Descriptor(name=name, file_path=path))


def _message_file(tmp_path, content: str = "") -> str:
    p = tmp_path / "COMMIT_EDITMSG"
    p.write_text(content)
    return str(p)


# -- G1: rule (a) — provenance.commit == HEAD ------------------------------------------


def test_rule_a_offers_record_stamped_with_head_then_stops_after_next_commit(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path)
    head = _commit_file(repo, "f.txt", "1", "initial")
    store = Store(repo / ".sidegraph")
    _decision(store, "captured this session", commit=head)
    store.close()
    monkeypatch.chdir(repo)

    msg = _message_file(tmp_path)
    rc = prepare_commit_msg_main([msg, None])
    assert rc == 0
    content = Path(msg).read_text()
    assert "Sidegraph-Decision" in content
    assert "captured this session" in content

    # A NEW commit moves HEAD -- the same record no longer matches rule (a) (no staged
    # bindings either), so nothing is offered.
    _commit_file(repo, "f.txt", "2", "second")
    msg2 = _message_file(tmp_path)
    rc = prepare_commit_msg_main([msg2, None])
    assert rc == 0
    assert Path(msg2).read_text() == ""


def test_hook_to_blame_round_trip_via_the_actual_documented_gesture(tmp_path, monkeypatch):
    """Review Major 1: the block must be APPENDED, never prepended. A prepended block
    puts the (post-uncomment) trailer line directly adjacent to the subject -- no blank
    line separates them, so git's cleanup folds them into ONE paragraph (the subject's
    own), and its trailer heuristic only ever looks at the message's LAST paragraph.
    Empirically confirmed against real git (2.54, `commit.cleanup=strip` -- the default
    for an editor-invoked commit, which is exactly what a `prepare-commit-msg` hook with
    `source` absent implies): a prepended block's trailer round-trips to
    ``TRAILERS:[]``; an appended one round-trips correctly.

    This drives the documented path end to end, mirroring how a real interactive commit
    actually reaches the hook: git seeds ``COMMIT_EDITMSG`` with a blank line (where the
    subject goes) followed by ITS OWN comment scaffold -- never a truly empty file. The
    hook inserts its block relative to that; the human then does exactly two things,
    nothing more: types the subject on the file's first line (standard editor
    behavior -- cursor starts at line 1, wherever the hook left it), and strips the
    comment-char prefix off the one candidate line they want. The resulting file is
    committed for real (``--cleanup=strip``, matching an editor-invoked commit's
    default) and ``gitio.trailer_decision_ids`` is asked to resolve the ULID from the
    genuine commit git produced -- not a hand-built message string.
    """
    repo = _init_repo(tmp_path)
    head = _commit_file(repo, "f.txt", "1", "initial")
    store = Store(repo / ".sidegraph")
    d = _decision(store, "round trip decision", commit=head)
    store.close()
    monkeypatch.chdir(repo)

    msg_path = repo / "COMMIT_EDITMSG"
    # What git itself seeds COMMIT_EDITMSG with before ANY hook runs, for a real
    # interactive commit: a blank first line (where the subject goes) plus git's own
    # comment scaffold -- never truly empty. This is what makes prepend-vs-append
    # actually observable; against a genuinely empty original the two are byte-identical.
    msg_path.write_text(
        "\n# Please enter the commit message for your changes. Lines starting\n"
        "# with '#' will be ignored, and an empty message aborts the commit.\n"
    )
    rc = prepare_commit_msg_main([str(msg_path), None])
    assert rc == 0

    hook_output = msg_path.read_text()
    assert "Sidegraph-Decision" in hook_output

    # The documented gesture, and nothing more: type the subject on line 1 (wherever
    # the hook left it -- standard editor behavior), then strip the comment-char prefix
    # off the one candidate line you want. No reordering, no manual trailer authoring.
    lines = hook_output.splitlines()
    lines[0] = "Add new_fn"
    for i, line in enumerate(lines):
        stripped = line.lstrip()
        if "Sidegraph-Decision:" in line and stripped[:1] in ("#", ";"):
            lines[i] = stripped.lstrip(stripped[0]).strip()
    msg_path.write_text("\n".join(lines) + "\n")

    (repo / "f.txt").write_text("2")
    _git(["add", "-A"], repo)
    _git(["commit", "-q", "--cleanup=strip", "-F", str(msg_path)], repo)
    new_head = _head(repo)

    resolved = gitio.trailer_decision_ids(new_head, repo)
    assert resolved == [d.id]


# -- G2: rule (b) — staged-file bindings, filter-before-cap ------------------------------


_UNRELATED_COMMIT = "0" * 40  # never equals a real HEAD -- keeps rule (a)'s pre-П0
# timestamp fallback from also matching these fixtures' freshly-created records, so
# these tests isolate rule (b) cleanly.


def test_rule_b_offers_staged_decision_not_unstaged(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path)
    _commit_file(repo, "base.txt", "x", "initial")
    store = Store(repo / ".sidegraph")

    staged_entity = _entity_with_path(store, "staged_fn", "staged.py")
    unstaged_entity = _entity_with_path(store, "unstaged_fn", "unstaged.py")
    staged_decision = _decision(store, "anchored to staged file", commit=_UNRELATED_COMMIT)
    unstaged_decision = _decision(store, "anchored to unstaged file", commit=_UNRELATED_COMMIT)
    store.add_binding(
        AnchorBinding(record_id=staged_decision.id, entity_id=staged_entity.entity_id, tier=2)
    )
    store.add_binding(
        AnchorBinding(record_id=unstaged_decision.id, entity_id=unstaged_entity.entity_id, tier=2)
    )
    store.close()

    _stage_file(repo, "staged.py", "def f(): pass")
    (repo / "unstaged.py").write_text("def g(): pass")  # written but never `git add`-ed
    monkeypatch.chdir(repo)

    msg = _message_file(tmp_path)
    rc = prepare_commit_msg_main([msg, None])
    assert rc == 0
    content = Path(msg).read_text()
    assert "anchored to staged file" in content
    assert "anchored to unstaged file" not in content


def test_rule_b_filters_path_less_bindings_before_capping_at_five(tmp_path, monkeypatch):
    """Fixture: 3 path-less (Tier-0/1, abstract/domain entities) + 6 path-carrying
    bindings on the staged file -> exactly 5 offered (the 5 strongest by weight). The
    3 path-less-anchored decisions must never consume a cap slot -- they never even
    enter the candidate pool, since only entities matching a staged path are ever
    looked up. Red against a cap-before-filter implementation (a global cap applied
    before restricting to path-carrying entities could let the path-less ones win)."""
    repo = _init_repo(tmp_path)
    _commit_file(repo, "base.txt", "x", "initial")
    store = Store(repo / ".sidegraph")

    staged_entity = _entity_with_path(store, "staged_fn", "staged.py")

    # 3 path-less (abstract/domain-shaped) entities, each bound to its OWN decision with
    # a high weight -- must never be offered, and must never crowd out a path-carrying
    # candidate.
    for i in range(3):
        abstract_entity = store.get_or_create_entity(Descriptor(name=f"domain:d{i}"))
        d = _decision(store, f"path-less decision {i}", commit=_UNRELATED_COMMIT)
        store.add_binding(
            AnchorBinding(record_id=d.id, entity_id=abstract_entity.entity_id, tier=0, weight=1.0)
        )

    # 6 path-carrying bindings, all on the ONE staged entity, distinct weights so the
    # top-5-by-weight cut is unambiguous (drop weight=0.1 -> decision index 0).
    weights = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6]
    for i, w in enumerate(weights):
        d = _decision(store, f"path-carrying decision {i}", commit=_UNRELATED_COMMIT)
        store.add_binding(
            AnchorBinding(record_id=d.id, entity_id=staged_entity.entity_id, tier=2, weight=w)
        )
    store.close()

    _stage_file(repo, "staged.py", "def f(): pass")
    monkeypatch.chdir(repo)

    msg = _message_file(tmp_path)
    rc = prepare_commit_msg_main([msg, None])
    assert rc == 0
    content = Path(msg).read_text()

    for i in range(3):
        assert f"path-less decision {i}" not in content
    # weakest (weight 0.1, index 0) dropped; the other 5 survive.
    assert "path-carrying decision 0" not in content
    for i in range(1, 6):
        assert f"path-carrying decision {i}" in content


# -- G3: no-op sources, store-less repo, empty repo (degrades to (b)-only) ---------------


@pytest.mark.parametrize("source", ["message", "merge", "squash", "commit"])
def test_noop_on_non_plain_commit_sources(tmp_path, monkeypatch, source):
    repo = _init_repo(tmp_path)
    head = _commit_file(repo, "f.txt", "1", "initial")
    store = Store(repo / ".sidegraph")
    _decision(store, "should never appear", commit=head)
    store.close()
    monkeypatch.chdir(repo)

    msg = _message_file(tmp_path, "original message\n")
    rc = prepare_commit_msg_main([msg, source])
    assert rc == 0
    assert Path(msg).read_text() == "original message\n"


def test_noop_store_less_repo(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path)
    _commit_file(repo, "f.txt", "1", "initial")
    # no .sidegraph/ at all
    monkeypatch.chdir(repo)
    msg = _message_file(tmp_path)
    rc = prepare_commit_msg_main([msg, None])
    assert rc == 0
    assert Path(msg).read_text() == ""


def test_empty_repo_degrades_to_rule_b_only(tmp_path, monkeypatch):
    """`git rev-parse HEAD` fails before the first commit -- rule (a) and its fallback
    are skipped entirely; rule (b) (`git diff --cached`) still works and still offers a
    staged-file candidate (review M7)."""
    repo = _init_repo(tmp_path)
    store = Store(repo / ".sidegraph")
    entity = _entity_with_path(store, "fn", "staged.py")
    d = _decision(store, "pre-first-commit decision")
    store.add_binding(AnchorBinding(record_id=d.id, entity_id=entity.entity_id, tier=2))
    store.close()

    assert gitio.head_commit(repo) is None  # sanity: genuinely no commits yet

    _stage_file(repo, "staged.py", "def f(): pass")
    monkeypatch.chdir(repo)
    msg = _message_file(tmp_path)
    rc = prepare_commit_msg_main([msg, None])
    assert rc == 0
    assert "pre-first-commit decision" in Path(msg).read_text()


# -- G3b: never-stall against a concurrent writer's lock ---------------------------------


def test_hook_never_stalls_against_a_concurrent_writer(tmp_path, monkeypatch):
    """Review Major 2: a bare held ``BEGIN IMMEDIATE`` only takes SQLite's RESERVED
    lock, which is compatible with a fresh reader's SHARED lock -- it does NOT block a
    plain ``Store()`` open whose index is already current (measured: 0ms). The
    fixture must force the COMPETING case §0 actually guards against: a plain,
    writable ``Store()`` open that itself needs to WRITE (index reload on a stale
    digest -- ``_refresh_freshness`` -> ``_reload_index_from_canonical``, its own
    ``BEGIN IMMEDIATE``), which genuinely conflicts with an already-held RESERVED lock
    and stalls for SQLite's default 5s busy-timeout (measured: >30s here, since the
    reload path itself raises its busy_timeout to 30s for the rebuild).

    So: bump every canonical file's mtime past the index's persisted digest (forces the
    NEXT ``Store()`` open down the reload path), hold ``BEGIN IMMEDIATE`` from another
    connection, then assert the ACTUAL hook (ro-open, never rebuilds per §0) still
    returns well under the 2s budget -- it never even touches the write path the stale
    digest would otherwise force."""
    repo = _init_repo(tmp_path)
    head = _commit_file(repo, "f.txt", "1", "initial")
    store = Store(repo / ".sidegraph")
    d = _decision(store, "irrelevant", commit=head)
    store.close()
    monkeypatch.chdir(repo)

    # Force a digest mismatch: bump the canonical decision file's mtime so the next
    # `Store()` open's `_refresh_freshness` sees stale != current and must reload
    # (a real write) rather than take the read-only fast path.
    decision_path = repo / ".sidegraph" / "decisions" / f"{d.id}.json"
    later = time.time() + 5
    os.utime(decision_path, (later, later))

    writer = sqlite3.connect(str(repo / ".sidegraph" / "index.db"))
    writer.execute("BEGIN IMMEDIATE")
    writer.execute("INSERT INTO meta (key, value) VALUES ('probe', 'x')")
    try:
        msg = _message_file(tmp_path)
        start = time.monotonic()
        rc = prepare_commit_msg_main([msg, None])
        elapsed = time.monotonic() - start
        assert rc == 0
        assert elapsed < 2.0, f"hook took {elapsed:.2f}s under a held writer lock"
    finally:
        writer.rollback()
        writer.close()


# -- G4: comment-char resolution ----------------------------------------------------------


def test_comment_char_default_hash(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path)
    head = _commit_file(repo, "f.txt", "1", "initial")
    store = Store(repo / ".sidegraph")
    _decision(store, "d1", commit=head)
    store.close()
    monkeypatch.chdir(repo)

    msg = _message_file(tmp_path)
    prepare_commit_msg_main([msg, None])
    lines = Path(msg).read_text().strip("\n").splitlines()
    assert lines[0].startswith("# ")


def test_comment_char_custom_semicolon(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path)
    _git(["config", "core.commentChar", ";"], repo)
    head = _commit_file(repo, "f.txt", "1", "initial")
    store = Store(repo / ".sidegraph")
    _decision(store, "d1", commit=head)
    store.close()
    monkeypatch.chdir(repo)

    msg = _message_file(tmp_path)
    prepare_commit_msg_main([msg, None])
    lines = Path(msg).read_text().strip("\n").splitlines()
    assert lines[0].startswith("; ")
    assert not lines[0].startswith("# ")


def test_comment_char_auto_writes_nothing_even_with_candidates(tmp_path, monkeypatch):
    """core.commentChar=auto + a commit.template with a leading '#' line: git resolves
    `auto` to a NON-'#' char before the hook runs, but `git config --get
    core.commentChar` still echoes the literal string "auto" -- unknowable from here.
    A hardcoded-'#' or resolve-auto-to-'#' implementation would write '#'-prefixed
    lines here that then survive uncommented (measured, design round 2)."""
    repo = _init_repo(tmp_path)
    _git(["config", "core.commentChar", "auto"], repo)
    template = repo / "template.txt"
    template.write_text("# leading hash forces git to pick a different auto char\n")
    _git(["config", "commit.template", str(template)], repo)
    head = _commit_file(repo, "f.txt", "1", "initial")
    store = Store(repo / ".sidegraph")
    _decision(store, "d1", commit=head)
    store.close()

    assert gitio.resolved_comment_char(repo) is None  # sanity on the resolver itself

    monkeypatch.chdir(repo)
    msg = _message_file(tmp_path, "original\n")
    rc = prepare_commit_msg_main([msg, None])
    assert rc == 0
    assert Path(msg).read_text() == "original\n"


def test_empty_candidate_set_writes_nothing(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path)
    _commit_file(repo, "f.txt", "1", "initial")
    store = Store(repo / ".sidegraph")  # store exists but has no records at all
    store.close()
    monkeypatch.chdir(repo)

    msg = _message_file(tmp_path, "original\n")
    rc = prepare_commit_msg_main([msg, None])
    assert rc == 0
    assert Path(msg).read_text() == "original\n"
