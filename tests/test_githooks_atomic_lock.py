"""Exclusive lock acquisition and upgrade from directory-based graph refresh helpers."""

from __future__ import annotations

import os

import pytest

from sidegraph import githooks
from tests import githooks_support
from tests.githooks_support import (
    HELPER_NAME,
    age,
    await_rebuilds,
    burst,
    dead_pid,
    fake,
    info_for,
    shim,
)

sandbox = githooks_support.sandbox


@pytest.mark.slow
def test_two_refreshes_do_not_overlap_when_external_mkdir_lies(sandbox, monkeypatch):
    """Reproduce a mkdir that reports success to both racers, including the EEXIST loser."""
    repo = sandbox.repo()
    info = info_for(repo)
    githooks.install(info, graphify=fake(sandbox))
    probe = sandbox.root / "mkdir-racer"
    shim(
        sandbox,
        monkeypatch,
        "mkdir",
        f'''case "$1" in
  *.lock)
    /bin/mkdir "$@" 2>/dev/null
    : > "{probe}.$$"
    i=0
    while [ "$i" -lt 200 ]; do
      set -- "{probe}".*
      [ "$#" -lt 2 ] || exit 0
      i=$((i+1)); sleep 0.01
    done
    exit 1 ;;
esac
exec /bin/mkdir "$@"''',
    )
    # Hold the first runner before it consumes want, so a second owner also enters
    # the rebuild loop; this makes the ownership error observable as overlap.
    shim(sandbox, monkeypatch, "rm", 'case "$*" in *.want) sleep 0.2 ;; esac\nexec /bin/rm "$@"')
    monkeypatch.setenv("FAKE_GRAPHIFY_SLEEP", "0.4")
    burst(repo, 2, "sh")
    assert await_rebuilds(sandbox, info.common)
    assert sandbox.max_concurrent() == 1, sandbox.log.read_text()


@pytest.mark.parametrize("suffix", ["lock", "lock.gate"])
@pytest.mark.parametrize("state", ["dead", "live", "empty-old", "empty-fresh"])
def test_install_handles_pid_files_without_removing_live_owners(sandbox, suffix, state):
    repo = sandbox.repo()
    info = info_for(repo)
    githooks.install(info, graphify=fake(sandbox))
    path = info.common / f"{HELPER_NAME}.{suffix}"
    text = f"{dead_pid()}\n" if state == "dead" else f"{os.getpid()}\n" if state == "live" else ""
    path.write_text(text)
    if state in {"live", "empty-old"}:
        age(path)
    githooks.install_helper(info, graphify=fake(sandbox))
    assert path.exists() is (state in {"live", "empty-fresh"})
    if path.exists():
        assert path.read_text() == text


@pytest.mark.parametrize("shape", ["file", "directory"])
def test_remove_cleans_both_lock_and_gate_shapes(sandbox, shape):
    repo = sandbox.repo()
    info = info_for(repo)
    githooks.install(info, graphify=fake(sandbox))
    paths = [info.common / f"{HELPER_NAME}.{suffix}" for suffix in ["lock", "lock.gate"]]
    for path in paths:
        if shape == "directory":
            path.mkdir()
            (path / "pid").write_text("1\n")
        else:
            path.write_text("1\n")
    githooks.remove(info)
    assert not any(path.exists() for path in paths)


def run_foreground_helper(info, repo):
    """Wait for the actual helper body; only detach/background redirection is disabled."""
    info.helper.write_text(
        info.helper.read_text().replace(
            ") </dev/null >/dev/null 2>&1 &", ") </dev/null >/dev/null 2>&1"
        )
    )
    assert githooks_support.run_helper(repo, "post-commit") == 0


@pytest.mark.slow
@pytest.mark.parametrize("state", ["dead", "live-old", "empty-old", "empty-fresh"])
def test_helper_recovers_dead_pid_files_and_respects_possible_live_owners(sandbox, state):
    repo = sandbox.repo()
    info = info_for(repo)
    githooks.install(info, graphify=fake(sandbox))
    lock = info.common / f"{HELPER_NAME}.lock"
    text = (
        f"{dead_pid()}\n" if state == "dead" else f"{os.getpid()}\n" if state == "live-old" else ""
    )
    lock.write_text(text)
    if state.endswith("old"):
        age(lock)
    run_foreground_helper(info, repo)
    if state in {"dead", "empty-old"}:
        assert len(sandbox.starts()) == 1
        assert not lock.exists()
    else:
        assert sandbox.starts() == []
        assert lock.read_text() == text
        assert (info.common / f"{HELPER_NAME}.want").exists()


