"""The hook-path drift refresh remembers each capture-commit diff whose inputs did not change.

Counts go through a wrapper around ``doctor._run_git``: a *batch* diff is a ``git diff`` with a
``--`` pathspec, the *moved* diff is the one ``H1 H2`` diff a HEAD move costs. Every test also
compares the memoized result with an unmemoized scan, because the memo is only allowed to make the
refresh faster, never different.
# see design/superpowers/specs/2026-11-15-sessionstart-drift-memo-design.md
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from sidegraph import doctor as doctor_mod
from sidegraph.drift_memo import DriftDiffMemo, entry_key, is_object_id
from sidegraph.retrieval import DRIFT_CACHE_KEY
from sidegraph.schema import AnchorBinding, Descriptor, Entity
from sidegraph.store import Store
from sidegraph.store_layout import DRIFT_MEMO_KEY
from sidegraph.sync import refresh_code_drift_cache
from tests.drift_groups_support import GroupRepo, git
from tests.test_sync_drift_cache import _commit_file, _git, _head, _stamped_decision


@pytest.fixture
def git_repo(tmp_path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-q"], cwd=repo)
    _git(["config", "user.email", "test@example.com"], cwd=repo)
    _git(["config", "user.name", "Test"], cwd=repo)
    return repo


@pytest.fixture
def diffs(monkeypatch) -> list[list[str]]:
    """Every ``git diff`` the doctor module starts, as its argument list."""
    calls: list[list[str]] = []
    real = doctor_mod._run_git

    def counting(args, cwd, **kw):
        if args and args[0] == "diff":
            calls.append(list(args))
        return real(args, cwd, **kw)

    monkeypatch.setattr(doctor_mod, "_run_git", counting)
    return calls


def _batches(calls: list[list[str]]) -> list[list[str]]:
    return [c for c in calls if "--" in c]


def _moved(calls: list[list[str]]) -> list[list[str]]:
    return [c for c in calls if "--" not in c]


def _memo(store: Store) -> dict:
    return json.loads(store.get_meta(DRIFT_MEMO_KEY))


def _by_commit(store: Store) -> dict:
    return json.loads(store.get_meta(DRIFT_CACHE_KEY))["by_commit"]


def _reference_by_commit(store: Store) -> dict[str, list[str]]:
    """What an unmemoized scan of the same state says, shaped like the cache's ``by_commit``."""
    scan = doctor_mod.scan_code_drift(store.path)
    return {b.commit: sorted(e.record_id for e in b.entries) for b in scan.batches if not b.failed}


@pytest.fixture
def three(git_repo):
    """Three records, each captured at its own commit and anchored to its own file, then one
    commit that drifts ``a.py``. Returns (repo, store, {name: (commit, decision)})."""
    # The store stays out of every commit, so a checkout of an older commit keeps the records.
    (git_repo / ".git" / "info" / "exclude").write_text(".sidegraph/\n", encoding="utf-8")
    commits = {}
    for name in ("a", "b", "c"):
        commits[name] = _commit_file(git_repo, f"{name}.py", f"{name}\n", f"add {name}")
    store = Store(git_repo / ".sidegraph")
    records = {
        name: (commits[name], _stamped_decision(store, commits[name], f"{name}.py", name.upper()))
        for name in commits
    }
    _commit_file(git_repo, "a.py", "changed\n", "drift a")
    return git_repo, store, records


def test_same_head_second_refresh_runs_no_diff(three, diffs):
    """Red target: unfixed code reruns one batch diff per capture commit on every refresh."""
    _repo, store, records = three
    assert refresh_code_drift_cache(store) == 1
    assert len(_batches(diffs)) == 3
    before = _by_commit(store)
    diffs.clear()
    assert refresh_code_drift_cache(store) == 1
    assert diffs == []
    assert _by_commit(store) == before == _reference_by_commit(store)
    assert before[records["a"][0]] == [records["a"][1].id]


def test_reused_entries_are_carried_into_the_next_memo(three, diffs):
    """Red target: a lookup that serves an entry without keeping it leaves the published memo
    without those keys, so every second start reruns every diff."""
    _repo, store, _records = three
    refresh_code_drift_cache(store)
    refresh_code_drift_cache(store)
    assert len(_memo(store)["entries"]) == 3
    diffs.clear()
    refresh_code_drift_cache(store)
    assert diffs == []


