"""The ``refresh-hook-missing`` integrity check: no git hook keeps the code graph fresh.

The detector reads the repository's hook files and the recorded choice through git, never writes,
and says nothing where the helper would not rebuild the graph the reader holds.
see design/superpowers/specs/2026-10-02-graph-refresh-hook-design.md (D7, T11)
"""

from __future__ import annotations

import io
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

import sidegraph.host.hooks as hooks
from sidegraph import githooks, integrity
from sidegraph.integrity import CHECKS, Inputs
from sidegraph.store import Store
from tests import githooks_support
from tests.githooks_support import HOOKS, git
from tests.test_cli_init_hooks import init, init_main

sandbox = githooks_support.sandbox  # the fixture, bound by name so that test arguments find it

NOW = datetime.now(UTC)
CHECK = "refresh-hook-missing"
LINE = (
    "Sidegraph: no git hook keeps the code graph fresh in this repository, so it goes stale as "
    "code changes. Ask the user whether to install one (`sidegraph-init --hooks`) or to turn "
    "this reminder off (`sidegraph-init --no-hooks`); do not install it unasked."
)
NOTICE = (
    "Sidegraph: no git hook keeps the code graph fresh in this repository, so it goes stale as "
    "code changes. Install it with `sidegraph-init --hooks`, or stop this reminder with "
    "`sidegraph-init --no-hooks`."
)


def project(sb, name: str = "main") -> tuple[Path, Path, Path]:
    """A repository with a store directory and a graph file: ``(repo, store_dir, graph)``."""
    repo = sb.repo(name)
    store_dir = repo / ".sidegraph"
    store_dir.mkdir()
    graph = repo / "graphify-out" / "graph.json"
    graph.parent.mkdir()
    graph.write_text(json.dumps({"nodes": [], "links": []}))
    return repo, store_dir, graph


def inputs(store_dir: Path, graph: Path | None, **over) -> Inputs:
    reader = SimpleNamespace(path=graph) if graph is not None else None
    kwargs = dict(store_dir=store_dir, now=NOW, reader=reader, graph_path=graph)
    kwargs.update(over)
    return Inputs(**kwargs)


def write_executable(path: Path, text: str, mode: int = 0o755) -> None:
    path.write_text(text)
    path.chmod(mode)


def detector():
    return next(c for c in CHECKS if c.id == CHECK)


def run_it(store_dir: Path, graph: Path | None) -> integrity.RunResult:
    return integrity.run(inputs(store_dir, graph), "session", (detector(),))


def install(sb, repo: Path) -> None:
    info = githooks.repo_info(repo)
    assert info is not None
    githooks.install(info, graphify=str(sb.fake_bin / "graphify"))


def test_the_check_is_for_the_session_only():  # its slot is pinned by test_integrity's order test
    assert detector().surfaces == frozenset({"session"})


def test_t11_no_hooks_and_a_graph_gives_the_line_and_the_notice(sandbox):
    _repo, store_dir, graph = project(sandbox)
    problem = detector().detect(inputs(store_dir, graph))
    assert problem is not None
    assert problem.check == CHECK
    assert problem.severity == "advisory"
    assert problem.line == LINE
    assert problem.notice == NOTICE
    assert problem.fix == "sidegraph-init --hooks"
    assert problem.summary == "no git hook keeps the code graph fresh"
    assert run_it(store_dir, graph).problems == [problem]


def test_a_complete_install_is_clean(sandbox):
    repo, store_dir, graph = project(sandbox)
    install(sandbox, repo)
    assert detector().detect(inputs(store_dir, graph)) is None
    assert CHECK in run_it(store_dir, graph).clean


@pytest.mark.parametrize("level", ["local", "global"])
def test_t11_a_recorded_decline_is_clean(sandbox, level):
    """``--no-hooks`` writes the local value; a global ``false`` counts too."""
    _repo, store_dir, graph = project(sandbox)
    git(store_dir, "config", f"--{level}", "sidegraph.graphRefresh", "false")
    assert detector().detect(inputs(store_dir, graph)) is None
    assert CHECK in run_it(store_dir, graph).clean


