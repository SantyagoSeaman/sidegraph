"""The graph refresh hook, library level: placement and removal of the marked block, the helper
script's locking, and the recorded choice. Every test uses a real ``git init`` repository and a
fake ``graphify`` on ``PATH``; none reads the real ``~/.gitconfig``.
see design/superpowers/specs/2026-10-02-graph-refresh-hook-design.md (D2-D5, T3-T10, T13-T15)
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from sidegraph import githooks
from tests import githooks_support
from tests.githooks_support import (
    CREATED_HEADER,
    HELPER_NAME,
    HOOKS,
    Sandbox,
    Start,
    await_rebuilds,
    commit,
    count_ends,
    dead_pid,
    expected_block,
    git,
    have_shell,
    idle,
    max_concurrent,
    wait_for,
)

sandbox = githooks_support.sandbox  # the fixture, bound by name so that test arguments find it

OLD_BLOCK = (
    "# >>> sidegraph graph refresh >>>\n# an older version\n"
    "g=old\n# <<< sidegraph graph refresh <<<\n"
)
START = "# >>> sidegraph graph refresh >>>\n"
END = "# <<< sidegraph graph refresh <<<\n"

SHELLS = [
    "sh",
    pytest.param(
        "dash", marks=pytest.mark.skipif(not have_shell("dash"), reason="dash not installed")
    ),
]


def info_for(path: Path) -> githooks.RepoInfo:
    info = githooks.repo_info(path)
    assert info is not None
    return info


def fake(sb: Sandbox) -> str:
    return str(sb.fake_bin / "graphify")


def hook_file(repo: Path, hook: str) -> Path:
    return repo / ".git" / "hooks" / hook


def write_hook(repo: Path, hook: str, content: bytes | str, mode: int = 0o755) -> Path:
    path = hook_file(repo, hook)
    path.write_bytes(content.encode() if isinstance(content, str) else content)
    path.chmod(mode)
    return path


def results(
    report: githooks.InstallReport | githooks.RemoveReport,
) -> dict[str, githooks.HookResult]:
    return {r.hook: r for r in report.hooks}


def snapshot(repo: Path) -> dict[str, tuple[bytes, int] | None]:
    """Each hook file's bytes and mode, or None when absent."""
    out: dict[str, tuple[bytes, int] | None] = {}
    for hook in HOOKS:
        path = hook_file(repo, hook)
        out[hook] = (path.read_bytes(), path.stat().st_mode & 0o777) if path.exists() else None
    return out


def run_helper(repo: Path, *args: str, shell: str | None = None, env: dict | None = None) -> int:
    helper = str(hook_file(repo, HELPER_NAME))
    cmd = [shell, helper, *args] if shell else [helper, *args]
    return subprocess.run(cmd, cwd=repo, env=env, capture_output=True).returncode


# -- the block and the helper text (D2, D5) ------------------------------------------------


def test_the_block_bytes_are_pinned():
    for hook in HOOKS:
        assert githooks.block(hook) == expected_block(hook)


def test_the_manual_line_is_the_blocks_command_line():
    for hook in HOOKS:
        assert githooks.manual_line(hook) == expected_block(hook).splitlines()[2]


# -- T3: where the block goes ------------------------------------------------------------


@pytest.mark.parametrize(
    "shebang",
    [
        "#!/bin/sh",
        "#!/bin/sh -e",
        "#!/bin/bash -eu",
        "#!/usr/bin/env bash",
        "#!/usr/bin/env zsh",
        "#! /bin/dash",
        "#!/bin/ksh -x",
    ],
)
def test_t3_the_block_goes_right_after_the_shebang_line(sandbox, shebang):
    repo = sandbox.repo()
    original = f"{shebang}\necho foreign\nexit 0\n"
    path = write_hook(repo, "post-commit", original, 0o750)
    report = githooks.install(info_for(repo), graphify=fake(sandbox))
    assert path.read_text() == f"{shebang}\n" + expected_block("post-commit") + (
        "echo foreign\nexit 0\n"
    )
    assert path.stat().st_mode & 0o777 == 0o750
    assert results(report)["post-commit"].action == "inserted"


def test_t3_a_commit_runs_the_foreign_line_and_the_rebuild(sandbox):
    repo = sandbox.repo()
    marker = sandbox.root / "foreign-ran"
    write_hook(repo, "post-commit", f'#!/bin/sh -e\n: > "{marker}"\nexit 0\n')
    info = info_for(repo)
    githooks.install(info, graphify=fake(sandbox))
    commit(repo)
    starts = await_rebuilds(sandbox, info.common)
    assert marker.exists()
    assert len(starts) == 1


def test_an_absent_hook_is_created_with_the_header_and_the_block(sandbox):
    repo = sandbox.repo()
    report = githooks.install(info_for(repo), graphify=fake(sandbox))
    for hook in HOOKS:
        path = hook_file(repo, hook)
        assert path.read_text() == CREATED_HEADER + expected_block(hook)
        assert path.stat().st_mode & 0o777 == 0o755
        assert results(report)[hook].action == "created"