def test_add_anchors_reruns_only_that_records_batch(three, diffs):
    """Red target: a memo that is a no-op whenever HEAD is unchanged (the discarded design)
    would keep the old answer for a record whose bindings changed."""
    repo, store, records = three
    refresh_code_drift_cache(store)
    entity = store.upsert_entity(
        Entity(canonical_name="C", descriptor=Descriptor(name="C", file_path="c.py"))
    )
    commit_b, decision_b = records["b"]
    store.add_binding(
        AnchorBinding(record_id=decision_b.id, entity_id=entity.entity_id, tier=2, status="live")
    )
    diffs.clear()
    refresh_code_drift_cache(store)
    assert [c[c.index("--") + 1 :] for c in _batches(diffs)] == [["b.py", "c.py"]]
    assert _moved(diffs) == []
    assert _by_commit(store)[commit_b] == [decision_b.id]  # c.py was added after b's capture
    assert _by_commit(store) == _reference_by_commit(store)


def test_new_commit_reruns_only_batches_naming_the_changed_path(three, diffs):
    """Red target: unfixed code reruns every batch after a commit, and has no moved diff."""
    repo, store, records = three
    refresh_code_drift_cache(store)
    old_head = _memo(store)["head"]
    _commit_file(repo, "b.py", "changed\n", "drift b")
    diffs.clear()
    assert refresh_code_drift_cache(store) == 2
    assert len(_batches(diffs)) == 1 and _batches(diffs)[0][-1] == "b.py"
    [moved] = _moved(diffs)
    assert moved[-2:] == [old_head, _head(repo)]
    assert "--no-renames" in moved
    assert _by_commit(store) == _reference_by_commit(store)
    assert _memo(store)["head"] == _head(repo)


def test_unrelated_branch_and_backward_move_match_an_unmemoized_scan(three, diffs):
    """Guard (green before and after, declared): moving HEAD to a side branch and back, and
    backward, leaves the cache equal to an unmemoized scan."""
    repo, store, _records = three
    refresh_code_drift_cache(store)
    first = _head(repo)
    _git(["checkout", "-q", "-b", "side", "HEAD~2"], cwd=repo)
    _commit_file(repo, "other.py", "x\n", "unrelated")
    assert refresh_code_drift_cache(store) is not None
    assert _by_commit(store) == _reference_by_commit(store)
    _git(["checkout", "-q", "--detach", first], cwd=repo)
    refresh_code_drift_cache(store)
    assert _by_commit(store) == _reference_by_commit(store)
    _git(["checkout", "-q", "--detach", "HEAD~3"], cwd=repo)
    refresh_code_drift_cache(store)
    assert _by_commit(store) == _reference_by_commit(store)


def test_unreachable_memo_head_reruns_everything(three, diffs):
    """Guard (declared): a memo whose head no longer exists reuses nothing, even with entries
    that would flip the answer if they were used."""
    _repo, store, _records = three
    refresh_code_drift_cache(store)
    memo = _memo(store)
    memo["head"] = "1" * 40
    memo["entries"] = {key: ["a.py", "b.py", "c.py"] for key in memo["entries"]}
    store.set_meta(DRIFT_MEMO_KEY, json.dumps(memo))
    diffs.clear()
    refresh_code_drift_cache(store)
    assert len(_batches(diffs)) == 3
    assert _by_commit(store) == _reference_by_commit(store)


@pytest.mark.parametrize(
    "damage",
    [
        lambda m: "not json at all",
        lambda m: "[]",
        lambda m: json.dumps({**m, "v": 2}),
        lambda m: json.dumps({**m, "head": None}),
        lambda m: json.dumps({**m, "entries": "oops"}),
        lambda m: json.dumps({**m, "entries": {k: 5 for k in m["entries"]}}),
        lambda m: json.dumps({**m, "entries": {k: [1, 2] for k in m["entries"]}}),
        lambda m: json.dumps({**m, "config": {"x": 1}}),
        lambda m: json.dumps({**m, "head": "abc"}),
        lambda m: json.dumps({**m, "head": m["head"].upper()}),
    ],
)
def test_malformed_memo_is_ignored(three, diffs, damage):
    """Guard (declared): any unreadable or misshapen memo value is ignored and the refresh
    produces the same cache as an unmemoized one."""
    _repo, store, _records = three
    refresh_code_drift_cache(store)
    store.set_meta(DRIFT_MEMO_KEY, damage(_memo(store)))
    diffs.clear()
    assert refresh_code_drift_cache(store) == 1
    assert len(_batches(diffs)) == 3
    assert _by_commit(store) == _reference_by_commit(store)
    assert _memo(store)["v"] == 1  # and the next scan republishes a good memo


