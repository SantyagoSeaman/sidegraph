"""Tests for the host-seam helper that writes SIDEGRAPH_RATIFY_POLICY into a project's
`.claude/settings.json` at `sidegraph-init` time (Task 1). Pure file-in/file-out behavior,
no store, no CLI, so these stay isolated from `test_cli_init.py`'s integration checks."""

import json
import os

import pytest

from sidegraph.host.claude_settings import (
    RATIFY_POLICY_DEFAULT,
    RATIFY_POLICY_ENV_VAR,
    current_ratify_policy,
    ensure_ratify_policy_setting,
)


def test_creates_settings_file_when_absent(tmp_path):
    result = ensure_ratify_policy_setting(tmp_path)
    settings_path = tmp_path / ".claude" / "settings.json"
    assert result.outcome == "written"
    assert settings_path.is_file()
    data = json.loads(settings_path.read_text())
    assert data == {"env": {RATIFY_POLICY_ENV_VAR: RATIFY_POLICY_DEFAULT}}
    assert settings_path.read_text().endswith("\n")


def test_merges_into_existing_settings_preserving_other_keys(tmp_path):
    settings_path = tmp_path / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(
        json.dumps({"hooks": {"SessionStart": []}, "env": {"OTHER_VAR": "1"}}) + "\n"
    )
    result = ensure_ratify_policy_setting(tmp_path)
    assert result.outcome == "written"
    data = json.loads(settings_path.read_text())
    assert data["hooks"] == {"SessionStart": []}
    assert data["env"]["OTHER_VAR"] == "1"
    assert data["env"][RATIFY_POLICY_ENV_VAR] == RATIFY_POLICY_DEFAULT


def test_adds_env_block_when_missing_entirely(tmp_path):
    settings_path = tmp_path / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(json.dumps({"hooks": {}}) + "\n")
    result = ensure_ratify_policy_setting(tmp_path)
    assert result.outcome == "written"
    data = json.loads(settings_path.read_text())
    assert data["hooks"] == {}
    assert data["env"][RATIFY_POLICY_ENV_VAR] == RATIFY_POLICY_DEFAULT


def test_never_overwrites_an_existing_policy_value(tmp_path):
    settings_path = tmp_path / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(json.dumps({"env": {RATIFY_POLICY_ENV_VAR: "manual"}}) + "\n")
    result = ensure_ratify_policy_setting(tmp_path)
    assert result.outcome == "already_set"
    assert result.existing_value == "manual"
    data = json.loads(settings_path.read_text())
    assert data == {"env": {RATIFY_POLICY_ENV_VAR: "manual"}}


def test_second_run_is_idempotent_and_reports_already_set(tmp_path):
    ensure_ratify_policy_setting(tmp_path)
    result = ensure_ratify_policy_setting(tmp_path)
    assert result.outcome == "already_set"
    assert result.existing_value == RATIFY_POLICY_DEFAULT


def test_invalid_json_is_never_touched(tmp_path):
    settings_path = tmp_path / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True)
    original = "{not valid json"
    settings_path.write_text(original)
    result = ensure_ratify_policy_setting(tmp_path)
    assert result.outcome == "skipped"
    assert settings_path.read_text() == original  # untouched, byte for byte


def test_non_object_json_is_never_touched(tmp_path):
    settings_path = tmp_path / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(json.dumps(["not", "an", "object"]))
    result = ensure_ratify_policy_setting(tmp_path)
    assert result.outcome == "skipped"
    assert json.loads(settings_path.read_text()) == ["not", "an", "object"]


def test_non_object_env_block_is_never_touched(tmp_path):
    settings_path = tmp_path / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(json.dumps({"env": "not-an-object"}) + "\n")
    result = ensure_ratify_policy_setting(tmp_path)
    assert result.outcome == "skipped"
    data = json.loads(settings_path.read_text())
    assert data == {"env": "not-an-object"}