def test_the_helper_is_installed_executable_in_the_common_hooks_directory(sandbox):
    repo = sandbox.repo()
    shutil.rmtree(repo / ".git" / "hooks")  # a repository created without templates
    info = info_for(repo)
    githooks.install(info, graphify=fake(sandbox))
    helper = info.common / "hooks" / HELPER_NAME
    assert helper.is_file()
    assert helper.stat().st_mode & 0o777 == 0o755
    assert info.helper == helper


def test_the_helper_lives_in_the_common_directory_even_from_a_linked_worktree(sandbox):
    repo = sandbox.repo()
    wt = sandbox.root / "wt"
    git(repo, "worktree", "add", "-q", "-b", "feature", str(wt))
    info = info_for(wt)
    githooks.install(info, graphify=fake(sandbox))
    assert (repo / ".git" / "hooks" / HELPER_NAME).is_file()
    assert hook_file(repo, "post-commit").read_text() == CREATED_HEADER + expected_block(
        "post-commit"
    )


# -- T4: hooks that are refused and left untouched --------------------------------------

REFUSED = {
    "python": (b"#!/usr/bin/env python3\nprint('x')\n", 0o755, "shebang"),
    "node": (b"#!/usr/bin/env node\nconsole.log(1)\n", 0o755, "shebang"),
    "no shebang": (b"echo hi\n", 0o755, "shebang"),
    "no newline after the shebang": (b"#!/bin/sh", 0o755, "shebang"),
    "non-executable": (b"#!/bin/sh\necho x\n", 0o644, "executable"),
    "crlf": (b"#!/bin/sh\r\necho x\r\n", 0o755, "CRLF"),
}


@pytest.mark.parametrize("case", sorted(REFUSED))
def test_t4_an_unsafe_hook_is_refused_and_left_untouched(sandbox, case):
    repo = sandbox.repo()
    content, mode, why = REFUSED[case]
    write_hook(repo, "post-commit", content, mode)
    before = snapshot(repo)
    report = githooks.install(info_for(repo), graphify=fake(sandbox))
    after = snapshot(repo)
    assert after["post-commit"] == before["post-commit"]
    refused = results(report)["post-commit"]
    assert refused.action == "refused"
    assert refused.reason is not None and why in refused.reason
    # The other two hooks are not held up by it.
    assert results(report)["post-merge"].action == "created"
    assert results(report)["post-checkout"].action == "created"


def test_t4_a_symlinked_hook_is_refused_and_its_target_untouched(sandbox):
    repo = sandbox.repo()
    target = sandbox.root / "shared-hook"
    target.write_text("#!/bin/sh\necho shared\n")
    target.chmod(0o755)
    link = hook_file(repo, "post-commit")
    link.symlink_to(target)
    report = githooks.install(info_for(repo), graphify=fake(sandbox))
    assert link.is_symlink()
    assert target.read_text() == "#!/bin/sh\necho shared\n"
    refused = results(report)["post-commit"]
    assert refused.action == "refused" and "symlink" in (refused.reason or "")


# -- T5: idempotent, replaced in place, damaged markers ------------------------------------


def test_t5_a_second_install_leaves_every_file_byte_identical(sandbox):
    repo = sandbox.repo()
    write_hook(repo, "post-commit", "#!/bin/sh -e\necho one\n")
    write_hook(repo, "post-merge", "#!/usr/bin/env bash\necho two\nexit 0\n", 0o700)
    info = info_for(repo)
    githooks.install(info, graphify=fake(sandbox))
    first = snapshot(repo)
    helper = info.helper.read_bytes()
    report = githooks.install(info, graphify=fake(sandbox))
    assert snapshot(repo) == first
    assert info.helper.read_bytes() == helper
    assert {r.action for r in report.hooks} == {"unchanged"}


def test_t5_an_older_block_is_replaced_in_place(sandbox):
    repo = sandbox.repo()
    write_hook(repo, "post-commit", f"#!/bin/sh\necho before\n{OLD_BLOCK}echo after\n")
    report = githooks.install(info_for(repo), graphify=fake(sandbox))
    assert hook_file(repo, "post-commit").read_text() == (
        "#!/bin/sh\necho before\n" + expected_block("post-commit") + "echo after\n"
    )
    assert results(report)["post-commit"].action == "replaced"


DAMAGED = {
    "a lone start marker": f"#!/bin/sh\n{START}echo x\n",
    "a lone end marker": f"#!/bin/sh\necho x\n{END}",
    "reversed markers": f"#!/bin/sh\n{END}echo x\n{START}",
    "two pairs": f"#!/bin/sh\n{OLD_BLOCK}echo x\n{OLD_BLOCK}",
    "two start markers": f"#!/bin/sh\n{START}{START}{END}",
}


@pytest.mark.parametrize("case", sorted(DAMAGED))
def test_t5_damaged_markers_are_left_untouched_and_reported(sandbox, case):
    repo = sandbox.repo()
    write_hook(repo, "post-commit", DAMAGED[case])
    before = snapshot(repo)
    report = githooks.install(info_for(repo), graphify=fake(sandbox))
    assert snapshot(repo)["post-commit"] == before["post-commit"]
    damaged = results(report)["post-commit"]
    assert damaged.action == "damaged"
    assert damaged.reason == "Sidegraph's block markers are damaged; fix or remove them by hand"


# -- T6: removal ------------------------------------------------------------------------


