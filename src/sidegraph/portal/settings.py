"""User-level Portal preferences (D12)."""

import json
import math
import os
from contextlib import suppress
from pathlib import Path
from tempfile import NamedTemporaryFile

from pydantic import Field

from .contract import Settings, Theme


class PortalSettings(Settings):
    """Effective settings with the user-facing defaults (D12)."""

    theme: Theme = "dark"
    poll_seconds: int = Field(default=5, ge=2, le=60)
    editor_url: str = "vscode://file/{path}:{line}"
    idle_minutes: float = Field(default=30.0, ge=0.05)


class SettingsUnreadableError(ValueError):
    """The preferences file could not be parsed as a JSON object."""


def _path() -> Path:
    config_home = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(config_home) / "sidegraph" / "portal.json"


def _reject_constant(token: str) -> None:
    raise ValueError(f"Invalid JSON constant {token}")


def _read(path: Path) -> dict[str, object]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"), parse_constant=_reject_constant)
    except FileNotFoundError:
        return {}
    except (ValueError, RecursionError) as exc:
        raise SettingsUnreadableError(f"Could not read portal settings in {path}.") from exc
    if isinstance(raw, dict):
        return raw
    raise SettingsUnreadableError(f"Expected a JSON object in {path}.")


def _effective(raw: dict[str, object]) -> PortalSettings:
    settings = PortalSettings()
    theme = raw.get("theme")
    if theme == "dark" or theme == "light":
        settings.theme = theme
    poll = raw.get("poll_seconds")
    if type(poll) is int:
        settings.poll_seconds = min(max(poll, 2), 60)
    editor = raw.get("editor_url")
    if isinstance(editor, str):
        settings.editor_url = editor
    idle = raw.get("idle_minutes")
    if isinstance(idle, int | float) and not isinstance(idle, bool):
        with suppress(OverflowError):
            if math.isfinite(idle):
                settings.idle_minutes = max(float(idle), 0.05)
    return settings


def load() -> PortalSettings:
    """Load user preferences, with defaults on unreadable values and bounds from D12."""
    try:
        raw = _read(_path())
    except (OSError, SettingsUnreadableError):
        return PortalSettings()
    return _effective(raw)


def save(**changes: object) -> PortalSettings:
    """Save user settings atomically, preserving other values in the file (D12)."""
    path = _path()
    raw = _read(path)
    settings = PortalSettings.model_validate(_effective(raw).model_dump() | changes)
    validated_changes = settings.model_dump(include=set(changes))
    contents = json.dumps(raw | validated_changes, indent=2)
    path.parent.mkdir(parents=True, exist_ok=True)
    pending: Path | None = None
    try:
        with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as file:
            pending = Path(file.name)
            file.write(contents + "\n")
        os.replace(pending, path)
    finally:
        if pending is not None:
            pending.unlink(missing_ok=True)
    return settings
