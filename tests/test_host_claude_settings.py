"""Tests for the host-seam helper that writes SIDEGRAPH_RATIFY_POLICY into a project's
`.claude/settings.json` at `sidegraph-init` time (Task 1). Pure file-in/file-out behavior,
no store, no CLI, so these stay isolated from `test_cli_init.py`'s integration checks."""

import json

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