def test_failed_batch_is_not_memoized(three, diffs, monkeypatch):
    """Red target: a naive memo stores the failed batch's empty answer and serves it as truth."""
    _repo, store, records = three
    commit_a = records["a"][0]
    real = doctor_mod._git_diff_name_only

    def failing(repo_root, commit, paths, **kw):
        if commit == commit_a:
            raise ValueError("simulated diff failure")
        return real(repo_root, commit, paths, **kw)

    monkeypatch.setattr(doctor_mod, "_git_diff_name_only", failing)
    refresh_code_drift_cache(store)
    assert entry_key(commit_a, ["a.py"], False) not in _memo(store)["entries"]
    monkeypatch.setattr(doctor_mod, "_git_diff_name_only", real)
    diffs.clear()
    assert refresh_code_drift_cache(store) == 1
    assert [c[c.index("--") + 1 :] for c in _batches(diffs)] == [["a.py"]]


def test_changed_diff_renames_config_discards_the_memo(three, diffs):
    """Red target: a memo without the config field keeps serving answers computed under the
    old rename setting."""
    repo, store, _records = three
    refresh_code_drift_cache(store)
    assert _memo(store)["config"] == []
    _git(["config", "diff.renames", "copies"], cwd=repo)
    diffs.clear()
    refresh_code_drift_cache(store)
    assert len(_batches(diffs)) == 3
    assert _memo(store)["config"] == ["diff.renames\ncopies"]
    diffs.clear()
    refresh_code_drift_cache(store)
    assert diffs == []


# --- the exactness property ---------------------------------------------------------------


def _shape(scan) -> list[tuple]:
    return [(b.commit, b.failed, [tuple(e) for e in b.entries]) for b in scan.batches]


def _history(repo: GroupRepo) -> dict[str, list[str]]:
    """Main history with edits, a rename, a deletion and a re-add; a side branch off it."""
    main = [
        repo.commit({"a.py": "a\n", "b.py": "b\n" * 20, "x/y.py": "y\n", "x/z.py": "z\n"}, "c0"),
        repo.commit({"d.py": "d\n", "e.py": "e\n"}, "c1"),
        repo.commit({"a.py": "a2\n"}, "c2"),
        repo.commit({"b.py": None, "b2.py": "b\n" * 20}, "rename b -> b2"),
        repo.commit({"d.py": None}, "delete d"),
        repo.commit({"x/y.py": "y2\n"}, "c5"),
        repo.commit({"e.py": "e2\n", "f.py": "f\n"}, "c6"),
        repo.commit({"d.py": "d again\n"}, "re-add d"),
        repo.commit({"x/z.py": None, "x/w.py": "z\n"}, "rename in a directory"),
        repo.commit({"a.py": "a3\n"}, "c9"),
    ]
    git(repo.root, "checkout", "-q", "-b", "side", main[3])
    side = [
        repo.commit({"x/y.py": "side\n"}, "side edit"),
        repo.commit({"e.py": None, "e3.py": "e2\n"}, "side rename"),
    ]
    return {"main": main, "side": side}


