"""``sidegraph-init`` and the graph refresh hook: the question, the three flags, what is printed,
and which repository the hook goes into. Real ``git init`` repositories, a fake ``graphify`` on
``PATH``, and a git environment that never reads the real ``~/.gitconfig``.
see design/superpowers/specs/2026-10-02-graph-refresh-hook-design.md (D1, D6, T1, T2, T4, T6, T7)
"""

from __future__ import annotations

import io
import shutil
import subprocess
from pathlib import Path

import pytest

from sidegraph import githooks
from sidegraph.cli import init_main
from tests import githooks_support
from tests.githooks_support import (
    CREATED_HEADER,
    HELPER_NAME,
    HOOKS,
    await_rebuilds,
    commit,
    expected_block,
    git,
)

sandbox = githooks_support.sandbox  # the fixture, bound by name so that test arguments find it

HINT = (
    "non-interactive: no git hook installed. To keep the code graph fresh, run "
    "`sidegraph-init --hooks` (or `--no-hooks` to stop the reminder)."
)
INSTALLED = "graph refresh hook: installed (post-commit, post-merge, post-checkout)"
QUESTION_START = "Keep the code graph fresh?"
D6_LINE = (
    "Graphify's own hook is also installed: it rebuilds in linked worktrees too, and in the main "
    "checkout both hooks rebuild on every commit (Graphify's lock serialises them). Remove it "
    "with `graphify hook uninstall` if you use worktrees."
)


class _FakeTTY(io.StringIO):
    def isatty(self) -> bool:
        return True


def init(repo: Path, monkeypatch, capsys, *argv: str, stdin: str | None = None) -> str:
    """Run ``sidegraph-init`` against ``repo``'s store from inside it, settings step off; returns
    what it printed. The store's graph is the default one, ``graphify-out/graph.json`` beside the
    store (which is what the hook rebuilds). ``stdin`` is typed at a (fake) terminal; ``None``
    means no terminal."""
    monkeypatch.chdir(repo)
    if stdin is not None:
        monkeypatch.setattr("sys.stdin", _FakeTTY(stdin))
    code = init_main(["--db", str(repo / ".sidegraph"), "--no-settings", *argv])
    assert code == 0
    return capsys.readouterr().out


def hooks_dir(repo: Path) -> Path:
    return repo / ".git" / "hooks"


def hook_texts(repo: Path) -> dict[str, str | None]:
    return {
        hook: (hooks_dir(repo) / hook).read_text() if (hooks_dir(repo) / hook).exists() else None
        for hook in HOOKS
    }


def listing(repo: Path) -> set[str]:
    return {str(p.relative_to(repo / ".git")) for p in (repo / ".git").rglob("*") if p.is_file()}


