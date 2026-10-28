"""Pure, local inspection of Sidegraph host integration configuration."""

from __future__ import annotations

import contextlib
import json
import os
import shlex
import stat
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

from ..host.claude_settings import enabled_plugin_key, plugin_settings_paths
from .model import HostKind, IntegrationResult

Status = Literal["verified", "missing", "invalid", "unsupported"]


def _is_config_file(path: Path) -> bool:
    """Keep stat failures observable; Python 3.14 is_file suppresses all OSError."""
    try:
        return stat.S_ISREG(path.stat().st_mode)
    except (FileNotFoundError, NotADirectoryError):
        return False


def _load_json(path: Path) -> tuple[Mapping[str, Any] | None, bool]:
    try:
        if not _is_config_file(path):
            return None, False
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return None, True
    return (data, False) if isinstance(data, Mapping) else (None, True)


def _load_toml(path: Path) -> tuple[Mapping[str, Any] | None, bool]:
    try:
        if not _is_config_file(path):
            return None, False
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, RecursionError):
        return None, True
    return data, False


CODEX_PLUGIN_NAME = "sidegraph"


def codex_user_config_path() -> Path | None:
    """The user config Codex reads, if its home can be resolved."""
    config_dir = os.environ.get("CODEX_HOME")
    if config_dir:
        return Path(config_dir) / "config.toml"
    try:
        return Path.home() / ".codex" / "config.toml"
    except RuntimeError:
        return None


def _codex_plugin_key() -> str | None:
    """Explicitly enabled Sidegraph evidence, independent of manual repo wiring."""
    try:
        path = codex_user_config_path()
        if path is None:
            return None
        config, invalid = _load_toml(path)
        if invalid or config is None:
            return None
        plugins = config.get("plugins")
        if not isinstance(plugins, Mapping):
            return None
        for key in sorted(plugins):
            value = plugins[key]
            if (
                key.partition("@")[0] == CODEX_PLUGIN_NAME
                and isinstance(value, Mapping)
                and value.get("enabled") is True
            ):
                return key
    except (OSError, ValueError, RecursionError):
        return None
    return None


def _command_strings(value: object) -> tuple[str, ...]:
    # Iterative, not recursive: value is untrusted, user-editable JSON/TOML (.mcp.json,
    # Codex hooks config), and a config author can nest it arbitrarily deep. A recursive
    # walk hits CPython's call-stack recursion limit on a few thousand levels of nesting
    # and raises RecursionError with no injection needed at all -- an explicit worklist
    # is bounded only by heap memory, not the call stack, so no plausible config depth
    # can trip it.
    commands: list[str] = []
    stack: list[object] = [value]
    while stack:
        current = stack.pop()
        if isinstance(current, Mapping):
            for key, child in current.items():
                if key == "command" and isinstance(child, str):
                    commands.append(child)
                elif key == "args" and isinstance(child, list):
                    commands.extend(item for item in child if isinstance(item, str))
                stack.append(child)
        elif isinstance(current, list):
            stack.extend(current)
    return tuple(commands)


def _contains_entrypoint(commands: tuple[str, ...], entrypoint: str) -> bool:
    for command in commands:
        try:
            words = shlex.split(command)
        except ValueError:
            continue
        # The plugin's Codex hot command is `sh -c '<script>' || printf …`: the entry point is
        # a word of that script, not of the outer command. Only `sh` is unwrapped, the script's
        # words extend the outer ones, and a script that does not split leaves them intact.
        # see design/superpowers/specs/2026-10-03-launch-from-session-commit-design.md (D3)
        if len(words) > 2 and words[0] in ("sh", "/bin/sh") and words[1] == "-c":
            with contextlib.suppress(ValueError):
                words += shlex.split(words[2])
        if entrypoint in words:
            return True
    return False


def _status(config: Mapping[str, Any] | None, invalid: bool, entrypoint: str) -> Status:
    if invalid:
        return "invalid"
    if config is None:
        return "missing"
    return "verified" if _contains_entrypoint(_command_strings(config), entrypoint) else "missing"


def _event_status(
    config: Mapping[str, Any] | None,
    invalid: bool,
    event: str,
    entrypoint: str,
) -> Status:
    if invalid:
        return "invalid"
    if config is None:
        return "missing"
    hooks = config.get("hooks")
    if not isinstance(hooks, Mapping):
        return "missing"
    event_config = hooks.get(event)
    return (
        "verified"
        if _contains_entrypoint(_command_strings(event_config), entrypoint)
        else "missing"
    )


def _repair_action(path: Path, root: Path) -> str:
    try:
        display_path = path.relative_to(root).as_posix()
    except ValueError:
        display_path = path.name
    return f"Repair {display_path}, then rerun sidegraph-bootstrap"


def _configure_action(entrypoint: str, path: Path, root: Path) -> str:
    try:
        display_path = path.relative_to(root).as_posix()
    except ValueError:
        display_path = path.name
    return f"Configure {entrypoint} in {display_path}, then rerun sidegraph-bootstrap"


def _next_action(checks: tuple[tuple[Status, str, Path, bool], ...], root: Path) -> str | None:
    for status, entrypoint, path, invalid in checks:
        if invalid:
            return _repair_action(path, root)
        if status != "verified":
            return _configure_action(entrypoint, path, root)
    return None