def test_t6_removal_restores_foreign_hooks_to_their_exact_bytes(sandbox):
    repo = sandbox.repo()
    foreign_a = b"#!/bin/sh -e\necho foreign a\nexit 0\n"
    foreign_b = b"#!/bin/sh\n"  # a foreign file that was only a shebang
    write_hook(repo, "post-commit", foreign_a, 0o750)
    write_hook(repo, "post-merge", foreign_b)
    info = info_for(repo)
    githooks.install(info, graphify=fake(sandbox))
    common = info.common
    (common / f"{HELPER_NAME}.want").write_text("")
    (common / f"{HELPER_NAME}.lock").mkdir()
    (common / f"{HELPER_NAME}.lock" / "pid").write_text("1\n")
    (common / f"{HELPER_NAME}.lock.gate").mkdir()
    assert githooks.record_choice(repo, declined=True)

    report = githooks.remove(info)

    assert hook_file(repo, "post-commit").read_bytes() == foreign_a
    assert hook_file(repo, "post-commit").stat().st_mode & 0o777 == 0o750
    assert hook_file(repo, "post-merge").read_bytes() == foreign_b
    assert not hook_file(repo, "post-checkout").exists()  # Sidegraph's own file is deleted
    assert {h: r.action for h, r in results(report).items()} == {
        "post-commit": "removed",
        "post-merge": "removed",
        "post-checkout": "deleted",
    }
    assert not info.helper.exists()
    for leftover in ("want", "lock", "lock.gate"):
        assert not (common / f"{HELPER_NAME}.{leftover}").exists()
    assert githooks.read_choice(repo) is None
    assert report.helper_removed


def test_t6_removal_touches_nothing_with_damaged_markers_and_says_so(sandbox):
    repo = sandbox.repo()
    write_hook(repo, "post-commit", DAMAGED["a lone start marker"])
    githooks.install(info_for(repo), graphify=fake(sandbox))
    before = snapshot(repo)
    report = githooks.remove(info_for(repo))
    assert snapshot(repo)["post-commit"] == before["post-commit"]
    assert results(report)["post-commit"].action == "damaged"


def test_removal_of_a_hook_without_a_block_changes_nothing(sandbox):
    repo = sandbox.repo()
    write_hook(repo, "post-commit", "#!/bin/sh\necho mine\n")
    before = snapshot(repo)
    report = githooks.remove(info_for(repo))
    assert snapshot(repo) == before
    assert results(report)["post-commit"].action == "unchanged"
    assert not report.helper_removed


def test_removal_leaves_a_symlinked_hook_alone(sandbox):
    repo = sandbox.repo()
    target = sandbox.root / "shared-hook"
    target.write_text("#!/bin/sh\n" + expected_block("post-commit"))
    target.chmod(0o755)
    hook_file(repo, "post-commit").symlink_to(target)
    before = target.read_bytes()
    report = githooks.remove(info_for(repo))
    assert target.read_bytes() == before
    assert results(report)["post-commit"].action == "refused"


def test_removal_after_a_file_was_edited_keeps_the_edit(sandbox):
    """A Sidegraph-created file the owner has since extended is kept, minus the block."""
    repo = sandbox.repo()
    info = info_for(repo)
    githooks.install(info, graphify=fake(sandbox))
    path = hook_file(repo, "post-commit")
    path.write_text(path.read_text() + "echo added later\n")
    githooks.remove(info)
    assert path.read_text() == CREATED_HEADER + "echo added later\n"


# -- T8: the main checkout only, and only for a real branch change ----------------------------


def sentinel(sb: Sandbox, main: Path) -> list[Start]:
    """A main-checkout commit whose rebuild is awaited. Whatever else was going to rebuild has
    started by then, so exactly one start in the log proves the earlier action rebuilt nothing."""
    head = commit(main, "sentinel")
    starts = await_rebuilds(sb, info_for(main).common)
    assert [(s.cwd.resolve(), s.head) for s in starts] == [(main.resolve(), head)]
    return starts


def test_t8_a_commit_in_a_linked_worktree_does_not_rebuild(sandbox):
    main = sandbox.repo()
    githooks.install(info_for(main), graphify=fake(sandbox))
    wt = sandbox.root / "wt"
    git(main, "worktree", "add", "-q", "-b", "feature", str(wt))  # post-checkout in the worktree
    commit(wt)  # post-commit in the worktree
    sentinel(sandbox, main)


def test_t8_a_file_checkout_does_not_rebuild(sandbox):
    main = sandbox.repo()
    githooks.install(info_for(main), graphify=fake(sandbox))
    (main / "a.txt").write_text("changed\n")
    git(main, "checkout", "--", "a.txt")  # post-checkout with $3 = 0
    sentinel(sandbox, main)


def test_t8_post_checkout_with_the_branch_flag_off_does_not_rebuild(sandbox):
    """Different shas and flag 0: only the ``$3`` test stops this one (git itself always passes
    equal shas for a file checkout, so the call is made by hand)."""
    main = sandbox.repo()
    old = git(main, "rev-parse", "HEAD")
    new = commit(main)  # before the hooks exist
    githooks.install(info_for(main), graphify=fake(sandbox))
    assert run_helper(main, "post-checkout", old, new, "0") == 0
    sentinel(sandbox, main)