def local_choice(repo: Path) -> str | None:
    done = subprocess.run(
        ["git", "config", "--local", "--get", "sidegraph.graphRefresh"],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    return done.stdout.strip() if done.returncode == 0 else None


def installed_everywhere(repo: Path) -> bool:
    return (
        all(
            text == CREATED_HEADER + expected_block(hook) for hook, text in hook_texts(repo).items()
        )
        and (hooks_dir(repo) / HELPER_NAME).is_file()
    )


# -- T1: the question ------------------------------------------------------------------------


@pytest.mark.parametrize("answer", ["y\n", "\n", "yes\n", "  Y  \n"])
def test_t1_a_terminal_yes_or_enter_installs_the_helper_and_three_blocks(
    sandbox, monkeypatch, capsys, answer
):
    repo = sandbox.repo()
    out = init(repo, monkeypatch, capsys, stdin=answer)
    assert QUESTION_START in out
    assert INSTALLED in out
    assert installed_everywhere(repo)
    assert local_choice(repo) is None


def test_t1_a_terminal_no_installs_nothing_and_records_the_decline(sandbox, monkeypatch, capsys):
    repo = sandbox.repo()
    before = listing(repo)
    out = init(repo, monkeypatch, capsys, stdin="n\n")
    assert QUESTION_START in out
    assert listing(repo) == before
    assert local_choice(repo) == "false"
    assert "graph refresh hook: declined" in out


@pytest.mark.parametrize("answers", ["maybe\nperhaps\n", ""])
def test_t1_two_unrecognised_answers_or_eof_take_the_default(sandbox, monkeypatch, capsys, answers):
    repo = sandbox.repo()
    init(repo, monkeypatch, capsys, stdin=answers)
    assert installed_everywhere(repo)


def test_t1_the_question_is_asked_once_and_a_decline_is_remembered(sandbox, monkeypatch, capsys):
    repo = sandbox.repo()
    init(repo, monkeypatch, capsys, stdin="n\n")
    out = init(repo, monkeypatch, capsys, stdin="y\n")  # an answer that must not be read
    assert QUESTION_START not in out
    assert (
        "graph refresh hook: declined earlier (git config sidegraph.graphRefresh false); "
        "install it with `sidegraph-init --hooks`"
    ) in out
    assert not (hooks_dir(repo) / HELPER_NAME).exists()


def test_a_global_false_is_reported_as_the_global_config(sandbox, monkeypatch, capsys):
    repo = sandbox.repo()
    git(repo, "config", "--global", "sidegraph.graphRefresh", "false")
    out = init(repo, monkeypatch, capsys, stdin="y\n")
    assert "graph refresh hook: declined in your global git config" in out
    assert not (hooks_dir(repo) / HELPER_NAME).exists()


# -- T2: no terminal, nothing written ---------------------------------------------------------


def test_t2_without_a_terminal_and_without_flags_nothing_is_written(sandbox, monkeypatch, capsys):
    repo = sandbox.repo()
    before = listing(repo)
    out = init(repo, monkeypatch, capsys)
    assert HINT in out
    assert listing(repo) == before
    assert local_choice(repo) is None


# -- the flags --------------------------------------------------------------------------------


def test_hooks_installs_without_a_terminal_and_clears_an_earlier_decline(
    sandbox, monkeypatch, capsys
):
    repo = sandbox.repo()
    git(repo, "config", "--local", "sidegraph.graphRefresh", "false")
    out = init(repo, monkeypatch, capsys, "--hooks")
    assert INSTALLED in out
    assert QUESTION_START not in out
    assert installed_everywhere(repo)
    assert local_choice(repo) is None


def test_hooks_says_so_when_a_global_false_still_stands(sandbox, monkeypatch, capsys):
    repo = sandbox.repo()
    git(repo, "config", "--global", "sidegraph.graphRefresh", "false")
    out = init(repo, monkeypatch, capsys, "--hooks")
    assert installed_everywhere(repo)
    assert "sidegraph.graphRefresh is still false in your global git config" in out


def test_no_hooks_installs_nothing_and_records_the_decline(sandbox, monkeypatch, capsys):
    repo = sandbox.repo()
    before = listing(repo)
    out = init(repo, monkeypatch, capsys, "--no-hooks")
    assert listing(repo) == before
    assert local_choice(repo) == "false"
    assert "sidegraph-init --hooks" in out


def test_the_three_hook_flags_exclude_each_other(sandbox, monkeypatch, capsys):
    repo = sandbox.repo()
    monkeypatch.chdir(repo)
    for pair in (
        ["--hooks", "--no-hooks"],
        ["--hooks", "--remove-hooks"],
        ["--no-hooks", "--remove-hooks"],
    ):
        with pytest.raises(SystemExit) as raised:
            init_main(["--db", str(repo / ".sidegraph"), *pair])
        assert raised.value.code == 2
    capsys.readouterr()


def test_a_second_hooks_run_changes_no_byte(sandbox, monkeypatch, capsys):
    repo = sandbox.repo()
    init(repo, monkeypatch, capsys, "--hooks")
    first = hook_texts(repo)
    out = init(repo, monkeypatch, capsys, "--hooks")
    assert hook_texts(repo) == first
    assert INSTALLED in out


def test_an_installed_repository_is_refreshed_without_asking(sandbox, monkeypatch, capsys):
    repo = sandbox.repo()
    init(repo, monkeypatch, capsys, "--hooks")
    path = hooks_dir(repo) / "post-commit"
    path.write_text(
        path.read_text().replace(
            "Managed by sidegraph-init;", "Managed by an older sidegraph-init;"
        )
    )
    out = init(repo, monkeypatch, capsys, stdin="n\n")  # a terminal, and an answer to ignore
    assert QUESTION_START not in out
    assert INSTALLED in out
    assert path.read_text() == CREATED_HEADER + expected_block("post-commit")
    assert local_choice(repo) is None


def test_outside_a_git_repository_init_says_so_and_goes_on(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    code = init_main(["--db", str(tmp_path / ".sidegraph"), "--no-settings", "--hooks"])
    assert code == 0
    out = capsys.readouterr().out
    assert "graph refresh hook: skipped (not a git repository)" in out
    assert (tmp_path / ".sidegraph").is_dir()


def test_the_hook_goes_into_the_stores_repository_not_the_shells(
    sandbox, tmp_path, monkeypatch, capsys
):
    repo = sandbox.repo()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    code = init_main(["--db", str(repo / ".sidegraph"), "--no-settings", "--hooks"])
    assert code == 0
    capsys.readouterr()
    assert installed_everywhere(repo)


# -- T4: refused hooks, with the manual line --------------------------------------------------


def test_t4_a_refused_hook_prints_the_line_to_add_by_hand(sandbox, monkeypatch, capsys):
    repo = sandbox.repo()
    py = hooks_dir(repo) / "post-commit"
    py.write_text("#!/usr/bin/env python3\nprint('x')\n")
    py.chmod(0o755)
    out = init(repo, monkeypatch, capsys, "--hooks")
    assert py.read_text() == "#!/usr/bin/env python3\nprint('x')\n"
    assert "post-commit: left untouched" in out
    assert "shebang" in out
    assert expected_block("post-commit").splitlines()[2] in out
    assert "graph refresh hook: installed (post-merge, post-checkout)" in out


def test_a_damaged_marker_pair_is_reported_in_the_pinned_words(sandbox, monkeypatch, capsys):
    repo = sandbox.repo()
    path = hooks_dir(repo) / "post-commit"
    path.write_text("#!/bin/sh\n# >>> sidegraph graph refresh >>>\n")
    path.chmod(0o755)
    out = init(repo, monkeypatch, capsys, "--hooks")
    assert "post-commit: Sidegraph's block markers are damaged; fix or remove them by hand" in out
    assert path.read_text() == "#!/bin/sh\n# >>> sidegraph graph refresh >>>\n"


# -- T6: removal ------------------------------------------------------------------------------


def test_t6_remove_hooks_restores_everything_and_creates_no_store(sandbox, monkeypatch, capsys):
    repo = sandbox.repo()
    foreign = hooks_dir(repo) / "post-commit"
    foreign.write_text("#!/bin/sh -e\necho mine\nexit 0\n")
    foreign.chmod(0o755)
    init(repo, monkeypatch, capsys, "--hooks")
    git(repo, "config", "--local", "sidegraph.graphRefresh", "false")
    store = repo / ".sidegraph"
    shutil.rmtree(store)  # a repository that has no store: removal must not create one
    code = init_main(["--db", str(store), "--remove-hooks"])
    out = capsys.readouterr().out
    assert code == 0
    assert foreign.read_text() == "#!/bin/sh -e\necho mine\nexit 0\n"
    assert not (hooks_dir(repo) / "post-merge").exists()
    assert not (hooks_dir(repo) / "post-checkout").exists()
    assert not (hooks_dir(repo) / HELPER_NAME).exists()
    assert local_choice(repo) is None
    assert not store.exists()  # it returned before creating a store
    assert "graph refresh hook: removed" in out
    assert "Auto-ratify" not in out and ".claude/settings.json" not in out


def test_remove_hooks_with_nothing_installed_says_so(sandbox, monkeypatch, capsys):
    repo = sandbox.repo()
    monkeypatch.chdir(repo)
    code = init_main(["--db", str(repo / ".sidegraph"), "--remove-hooks"])
    assert code == 0
    assert "graph refresh hook: nothing to remove" in capsys.readouterr().out
    assert not (repo / ".sidegraph").exists()


def test_remove_hooks_outside_a_repository_says_so(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    code = init_main(["--db", str(tmp_path / ".sidegraph"), "--remove-hooks"])
    assert code == 0
    assert "graph refresh hook: skipped (not a git repository)" in capsys.readouterr().out
    assert not (tmp_path / ".sidegraph").exists()


# -- T7: core.hooksPath -----------------------------------------------------------------------


@pytest.mark.parametrize("level", ["local", "global"])
def test_t7_hooks_path_gets_no_file_and_the_printed_line_works_by_hand(
    sandbox, monkeypatch, capsys, level
):
    repo = sandbox.repo()
    managed = repo / ".githooks"
    managed.mkdir()
    git(repo, "config", f"--{level}", "core.hooksPath", str(managed))
    out = init(repo, monkeypatch, capsys, "--hooks")

    assert list(managed.iterdir()) == []  # a hooks manager owns this directory
    assert (repo / ".git" / "hooks" / HELPER_NAME).is_file()
    assert not (repo / ".git" / "hooks" / "post-commit").exists()
    assert "core.hooksPath is set" in out
    lines = [ln for ln in out.splitlines() if ln.startswith('g="$(git rev-parse')]
    assert [ln.split(' || "$g" ')[1].split(" ")[0] for ln in lines] == list(HOOKS)

    for hook, line in zip(HOOKS, lines, strict=True):  # what a person would paste
        path = managed / hook
        path.write_text(f"#!/bin/sh\n{line}\n")
        path.chmod(0o755)
    common = repo / ".git"
    wt = sandbox.root / "wt"
    git(repo, "worktree", "add", "-q", "-b", "feature", str(wt))
    commit(wt)  # a linked worktree: no rebuild
    head = commit(repo)  # the main checkout: one
    starts = await_rebuilds(sandbox, common)
    assert [(s.cwd.resolve(), s.head) for s in starts] == [(repo.resolve(), head)]


def test_hooks_path_with_a_declined_choice_installs_nothing(sandbox, monkeypatch, capsys):
    repo = sandbox.repo()
    git(repo, "config", "core.hooksPath", str(repo / ".githooks"))
    git(repo, "config", "--local", "sidegraph.graphRefresh", "false")
    out = init(repo, monkeypatch, capsys)
    assert "graph refresh hook: declined earlier" in out
    assert not (repo / ".git" / "hooks" / HELPER_NAME).exists()


# -- D6: Graphify's own hook ------------------------------------------------------------------


def test_graphifys_own_hook_is_reported_and_left_alone(sandbox, monkeypatch, capsys):
    repo = sandbox.repo()
    theirs = hooks_dir(repo) / "post-commit"
    theirs.write_text("#!/bin/sh\n# graphify-hook-start\necho graphify\n# graphify-hook-end\n")
    theirs.chmod(0o755)
    out = init(repo, monkeypatch, capsys, "--hooks")
    assert D6_LINE in out
    text = theirs.read_text()
    assert text.endswith("# graphify-hook-start\necho graphify\n# graphify-hook-end\n")
    assert text.startswith("#!/bin/sh\n" + expected_block("post-commit"))
    other = sandbox.repo("second")
    assert D6_LINE not in init(other, monkeypatch, capsys, "--hooks")


# -- the flags are documented -------------------------------------------------------------------


def test_the_help_names_the_three_flags(capsys):
    with pytest.raises(SystemExit):
        init_main(["--help"])
    out = capsys.readouterr().out
    for flag in ("--hooks", "--no-hooks", "--remove-hooks"):
        assert flag in out


# -- --no-hooks on an installed repository, and a hook wired by hand ------------------------------


def test_no_hooks_on_an_installed_repository_says_the_hook_stays(sandbox, monkeypatch, capsys):
    repo = sandbox.repo()
    init(repo, monkeypatch, capsys, "--hooks")
    out = init(repo, monkeypatch, capsys, "--no-hooks")
    assert installed_everywhere(repo)  # nothing was removed
    assert local_choice(repo) == "false"
    assert "not installed" not in out
    assert "stays installed" in out and "sidegraph-init --remove-hooks" in out


def wire_by_hand(sandbox, repo: Path) -> None:
    """The refused-hook case: ``post-commit`` is a symlink whose target got the printed line."""
    target = sandbox.root / "shared-post-commit"
    target.write_text("#!/bin/sh\n" + githooks.manual_line("post-commit") + "\n")
    target.chmod(0o755)
    (hooks_dir(repo) / "post-commit").symlink_to(target)


def test_a_hook_wired_by_hand_counts_as_installed(sandbox, monkeypatch, capsys):
    """Plain init used to say no git hook was installed, or asked every time, because one hook had
    the call by hand and no block."""
    repo = sandbox.repo()
    wire_by_hand(sandbox, repo)
    init(repo, monkeypatch, capsys, "--hooks")  # the other two hooks get the block
    before = hook_texts(repo)
    out = init(repo, monkeypatch, capsys, stdin="n\n")  # a terminal: it must not ask
    assert QUESTION_START not in out
    assert "no git hook installed" not in out
    assert "already wired" in out
    assert hook_texts(repo) == before
    assert local_choice(repo) is None
    quiet = init(repo, monkeypatch, capsys)  # and no terminal: no hint either
    assert "no git hook installed" not in quiet


def test_no_hooks_on_a_hand_wired_repository_says_the_hook_stays(sandbox, monkeypatch, capsys):
    repo = sandbox.repo()
    wire_by_hand(sandbox, repo)
    init(repo, monkeypatch, capsys, "--hooks")
    out = init(repo, monkeypatch, capsys, "--no-hooks")
    assert "stays installed" in out


# -- a hook that could never rebuild the store's graph ---------------------------------------


def test_a_graph_elsewhere_is_skipped_naming_the_graph_the_hook_would_rebuild(
    sandbox, monkeypatch, capsys
):
    repo = sandbox.repo()
    monkeypatch.chdir(repo)
    elsewhere = repo / "elsewhere" / "graph.json"
    code = init_main(
        ["--db", str(repo / ".sidegraph"), "--no-settings", "--hooks", "--graph", str(elsewhere)]
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "graph refresh hook: skipped" in out
    assert str(repo / "graphify-out" / "graph.json") in out
    assert str(elsewhere) in out
    assert not (hooks_dir(repo) / HELPER_NAME).exists()


def test_a_nested_store_is_skipped_naming_the_graph_the_hook_would_rebuild(
    sandbox, monkeypatch, capsys
):
    repo = sandbox.repo()
    package = repo / "pkg"
    package.mkdir()
    monkeypatch.chdir(package)
    code = init_main(["--db", str(package / ".sidegraph"), "--no-settings", "--hooks"])
    out = capsys.readouterr().out
    assert code == 0
    assert "graph refresh hook: skipped" in out
    assert str(repo / "graphify-out" / "graph.json") in out
    assert str(package / "graphify-out" / "graph.json") in out
    assert not (hooks_dir(repo) / HELPER_NAME).exists()


def test_a_linked_worktree_with_a_graph_of_its_own_is_skipped(sandbox, monkeypatch, capsys):
    repo = sandbox.repo()
    wt = sandbox.root / "wt"
    git(repo, "worktree", "add", "-q", "-b", "feature", str(wt))
    (wt / "graphify-out").mkdir()
    (wt / "graphify-out" / "graph.json").write_text('{"nodes": [], "links": []}')
    out = init(wt, monkeypatch, capsys, "--hooks")
    assert "graph refresh hook: skipped" in out
    assert str(repo / "graphify-out" / "graph.json") in out
    assert not (hooks_dir(repo) / HELPER_NAME).exists()


def test_a_linked_worktree_that_borrows_the_main_graph_installs(sandbox, monkeypatch, capsys):
    """It has no graph of its own and reads the main checkout's, which the hook rebuilds; the
    reminder asks such a session to run init, so init must not refuse it."""
    repo = sandbox.repo()
    wt = sandbox.root / "wt"
    git(repo, "worktree", "add", "-q", "-b", "feature", str(wt))
    out = init(wt, monkeypatch, capsys, "--hooks")
    assert INSTALLED in out
    assert installed_everywhere(repo)


def test_a_repository_with_no_main_checkout_is_skipped(sandbox, monkeypatch, capsys):
    source = sandbox.repo("source")
    bare = sandbox.root / "bare" / ".bare"
    bare.parent.mkdir()
    git(sandbox.root, "clone", "-q", "--bare", str(source), str(bare))
    wt = sandbox.root / "bare" / "wt"
    git(bare, "worktree", "add", "-q", str(wt), "main")
    out = init(wt, monkeypatch, capsys, "--hooks")
    assert "graph refresh hook: skipped" in out
    assert "no main checkout" in out
    assert not (bare / "hooks" / HELPER_NAME).exists()


# -- the documented exit codes of the hook flags -------------------------------------------------


def _unwritable(*_args, **_kwargs):
    raise OSError("read-only file system")


def test_hooks_exits_1_when_it_cannot_write_after_running_the_rest_of_init(
    sandbox, monkeypatch, capsys
):
    repo = sandbox.repo()
    monkeypatch.chdir(repo)
    monkeypatch.setattr(githooks, "install", _unwritable)
    code = init_main(["--db", str(repo / ".sidegraph"), "--no-settings", "--hooks"])
    out = capsys.readouterr().out
    assert code == 1
    assert "graph refresh hook: could not write (read-only file system)" in out
    assert (repo / ".sidegraph").is_dir()  # the rest of init ran


def test_remove_hooks_exits_1_when_it_cannot_remove(sandbox, monkeypatch, capsys):
    repo = sandbox.repo()
    monkeypatch.chdir(repo)
    monkeypatch.setattr(githooks, "remove", _unwritable)
    code = init_main(["--db", str(repo / ".sidegraph"), "--remove-hooks"])
    assert code == 1
    assert "graph refresh hook: could not remove (read-only file system)" in capsys.readouterr().out


PARTIAL = "graph refresh hook: removal was partial"


def test_remove_hooks_exits_1_when_a_hook_keeps_its_block_because_of_damaged_markers(
    sandbox, monkeypatch, capsys
):
    """The other two hooks are cleaned and the helper goes, but the damaged one still holds a
    marker, so a script must not read the run as a clean removal."""
    repo = sandbox.repo()
    init(repo, monkeypatch, capsys, "--hooks")
    damaged = hooks_dir(repo) / "post-merge"
    lone_marker = damaged.read_text().replace(githooks.END_MARKER + "\n", "")
    damaged.write_text(lone_marker)
    code = init_main(["--db", str(repo / ".sidegraph"), "--remove-hooks"])
    out = capsys.readouterr().out
    assert code == 1
    partial = [line for line in out.splitlines() if line.startswith(PARTIAL)]
    assert len(partial) == 1
    assert "post-merge (damaged markers)" in partial[0]
    assert "post-commit" not in partial[0] and "post-checkout" not in partial[0]
    assert damaged.read_text() == lone_marker  # left untouched, for the owner to fix
    assert not (hooks_dir(repo) / "post-commit").exists()
    assert not (hooks_dir(repo) / "post-checkout").exists()
    assert not (hooks_dir(repo) / HELPER_NAME).exists()  # the helper is removed anyway


def test_remove_hooks_exits_1_when_a_hook_is_a_symlink_it_left_alone(sandbox, monkeypatch, capsys):
    repo = sandbox.repo()
    wire_by_hand(sandbox, repo)  # post-commit is a symlink whose target carries the call
    init(repo, monkeypatch, capsys, "--hooks")  # the other two hooks get the block
    code = init_main(["--db", str(repo / ".sidegraph"), "--remove-hooks"])
    out = capsys.readouterr().out
    assert code == 1
    partial = [line for line in out.splitlines() if line.startswith(PARTIAL)]
    assert len(partial) == 1 and "post-commit (symlink)" in partial[0]
    assert "nothing to remove" not in out
    assert not (hooks_dir(repo) / "post-merge").exists()


def test_remove_hooks_exits_1_and_not_nothing_to_remove_when_only_a_damaged_hook_is_left(
    sandbox, monkeypatch, capsys
):
    repo = sandbox.repo()
    path = hooks_dir(repo) / "post-commit"
    path.write_text("#!/bin/sh\n# >>> sidegraph graph refresh >>>\n")
    path.chmod(0o755)
    code = init_main(["--db", str(repo / ".sidegraph"), "--remove-hooks"])
    out = capsys.readouterr().out
    assert code == 1
    assert PARTIAL in out and "nothing to remove" not in out


def test_a_failed_write_after_the_question_keeps_exit_0(sandbox, monkeypatch, capsys):
    repo = sandbox.repo()
    monkeypatch.chdir(repo)
    monkeypatch.setattr(githooks, "install", _unwritable)
    monkeypatch.setattr("sys.stdin", _FakeTTY("y\n"))
    code = init_main(["--db", str(repo / ".sidegraph"), "--no-settings"])
    assert code == 0
    assert "graph refresh hook: could not write" in capsys.readouterr().out
