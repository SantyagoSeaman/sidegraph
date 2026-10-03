"""A store's git lookups name the repository of the project that holds the store, not the one
a symlinked ``.sidegraph`` happens to point into.

Capture commit, code drift and the refresh-hook check agree on one repository: the logical
parent of the store path (``config._store_project_root``). The capture commit and code drift fall
back to the store directory itself when that parent is in no repository; the refresh-hook check
does not, like ``sidegraph-init``. ``--against`` (``sidegraph-verify``, ``sidegraph-doctor``)
keeps the physical repository, where the store's files are committed. Every test builds real
repositories in ``tmp_path``; ``A`` is the project that holds the link and ``B`` the repository
the link points into.
see design/superpowers/specs/2026-10-02-store-git-root-symlink-design.md (T1-T12)
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from sidegraph import githooks
from sidegraph.capture import _capture_commit
from sidegraph.doctor import CODE_DRIFT, curate
from sidegraph.integrity import CHECKS, Inputs
from sidegraph.retrieval import DRIFT_CACHE_KEY
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
from sidegraph.sync import refresh_code_drift_cache
from sidegraph.verify import ILLEGAL_DELETION, verify_against
from tests import githooks_support
from tests.githooks_support import git as hooks_git

sandbox = githooks_support.sandbox  # the fixture, bound by name so that test arguments find it

GIT_NOTE = "code-drift: git unavailable — check skipped"


@pytest.fixture(autouse=True)
def _commit_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    for who in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{who}_NAME", "T")
        monkeypatch.setenv(f"GIT_{who}_EMAIL", "t@example.com")


def _git(cwd: Path, *args: str) -> str:
    done = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)
    return done.stdout.strip()


def _head(repo: Path) -> str:
    return _git(repo, "rev-parse", "HEAD")


def _repo(path: Path, *, commit: bool = True, files: dict[str, str] | None = None) -> Path:
    """A repository on ``main``; its seed file names the directory, so no two HEADs coincide."""
    path.mkdir(parents=True)
    _git(path, "init", "-q", "-b", "main")
    if commit:
        for rel, text in (files or {"seed.txt": path.name}).items():
            (path / rel).parent.mkdir(parents=True, exist_ok=True)
            (path / rel).write_text(text)
        _git(path, "add", "-A")
        _git(path, "commit", "-q", "-m", f"initial {path.name}")
    return path


def _commit_file(repo: Path, rel: str, text: str) -> str:
    (repo / rel).parent.mkdir(parents=True, exist_ok=True)
    (repo / rel).write_text(text)
    _git(repo, "add", rel)  # the path alone: a symlinked .sidegraph stays untracked
    _git(repo, "commit", "-q", "-m", f"change {rel}")
    return _head(repo)


def _link(project: Path, target: Path) -> Path:
    """``project/.sidegraph -> target``, the supported symlinked-store-root shape."""
    target.mkdir(parents=True, exist_ok=True)
    link = project / ".sidegraph"
    link.symlink_to(os.path.relpath(target, project), target_is_directory=True)
    return link


def _anchored_decision(store: Store, commit: str | None, file_path: str) -> Decision:
    """An accepted decision stamped with ``commit`` and bound to one file's entity."""
    d = store.add_decision(
        Decision(
            title="About the trader",
            kind=DecisionKind.GOTCHA,
            context="c",
            choice="ch",
            status=DecisionStatus.ACCEPTED,
            valid_from=datetime.now(UTC),
            provenance=Provenance(source="agent", commit=commit),
        )
    )
    e = store.upsert_entity(
        Entity(canonical_name="Trader", descriptor=Descriptor(name="Trader", file_path=file_path))
    )
    store.add_binding(AnchorBinding(record_id=d.id, entity_id=e.entity_id, tier=2, status="live"))
    return d


def _drift_findings(store_dir: Path) -> list:
    return [f for f in curate(store_dir).findings if f.code == CODE_DRIFT]


def _into_b(tmp_path: Path) -> tuple[Path, Path, Path]:
    """``(a, b, link)``: A holds ``.sidegraph -> ../b/store``, inside repository B."""
    a = _repo(tmp_path / "a", files={"trader/exec.py": "v1\n"})
    b = _repo(tmp_path / "b")
    return a, b, _link(a, b / "store")


# -- T1, T2, T10: the capture commit ---------------------------------------------------------