def test_t8_switch_c_does_not_rebuild(sandbox):
    main = sandbox.repo()
    githooks.install(info_for(main), graphify=fake(sandbox))
    git(main, "switch", "-q", "-c", "topic")  # post-checkout with $1 = $2
    sentinel(sandbox, main)


def test_t8_a_branch_switch_to_another_commit_rebuilds_once(sandbox):
    main = sandbox.repo()
    git(main, "switch", "-q", "-c", "other")
    other_head = commit(main, "on other")
    git(main, "switch", "-q", "main")
    githooks.install(info_for(main), graphify=fake(sandbox))
    git(main, "switch", "-q", "other")
    starts = await_rebuilds(sandbox, info_for(main).common)
    assert [(s.cwd.resolve(), s.head) for s in starts] == [(main.resolve(), other_head)]


def test_each_of_the_three_hooks_rebuilds_in_the_main_checkout(sandbox):
    main = sandbox.repo()
    git(main, "switch", "-q", "-c", "b1")
    commit(main, "b1")
    git(main, "switch", "-q", "-c", "b3")
    b3 = commit(main, "b3")
    git(main, "switch", "-q", "main")
    common = info_for(main).common
    githooks.install(info_for(main), graphify=fake(sandbox))

    commit(main, "post-commit")
    assert len(await_rebuilds(sandbox, common, at_least=1)) == 1
    git(main, "switch", "-q", "b1")  # post-checkout
    assert len(await_rebuilds(sandbox, common, at_least=2)) == 2
    git(main, "merge", "-q", "--ff-only", "b3")  # post-merge
    starts = await_rebuilds(sandbox, common, at_least=3)
    assert len(starts) == 3
    assert starts[-1].head == b3


def test_the_rebuild_is_graphify_update_with_a_pinned_hash_seed_and_a_log(sandbox):
    main = sandbox.repo()
    info = info_for(main)
    githooks.install(info, graphify=fake(sandbox))
    commit(main)
    starts = await_rebuilds(sandbox, info.common)
    assert starts[0].args == "update ."
    assert "seed=0" in sandbox.log.read_text()
    log = (info.common / f"{HELPER_NAME}.log").read_text()
    assert log == "fake graphify ran: update .\n"


# -- T9: concurrency --------------------------------------------------------------------