def test_ensure_writes_an_explicit_non_default_value(tmp_path):
    """`init_main` writes whatever the caller decided (a declined prompt, an explicit
    --ratify-policy flag) -- this function must not hardcode RATIFY_POLICY_DEFAULT."""
    result = ensure_ratify_policy_setting(tmp_path, "manual")
    settings_path = tmp_path / ".claude" / "settings.json"
    assert result.outcome == "written"
    data = json.loads(settings_path.read_text())
    assert data == {"env": {RATIFY_POLICY_ENV_VAR: "manual"}}


# -- current_ratify_policy: the read-only peek init_main uses to decide whether asking is
# -- even worth it -------------------------------------------------------------------


def test_current_policy_is_unset_when_file_absent(tmp_path):
    result = current_ratify_policy(tmp_path)
    assert result.outcome == "unset"
    assert not (tmp_path / ".claude" / "settings.json").exists()  # peek never creates it


def test_current_policy_is_unset_when_file_present_without_the_key(tmp_path):
    settings_path = tmp_path / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(json.dumps({"hooks": {}}) + "\n")
    result = current_ratify_policy(tmp_path)
    assert result.outcome == "unset"


def test_current_policy_reports_already_set(tmp_path):
    settings_path = tmp_path / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(json.dumps({"env": {RATIFY_POLICY_ENV_VAR: "auto-all"}}) + "\n")
    result = current_ratify_policy(tmp_path)
    assert result.outcome == "already_set"
    assert result.existing_value == "auto-all"


def test_current_policy_reports_skipped_for_unsafe_file(tmp_path):
    settings_path = tmp_path / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text("{not valid json")
    result = current_ratify_policy(tmp_path)
    assert result.outcome == "skipped"
    assert result.skip_reason == "invalid_json"
    assert settings_path.read_text() == "{not valid json"  # peek never writes


# -- symlinks are never written through; the merge is published atomically ------------


def test_broken_settings_symlink_is_skipped_not_raised(tmp_path):
    for target_dir_exists in (False, True):
        root = tmp_path / f"repo_{target_dir_exists}"
        (root / ".claude").mkdir(parents=True)
        outside = tmp_path / f"outside_{target_dir_exists}"
        if target_dir_exists:
            outside.mkdir()
        target = outside / "settings.json"
        (root / ".claude" / "settings.json").symlink_to(target)
        result = ensure_ratify_policy_setting(root)
        assert result.outcome == "skipped"
        assert result.skip_reason == "symlink"
        assert not target.exists()


def test_live_settings_symlink_is_not_written_through(tmp_path):
    root = tmp_path / "repo"
    (root / ".claude").mkdir(parents=True)
    target = tmp_path / "outside.json"
    target.write_text(json.dumps({"hooks": {}}) + "\n")
    before = target.read_bytes()
    (root / ".claude" / "settings.json").symlink_to(target)
    result = ensure_ratify_policy_setting(root)
    assert result.outcome == "skipped"
    assert result.skip_reason == "symlink"
    assert target.read_bytes() == before


def test_symlinked_dot_claude_is_not_written_through(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    outside = tmp_path / "outside_dir"
    outside.mkdir()
    (root / ".claude").symlink_to(outside, target_is_directory=True)
    result = ensure_ratify_policy_setting(root)
    assert result.outcome == "skipped"
    assert result.skip_reason == "symlink"
    assert list(outside.iterdir()) == []


def test_merge_replaces_the_file_atomically(tmp_path):
    settings_path = tmp_path / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True)
    original = {"hooks": {"Stop": []}, "permissions": {"allow": ["x"]}, "env": {"A": "1"}}
    settings_path.write_text(json.dumps(original) + "\n")
    settings_path.chmod(0o640)
    inode_before = settings_path.stat().st_ino
    result = ensure_ratify_policy_setting(tmp_path)
    assert result.outcome == "written"
    assert settings_path.stat().st_ino != inode_before
    assert settings_path.stat().st_mode & 0o777 == 0o640
    data = json.loads(settings_path.read_text())
    assert data["hooks"] == original["hooks"]
    assert data["permissions"] == original["permissions"]
    assert data["env"] == {"A": "1", RATIFY_POLICY_ENV_VAR: RATIFY_POLICY_DEFAULT}