def test_memoized_scan_equals_unmemoized_across_head_moves(tmp_path, diffs):
    """Guard (declared; the core property): over a sequence of HEAD moves on a repo with renames,
    deletions and directory anchors, in both deletion modes, the memoized scan returns exactly
    the batches an unmemoized scan does at every step, and it does reuse something."""
    repo = GroupRepo(tmp_path)
    shas = _history(repo)
    main, side = shas["main"], shas["side"]
    repo.record(1, commit=main[0], files=["a.py", "x/y.py"])
    repo.record(2, commit=main[0], files=["b.py"])
    repo.record(3, commit=main[1], files=["d.py", "e.py"])
    repo.record(4, commit=main[2], files=["b2.py"])
    repo.record(5, commit=main[3], files=["x/z.py"])
    repo.record(6, commit=main[5], files=["x/y.py", "f.py"])
    repo.record(7, commit=main[6], files=["a.py"])
    repo.record(8, commit=main[8], files=["x/w.py", "d.py"])
    repo.record(9, commit=main[1], files=["e3.py"])
    # forward 1, forward 5, backward 3, side branch, back, before every capture, forward again
    sequence = [3, 4, 9, 6, "side", 9, 0, 9]
    memo_raw: str | None = None
    reused_somewhere = False
    ran_somewhere = False
    for step, target in enumerate(sequence):
        sha = side[-1] if target == "side" else main[target]
        git(repo.root, "checkout", "-q", "--detach", sha)
        if step < 4:
            repo.write_index()  # computed statuses: deletions stay in the scan
        else:
            (repo.store / "index.db").unlink(missing_ok=True)  # uncomputed: deletions excluded
        decisions, bindings, entities = repo.records()
        reference = doctor_mod._scan_code_drift(
            decisions, bindings, entities, repo.store, repo.root, resolve_head=True
        )
        memo = DriftDiffMemo(memo_raw)
        diffs.clear()
        got = doctor_mod._scan_code_drift(
            decisions, bindings, entities, repo.store, repo.root, resolve_head=True, memo=memo
        )
        ran = len(_batches(diffs))
        assert _shape(got) == _shape(reference), f"step {step} -> {target}"
        assert got.head == reference.head == sha
        assert memo.next is not None
        if memo_raw is not None:
            reused_somewhere |= ran < len(reference.batches)
            ran_somewhere |= ran > 0
        memo_raw = json.dumps(memo.next)
    assert reused_somewhere and ran_somewhere  # the test would be vacuous otherwise


# --- exactness rules the property test cannot reach: each scenario is a history on which the
# --- memo, with one rule removed, returns a different answer than an unmemoized scan ---------


def _scan(repo: GroupRepo, memo: DriftDiffMemo | None = None):
    decisions, bindings, entities = repo.records()
    return doctor_mod._scan_code_drift(
        decisions, bindings, entities, repo.store, repo.root, resolve_head=True, memo=memo
    )


def _memo_pass(repo: GroupRepo, raw: str | None) -> tuple[list, list, str]:
    """(memoized shape, unmemoized shape, the memo this scan publishes)."""
    reference = _shape(_scan(repo))
    memo = DriftDiffMemo(raw)
    got = _shape(_scan(repo, memo))
    assert memo.next is not None
    return got, reference, json.dumps(memo.next)


def _rename_limit_repo(tmp_path: Path) -> GroupRepo:
    """Four files renamed with small edits: whether a rename is paired (and so whether the old
    name is listed) depends on ``diff.renameLimit``."""
    repo = GroupRepo(tmp_path)
    base = {f"old{i}.py": "".join(f"{i} line {j}\n" for j in range(100)) for i in range(4)}
    first = repo.commit(base, "base")
    repo.record(1, commit=first, files=["old0.py"])
    repo.record(2, commit=first, files=[*base, *(f"new{i}.py" for i in range(4))])
    repo.write_index()
    renamed = {
        f"new{i}.py": base[f"old{i}.py"].replace(f"{i} line 0", f"{i} altered") for i in range(4)
    }
    repo.commit({**dict.fromkeys(base), **renamed}, "renames")
    return repo


def _gitlink_repo(tmp_path: Path) -> GroupRepo:
    """A record anchored to a submodule path whose gitlink moved after the capture."""
    repo = GroupRepo(tmp_path)
    inner = repo.commit({"dummy": "x\n"})
    git(repo.root, "update-index", "--add", "--cacheinfo", f"160000,{inner},sub")
    git(repo.root, "commit", "-qm", "add gitlink")
    capture = repo.head()
    repo.record(1, commit=capture, files=["sub"])
    repo.write_index()
    moved = repo.commit({"dummy": "y\n"})
    git(repo.root, "update-index", "--cacheinfo", f"160000,{moved},sub")
    git(repo.root, "commit", "-qm", "move gitlink")
    return repo