def burst(repo: Path, calls: int, shell: str, *, spread: float = 0.0) -> None:
    """``calls`` helper invocations. With no ``spread`` they are released at the same instant by a
    gate file; with one, each starts ``spread`` seconds after the previous."""
    helper = str(hook_file(repo, HELPER_NAME))
    gate = repo / ".git" / "go"
    gate.unlink(missing_ok=True)
    quiet = {"cwd": repo, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    procs = []
    if spread:
        for _ in range(calls):
            procs.append(subprocess.Popen([shell, helper, "post-commit"], **quiet))
            time.sleep(spread)
    else:
        script = f'while [ ! -e "{gate}" ]; do :; done; exec {shell} "{helper}" post-commit'
        procs = [subprocess.Popen(["sh", "-c", script], **quiet) for _ in range(calls)]
        gate.write_text("")
    for proc in procs:
        assert proc.wait(timeout=30) == 0
    gate.unlink(missing_ok=True)


@pytest.mark.parametrize("shell", SHELLS)
@pytest.mark.parametrize("calls", [2, 20])
def test_t9_simultaneous_calls_never_overlap_on_a_fresh_lock(
    sandbox,
    monkeypatch,
    shell,
    calls,
):
    """With no lock at the start. Afterwards no lock and no want flag remain, which means the
    last request was consumed by a rebuild that started after it."""
    repo = sandbox.repo()
    info = info_for(repo)
    githooks.install(info, graphify=fake(sandbox))
    monkeypatch.setenv("FAKE_GRAPHIFY_SLEEP", "0.2")
    burst(repo, calls, shell)
    starts = await_rebuilds(sandbox, info.common)
    assert starts
    assert max_concurrent(sandbox.log) == 1


def shim(sb: Sandbox, monkeypatch, name: str, body: str) -> None:
    """Put an executable ``name`` first on ``PATH``, running the shell ``body``."""
    folder = sb.root / f"shim-{name}"
    folder.mkdir(exist_ok=True)
    (folder / name).write_text(f"#!/bin/sh\n{body}\n")
    (folder / name).chmod(0o755)
    monkeypatch.setenv("PATH", f"{folder}{os.pathsep}{os.environ['PATH']}")


def slow_lock_removal(sb: Sandbox, monkeypatch, *, delay: float = 0.15) -> None:
    """Put an ``rm`` first on ``PATH`` that dawdles ``delay`` seconds before ``rm -rf <lock>``.
    That widens the window between a stale-breaker's check and its removal, which is where a
    second breaker would delete a lock that the first one already replaced with its own, and the
    window in which a runner has finished but not yet released the lock."""
    shim(
        sb,
        monkeypatch,
        "rm",
        f'case "$*" in *-rf*.lock) sleep {delay} ;; esac\nexec /bin/rm "$@"',
    )


def age(path: Path, minutes: float = 20) -> None:
    """Make ``path``'s modification time ``minutes`` old."""
    past = time.time() - minutes * 60
    os.utime(path, (past, past))


def make_lock(info: githooks.RepoInfo, state: str) -> Path:
    """A lock directory in one of the states a dead job leaves: ``dead pid`` (a pid that no
    longer runs), ``no pid, aged`` and ``empty pid, aged`` (a job that died, or could not write,
    between creating the lock and writing its pid, 20 minutes ago), and the fresh forms of the
    last two, which are an owner that is still writing; and ``live pid``, a runner that runs."""
    lock = info.common / f"{HELPER_NAME}.lock"
    lock.mkdir()
    if state == "dead pid":
        (lock / "pid").write_text(f"{dead_pid()}\n")
        return lock
    if state == "live pid":
        (lock / "pid").write_text(f"{os.getpid()}\n")
        return lock
    if state.startswith("empty pid"):
        (lock / "pid").write_bytes(b"")
    if state.endswith("aged"):
        age(lock)
    return lock


STALE_STATES = ["dead pid", "no pid, aged", "empty pid, aged"]


@pytest.mark.parametrize("shell", SHELLS)
@pytest.mark.parametrize("calls", [2, 20])
@pytest.mark.parametrize("state", STALE_STATES)
def test_t9_simultaneous_calls_never_overlap_on_a_stale_lock(
    sandbox, monkeypatch, shell, calls, state
):
    """The lock's owner is dead, or died before it wrote its pid. One call may break it (the
    ``.gate``); a second must not delete the lock the first one just took, or a later call takes
    it and runs alongside."""
    repo = sandbox.repo()
    info = info_for(repo)
    githooks.install(info, graphify=fake(sandbox))
    make_lock(info, state)
    monkeypatch.setenv("FAKE_GRAPHIFY_SLEEP", "0.3")
    slow_lock_removal(sandbox, monkeypatch)
    # Staggered, so that a second breaker's slow removal lands after the first one has taken the
    # lock, and later calls then find it gone: that is the overlap the gate prevents.
    burst(repo, calls, shell, spread=0.03)
    starts = await_rebuilds(sandbox, info.common)
    assert starts
    assert max_concurrent(sandbox.log) == 1


def test_t9_a_burst_during_a_rebuild_gives_a_rebuild_after_it(sandbox, monkeypatch):
    """The burst lands while a rebuild runs: that rebuild cannot have consumed it, so a second
    one must start after, and never alongside."""
    repo = sandbox.repo()
    info = info_for(repo)
    githooks.install(info, graphify=fake(sandbox))
    monkeypatch.setenv("FAKE_GRAPHIFY_SLEEP", "0.4")
    assert run_helper(repo, "post-commit") == 0
    wait_for(lambda: len(sandbox.starts()) >= 1, what="the first rebuild")
    burst(repo, 20, "sh")
    starts = await_rebuilds(sandbox, info.common, at_least=2)
    assert len(starts) >= 2
    assert max_concurrent(sandbox.log) == 1


# -- T10: a request during a rebuild ----------------------------------------------------


def test_t10_a_request_during_a_rebuild_gives_a_second_rebuild(sandbox, monkeypatch):
    """Commit, then switch away and back while the rebuild runs: HEAD is unchanged at the end,
    and the tree moved in between, so a HEAD comparison would rebuild once, and this must twice."""
    main = sandbox.repo()
    git(main, "switch", "-q", "-c", "other")
    commit(main, "on other")
    git(main, "switch", "-q", "main")
    info = info_for(main)
    githooks.install(info, graphify=fake(sandbox))
    monkeypatch.setenv("FAKE_GRAPHIFY_SLEEP", "0.6")
    head = commit(main)  # the first rebuild starts and sleeps
    wait_for(lambda: len(sandbox.starts()) >= 1, what="the first rebuild")
    git(main, "switch", "-q", "other")
    git(main, "switch", "-q", "main")
    starts = await_rebuilds(sandbox, info.common, at_least=2)
    assert len(starts) >= 2
    assert starts[-1].head == head
    assert max_concurrent(sandbox.log) == 1


# -- a lock without a usable pid ----------------------------------------------------------------


@pytest.mark.parametrize("pid_file", [None, b""], ids=["no pid file", "empty pid file"])
def test_a_lock_without_a_usable_pid_is_broken_once_it_is_old(sandbox, pid_file):
    """The job died (or could not write: a full disk, a quota) between creating the lock and
    writing its pid. Nothing is left to say whether it lives, so only its age can: after ten
    minutes the next call breaks the lock and rebuilds. Before, every rebuild was blocked for
    good, while the reminder said all was well."""
    repo = sandbox.repo()
    info = info_for(repo)
    githooks.install(info, graphify=fake(sandbox))
    lock = info.common / f"{HELPER_NAME}.lock"
    lock.mkdir()
    if pid_file is not None:
        (lock / "pid").write_bytes(pid_file)
    age(lock)
    commit(repo)
    starts = await_rebuilds(sandbox, info.common)
    assert len(starts) == 1


def log_find(sb: Sandbox, monkeypatch, calls: Path) -> None:
    """A ``find`` that gives the real answer, then records the call: a test waits for the record
    to know the helper has decided, whatever it decided."""
    shim(
        sb,
        monkeypatch,
        "find",
        f'out=$(/usr/bin/find "$@")\necho "$*" >> "{calls}"\n[ -z "$out" ] || echo "$out"',
    )


@pytest.mark.parametrize("pid_file", [None, b""], ids=["no pid file", "empty pid file"])
def test_a_fresh_lock_without_a_pid_is_an_owner_still_writing(
    sandbox, monkeypatch, tmp_path, pid_file
):
    repo = sandbox.repo()
    info = info_for(repo)
    githooks.install(info, graphify=fake(sandbox))
    lock = info.common / f"{HELPER_NAME}.lock"
    lock.mkdir()
    if pid_file is not None:
        (lock / "pid").write_bytes(pid_file)
    calls = tmp_path / "find-calls"
    log_find(sandbox, monkeypatch, calls)
    assert run_helper(repo, "post-commit") == 0  # respected: it must not break a fresh lock
    wait_for(calls.exists, what="the helper to look at the lock's age")
    assert (info.common / f"{HELPER_NAME}.want").exists()
    age(lock)  # the sentinel: the same lock, old now, is broken, and nothing ran before it
    assert run_helper(repo, "post-commit") == 0
    starts = await_rebuilds(sandbox, info.common)
    assert len(starts) == 1


def test_a_pid_write_that_fails_does_not_block_later_rebuilds(sandbox):
    """The reproduction: a process that cannot write files takes the lock and leaves it with an
    empty pid file. Ten minutes on, a normal call recovers."""
    repo = sandbox.repo()
    info = info_for(repo)
    githooks.install(info, graphify=fake(sandbox))
    helper = str(info.helper)
    done = subprocess.run(
        ["sh", "-c", 'ulimit -f 0; exec "$0" post-commit', helper],
        cwd=repo,
        capture_output=True,
        check=False,
    )
    assert done.returncode == 0
    lock = info.common / f"{HELPER_NAME}.lock"
    wait_for(lambda: (lock / "pid").exists(), what="the lock the failed write left")
    assert (lock / "pid").read_bytes() == b""
    age(lock)
    commit(repo)
    starts = await_rebuilds(sandbox, info.common)
    assert len(starts) == 1


def test_the_age_is_checked_again_under_the_gate(sandbox, monkeypatch, tmp_path):
    """A call may see an old lock, and reach the gate after a live owner replaced that lock with
    its own, still without a pid. The first ``find`` of the test lies (the lock is "old"); the
    answer under the gate is the real one, and must keep that fresh lock."""
    repo = sandbox.repo()
    info = info_for(repo)
    githooks.install(info, graphify=fake(sandbox))
    lock = info.common / f"{HELPER_NAME}.lock"
    lock.mkdir()  # fresh, and no pid yet: its owner is mid-write
    calls = tmp_path / "find-calls"
    shim(
        sandbox,
        monkeypatch,
        "find",
        f'first=$([ -e "{calls}" ] || echo yes)\necho "$*" >> "{calls}"\n'
        '[ -z "$first" ] || { echo "$1"; exit 0; }\nexec /usr/bin/find "$@"',
    )
    assert run_helper(repo, "post-commit") == 0

    def found_twice() -> bool:
        return calls.exists() and len(calls.read_text().splitlines()) >= 2

    wait_for(found_twice, what="the age to be checked a second time, under the gate")
    assert lock.is_dir()  # the fresh lock was kept
    assert sandbox.starts() == []


@pytest.mark.parametrize("state", STALE_STATES)
def test_install_removes_a_dead_lock(sandbox, state):
    """Re-running ``--hooks`` is the documented way out of a lock that a dead job left."""
    repo = sandbox.repo()
    info = info_for(repo)
    githooks.install(info, graphify=fake(sandbox))
    lock = make_lock(info, state)
    githooks.install(info, graphify=fake(sandbox))
    assert not lock.exists()


@pytest.mark.parametrize("state", ["live pid", "no pid, fresh", "empty pid, fresh"])
def test_install_keeps_a_lock_that_may_be_live(sandbox, state):
    repo = sandbox.repo()
    info = info_for(repo)
    githooks.install(info, graphify=fake(sandbox))
    lock = make_lock(info, state)
    githooks.install(info, graphify=fake(sandbox))
    assert lock.is_dir()


# -- a request while the lock is being released ------------------------------------------------


def test_a_request_while_the_lock_is_being_released_is_not_lost(sandbox, monkeypatch):
    """The runner has finished and is about to release the lock (its removal is slow here); a
    call arrives in that gap, finds the lock held, and leaves. The runner must look at the flag
    again after releasing, or that request is lost and the flag is left over."""
    repo = sandbox.repo()
    info = info_for(repo)
    githooks.install(info, graphify=fake(sandbox))
    slow_lock_removal(sandbox, monkeypatch, delay=0.4)
    assert run_helper(repo, "post-commit") == 0
    wait_for(lambda: count_ends(sandbox.log) >= 1, what="the first rebuild to end")
    time.sleep(0.15)  # inside the 0.4 s the lock removal takes
    assert run_helper(repo, "post-commit") == 0
    starts = await_rebuilds(sandbox, info.common, at_least=2)
    assert len(starts) == 2
    assert idle(info.common)


# -- T13: Graphify's own hook ----------------------------------------------------------


REAL_GRAPHIFY = shutil.which("graphify")


@pytest.mark.skipif(REAL_GRAPHIFY is None, reason="graphify is not installed")
def test_t13_graphifys_own_hook_and_ours_leave_each_other_byte_identical(sandbox):
    repo = sandbox.repo()
    info = info_for(repo)
    githooks.install(info, graphify=fake(sandbox))
    ours = snapshot(repo)

    subprocess.run([REAL_GRAPHIFY, "hook", "install"], cwd=repo, check=True, capture_output=True)
    both = snapshot(repo)
    assert both != ours
    report = githooks.install(info, graphify=fake(sandbox))  # refresh ours
    assert snapshot(repo) == both  # Graphify's blocks, after ours, are untouched
    assert report.graphify_hook

    subprocess.run([REAL_GRAPHIFY, "hook", "uninstall"], cwd=repo, check=True, capture_output=True)
    assert snapshot(repo) == ours


def test_graphifys_markers_are_reported(sandbox):
    repo = sandbox.repo()
    write_hook(
        repo,
        "post-commit",
        "#!/bin/sh\n# graphify-hook-start\necho graphify\n# graphify-hook-end\n",
    )
    assert githooks.install(info_for(repo), graphify=fake(sandbox)).graphify_hook
    other = sandbox.repo("second")
    assert not githooks.install(info_for(other), graphify=fake(sandbox)).graphify_hook


# -- T14: the scripts are valid shell ------------------------------------------------------


@pytest.mark.parametrize("shell", SHELLS)
def test_t14_the_helper_and_the_hooks_parse(sandbox, shell):
    repo = sandbox.repo()
    githooks.install(info_for(repo), graphify=fake(sandbox))
    for path in [hook_file(repo, HELPER_NAME), *(hook_file(repo, h) for h in HOOKS)]:
        done = subprocess.run([shell, "-n", str(path)], capture_output=True, text=True)
        assert done.returncode == 0, done.stderr


@pytest.mark.parametrize("shell", ["sh", "bash", "dash", "zsh", "ksh"])
@pytest.mark.parametrize("flags", ["-e", "-u", "-eu"])
def test_the_block_line_passes_strict_shebangs(sandbox, shell, flags):
    if not have_shell(shell):
        pytest.skip(f"{shell} is not installed")
    repo = sandbox.repo()
    githooks.install(info_for(repo), graphify=fake(sandbox))
    script = sandbox.root / "strict-hook"
    script.write_text(f"#!/usr/bin/env {shell}\n" + expected_block("post-commit"))
    script.chmod(0o755)
    done = subprocess.run([shell, flags, str(script)], cwd=repo, capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    await_rebuilds(sandbox, info_for(repo).common)
    assert len(sandbox.starts()) == 1


# -- T15: the install-time graphify path ---------------------------------------------------


def only_system_path() -> dict:
    env = dict(os.environ)
    env["PATH"] = "/usr/bin:/bin"
    return env


def test_t15_a_path_with_a_space_is_quoted_and_used_when_path_has_no_graphify(
    sandbox,
    tmp_path,
):
    spaced = tmp_path / "bin with space"
    spaced.mkdir()
    shutil.copy(sandbox.fake_bin / "graphify", spaced / "graphify")
    (spaced / "graphify").chmod(0o755)
    repo = sandbox.repo()
    info = info_for(repo)
    githooks.install(info, graphify=str(spaced / "graphify"))
    assert run_helper(repo, "post-commit", env=only_system_path()) == 0
    starts = await_rebuilds(sandbox, info.common)
    assert len(starts) == 1


def test_t15_a_path_with_a_newline_is_not_recorded(sandbox):
    repo = sandbox.repo()
    info = info_for(repo)
    githooks.install(info, graphify="/opt/new\nline/graphify")
    text = info.helper.read_text()
    assert "/opt/new" not in text
    assert "graphify=$(command -v graphify 2>/dev/null) || graphify=''\n" in text
    githooks.install(info, graphify="/opt/nul\0byte/graphify")
    assert "/opt/nul" not in info.helper.read_text()
    assert run_helper(repo, "post-commit", env=only_system_path()) == 0  # no graphify: quietly out


def test_a_recorded_path_that_is_gone_exits_quietly(sandbox, tmp_path):
    repo = sandbox.repo()
    info = info_for(repo)
    githooks.install(info, graphify=str(tmp_path / "gone" / "graphify"))
    assert run_helper(repo, "post-commit", env=only_system_path()) == 0
    assert idle(info.common)


# -- D4: the recorded choice -----------------------------------------------------------------


def test_the_choice_round_trips_through_the_local_config(sandbox):
    repo = sandbox.repo()
    assert githooks.read_choice(repo) is None
    assert githooks.record_choice(repo, declined=True)
    assert git(repo, "config", "--local", "--get", "sidegraph.graphRefresh") == "false"
    assert githooks.read_choice(repo) == githooks.Choice(declined=True, scope="local")
    assert githooks.record_choice(repo, declined=False)
    assert githooks.read_choice(repo) is None
    assert githooks.record_choice(repo, declined=False)  # an absent key is fine


def test_a_raw_no_in_the_config_reads_as_declined(sandbox):
    repo = sandbox.repo()
    git(repo, "config", "--local", "sidegraph.graphRefresh", "no")
    choice = githooks.read_choice(repo)
    assert choice is not None and choice.declined


def test_a_global_false_counts_as_declined_and_survives_the_local_unset(sandbox):
    repo = sandbox.repo()
    git(repo, "config", "--global", "sidegraph.graphRefresh", "false")
    assert githooks.read_choice(repo) == githooks.Choice(declined=True, scope="global")
    githooks.record_choice(repo, declined=False)  # the unset of the local value
    choice = githooks.read_choice(repo)
    assert choice is not None and choice.declined and choice.scope == "global"


def test_a_true_value_is_not_declined(sandbox):
    repo = sandbox.repo()
    git(repo, "config", "--local", "sidegraph.graphRefresh", "yes")
    assert githooks.read_choice(repo) == githooks.Choice(declined=False, scope="local")


def test_the_choice_from_a_linked_worktree_lands_in_the_common_config(sandbox):
    repo = sandbox.repo()
    git(repo, "config", "extensions.worktreeConfig", "true")
    wt = sandbox.root / "wt"
    git(repo, "worktree", "add", "-q", "-b", "feature", str(wt))
    assert githooks.record_choice(wt, declined=True)
    assert git(repo, "config", "--local", "--get", "sidegraph.graphRefresh") == "false"


# -- status helpers ------------------------------------------------------------------------


def test_is_installed_needs_the_helper_and_all_three_blocks(sandbox):
    repo = sandbox.repo()
    info = info_for(repo)
    assert not githooks.status(info).installed
    githooks.install(info, graphify=fake(sandbox))
    assert githooks.status(info).installed
    hook_file(repo, "post-merge").unlink()
    assert not githooks.status(info).installed
    githooks.install(info, graphify=fake(sandbox))
    info.helper.unlink()
    assert not githooks.status(info).installed


def test_is_wired_counts_a_call_added_by_hand(sandbox):
    repo = sandbox.repo()
    info = info_for(repo)
    assert not githooks.status(info).wired
    for hook in HOOKS:
        write_hook(repo, hook, f"#!/bin/sh\n{githooks.manual_line(hook)}\n")
    githooks.install(info, graphify=fake(sandbox))  # the helper (the blocks are already there)
    assert githooks.status(info).wired
    info.helper.unlink()
    assert not githooks.status(info).wired


def wire_all(repo: Path, body, mode: int = 0o755) -> None:
    """Every hook gets ``body(hook)`` as its text after the shebang line, at ``mode``."""
    for hook in HOOKS:
        write_hook(repo, hook, "#!/bin/sh\n" + body(hook), mode)


def test_a_hook_that_only_names_the_helper_in_a_comment_is_not_wired(sandbox):
    """A comment runs nothing, so a hook that holds one is not wired (D7 would read clean while
    no refresh runs)."""
    repo = sandbox.repo()
    info = info_for(repo)
    githooks.install_helper(info, graphify=fake(sandbox))
    wire_all(repo, lambda hook: f"# TODO: call {HELPER_NAME} {hook} from here\n")
    assert not githooks.status(info).wired
    wire_all(repo, lambda hook: f"   \t# {githooks.manual_line(hook)}\n")  # indented, commented out
    assert not githooks.status(info).wired


def test_one_hook_that_only_comments_the_helper_out_is_not_wired(sandbox):
    repo = sandbox.repo()
    info = info_for(repo)
    githooks.install_helper(info, graphify=fake(sandbox))
    wire_all(repo, githooks.manual_line)
    assert githooks.status(info).wired
    write_hook(repo, "post-merge", f"#!/bin/sh\n# {githooks.manual_line('post-merge')}\n")
    assert not githooks.status(info).wired


def test_a_hook_git_would_not_run_is_not_wired(sandbox):
    """Git ignores a hook that is not executable, whatever line it carries."""
    repo = sandbox.repo()
    info = info_for(repo)
    githooks.install_helper(info, graphify=fake(sandbox))
    wire_all(repo, githooks.manual_line, 0o644)
    assert not githooks.status(info).wired
    write_hook(repo, "post-commit", "#!/bin/sh\n" + githooks.manual_line("post-commit"))
    write_hook(repo, "post-merge", "#!/bin/sh\n" + githooks.manual_line("post-merge"))
    assert not githooks.status(info).wired  # post-checkout is still not executable
    write_hook(repo, "post-checkout", "#!/bin/sh\n" + githooks.manual_line("post-checkout"))
    assert githooks.status(info).wired


def test_the_installed_block_is_wired_and_a_real_line_after_a_comment_counts(sandbox):
    repo = sandbox.repo()
    info = info_for(repo)
    githooks.install(info, graphify=fake(sandbox))
    assert githooks.status(info).wired  # Sidegraph's own block line
    wire_all(repo, lambda hook: f"# see {HELPER_NAME}\n  {githooks.manual_line(hook)}\n")
    assert githooks.status(info).wired  # an indented real line after a comment


def test_repo_info_outside_a_repository_is_none(tmp_path):
    assert githooks.repo_info(tmp_path / "nowhere") is None
