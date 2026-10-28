"""Enabled-plugin evidence follows the host's launch-directory settings merge.

See design/superpowers/specs/2026-10-28-bootstrap-recognises-plugin-install-design.md (D1).
"""

import json
import os
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "home"
    path.mkdir()
    monkeypatch.setenv("HOME", str(path))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: path))
    monkeypatch.setenv("CODEX_HOME", str(path / "codex"))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    return path


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    return path


def _settings(path: Path, plugins: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"enabledPlugins": plugins}) + "\n", encoding="utf-8")


def test_user_scope_enables(home: Path, repo: Path) -> None:
    from sidegraph.host.claude_settings import enabled_plugin_key

    _settings(home / ".claude/settings.json", {"sidegraph@sidegraph": True})
    assert enabled_plugin_key(repo) == "sidegraph@sidegraph"


def test_claude_config_dir_wins_over_home(
    home: Path, repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sidegraph.host.claude_settings import enabled_plugin_key

    cfg = tmp_path / "cfg"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
    _settings(home / ".claude/settings.json", {"sidegraph@sidegraph": True})
    _settings(cfg / "settings.json", {"sidegraph@sidegraph": False})
    assert enabled_plugin_key(repo) is None
    _settings(home / ".claude/settings.json", {"sidegraph@sidegraph": False})
    _settings(cfg / "settings.json", {"sidegraph@sidegraph": True})
    assert enabled_plugin_key(repo) == "sidegraph@sidegraph"


def test_project_scope_enables(repo: Path) -> None:
    from sidegraph.host.claude_settings import enabled_plugin_key

    _settings(repo / ".claude/settings.json", {"sidegraph@sidegraph": True})
    assert enabled_plugin_key(repo) == "sidegraph@sidegraph"


def test_local_overrides_project(repo: Path) -> None:
    from sidegraph.host.claude_settings import enabled_plugin_key

    _settings(repo / ".claude/settings.json", {"sidegraph@sidegraph": True})
    _settings(repo / ".claude/settings.local.json", {"sidegraph@sidegraph": False})
    assert enabled_plugin_key(repo) is None
    _settings(repo / ".claude/settings.json", {"sidegraph@sidegraph": False})
    _settings(repo / ".claude/settings.local.json", {"sidegraph@sidegraph": True})
    assert enabled_plugin_key(repo) == "sidegraph@sidegraph"


def test_project_overrides_user(home: Path, repo: Path) -> None:
    from sidegraph.host.claude_settings import enabled_plugin_key

    _settings(home / ".claude/settings.json", {"sidegraph@sidegraph": True})
    _settings(repo / ".claude/settings.json", {"sidegraph@sidegraph": False})
    assert enabled_plugin_key(repo) is None


def test_git_root_local_applies_to_a_subdirectory_root(repo: Path) -> None:
    from sidegraph.host.claude_settings import enabled_plugin_key

    (repo / ".git").mkdir()
    root = repo / "pkg"
    root.mkdir()
    _settings(repo / ".claude/settings.local.json", {"sidegraph@sidegraph": True})
    assert enabled_plugin_key(root) == "sidegraph@sidegraph"


def test_git_root_project_settings_do_not_apply_to_a_subdirectory_root(repo: Path) -> None:
    from sidegraph.host.claude_settings import enabled_plugin_key

    (repo / ".git").mkdir()
    root = repo / "pkg"
    root.mkdir()
    _settings(repo / ".claude/settings.json", {"sidegraph@sidegraph": True})
    assert enabled_plugin_key(root) is None


@pytest.mark.parametrize(
    "plugins",
    [
        {"sidegraph@sidegraph": "true"},
        {"sidegraph-extra@x": True},
        {"other@sidegraph": True},
        [],
        "oops",
    ],
)
def test_only_true_boolean_and_exact_plugin_name_count(repo: Path, plugins: object) -> None:
    from sidegraph.host.claude_settings import enabled_plugin_key

    _settings(repo / ".claude/settings.json", plugins)
    assert enabled_plugin_key(repo) is None


@pytest.mark.parametrize(
    "bad", ["{not json\n", "[" * 50000 + "]" * 50000], ids=["malformed", "too-deep"]
)
def test_malformed_json_is_no_say(home: Path, repo: Path, bad: str) -> None:
    from sidegraph.host.claude_settings import enabled_plugin_key

    _settings(home / ".claude/settings.json", {"sidegraph@sidegraph": True})
    path = repo / ".claude/settings.json"
    path.parent.mkdir()
    path.write_text(bad, encoding="utf-8")
    assert enabled_plugin_key(repo) == "sidegraph@sidegraph"


def test_first_enabling_key_in_sorted_order(repo: Path) -> None:
    from sidegraph.host.claude_settings import enabled_plugin_key

    _settings(
        repo / ".claude/settings.json",
        {"sidegraph@b": True, "sidegraph@a": True, "sidegraph@c": False},
    )
    assert enabled_plugin_key(repo) == "sidegraph@a"


def test_plugin_settings_paths_order(home: Path, repo: Path) -> None:
    from sidegraph.host.claude_settings import plugin_settings_paths

    (repo / ".git").mkdir()
    root = repo / "pkg"
    root.mkdir()
    want = (
        home / ".claude/settings.json",
        root / ".claude/settings.json",
        root / ".claude/settings.local.json",
        repo / ".claude/settings.local.json",
    )
    assert plugin_settings_paths(root) == tuple(Path(os.path.abspath(p)) for p in want)
    assert plugin_settings_paths(repo) == (
        home / ".claude/settings.json",
        repo / ".claude/settings.json",
        repo / ".claude/settings.local.json",
    )
