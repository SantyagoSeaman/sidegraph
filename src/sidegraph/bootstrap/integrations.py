"""Pure, local inspection of Sidegraph host integration configuration."""

from __future__ import annotations

import json
import shlex
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

from .model import HostKind, IntegrationResult

Status = Literal["verified", "missing", "invalid", "unsupported"]


def _load_json(path: Path) -> tuple[Mapping[str, Any] | None, bool]:
    if not path.is_file():
        return None, False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None, True
    return (data, False) if isinstance(data, Mapping) else (None, True)


def _load_toml(path: Path) -> tuple[Mapping[str, Any] | None, bool]:
    if not path.is_file():
        return None, False
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError):
        return None, True
    return data, False


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
            if entrypoint in shlex.split(command):
                return True
        except ValueError:
            continue
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


def _verify_claude(root: Path) -> IntegrationResult:
    mcp_path = root / ".mcp.json"
    hooks_path = root / ".claude" / "settings.json"
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
    return IntegrationResult(
        host=HostKind.CLAUDE_CODE,
        mcp=mcp,
        session_start=session_start,
        stop=stop,
        pretool_read_grep=pretool,
        fully_supported=fully_supported,
        next_action=None if fully_supported else _next_action(checks, root),
    )


def _verify_codex(root: Path, codex_config: Path | None) -> IntegrationResult:
    mcp_path = codex_config or root / ".codex" / "config.toml"
    hooks_path = root / ".codex" / "hooks.json"
    legacy_hooks_path = root / ".codex" / "hooks" / "hooks.json"
    if not hooks_path.is_file() and legacy_hooks_path.is_file():
        hooks_path = legacy_hooks_path
    mcp_config, mcp_invalid = _load_toml(mcp_path)
    hooks_config, hooks_invalid = _load_json(hooks_path)
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
    return IntegrationResult(
        host=HostKind.CODEX,
        mcp=mcp_status,
        session_start=session_status,
        stop=stop_status,
        pretool_read_grep="unsupported",
        fully_supported=False,
        next_action=next_action,
    )


def verify_integration(
    root: Path, host: HostKind, *, codex_config: Path | None = None
) -> IntegrationResult:
    """Inspect host configuration without executing a host or modifying its files."""
    if host == HostKind.CLAUDE_CODE:
        return _verify_claude(root)
    return _verify_codex(root, codex_config)