def test_a_raw_no_in_the_config_is_a_decline_too(sandbox):
    _repo, store_dir, graph = project(sandbox)
    git(store_dir, "config", "--local", "sidegraph.graphRefresh", "no")
    assert detector().detect(inputs(store_dir, graph)) is None


def test_t11_no_graph_means_not_run_so_a_repository_without_one_is_never_nagged(sandbox):
    _repo, store_dir, _graph = project(sandbox)
    result = run_it(store_dir, None)
    assert result.problems == [] and CHECK not in result.clean
    with pytest.raises(integrity._NotRun):
        detector().detect(inputs(store_dir, None))


def test_a_graph_the_helper_does_not_rebuild_means_not_run(sandbox, tmp_path):
    """A graph outside ``<toplevel>/graphify-out/graph.json`` (a configured path elsewhere)."""
    _repo, store_dir, _graph = project(sandbox)
    elsewhere = tmp_path / "elsewhere" / "graph.json"
    elsewhere.parent.mkdir()
    elsewhere.write_text("{}")
    result = run_it(store_dir, elsewhere)
    assert result.problems == [] and CHECK not in result.clean


def test_a_store_outside_a_git_repository_means_not_run(tmp_path):
    store_dir = tmp_path / ".sidegraph"
    store_dir.mkdir()
    graph = tmp_path / "graphify-out" / "graph.json"
    graph.parent.mkdir()
    graph.write_text("{}")
    result = run_it(store_dir, graph)
    assert result.problems == [] and CHECK not in result.clean


def test_a_missing_helper_is_a_problem_even_with_the_blocks_in_place(sandbox):
    repo, store_dir, graph = project(sandbox)
    install(sandbox, repo)
    (repo / ".git" / "hooks" / githooks.HELPER_NAME).unlink()
    assert detector().detect(inputs(store_dir, graph)) is not None


def test_a_hook_that_does_not_mention_the_helper_is_a_problem(sandbox):
    repo, store_dir, graph = project(sandbox)
    install(sandbox, repo)
    (repo / ".git" / "hooks" / "post-merge").write_text("#!/bin/sh\necho mine\n")
    assert detector().detect(inputs(store_dir, graph)) is not None
    (repo / ".git" / "hooks" / "post-merge").unlink()
    assert detector().detect(inputs(store_dir, graph)) is not None


def test_a_hook_that_runs_nothing_is_a_problem_though_it_names_the_helper(sandbox):
    """Git runs a hook only when it is executable, and a comment runs nothing: a hook of either
    kind does not keep the graph fresh, so the reminder must not read clean."""
    repo, store_dir, graph = project(sandbox)
    install(sandbox, repo)
    assert detector().detect(inputs(store_dir, graph)) is None
    hook = repo / ".git" / "hooks" / "post-commit"
    original = hook.read_text()
    write_executable(hook, f"#!/bin/sh\n# {githooks.manual_line('post-commit')}\n")
    assert detector().detect(inputs(store_dir, graph)) is not None
    write_executable(hook, original, 0o644)
    assert detector().detect(inputs(store_dir, graph)) is not None
    write_executable(hook, original)
    assert detector().detect(inputs(store_dir, graph)) is None


@pytest.mark.parametrize("level", ["local", "global"])
def test_t11_a_call_added_by_hand_in_the_hooks_path_directory_is_clean(sandbox, level):
    repo, store_dir, graph = project(sandbox)
    managed = repo / ".githooks"
    managed.mkdir()
    git(repo, "config", f"--{level}", "core.hooksPath", str(managed))
    install(sandbox, repo)  # the helper only: a hooks manager owns the directory
    assert detector().detect(inputs(store_dir, graph)) is not None  # nothing calls it yet
    for hook in HOOKS:
        write_executable(managed / hook, f"#!/bin/sh\n{githooks.manual_line(hook)}\n")
    assert detector().detect(inputs(store_dir, graph)) is None