def test_t1_a_link_into_no_repository_stamps_the_projects_head(tmp_path):
    a = _repo(tmp_path / "a")
    link = _link(a, tmp_path / "shared" / "store")  # shared/ is in no repository
    assert _capture_commit(Store(link)) == _head(a)


def test_t2_a_link_into_repository_b_stamps_the_projects_head_not_bs(tmp_path):
    a, b, link = _into_b(tmp_path)
    assert _head(a) != _head(b)
    assert _capture_commit(Store(link)) == _head(a)


def test_t10_a_project_with_no_commits_stamps_nothing_not_the_link_targets_head(tmp_path):
    a = _repo(tmp_path / "a", commit=False)
    b = _repo(tmp_path / "b")
    link = _link(a, b / "store")
    assert _capture_commit(Store(link)) is None


# -- T3, T4, T11: code drift -----------------------------------------------------------------


def test_t3_doctor_reports_a_file_changed_in_the_project_since_capture(tmp_path):
    a, _b, link = _into_b(tmp_path)
    store = Store(link)
    commit = _capture_commit(store)
    d = _anchored_decision(store, commit, "trader/exec.py")
    _commit_file(a, "trader/exec.py", "v2\n")
    found = _drift_findings(link)
    assert [f.path for f in found] == [str(link / "decisions" / f"{d.id}.json")]
    assert "trader/exec.py" in found[0].detail
    assert commit is not None and commit[:8] in found[0].detail


def test_t4_the_refresh_hook_marks_the_record_drifted(tmp_path):
    a, _b, link = _into_b(tmp_path)
    store = Store(link)
    commit = _capture_commit(store)
    d = _anchored_decision(store, commit, "trader/exec.py")
    _commit_file(a, "trader/exec.py", "v2\n")
    assert refresh_code_drift_cache(store) == 1
    cache = json.loads(store.get_meta(DRIFT_CACHE_KEY) or "null")
    assert cache["head"] == _head(a)
    assert cache["by_commit"] == {commit: [d.id]}


def test_t11_a_record_stamped_with_bs_head_before_the_fix_gets_the_git_note(tmp_path):
    a, b, link = _into_b(tmp_path)
    store = Store(link)
    _anchored_decision(store, _head(b), "trader/exec.py")
    _commit_file(a, "trader/exec.py", "v2\n")
    assert [f.detail for f in _drift_findings(link)] == [GIT_NOTE]


# -- T5: the store's own history stays physical ----------------------------------------------


def test_t5_verify_against_still_diffs_the_repository_that_holds_the_store_files(tmp_path):
    _a, b, link = _into_b(tmp_path)
    with Store(link) as s:
        d = s.add_decision(
            Decision(
                title="Use file-per-record JSON",
                kind=DecisionKind.ADR,
                context="c",
                choice="ch",
                valid_from=datetime(2026, 1, 10, tzinfo=UTC),
                provenance=Provenance(source="manual"),
            )
        )
    _git(b, "add", "store")
    _git(b, "commit", "-q", "-m", "store")
    (b / "store" / "decisions" / f"{d.id}.json").unlink()
    _git(b, "add", "-A")
    _git(b, "commit", "-q", "-m", "hand delete")
    violations = verify_against(link, "HEAD~1")
    assert [v.code for v in violations] == [ILLEGAL_DELETION]


# -- T6-T8: layouts without a symlink --------------------------------------------------------


def test_t6_a_plain_store_stamps_and_measures_drift_in_its_repository(tmp_path):
    a = _repo(tmp_path / "a", files={"trader/exec.py": "v1\n"})
    store = Store(a / ".sidegraph")
    commit = _capture_commit(store)
    assert commit == _head(a)
    _anchored_decision(store, commit, "trader/exec.py")
    _commit_file(a, "trader/exec.py", "v2\n")
    assert len(_drift_findings(a / ".sidegraph")) == 1


def test_t7_a_nested_store_repository_stamps_the_enclosing_projects_head(tmp_path):
    a = _repo(tmp_path / "a")
    nested = _repo(a / ".sidegraph")  # the store directory is a repository of its own
    assert _head(a) != _head(nested)
    assert _capture_commit(Store(nested)) == _head(a)


def test_t8_a_top_level_store_repository_stamps_its_own_head(tmp_path):
    project = tmp_path / "project"  # no repository around it
    project.mkdir()
    own = _repo(project / ".sidegraph")
    assert _capture_commit(Store(own)) == _head(own)


# -- T9: the environment ---------------------------------------------------------------------