def test_failed_publish_leaves_the_original_intact(tmp_path, monkeypatch):
    settings_path = tmp_path / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(json.dumps({"hooks": {}}) + "\n")
    before = settings_path.read_bytes()

    def _boom(*_a, **_k):
        raise OSError("replace failed")

    monkeypatch.setattr(os, "replace", _boom)
    result = ensure_ratify_policy_setting(tmp_path)
    assert result.outcome == "skipped"
    assert result.skip_reason == "write_failed"
    assert settings_path.read_bytes() == before
    assert list(settings_path.parent.glob(".settings.json.*.tmp")) == []


# -- the new-file path, write protection and races ---------------------------------------


def _plant_after_peek(monkeypatch, plant):
    """Run the real peek, then `plant(result)` -- a change between the check and the write."""
    from sidegraph.host import claude_settings

    real = claude_settings.current_ratify_policy

    def _peek(repo_root):
        result = real(repo_root)
        plant(result)
        return result

    monkeypatch.setattr(claude_settings, "current_ratify_policy", _peek)


def test_dangling_link_planted_after_the_peek_is_not_followed(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    (root / ".claude").mkdir(parents=True)
    target = tmp_path / "outside" / "created.json"
    target.parent.mkdir()

    def _plant(result):
        assert result.outcome == "unset"
        result.path.symlink_to(target)

    _plant_after_peek(monkeypatch, _plant)
    result = ensure_ratify_policy_setting(root)
    assert result.outcome == "skipped"
    assert result.skip_reason == "symlink"
    assert not target.exists()


def test_dangling_link_that_slips_past_the_recheck_is_refused_by_the_exclusive_create(
    tmp_path, monkeypatch
):
    from sidegraph.host import claude_settings

    root = tmp_path / "repo"
    (root / ".claude").mkdir(parents=True)
    target = tmp_path / "outside" / "created.json"
    target.parent.mkdir()
    _plant_after_peek(monkeypatch, lambda result: result.path.symlink_to(target))
    monkeypatch.setattr(claude_settings, "_link_in_the_way", lambda _root: False)
    result = ensure_ratify_policy_setting(root)
    assert result.outcome == "skipped"
    assert result.skip_reason == "write_failed"
    assert not target.exists()


@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores directory permissions"
)
def test_read_only_repo_root_without_dot_claude_is_write_failed(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    root.chmod(0o555)
    try:
        result = ensure_ratify_policy_setting(root)
    finally:
        root.chmod(0o755)
    assert result.outcome == "skipped"
    assert result.skip_reason == "write_failed"
    assert not (root / ".claude").exists()


@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores directory permissions"
)
def test_an_unsearchable_dot_claude_is_write_failed_not_a_traceback(tmp_path):
    """On Python 3.13 ``Path.is_symlink`` re-raises EACCES: a ``.claude`` with no search bit
    made the peek raise out of ``sidegraph-init``."""
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir()
    settings.write_text("{}\n")
    settings.parent.chmod(0o444)
    try:
        peeked = current_ratify_policy(tmp_path)
        ensured = ensure_ratify_policy_setting(tmp_path)
    finally:
        settings.parent.chmod(0o755)
    for result in (peeked, ensured):
        assert result.outcome == "skipped"
        assert result.skip_reason == "write_failed"


def test_failed_write_to_a_new_file_leaves_no_settings_file(tmp_path, monkeypatch):
    import builtins

    from sidegraph.host import claude_settings

    class _Fh:
        def __init__(self, fh):
            self._fh = fh

        def __enter__(self):
            self._fh.__enter__()
            return self

        def __exit__(self, *exc):
            return self._fh.__exit__(*exc)

        def fileno(self):
            return self._fh.fileno()

        def write(self, _text):
            raise OSError("disk full")

    real_open = builtins.open

    def _open(path, mode="r", *a, **k):
        fh = real_open(path, mode, *a, **k)
        return _Fh(fh) if mode == "x" else fh

    monkeypatch.setattr(claude_settings, "open", _open, raising=False)
    result = ensure_ratify_policy_setting(tmp_path)
    assert result.outcome == "skipped"
    assert result.skip_reason == "write_failed"
    assert not (tmp_path / ".claude" / "settings.json").exists()