def test_a_relative_hooks_path_is_read_from_the_working_tree_root(sandbox):
    repo, store_dir, graph = project(sandbox)
    (repo / ".githooks").mkdir()
    git(repo, "config", "core.hooksPath", ".githooks")
    install(sandbox, repo)
    for hook in HOOKS:
        write_executable(repo / ".githooks" / hook, f"#!/bin/sh\n{githooks.manual_line(hook)}\n")
    assert detector().detect(inputs(store_dir, graph)) is None


def test_the_detector_writes_nothing(sandbox):
    repo, store_dir, graph = project(sandbox)
    before = sorted(p.relative_to(repo) for p in repo.rglob("*") if ".git/" not in str(p))
    git_before = sorted(p for p in (repo / ".git").rglob("*") if p.is_file())
    detector().detect(inputs(store_dir, graph))
    after = sorted(p.relative_to(repo) for p in repo.rglob("*") if ".git/" not in str(p))
    assert before == after
    assert sorted(p for p in (repo / ".git").rglob("*") if p.is_file()) == git_before


# -- SessionStart ---------------------------------------------------------------------------


def start_session(monkeypatch, capsys, store_dir: Path, graph: Path) -> dict:
    monkeypatch.setenv("SIDEGRAPH_DIR", str(store_dir))
    monkeypatch.setenv("SIDEGRAPH_GRAPH", str(graph))
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "s1"})))
    hooks.session_start()
    return json.loads(capsys.readouterr().out)


def test_session_start_tells_the_model_and_the_human(sandbox, monkeypatch, capsys):
    _repo, store_dir, graph = project(sandbox)
    Store(store_dir).close()
    out = start_session(monkeypatch, capsys, store_dir, graph)
    assert LINE in out["hookSpecificOutput"]["additionalContext"]
    assert NOTICE in out["systemMessage"]


def test_session_start_is_quiet_once_the_hook_is_installed_or_declined(
    sandbox, monkeypatch, capsys
):
    repo, store_dir, graph = project(sandbox)
    Store(store_dir).close()
    install(sandbox, repo)
    out = start_session(monkeypatch, capsys, store_dir, graph)
    assert LINE not in out["hookSpecificOutput"]["additionalContext"]
    assert "systemMessage" not in out or NOTICE not in out["systemMessage"]
    other, other_store, other_graph = project(sandbox, "other")
    Store(other_store).close()
    githooks.record_choice(other, declined=True)
    out = start_session(monkeypatch, capsys, other_store, other_graph)
    assert LINE not in out["hookSpecificOutput"]["additionalContext"]


# -- linked worktrees: the one graph the helper rebuilds is the main checkout's -----------------


def worktree_project(sb, repo: Path, name: str = "wt") -> tuple[Path, Path]:
    """A linked worktree of ``repo`` with a store directory: ``(worktree, store_dir)``."""
    wt = sb.root / name
    git(repo, "worktree", "add", "-q", "-b", f"{name}-branch", str(wt))
    store_dir = wt / ".sidegraph"
    store_dir.mkdir()
    return wt, store_dir


def test_a_linked_worktree_that_borrows_the_main_graph_is_checked(sandbox):
    """The hooks are shared, so a worktree session can fix what the main checkout lacks. Its
    reader holds the main checkout's graph, which is the one the helper rebuilds."""
    repo, _store_dir, graph = project(sandbox)
    _wt, wt_store = worktree_project(sandbox, repo)
    problem = detector().detect(inputs(wt_store, graph))
    assert problem is not None and problem.check == CHECK
    install(sandbox, repo)
    assert detector().detect(inputs(wt_store, graph)) is None


def test_a_linked_worktree_with_its_own_graph_is_not_run(sandbox):
    """The helper never rebuilds a worktree's graph, so the hook would not help it."""
    repo, _store_dir, _graph = project(sandbox)
    wt, wt_store = worktree_project(sandbox, repo)
    own = wt / "graphify-out" / "graph.json"
    own.parent.mkdir()
    own.write_text(json.dumps({"nodes": [], "links": []}))
    result = run_it(wt_store, own)
    assert result.problems == [] and CHECK not in result.clean