def host_config_paths(
    root: Path, host: HostKind, *, codex_config: Path | None = None
) -> tuple[tuple[str, Path], ...]:
    """Every config path the verifier for ``host`` may read, labelled. The verifiers and
    bootstrap's ``--report`` guard both take their paths from here, so a path the verifier
    starts reading cannot fall out of the guard. User settings also depend on host config
    environment variables and the home directory; repository-local settings follow the boundary.
    # see design/superpowers/specs/2026-09-29-store-symlinks-and-bootstrap-guards-design.md D6
    # see design/superpowers/specs/2026-10-28-bootstrap-recognises-plugin-install-design.md D5
    """
    if host == HostKind.CLAUDE_CODE:
        paths = [
            ("Claude Code MCP config", root / ".mcp.json"),
            ("Claude Code hooks config", root / ".claude" / "settings.json"),
        ]
        absolute_root = Path(os.path.abspath(root))
        for path in plugin_settings_paths(root):
            if path == absolute_root / ".claude" / "settings.json":
                continue
            if path.name == "settings.local.json":
                label = (
                    "Claude Code local settings"
                    if path.parent.parent == absolute_root
                    else "Claude Code repository local settings"
                )
            else:
                label = "Claude Code user settings"
            paths.append((label, path))
        return tuple(paths)
    paths = [
        ("Codex config", codex_config or root / ".codex" / "config.toml"),
        ("Codex hooks config", root / ".codex" / "hooks.json"),
        ("Codex legacy hooks config", root / ".codex" / "hooks" / "hooks.json"),
    ]
    user = codex_user_config_path()
    if user is not None:
        paths.append(("Codex user config", user))
    return tuple(paths)


def _verify_claude(root: Path) -> IntegrationResult:
    paths = host_config_paths(root, HostKind.CLAUDE_CODE)
    mcp_path, hooks_path = paths[0][1], paths[1][1]
    mcp_config, mcp_invalid = _load_json(mcp_path)
    hooks_config, hooks_invalid = _load_json(hooks_path)
    mcp = _status(mcp_config, mcp_invalid, "sidegraph-mcp")
    session_start = _event_status(
        hooks_config, hooks_invalid, "SessionStart", "sidegraph-session-start"
    )
    stop = _event_status(hooks_config, hooks_invalid, "Stop", "sidegraph-stop")
    pretool = _event_status(hooks_config, hooks_invalid, "PreToolUse", "sidegraph-pre-tool-use")
    checks = (
        (mcp, "sidegraph-mcp", mcp_path, mcp_invalid),
        (session_start, "sidegraph-session-start", hooks_path, hooks_invalid),
        (stop, "sidegraph-stop", hooks_path, hooks_invalid),
        (pretool, "sidegraph-pre-tool-use", hooks_path, hooks_invalid),
    )
    fully_supported = all(status == "verified" for status, *_ in checks)
    manual = IntegrationResult(
        host=HostKind.CLAUDE_CODE,
        mcp=mcp,
        session_start=session_start,
        stop=stop,
        pretool_read_grep=pretool,
        fully_supported=fully_supported,
        next_action=None if fully_supported else _next_action(checks, root),
    )

    if manual.fully_supported:
        return manual
    # see design/superpowers/specs/2026-10-28-bootstrap-recognises-plugin-install-design.md (D2)
    key = enabled_plugin_key(root)
    if key is not None:
        return IntegrationResult(
            host=HostKind.CLAUDE_CODE,
            mcp="verified",
            session_start="verified",
            stop="verified",
            pretool_read_grep="verified",
            fully_supported=True,
            next_action=None,
            plugin=key,
        )
    return manual


def _verify_codex(root: Path, codex_config: Path | None) -> IntegrationResult:
    paths = host_config_paths(root, HostKind.CODEX, codex_config=codex_config)
    mcp_path, hooks_path, legacy_hooks_path = (path for _, path in paths[:3])
    canonical_hooks_path = hooks_path
    try:
        if not _is_config_file(hooks_path):
            # Retain the path whose inspection fails; use canonical only when both are absent.
            hooks_path = legacy_hooks_path
            if not _is_config_file(hooks_path):
                hooks_path = canonical_hooks_path
    except OSError:
        hooks_config, hooks_invalid = None, True
    else:
        hooks_config, hooks_invalid = _load_json(hooks_path)
    mcp_config, mcp_invalid = _load_toml(mcp_path)
    mcp_status = _status(mcp_config, mcp_invalid, "sidegraph-mcp")
    session_status = _event_status(
        hooks_config, hooks_invalid, "SessionStart", "sidegraph-session-start"
    )
    stop_status = _event_status(hooks_config, hooks_invalid, "Stop", "sidegraph-stop")
    checks = (
        (mcp_status, "sidegraph-mcp", mcp_path, mcp_invalid),
        (session_status, "sidegraph-session-start", hooks_path, hooks_invalid),
        (stop_status, "sidegraph-stop", hooks_path, hooks_invalid),
    )
    next_action = _next_action(checks, root)
    manual = IntegrationResult(
        host=HostKind.CODEX,
        mcp=mcp_status,
        session_start=session_status,
        stop=stop_status,
        pretool_read_grep="unsupported",
        fully_supported=False,
        next_action=next_action,
    )

    if mcp_status == session_status == stop_status == "verified":
        return manual
    # see design/superpowers/specs/2026-10-28-bootstrap-recognises-plugin-install-design.md (D2)
    key = _codex_plugin_key()
    if key is not None:
        return IntegrationResult(
            host=HostKind.CODEX,
            mcp="verified",
            session_start="verified",
            stop="verified",
            pretool_read_grep="unsupported",
            fully_supported=False,
            next_action=None,
            plugin=key,
        )
    return manual


def verify_integration(
    root: Path, host: HostKind, *, codex_config: Path | None = None
) -> IntegrationResult:
    """Inspect host configuration without executing a host or modifying its files."""
    if host == HostKind.CLAUDE_CODE:
        return _verify_claude(root)
    return _verify_codex(root, codex_config)