def _named_gitlink_repo(tmp_path: Path) -> GroupRepo:
    """The gitlink repo with a committed ``.gitmodules`` that names the submodule."""
    repo = _gitlink_repo(tmp_path)
    repo.commit({".gitmodules": '[submodule "sub"]\n\tpath = sub\n\turl = ./inner\n'}, "name it")
    return repo


@pytest.mark.parametrize(
    ("build", "setting"),
    [
        (_rename_limit_repo, ("diff.renameLimit", "1")),
        (_gitlink_repo, ("diff.ignoreSubmodules", "all")),
        (_named_gitlink_repo, ("submodule.sub.ignore", "all")),
    ],
    ids=["renameLimit", "ignoreSubmodules", "submodule.ignore"],
)
def test_changed_git_diff_setting_discards_the_memo(tmp_path, build, setting):
    """Red target: a memo fingerprinted on ``diff.renames`` alone keeps serving the answer given
    under the old ``diff.renameLimit``, ``diff.ignoreSubmodules`` or ``submodule.<name>.ignore``."""
    repo = build(tmp_path)
    _, _, raw = _memo_pass(repo, None)
    before = _shape(_scan(repo))
    git(repo.root, "config", *setting)
    got, reference, _ = _memo_pass(repo, raw)
    assert reference != before  # the setting really changes git's answer, or this proves nothing
    assert got == reference


def test_committed_gitmodules_change_discards_the_memo(tmp_path):
    """Red target: a memo that checks git configuration only keeps a gitlink's answer after a
    committed ``.gitmodules`` with ``ignore = all`` changed what git reports for it."""
    repo = _gitlink_repo(tmp_path)
    _, _, raw = _memo_pass(repo, None)
    before = _shape(_scan(repo))
    repo.commit(
        {".gitmodules": '[submodule "sub"]\n\tpath = sub\n\turl = ./inner\n\tignore = all\n'},
        "ignore the submodule",
    )
    got, reference, _ = _memo_pass(repo, raw)
    assert reference != before
    assert got == reference


_NAMED = '[submodule "sub"]\n\tpath = sub\n\turl = ./inner\n'


@pytest.mark.parametrize(
    ("committed", "edited"),
    [
        (None, _NAMED + "\tignore = all\n"),  # untracked file appears
        (_NAMED, _NAMED + "\tignore = all\n"),  # tracked file gains a rule
        (_NAMED + "\tignore = all\n", _NAMED),  # tracked file loses it: drift was hidden
    ],
    ids=["untracked", "rule-added", "rule-removed"],
)
def test_an_uncommitted_gitmodules_edit_discards_the_memo(tmp_path, committed, edited):
    """Red target: a fingerprint without the work-tree ``.gitmodules`` keeps serving the answer
    given before an uncommitted ``ignore`` edit at an unchanged HEAD, because git reads that file
    from disk and no commit or configuration value moved."""
    repo = _gitlink_repo(tmp_path)
    if committed is not None:
        repo.commit({".gitmodules": committed}, "name it")
    _, _, raw = _memo_pass(repo, None)
    before = _shape(_scan(repo))
    (repo.root / ".gitmodules").write_text(edited, encoding="utf-8")
    got, reference, _ = _memo_pass(repo, raw)
    assert reference != before  # the edit really changes git's answer, or this proves nothing
    assert got == reference


def test_an_unchanged_gitmodules_keeps_the_memo(tmp_path, diffs):
    """The fingerprint must not cost reuse: the same ``.gitmodules`` at the same HEAD runs no
    diff."""
    repo = _named_gitlink_repo(tmp_path)
    _, _, raw = _memo_pass(repo, None)
    diffs.clear()
    _scan(repo, DriftDiffMemo(raw))
    assert _batches(diffs) == []