def test_a_worktree_with_no_main_checkout_is_not_run(sandbox):
    """A bare repository with worktrees (the ``.bare`` layout) has no main checkout where the
    helper could run."""
    source = sandbox.repo("source")
    bare = sandbox.root / "bare" / ".bare"
    bare.parent.mkdir()
    git(sandbox.root, "clone", "-q", "--bare", str(source), str(bare))
    wt = sandbox.root / "bare" / "wt"
    git(bare, "worktree", "add", "-q", str(wt), "main")
    store_dir = wt / ".sidegraph"
    store_dir.mkdir()
    own = wt / "graphify-out" / "graph.json"
    own.parent.mkdir()
    own.write_text(json.dumps({"nodes": [], "links": []}))
    result = run_it(store_dir, own)
    assert result.problems == [] and CHECK not in result.clean


OUTDATED = "refresh-helper-outdated"
OUTDATED_LINE = (
    "Sidegraph: the graph refresh hook in this repository runs Graphify without "
    "GRAPHIFY_NO_AUTO_REFRESH=1, so Graphify 0.9.87 and later can rewrite the agent skills they "
    "installed on the first rebuild after an upgrade. Ask the user to run `sidegraph-init`, "
    "which refreshes the helper and keeps their recorded hook choice; do not run it unasked."
)
OUTDATED_NOTICE = (
    "Sidegraph: the graph refresh hook's helper is out of date: it runs Graphify without "
    "GRAPHIFY_NO_AUTO_REFRESH=1, so Graphify 0.9.87 and later can rewrite your agent skills "
    "after an upgrade. Refresh it with `sidegraph-init`."
)
OUTDATED_DETAIL = (
    "the installed graph refresh helper runs Graphify without GRAPHIFY_NO_AUTO_REFRESH=1; run "
    "`sidegraph-init` to refresh it"
)


def outdated_detector():
    return next(c for c in CHECKS if c.id == OUTDATED)


def write_old_helper(sb, repo: Path) -> githooks.RepoInfo:
    """The helper a 0.13.x ``sidegraph-init`` wrote: today's text without the variable."""
    info = githooks.repo_info(repo)
    assert info is not None
    old = githooks.helper_script(str(sb.fake_bin / "graphify"))
    info.helper.write_text(old.replace(f"{githooks.GRAPH_REBUILD_ENV} ", ""))
    return info


def test_the_outdated_check_is_for_session_and_doctor():  # slot: test_integrity's order test
    assert outdated_detector().surfaces == frozenset({"session", "doctor"})


def test_an_older_helper_is_reported_and_sidegraph_init_clears_it(sandbox, monkeypatch, capsys):
    repo, store_dir, graph = project(sandbox)
    install(sandbox, repo)
    info = write_old_helper(sandbox, repo)
    problem = outdated_detector().detect(inputs(store_dir, graph))
    assert problem is not None
    assert (problem.check, problem.severity, problem.fix) == (
        OUTDATED,
        "advisory",
        "sidegraph-init",
    )
    assert (problem.line, problem.notice) == (OUTDATED_LINE, OUTDATED_NOTICE)
    assert problem.findings == ((str(info.helper), OUTDATED_DETAIL),)
    init(repo, monkeypatch, capsys)
    assert outdated_detector().detect(inputs(store_dir, graph)) is None


def test_sidegraph_init_refreshes_the_helper_and_keeps_a_recorded_decline(
    sandbox, monkeypatch, capsys
):
    # a `--hooks` fix text would also reset the recorded choice; plain `sidegraph-init` must not
    repo, store_dir, graph = project(sandbox)
    install(sandbox, repo)
    write_old_helper(sandbox, repo)
    git(store_dir, "config", "--local", "sidegraph.graphRefresh", "false")
    init(repo, monkeypatch, capsys)
    assert outdated_detector().detect(inputs(store_dir, graph)) is None
    assert git(store_dir, "config", "--local", "--get", "sidegraph.graphRefresh") == "false"