def test_failed_create_does_not_remove_a_file_that_replaced_ours(tmp_path, monkeypatch):
    import builtins

    from sidegraph.host import claude_settings

    settings_path = tmp_path / ".claude" / "settings.json"
    manual = json.dumps({"env": {RATIFY_POLICY_ENV_VAR: "manual"}})

    class _Fh:
        def __init__(self, fh):
            self._fh = fh

        def __enter__(self):
            self._fh.__enter__()
            return self

        def __exit__(self, *exc):
            return self._fh.__exit__(*exc)

        def fileno(self):
            return self._fh.fileno()

        def write(self, _text):
            # Another writer replaces the file we created, then our write fails.
            sibling = settings_path.with_name("settings.json.other")
            sibling.write_text(manual)
            os.replace(sibling, settings_path)
            raise OSError("disk full")

    real_open = builtins.open

    def _open(path, mode="r", *a, **k):
        fh = real_open(path, mode, *a, **k)
        return _Fh(fh) if mode == "x" else fh

    monkeypatch.setattr(claude_settings, "open", _open, raising=False)
    result = ensure_ratify_policy_setting(tmp_path)
    assert result.outcome == "skipped"
    assert result.skip_reason == "write_failed"
    assert settings_path.read_text() == manual


@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores file permissions"
)
def test_write_protected_settings_file_is_respected(tmp_path):
    settings_path = tmp_path / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(json.dumps({"hooks": {}}) + "\n")
    settings_path.chmod(0o444)
    before = settings_path.read_bytes()
    result = ensure_ratify_policy_setting(tmp_path)
    assert result.outcome == "skipped"
    assert result.skip_reason == "write_failed"
    assert settings_path.read_bytes() == before
    assert settings_path.stat().st_mode & 0o777 == 0o444


def test_file_broken_between_the_peek_and_the_write_is_write_failed(tmp_path, monkeypatch):
    settings_path = tmp_path / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(json.dumps({"hooks": {}}) + "\n")
    _plant_after_peek(monkeypatch, lambda result: settings_path.write_text("{broken"))
    result = ensure_ratify_policy_setting(tmp_path)
    assert result.outcome == "skipped"
    assert result.skip_reason == "write_failed"
    assert settings_path.read_text() == "{broken"


def test_write_protected_file_is_respected_even_when_access_says_writable(tmp_path, monkeypatch):
    """Root's `os.access(W_OK)` is always true; the mode bits must still protect the file."""
    settings_path = tmp_path / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(json.dumps({"hooks": {}}) + "\n")
    settings_path.chmod(0o444)
    before = settings_path.read_bytes()
    monkeypatch.setattr(os, "access", lambda *_a, **_k: True)
    result = ensure_ratify_policy_setting(tmp_path)
    assert result.outcome == "skipped"
    assert result.skip_reason == "write_failed"
    assert settings_path.read_bytes() == before
    assert settings_path.stat().st_mode & 0o777 == 0o444


# -- one classification for the peek and the re-read -------------------------------------


def test_env_null_is_skipped_by_the_peek_and_the_write(tmp_path):
    settings_path = tmp_path / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text('{"env": null}\n')
    before = settings_path.read_bytes()
    for fn in (current_ratify_policy, ensure_ratify_policy_setting):
        result = fn(tmp_path)
        assert result.outcome == "skipped"
        assert result.skip_reason == "env_not_an_object"
    assert settings_path.read_bytes() == before


