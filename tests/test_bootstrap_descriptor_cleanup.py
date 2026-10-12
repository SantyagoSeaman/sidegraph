"""A failed setup must close the owned descriptor before removing its temporary path."""

from __future__ import annotations

import os

import pytest

from sidegraph.bootstrap import review
from sidegraph.host import claude_settings
from tests.test_bootstrap_review import candidate


def _same_file(fd, identity):
    try:
        stat = os.fstat(fd)
    except OSError:
        return False
    return (stat.st_dev, stat.st_ino) == identity


@pytest.mark.parametrize("failure", ["fchmod", "fdopen"])
def test_editor_setup_failure_closes_its_descriptor_before_unlink(tmp_path, monkeypatch, failure):
    observed = {}
    original = review.tempfile.mkstemp

    def create(*args, **kwargs):
        fd, name = original(*args, **kwargs, dir=tmp_path)
        stat = os.fstat(fd)
        observed.update(fd=fd, name=name, identity=(stat.st_dev, stat.st_ino))
        return fd, name

    def fail(*args, **kwargs):
        raise OSError("setup failed")

    monkeypatch.setattr(review.tempfile, "mkstemp", create)
    monkeypatch.setattr(review.os, failure, fail)
    invoked = []
    try:
        with pytest.raises(OSError):
            review.edit_candidate(candidate(), editor="editor", run_editor=invoked.append)
        assert not _same_file(observed["fd"], observed["identity"]), "original fd leaked"
        assert not list(tmp_path.iterdir())
        assert not invoked
    finally:
        if _same_file(observed["fd"], observed["identity"]):
            os.close(observed["fd"])
        if os.path.exists(observed["name"]):
            os.unlink(observed["name"])


@pytest.mark.parametrize("operation", ["read", "replace"])
def test_settings_fdopen_failure_closes_the_original_file(tmp_path, monkeypatch, operation):
    path = tmp_path / ".claude" / "settings.json"
    path.parent.mkdir()
    path.write_text("{}\n", encoding="utf-8")
    original = path.read_bytes()
    observed = {}

    def fail(fd, *args, **kwargs):
        stat = os.fstat(fd)
        observed.update(fd=fd, identity=(stat.st_dev, stat.st_ino))
        raise OSError("fdopen failed")

    monkeypatch.setattr(claude_settings.os, "fdopen", fail)
    try:
        if operation == "read":
            assert claude_settings.ensure_ratify_policy_setting(tmp_path).outcome == "skipped"
        else:
            with pytest.raises(OSError):
                claude_settings._replace_atomically(path, '{"name": "changed"}\n')
        assert not _same_file(observed["fd"], observed["identity"]), "original fd leaked"
        assert path.read_bytes() == original
        assert list(path.parent.iterdir()) == [path]
    finally:
        if _same_file(observed["fd"], observed["identity"]):
            os.close(observed["fd"])
        for temp in path.parent.glob("*.tmp"):
            temp.unlink()