def test_a_current_helper_is_clean_whatever_graphify_path_it_recorded(sandbox):
    repo, store_dir, graph = project(sandbox)
    info = githooks.repo_info(repo)
    assert info is not None
    githooks.install(info, graphify="/opt/elsewhere/graphify")
    assert outdated_detector().detect(inputs(store_dir, graph)) is None


def test_no_helper_means_not_run(sandbox):
    _repo, store_dir, graph = project(sandbox)
    result = integrity.run(inputs(store_dir, graph), "session", (outdated_detector(),))
    assert result.problems == [] and OUTDATED not in result.clean


def run_outdated(store_dir: Path, graph: Path | None) -> integrity.RunResult:
    return integrity.run(inputs(store_dir, graph), "session", (outdated_detector(),))


def reported(store_dir: Path, graph: Path | None) -> bool:
    return [p.check for p in run_outdated(store_dir, graph).problems] == [OUTDATED]


def choice_of(repo: Path) -> str:
    return git(repo, "config", "--local", "--get-all", "sidegraph.graphRefresh", check=False)


def test_a_helper_that_keeps_the_variable_is_current_whatever_else_it_says(sandbox):
    """The feature is the variable on the line that runs Graphify, not the exact text: a helper
    with an extra line, or one a newer Sidegraph wrote differently, is current."""
    repo, _store_dir, _graph = project(sandbox)
    install(sandbox, repo)
    info = githooks.repo_info(repo)
    assert info is not None
    text = info.helper.read_text()
    assert githooks.helper_is_current(info) is True
    info.helper.write_text(text + "echo harmless >/dev/null\n")
    assert githooks.helper_is_current(info) is True
    info.helper.write_text(text.replace("PYTHONHASHSEED=0 ", ""))
    assert githooks.helper_is_current(info) is True
    info.helper.write_bytes(text.replace("\n", "\r\n").encode())
    assert githooks.helper_is_current(info) is True


def test_a_helper_whose_graphify_line_lacks_the_variable_is_not_current(sandbox):
    repo, _store_dir, _graph = project(sandbox)
    install(sandbox, repo)
    info = write_old_helper(sandbox, repo)
    assert githooks.helper_is_current(info) is False
    # a comment that names the variable does not set it
    info.helper.write_text(info.helper.read_text() + f"# {githooks.GRAPH_REBUILD_ENV}\n")
    assert githooks.helper_is_current(info) is False


def make_partial(repo: Path, info: githooks.RepoInfo, *removed: str) -> None:
    for hook in removed:
        (info.hooks_dir / hook).unlink()


def test_one_hook_that_runs_the_old_helper_is_enough_even_with_a_recorded_decline(sandbox):
    repo, store_dir, graph = project(sandbox)
    install(sandbox, repo)
    info = write_old_helper(sandbox, repo)
    make_partial(repo, info, "post-checkout", "post-merge")
    assert not githooks.status(info).wired
    git(store_dir, "config", "--local", "sidegraph.graphRefresh", "false")
    assert reported(store_dir, graph)


def test_a_helper_no_hook_runs_is_not_checked(sandbox):
    repo, store_dir, graph = project(sandbox)
    install(sandbox, repo)
    info = write_old_helper(sandbox, repo)
    make_partial(repo, info, *githooks.HOOKS)
    result = run_outdated(store_dir, graph)
    assert result.problems == [] and OUTDATED not in result.clean


def refuse_post_merge(info: githooks.RepoInfo) -> None:
    """``githooks.install`` leaves a symlinked hook alone ("it is a symlink")."""
    target = info.hooks_dir / "elsewhere"
    target.write_text("#!/bin/sh\nexit 0\n")
    target.chmod(0o755)
    (info.hooks_dir / "post-merge").unlink()
    (info.hooks_dir / "post-merge").symlink_to(target)