@pytest.mark.parametrize(
    ("planted", "reason"),
    [
        ("[]", "not_an_object"),
        ('{"env": "x"}', "env_not_an_object"),
        ('{"env": []}', "env_not_an_object"),
        ('{"env": null}', "env_not_an_object"),
    ],
)
def test_shape_changed_between_the_peek_and_the_write_is_skipped(
    tmp_path, monkeypatch, planted, reason
):
    settings_path = tmp_path / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(json.dumps({"hooks": {}}) + "\n")
    _plant_after_peek(monkeypatch, lambda result: settings_path.write_text(planted))
    result = ensure_ratify_policy_setting(tmp_path)
    assert result.outcome == "skipped"
    assert result.skip_reason == reason
    assert settings_path.read_text() == planted


def test_policy_added_between_the_peek_and_the_write_is_never_overwritten(tmp_path, monkeypatch):
    settings_path = tmp_path / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(json.dumps({"hooks": {}}) + "\n")
    planted = json.dumps({"env": {RATIFY_POLICY_ENV_VAR: "manual"}})
    _plant_after_peek(monkeypatch, lambda result: settings_path.write_text(planted))
    result = ensure_ratify_policy_setting(tmp_path, "auto-low-risk")
    assert result.outcome == "already_set"
    assert result.existing_value == "manual"
    assert settings_path.read_text() == planted


# -- symlinks are re-checked immediately before the write --------------------------------


@pytest.mark.parametrize("outside_has_settings", [False, True])
def test_dot_claude_symlink_planted_after_the_peek_is_not_followed(
    tmp_path, monkeypatch, outside_has_settings
):
    root = tmp_path / "repo"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    if outside_has_settings:
        (outside / "settings.json").write_text('{"keep": 1}\n')
    snapshot = {p.name: (p.read_bytes(), p.stat().st_ino) for p in outside.iterdir()}

    def _plant(result):
        assert result.outcome == "unset"
        (root / ".claude").symlink_to(outside, target_is_directory=True)

    _plant_after_peek(monkeypatch, _plant)
    result = ensure_ratify_policy_setting(root)
    assert result.outcome == "skipped"
    assert result.skip_reason == "symlink"
    assert {p.name: (p.read_bytes(), p.stat().st_ino) for p in outside.iterdir()} == snapshot


def test_file_symlink_swapped_in_after_the_peek_is_not_read(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    settings_path = root / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(json.dumps({"hooks": {}}) + "\n")
    outside = tmp_path / "outside.json"
    outside.write_text('{"token": "secret"}\n')
    before = outside.read_bytes()

    def _plant(result):
        settings_path.unlink()
        settings_path.symlink_to(outside)

    _plant_after_peek(monkeypatch, _plant)
    result = ensure_ratify_policy_setting(root)
    assert result.outcome == "skipped"
    assert result.skip_reason == "symlink"
    assert settings_path.is_symlink()
    assert outside.read_bytes() == before
    assert not any(
        "secret" in p.read_text() for p in root.rglob("*") if p.is_file() and not p.is_symlink()
    )


def test_merge_read_refuses_a_link_swapped_in_after_the_recheck(tmp_path, monkeypatch):
    """The re-check passes once, then the file is swapped for a link: the read itself
    must refuse to follow it (O_NOFOLLOW), not rely on the re-check alone."""
    from sidegraph.host import claude_settings

    root = tmp_path / "repo"
    settings_path = root / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(json.dumps({"hooks": {}}) + "\n")
    outside = tmp_path / "outside.json"
    outside.write_text('{"token": "secret"}\n')
    real = claude_settings._link_in_the_way

    def _clean_once_then_swap(repo_root):
        result = real(repo_root)
        settings_path.unlink()
        settings_path.symlink_to(outside)
        return result

    monkeypatch.setattr(claude_settings, "_link_in_the_way", _clean_once_then_swap)
    result = ensure_ratify_policy_setting(root)
    assert result.outcome == "skipped"
    assert result.skip_reason == "symlink"
    assert not any(
        "secret" in p.read_text() for p in root.rglob("*") if p.is_file() and not p.is_symlink()
    )
