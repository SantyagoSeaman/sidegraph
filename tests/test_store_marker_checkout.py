"""Cold Store opens cannot publish into Git's missing-format window.

Only marker backfill changes; existing markers, unavailable Git and intentional committed
removals retain their behavior.
# see design/superpowers/specs/2026-10-28-format-marker-checkout-race-design.md
"""

# ruff: noqa: F811

from __future__ import annotations

import shlex
import subprocess
import threading
from pathlib import Path

import pytest

from sidegraph import checkout_window as cw
from sidegraph import cli
from sidegraph import store as store_module
from sidegraph.store import Store
from sidegraph.store_format import marker_text
from sidegraph.store_layout import StoreRequirementError, store_marker, store_requirement
from tests.store_format2_support import (
    commit_all,
    entity_body,
    git,
    git_env,
    git_repo,  # noqa: F401
    legacy_store,
    make_store,
    put_file,
)

_DATE = "2026-10-01T00:00:00Z"


def test_head_marker_with_idle_git_refuses_and_writes_nothing(tmp_path, monkeypatch):
    store = legacy_store(tmp_path / "s")
    monkeypatch.setattr(cw, "git_view", lambda _: cw.GitView(True, tmp_path / "index.lock"))
    with pytest.raises(StoreRequirementError) as caught:
        Store(store)
    assert caught.value.kind == "missing"
    assert not (store / "format").exists()


def test_lock_held_takes_the_marker_git_writes(tmp_path, monkeypatch):
    store = legacy_store(tmp_path / "s")
    lock = tmp_path / "index.lock"
    lock.touch()
    monkeypatch.setattr(cw, "git_view", lambda _: cw.GitView(False, lock))
    real_create = store_module._create_format_marker
    calls = []

    def create(*args):
        calls.append(args)
        return real_create(*args)

    monkeypatch.setattr(store_module, "_create_format_marker", create)
    expected = marker_text(1, "0.16.0", None)

    def finish():
        (store / "format").write_text(expected)
        lock.unlink()

    timer = threading.Timer(0.1, finish)
    timer.start()
    try:
        with Store(store):
            pass
    finally:
        timer.join(timeout=2)
    assert not timer.is_alive()
    assert calls == []
    assert (store / "format").read_text() == expected
    assert store_requirement(store) == "0.16.0"


def test_lock_held_past_the_wait_refuses(tmp_path, monkeypatch):
    store = legacy_store(tmp_path / "s")
    lock = tmp_path / "index.lock"
    lock.touch()
    monkeypatch.setattr(cw, "git_view", lambda _: cw.GitView(False, lock))
    monkeypatch.setattr(cw, "MARKER_WAIT_SECONDS", 0.04)
    with pytest.raises(StoreRequirementError) as caught:
        Store(store)
    assert caught.value.kind == "missing"
    assert caught.value.lock == lock
    assert not (store / "format").exists()


@pytest.mark.parametrize("existing_records", [False, True])
def test_unanswered_git_keeps_todays_backfill(tmp_path, monkeypatch, existing_records):
    """Guard: non-git/unavailable Git keeps both historical marker choices."""
    store = legacy_store(tmp_path / "s") if existing_records else tmp_path / "s"
    monkeypatch.setattr(cw, "git_view", lambda _: None)
    Store(store).close()
    if existing_records:
        assert (store / "format").read_text() == "sidegraph-store 0.6.0\n"
    else:
        assert store_marker(store).major == 2


def test_present_marker_runs_no_git(tmp_path, monkeypatch):
    def fail(_):
        raise AssertionError("git ran")

    monkeypatch.setattr(cw, "git_view", fail)
    store = make_store(tmp_path)
    Store(store).close()
    Store(store).close()