def test_a_hook_install_refuses_does_not_hide_the_old_helper_the_others_run(sandbox):
    repo, store_dir, graph = project(sandbox)
    install(sandbox, repo)
    info = write_old_helper(sandbox, repo)
    refuse_post_merge(info)
    report = githooks.install(info, graphify=str(sandbox.fake_bin / "graphify"))
    assert [r.action for r in report.hooks if r.hook == "post-merge"] == ["refused"]
    write_old_helper(sandbox, repo)
    assert not githooks.status(info).wired
    assert reported(store_dir, graph)


def init_clears(repo, store_dir, graph, monkeypatch, capsys, *argv) -> tuple[str, str]:
    assert reported(store_dir, graph)
    before = choice_of(repo)
    out = init(repo, monkeypatch, capsys, *argv)
    assert not reported(store_dir, graph)
    assert OUTDATED not in [p.check for p in run_outdated(store_dir, graph).problems]
    return out, before


def test_init_refreshes_the_helper_of_a_partly_wired_repository_and_keeps_a_decline(
    sandbox, monkeypatch, capsys
):
    repo, store_dir, graph = project(sandbox)
    install(sandbox, repo)
    info = write_old_helper(sandbox, repo)
    make_partial(repo, info, "post-checkout", "post-merge")
    git(store_dir, "config", "--local", "sidegraph.graphRefresh", "false")
    out, before = init_clears(repo, store_dir, graph, monkeypatch, capsys)
    assert choice_of(repo) == before == "false"
    assert out.count("refreshed the helper") == 1
    assert not (info.hooks_dir / "post-merge").exists()  # no hook was written


def test_init_refreshes_the_helper_when_a_hook_is_refused(sandbox, monkeypatch, capsys):
    repo, store_dir, graph = project(sandbox)
    install(sandbox, repo)
    info = write_old_helper(sandbox, repo)
    refuse_post_merge(info)
    out, before = init_clears(repo, store_dir, graph, monkeypatch, capsys)
    assert choice_of(repo) == before
    assert (info.hooks_dir / "post-merge").is_symlink()


def test_init_refreshes_the_helper_of_a_worktree_that_has_its_own_graph(
    sandbox, monkeypatch, capsys
):
    repo, _store_dir, _graph = project(sandbox)
    install(sandbox, repo)
    write_old_helper(sandbox, repo)
    wt, wt_store = worktree_project(sandbox, repo)
    own = wt / "graphify-out" / "graph.json"
    own.parent.mkdir()
    own.write_text(json.dumps({"nodes": [], "links": []}))
    # the graph guard would skip the hook; the helper is per repository, so it is refreshed anyway
    out, before = init_clears(wt, wt_store, own, monkeypatch, capsys)
    assert choice_of(repo) == before


def test_init_refreshes_the_helper_when_the_store_is_in_a_subdirectory(
    sandbox, monkeypatch, capsys
):
    repo, _store_dir, _graph = project(sandbox)
    install(sandbox, repo)
    write_old_helper(sandbox, repo)
    sub = repo / "pkg"
    sub.mkdir()
    sub_store = sub / ".sidegraph"
    sub_store.mkdir()
    before = choice_of(repo)
    assert reported(sub_store, None)
    monkeypatch.chdir(sub)
    assert init_main(["--db", str(sub_store), "--no-settings"]) == 0
    capsys.readouterr()
    assert not reported(sub_store, None)
    assert choice_of(repo) == before


def test_no_hooks_refreshes_an_old_helper_and_records_only_the_decline(
    sandbox, monkeypatch, capsys
):
    repo, store_dir, graph = project(sandbox)
    install(sandbox, repo)
    write_old_helper(sandbox, repo)
    assert reported(store_dir, graph)
    init(repo, monkeypatch, capsys, "--no-hooks")
    assert not reported(store_dir, graph)
    assert choice_of(repo) == "false"