@pytest.mark.parametrize("head", ["HEAD", "--cached", "OUTPUT", "abc", "", "A" * 40])
def test_a_memo_head_that_is_not_an_object_id_discards_the_memo(three, diffs, tmp_path, head):
    """Red target: a memo head is handed to git as a revision, so ``HEAD`` compares HEAD with
    itself and reuses everything, and ``--output=<path>`` makes git write that file."""
    repo, store, _records = three
    refresh_code_drift_cache(store)
    _commit_file(repo, "b.py", "changed\n", "drift b")
    out = tmp_path / "written-by-git.txt"
    memo = _memo(store)
    memo["head"] = f"--output={out}" if head == "OUTPUT" else head
    store.set_meta(DRIFT_MEMO_KEY, json.dumps(memo))
    diffs.clear()
    refresh_code_drift_cache(store)
    assert _moved(diffs) == []
    assert len(_batches(diffs)) == 3
    assert not out.exists()
    assert _by_commit(store) == _reference_by_commit(store)


def test_a_capture_ref_that_can_move_is_never_memoized(tmp_path):
    """Red target: a capture commit that is a tag is memoized as if immutable, so moving the tag
    at the same HEAD keeps the old answer."""
    repo = GroupRepo(tmp_path)
    first = repo.commit({"a.py": "a\n"}, "c0")
    git(repo.root, "tag", "capture", first)
    repo.record(1, commit="capture", files=["a.py"])
    repo.write_index()
    head = repo.commit({"a.py": "b\n"}, "drift")
    memo = DriftDiffMemo(None)
    before = _shape(_scan(repo, memo))
    assert memo.next is not None and memo.next["entries"] == {}
    git(repo.root, "tag", "-f", "capture", head)
    got, reference, _ = _memo_pass(repo, json.dumps(memo.next))
    assert reference != before
    assert got == reference


def test_an_anchor_that_cannot_be_encoded_fails_its_batch_like_the_uncached_scan(tmp_path):
    """Red target: the batch key is built outside the batch's failure boundary, so a lone
    surrogate in an anchor raises out of the memoized scan."""
    repo = GroupRepo(tmp_path)
    first = repo.commit({"a.py": "a\n"}, "c0")
    repo.record(1, commit=first, files=["\ud800.py"])
    repo.write_index()
    reference = _shape(_scan(repo))
    assert reference[0][1] is True  # the uncached scan marks the batch failed
    assert _shape(_scan(repo, DriftDiffMemo(None))) == reference


def test_entry_key_accepts_any_string_an_anchor_can_hold():
    """Red target: a key built with ``ensure_ascii=False`` and a strict encoder raises on a lone
    surrogate."""
    assert entry_key("a" * 40, ["\ud800.py", "é.py"], False) != entry_key("a" * 40, ["x.py"], False)


def test_begin_refuses_a_head_that_is_not_an_object_id():
    """Red target: a head handed to ``begin`` that is not an object id is used as a revision."""
    memo = DriftDiffMemo(None)
    memo.begin("HEAD", _fake_git(b""))
    assert memo.batch_key("a" * 40, ["a.py"], False) is None
    memo.finish()
    assert memo.next is None


def test_the_config_fingerprint_does_not_depend_on_the_order_git_lists_it():
    """Red target: an unsorted fingerprint discards a valid memo when only the order of the
    configuration entries changed (a cost, not a wrong answer)."""
    stored = ["diff.renames\ntrue", "submodule.sub.ignore\nall"]
    raw = json.dumps({"v": 1, "head": "1" * 40, "config": stored, "entries": {"k": ["a.py"]}})
    memo = DriftDiffMemo(raw)
    listed = b"submodule.sub.ignore\nall\0diff.renames\ntrue\0"
    memo.begin("1" * 40, _fake_git(b"", config=listed, code=0))
    assert memo.lookup("k", ["a.py"]) == ["a.py"]


def test_batch_key_contains_a_failing_key_function(monkeypatch):
    """Red target: an error while building a key escapes the memo instead of meaning "no reuse"."""
    raw = json.dumps({"v": 1, "head": "1" * 40, "config": [], "entries": {}})
    memo = DriftDiffMemo(raw)
    memo.begin("1" * 40, _fake_git(b""))

    def broken(*_args):
        raise RuntimeError("simulated")

    monkeypatch.setattr("sidegraph.drift_memo.entry_key", broken)
    assert memo.batch_key("a" * 40, ["a.py"], False) is None