@pytest.mark.slow
def test_held_file_gate_survives_even_when_external_mkdir_lies(sandbox, monkeypatch):
    repo = sandbox.repo()
    info = info_for(repo)
    githooks.install(info, graphify=fake(sandbox))
    lock = info.common / f"{HELPER_NAME}.lock"
    lock.write_text(f"{dead_pid()}\n")
    before = lock.read_bytes()
    gate = info.common / f"{HELPER_NAME}.lock.gate"
    gate.write_text(f"{os.getpid()}\n")
    gate_before = gate.read_bytes()
    shim(sandbox, monkeypatch, "mkdir", "exit 0")
    run_foreground_helper(info, repo)
    assert lock.read_bytes() == before
    assert gate.read_bytes() == gate_before
    assert sandbox.starts() == []


@pytest.mark.slow
def test_installer_holds_the_breaker_gate_while_removing_a_dead_lock(sandbox, monkeypatch):
    """A hook arriving after the dead-owner decision must not replace the installer's lock."""
    repo = sandbox.repo()
    info = info_for(repo)
    githooks.install(info, graphify=fake(sandbox))
    lock = info.common / f"{HELPER_NAME}.lock"
    lock.write_text(f"{dead_pid()}\n")
    remove = githooks._remove_lock

    def competing_hook(path):
        if path == lock:
            run_foreground_helper(info, repo)
            assert sandbox.starts() == [], "hook replaced the lock during installer cleanup"
        remove(path)

    monkeypatch.setattr(githooks, "_remove_lock", competing_hook)
    githooks.install_helper(info, graphify=fake(sandbox))
    run_foreground_helper(info, repo)
    assert len(sandbox.starts()) == 1


def test_installer_keeps_a_lock_replaced_during_the_pid_liveness_check(sandbox, monkeypatch):
    repo = sandbox.repo()
    info = info_for(repo)
    githooks.install(info, graphify=fake(sandbox))
    lock = info.common / f"{HELPER_NAME}.lock"
    previous = dead_pid()
    lock.write_text(f"{previous}\n")
    real_kill = os.kill

    def replacement_during_lookup(pid, signal):
        if pid == previous:
            replacement = lock.with_suffix(".replacement")
            replacement.write_text(f"{os.getpid()}\n")
            replacement.replace(lock)
            raise ProcessLookupError
        return real_kill(pid, signal)

    monkeypatch.setattr(os, "kill", replacement_during_lookup)
    githooks.install_helper(info, graphify=fake(sandbox))
    assert lock.read_text() == f"{os.getpid()}\n"


@pytest.mark.slow
def test_a_failed_gate_pid_write_does_not_leave_a_permanent_breaker_gate(sandbox):
    import subprocess

    repo = sandbox.repo()
    info = info_for(repo)
    githooks.install(info, graphify=fake(sandbox))
    lock = info.common / f"{HELPER_NAME}.lock"
    lock.write_text(f"{dead_pid()}\n")
    info.helper.write_text(
        info.helper.read_text().replace(
            ") </dev/null >/dev/null 2>&1 &", ") </dev/null >/dev/null 2>&1"
        )
    )
    result = subprocess.run(
        ["sh", "-c", 'ulimit -f 0; exec "$0" post-commit', str(info.helper)],
        cwd=repo,
        capture_output=True,
    )
    assert result.returncode == 0
    assert not (info.common / f"{HELPER_NAME}.lock.gate").exists()
    age(lock)
    assert githooks_support.run_helper(repo, "post-commit") == 0
    assert len(sandbox.starts()) == 1


@pytest.mark.skipif(os.name == "nt", reason="POSIX advisory-lock failure")
def test_helper_install_still_publishes_when_native_cleanup_lock_is_unsupported(
    sandbox, monkeypatch
):
    import errno
    import fcntl

    repo = sandbox.repo()
    info = info_for(repo)

    def unsupported(*args):
        raise OSError(errno.ENOLCK, "no native locks")

    monkeypatch.setattr(fcntl, "flock", unsupported)
    githooks.install_helper(info, graphify=fake(sandbox))
    run_foreground_helper(info, repo)
    assert len(sandbox.starts()) == 1


def test_windows_pid_probe_is_never_a_signal_from_python(sandbox, monkeypatch):
    from types import SimpleNamespace

    repo = sandbox.repo()
    info = info_for(repo)
    lock = info.common / f"{HELPER_NAME}.lock"
    lock.write_text(f"{os.getpid()}\n")
    age(lock)
    monkeypatch.setattr(githooks, "sys", SimpleNamespace(platform="win32"))

    def unsafe_signal(*args):
        raise AssertionError("Windows signal 0 is not a POSIX liveness probe")

    monkeypatch.setattr(os, "kill", unsafe_signal)
    assert githooks._lock_is_dead(lock) is False