@pytest.mark.parametrize("space", [False, True])
def test_deleted_committed_marker_is_refused_and_restorable(git_repo, space):
    parent = git_repo / "space dir" if space else git_repo
    store = make_store(parent, fmt=1)
    put_file(store, "entity", entity_body("E0001"))
    Store(store).close()
    commit_all(git_repo, "store", date=_DATE)
    (store / "format").unlink()
    with pytest.raises(StoreRequirementError) as caught:
        Store(store)
    assert caught.value.kind == "missing"
    command = str(caught.value).split("restore it with: ", 1)[1].split(" (to remove", 1)[0]
    subprocess.run(
        shlex.split(command),
        check=True,
        capture_output=True,
        stdin=subprocess.DEVNULL,
        env=git_env(),
        timeout=10,
    )
    Store(store).close()
    assert store_requirement(store) == "0.16.0"


def test_committed_removal_backfills_as_before(git_repo):
    """Guard: committing the removal is intentional, unlike a checkout window."""
    store = make_store(git_repo, fmt=1)
    put_file(store, "entity", entity_body("E0001"))
    Store(store).close()
    commit_all(git_repo, "store", date=_DATE)
    git(git_repo, "rm", "-q", ".sidegraph/format")
    commit_all(git_repo, "remove marker", date=_DATE)
    Store(store).close()
    assert (store / "format").read_text() == "sidegraph-store 0.6.0\n"


@pytest.mark.parametrize("committed", [False, True])
def test_fresh_store_in_a_repository(git_repo, committed):
    """Guard: new stores in unborn and existing repositories still initialize."""
    if committed:
        (git_repo / "README").write_text("root\n")
        commit_all(git_repo, "root", date=_DATE)
    store = git_repo / ".sidegraph"
    Store(store).close()
    assert store_marker(store).major == 2


@pytest.mark.parametrize(
    "creating,existing_records", [(False, False), (False, True), (True, False)]
)
def test_store_creation_is_the_only_busy_timeout_exception(
    tmp_path, monkeypatch, creating, existing_records
):
    store = legacy_store(tmp_path / "s") if existing_records else tmp_path / "s"
    monkeypatch.setattr(cw, "MARKER_WAIT_SECONDS", 0.02)
    real_run = subprocess.run

    def timeout(command, **kwargs):
        if command[0] == "git" and "rev-parse" in command:
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        return real_run(command, **kwargs)

    monkeypatch.setattr(cw.subprocess, "run", timeout)
    if creating:
        Store(store, creating=True).close()
        assert store_marker(store).major == 2
    else:
        with pytest.raises(StoreRequirementError) as caught:
            Store(store)
        assert caught.value.busy and caught.value.kind == "missing"
        assert not (store / "format").exists()


@pytest.mark.parametrize("existing", [False, True])
def test_init_declares_creation_only_for_its_new_store_path(tmp_path, monkeypatch, existing):
    """Existing-store half guards unchanged behavior; fresh half is red before wiring."""
    store = make_store(tmp_path) if existing else tmp_path / "fresh"
    calls = []

    def open_store(path, **kwargs):
        calls.append((Path(path), kwargs.get("creating", False)))
        raise RuntimeError("stop after observing the creation declaration")

    monkeypatch.setattr(cli, "Store", open_store)
    assert cli.init_main(["--db", str(store), "--no-settings", "--no-hooks"]) == 1
    assert calls == [(store, not existing)]


@pytest.mark.parametrize("existing_records", [False, True])
def test_init_refuses_busy_git_for_an_existing_markerless_store(
    git_repo, monkeypatch, capsys, existing_records
):
    """Marker absence cannot declare creation, even when no records have arrived."""
    store = make_store(git_repo, fmt=2, n=256)
    (store / "format").write_text(marker_text(2, "0.19.0", 256))
    if existing_records:
        put_file(store, "entity", entity_body("E0001"))
    Store(store).close()
    commit_all(git_repo, "existing store", date=_DATE)
    (store / "format").unlink()
    monkeypatch.setattr(cw, "git_view", lambda _: cw.GIT_BUSY)
    monkeypatch.setattr(cw, "MARKER_WAIT_SECONDS", 0.0)
    rc = cli.init_main(["--db", str(store), "--no-settings", "--no-hooks"])
    stdout, stderr = capsys.readouterr()
    actual = (store / "format").read_text() if (store / "format").exists() else None
    assert rc == 1, (stdout, stderr, actual)
    assert "git did not answer in time" in stdout
    assert actual is None