def test_a_memo_failure_runs_the_batch_uncached(three, diffs, monkeypatch):
    """Red target: a failure inside the memo's lookup (outside the batch's own failure boundary)
    fails or changes the scan instead of only costing the reuse."""
    _repo, store, _records = three
    refresh_code_drift_cache(store)

    def broken(*_args, **_kwargs):
        raise RuntimeError("simulated memo failure")

    monkeypatch.setattr("sidegraph.drift_memo._fold", broken)
    diffs.clear()
    assert refresh_code_drift_cache(store) == 1
    assert len(_batches(diffs)) == 3
    assert _by_commit(store) == _reference_by_commit(store)


def test_memo_is_pruned_to_the_keys_of_the_last_scan(tmp_path):
    """Red target: a memo that keeps keys from earlier scans serves a stale answer when a record
    is bound back to a pathspec set it had before HEAD moved."""
    repo = GroupRepo(tmp_path)
    first = repo.commit({"a.py": "a\n", "b.py": "b\n"}, "c0")
    repo.record(1, commit=first, files=["a.py"])
    repo.write_index()
    one = entry_key(first, ["a.py"], False)
    two = entry_key(first, ["a.py", "b.py"], False)
    got, reference, raw = _memo_pass(repo, None)
    assert set(json.loads(raw)["entries"]) == {one}
    repo.commit({"a.py": "a2\n"}, "drift a")
    repo.record(1, commit=first, files=["a.py", "b.py"])
    repo.write_index()
    got, reference, raw = _memo_pass(repo, raw)
    assert got == reference
    assert set(json.loads(raw)["entries"]) == {two}  # `one` is gone
    repo.record(1, commit=first, files=["a.py"])
    repo.write_index()
    got, reference, raw = _memo_pass(repo, raw)
    assert got == reference
    assert set(json.loads(raw)["entries"]) == {one}


def test_head_that_moves_during_the_scan_cannot_leak_into_the_memo(tmp_path, monkeypatch):
    """Red target: diffing the literal ``HEAD`` instead of the sha resolved at the start records
    answers for another commit under the resolved sha, and the next start at that sha reuses
    them."""
    repo = GroupRepo(tmp_path)
    first = repo.commit({"a.py": "a\n"}, "c0")
    repo.record(1, commit=first, files=["a.py"])
    repo.write_index()
    other = repo.commit({"a.py": "a2\n"}, "drift a")
    git(repo.root, "checkout", "-q", "--detach", first)
    real = doctor_mod._run_git
    moved: list[bool] = []

    def moving(args, cwd, **kw):
        result = real(args, cwd, **kw)
        if args[:2] == ["rev-parse", "HEAD"] and not moved:
            moved.append(True)
            git(repo.root, "checkout", "-q", "--detach", other)  # HEAD moves after resolution
        return result

    monkeypatch.setattr(doctor_mod, "_run_git", moving)
    memo = DriftDiffMemo(None)
    _scan(repo, memo)
    monkeypatch.setattr(doctor_mod, "_run_git", real)
    assert moved and memo.next is not None and memo.next["head"] == first
    git(repo.root, "checkout", "-q", "--detach", first)
    got, reference, _ = _memo_pass(repo, json.dumps(memo.next))
    assert got == reference


def test_deletion_mode_is_part_of_the_key(tmp_path):
    """Red target: a key without the deletion mode serves the computed-status answer (which
    reports a deleted file) to a scan whose statuses are uncomputed (which excludes it)."""
    repo = GroupRepo(tmp_path)
    first = repo.commit({"a.py": "a\n", "k.py": "k\n"}, "c0")
    repo.record(1, commit=first, files=["a.py"])
    repo.commit({"a.py": None}, "delete a")
    repo.write_index()
    computed, reference, raw = _memo_pass(repo, None)
    assert computed == reference
    (repo.store / "index.db").unlink()  # uncomputed: deletions are left out, at the same HEAD
    got, reference_uncomputed, _ = _memo_pass(repo, raw)
    assert reference_uncomputed != reference
    assert got == reference_uncomputed