def test_t9_an_inherited_git_dir_does_not_select_the_repository(tmp_path, monkeypatch):
    a, _b, link = _into_b(tmp_path)
    unrelated = _repo(tmp_path / "unrelated")
    store = Store(link)
    expected = _head(a)
    monkeypatch.setenv("GIT_DIR", str(unrelated / ".git"))
    assert _capture_commit(store) == expected


# -- the helper's own contract ---------------------------------------------------------------


def test_the_helper_is_none_when_neither_the_project_nor_the_store_is_in_a_repository(tmp_path):
    from sidegraph import verify

    project = tmp_path / "project"
    project.mkdir()
    store = project / ".sidegraph"
    store.mkdir()
    assert verify.find_store_project_repo(store) is None


def test_the_helper_gives_up_once_its_budget_is_spent(tmp_path):
    from sidegraph import verify

    a = _repo(tmp_path / "a")
    store = a / ".sidegraph"
    store.mkdir()
    assert verify.find_store_project_repo(store) == a.resolve()
    assert verify.find_store_project_repo(store, timeout=0) is None


def test_the_helper_splits_one_budget_across_both_attempts(tmp_path, monkeypatch):
    """The second attempt gets only what the first left of ``timeout``, never a fresh one."""
    from sidegraph import verify

    clock = iter([100.0, 100.0, 100.7])
    monkeypatch.setattr(verify.time, "monotonic", lambda: next(clock))
    seen: list[float | None] = []

    def fake_run_git(args, cwd, *, timeout=None):
        seen.append(timeout)
        return subprocess.CompletedProcess(args, 128, "", "")

    monkeypatch.setattr(verify, "_run_git", fake_run_git)
    assert verify.find_store_project_repo(tmp_path / ".sidegraph", timeout=1.0) is None
    assert seen[0] == 1.0
    assert seen[1] is not None and abs(seen[1] - 0.3) < 1e-9


# -- T12: the refresh-hook check -------------------------------------------------------------

CHECK = "refresh-hook-missing"


def _hook_inputs(store_dir: Path, graph: Path) -> Inputs:
    return Inputs(
        store_dir=store_dir,
        now=datetime.now(UTC),
        reader=SimpleNamespace(path=graph),
        graph_path=graph,
    )


def _detect(store_dir: Path, graph: Path):
    detector = next(c for c in CHECKS if c.id == CHECK)
    return detector.detect(_hook_inputs(store_dir, graph))


def _project_with_graph(sb) -> tuple[Path, Path]:
    a = sb.repo("a")
    graph = a / "graphify-out" / "graph.json"
    graph.parent.mkdir()
    graph.write_text(json.dumps({"nodes": [], "links": []}))
    return a, graph


def _install(sb, repo: Path) -> None:
    info = githooks.repo_info(repo)
    assert info is not None
    githooks.install(info, graphify=str(sb.fake_bin / "graphify"))


def test_t12_a_link_into_b_reads_the_projects_hooks_missing_in_a_is_the_problem(sandbox):
    a, graph = _project_with_graph(sandbox)
    b = sandbox.repo("b")
    _install(sandbox, b)  # B is wired; A is not
    link = _link(a, b / "store")
    problem = _detect(link, graph)
    assert problem is not None and problem.check == CHECK


def test_t12_a_link_into_b_reads_the_projects_hooks_installed_in_a_is_clean(sandbox):
    a, graph = _project_with_graph(sandbox)
    b = sandbox.repo("b")
    link = _link(a, b / "store")  # B is not wired; A is
    _install(sandbox, a)
    assert _detect(link, graph) is None


def test_t12_a_link_into_no_repository_still_runs_the_check_in_the_project(sandbox):
    a, graph = _project_with_graph(sandbox)
    link = _link(a, sandbox.root / "shared" / "store")
    problem = _detect(link, graph)
    assert problem is not None and problem.check == CHECK
    _install(sandbox, a)
    assert _detect(link, graph) is None


def test_t12_a_nested_store_repository_checks_the_enclosing_projects_hooks(sandbox):
    a, graph = _project_with_graph(sandbox)
    nested = a / ".sidegraph"  # the store directory is a repository of its own
    nested.mkdir()
    hooks_git(nested, "init", "-q", "-b", "main")
    problem = _detect(nested, graph)
    assert problem is not None and problem.check == CHECK
    _install(sandbox, a)
    assert _detect(nested, graph) is None