@pytest.mark.parametrize("lock_target", ["$lock", "$lock.gate"])
def test_directory_lock_helper_is_reported_and_init_repairs_it(
    sandbox, monkeypatch, capsys, lock_target
):
    repo, store_dir, graph = project(sandbox)
    install(sandbox, repo)
    info = githooks.repo_info(repo)
    assert info is not None
    # A hand-edited/older helper: the unsafe primitive must be detected even when the
    # Graphify environment is already correct and the rest of the template differs.
    info.helper.write_text(
        "#!/bin/sh\n"
        f'mkdir "{lock_target}" 2>/dev/null || exit 0\n'
        'GRAPHIFY_NO_AUTO_REFRESH=1 "$graphify" update .\n'
    )
    git(store_dir, "config", "--local", "sidegraph.graphRefresh", "false")
    before = {h: (info.hooks_dir / h).read_bytes() for h in githooks.HOOKS}
    problem = outdated_detector().detect(inputs(store_dir, graph))
    assert problem is not None
    assert "lock" in problem.findings[0][1]
    assert "without GRAPHIFY_NO_AUTO_REFRESH" not in problem.line
    init(repo, monkeypatch, capsys)
    assert outdated_detector().detect(inputs(store_dir, graph)) is None
    assert choice_of(repo) == "false"
    assert {h: (info.hooks_dir / h).read_bytes() for h in githooks.HOOKS} == before


def test_a_directory_lock_mention_does_not_reinstall_a_custom_current_helper(
    sandbox, monkeypatch, capsys
):
    repo, store_dir, graph = project(sandbox)
    install(sandbox, repo)
    info = githooks.repo_info(repo)
    assert info is not None
    with info.helper.open("a") as out:
        out.write('# Old lock: mkdir "$lock"\necho mkdir "$lock.gate" >/dev/null\n')
    make_partial(repo, info, "post-merge", "post-checkout")
    git(store_dir, "config", "--local", "sidegraph.graphRefresh", "false")
    before = info.helper.read_bytes()
    assert outdated_detector().detect(inputs(store_dir, graph)) is None
    init(repo, monkeypatch, capsys)
    assert info.helper.read_bytes() == before


def test_old_lock_examples_in_heredocs_do_not_trigger_helper_repair(sandbox):
    repo, store_dir, graph = project(sandbox)
    install(sandbox, repo)
    info = githooks.repo_info(repo)
    assert info is not None
    with info.helper.open("a") as out:
        out.write(
            ": <<'OLD_LOCK_EXAMPLE'\nmkdir \"$lock\" 2>/dev/null || exit 0\nOLD_LOCK_EXAMPLE\n"
        )
    assert outdated_detector().detect(inputs(store_dir, graph)) is None


def test_the_complete_released_directory_lock_helper_is_detected_and_repaired(
    sandbox, monkeypatch, capsys
):
    from tests.githooks_legacy_helper import LEGACY_HELPER

    repo, store_dir, graph = project(sandbox)
    install(sandbox, repo)
    info = githooks.repo_info(repo)
    assert info is not None
    info.helper.write_text(LEGACY_HELPER.replace("@GRAPHIFY@", "''"))
    make_partial(repo, info, "post-merge", "post-checkout")
    git(store_dir, "config", "--local", "sidegraph.graphRefresh", "false")
    assert githooks.helper_is_current(info) is True
    assert reported(store_dir, graph)
    init(repo, monkeypatch, capsys)
    assert not reported(store_dir, graph)
    assert not githooks.helper_uses_directory_lock(info)
    assert choice_of(repo) == "false"


@pytest.mark.parametrize("word", ["if", "then", "do", "!", "{"])
def test_shell_keywords_in_echo_arguments_are_not_lock_commands(sandbox, word):
    repo, store_dir, graph = project(sandbox)
    install(sandbox, repo)
    info = githooks.repo_info(repo)
    assert info is not None
    with info.helper.open("a") as out:
        out.write(f'echo {word} mkdir "$lock" >/dev/null\n')
    assert outdated_detector().detect(inputs(store_dir, graph)) is None