def test_pathspec_with_glob_characters_always_runs(tmp_path, diffs):
    """Red target: reuse that trusts the literal pathspec text skips a batch whose glob matches
    the changed file (counted as a rerun: the matched file is not one the record names, so the
    record-level result is empty either way)."""
    repo = GroupRepo(tmp_path)
    first = repo.commit({"pages/[id].tsx": "p\n", "pages/i.tsx": "i\n"}, "c0")
    repo.record(1, commit=first, files=["pages/[id].tsx"])
    repo.write_index()
    _, _, raw = _memo_pass(repo, None)
    repo.commit({"pages/i.tsx": "changed\n"}, "touch the file the glob matches")
    diffs.clear()
    got, reference, _ = _memo_pass(repo, raw)
    assert got == reference
    assert len(_batches(diffs)) == 2  # one for the reference scan, one for the memoized scan


def test_a_moved_name_with_raw_bytes_still_affects_the_pathspec_that_names_it():
    """Red target: decoding the moved names lossily turns an undecodable name into a different
    string than the pathspec it came from, so the batch is wrongly reused."""
    name = os.fsdecode(b"a\xff.py")
    raw = json.dumps({"v": 1, "head": "1" * 40, "config": [], "entries": {"k": []}})
    memo = DriftDiffMemo(raw)
    memo.begin("2" * 40, _fake_git(b"a\xff.py\0"))
    assert memo.lookup("k", [name]) is None


def test_a_raw_byte_file_name_is_rerun_after_it_changes(tmp_path):
    """Red target: as above, end to end, on a filesystem that accepts non-UTF-8 names."""
    name = os.fsdecode(b"a\xff.py")
    repo = GroupRepo(tmp_path)
    try:
        first = repo.commit({name: "a\n"}, "c0")
    except (OSError, UnicodeError, subprocess.CalledProcessError):
        pytest.skip("this filesystem refuses non-UTF-8 file names")
    repo.record(1, commit=first, files=[name])
    repo.write_index()
    _, _, raw = _memo_pass(repo, None)
    repo.commit({name: "changed\n"}, "drift")
    got, reference, _ = _memo_pass(repo, raw)
    assert reference[0][2]  # the record drifted
    assert got == reference


@pytest.mark.parametrize(
    ("text", "expected"),
    [("a" * 40, True), ("0" * 64, True), ("a" * 39, False), ("a" * 41, False), ("A" * 40, False)],
)
def test_only_a_full_lowercase_hex_id_is_an_object_id(text, expected):
    """Guard (declared): the rule the head and capture-commit checks share."""
    assert is_object_id(text) is expected


def test_curate_path_passes_no_memo_and_still_diffs_head(three, diffs):
    """Guard (declared): the doctor's own scan stays uncached, diffing the literal ``HEAD``."""
    _repo, store, _records = three
    doctor_mod.curate(store.path)
    doctor_mod.curate(store.path)
    assert len(_batches(diffs)) == 6
    assert all(any(arg.endswith("..HEAD") for arg in c) for c in _batches(diffs))
    assert store.get_meta(DRIFT_MEMO_KEY) is None


def _fake_git(head_diff: bytes, *, config: bytes = b"", code: int = 1):
    def run(args):
        out = head_diff if args[0] == "diff" else config
        return subprocess.CompletedProcess(args, 0 if args[0] == "diff" else code, out, b"")

    return run


@pytest.mark.parametrize(
    ("pathspec", "moved", "reused"),
    [
        ("x", b"x/y.py\0", False),  # a directory pathspec holds the changed file
        ("x/y.py", b"x/z.py\0", True),  # a sibling does not
        ("X/Y.py", b"x/y.py\0", False),  # case folds, as core.ignorecase would
        ("a/b.py", b"a\0", False),  # a file replaced a directory above the pathspec
        ("a.py", b"", True),
    ],
)
def test_lookup_treats_a_changed_path_or_directory_as_affecting_a_pathspec(pathspec, moved, reused):
    """Red target: a lookup that compares only literal pathspec text against the changed paths."""
    raw = json.dumps({"v": 1, "head": "1" * 40, "config": [], "entries": {"k": ["a.py"]}})
    memo = DriftDiffMemo(raw)
    memo.begin("2" * 40, _fake_git(moved))
    assert (memo.lookup("k", [pathspec]) is not None) is reused
