"""The four plugin manifests carry the SubagentStart entry (T6).

The public manifests run it from the hot form that launches from the commit SessionStart
recorded, and the Codex public one is wrapped in ``sh -c``. The guard on every one answers
``{}``, because a start failure on this event must never put a message in front of a subagent.
The shape of each command is pinned word for word in tests/test_public_hook_launch.py, and the
guard's behaviour in tests/test_hook_spawn_guard.py; this file pins that every manifest has the
entry at all.

This file ships, and release verification runs it on the public snapshot, so it globs the
manifests that exist: the ``*.public.json`` twins are renamed onto the base names there.

see design/superpowers/specs/2026-10-03-subagent-start-brief-design.md (D4)
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

# -- T6: the four manifests --------------------------------------------------------------------

_PLUGIN = Path(__file__).resolve().parent.parent / "plugin" / "sidegraph"
# Every hook manifest that exists: the dev and public twins here, the renamed one on a snapshot.
_MANIFESTS = sorted((_PLUGIN / "hooks").glob("hooks*.json")) + sorted(
    (_PLUGIN / "codex").glob("hooks*.json")
)
_GUARD = " || printf '{}\\n'"  # the Python literal is the two characters backslash and n


def _label(path: Path) -> str:
    return path.relative_to(_PLUGIN).as_posix()


def test_t6_the_scan_found_the_manifests() -> None:
    assert len(_MANIFESTS) >= 2, _MANIFESTS


@pytest.mark.parametrize("manifest", _MANIFESTS, ids=_label)
def test_t6_every_manifest_carries_the_subagent_start_entry(manifest: Path) -> None:
    """Red against a manifest without the entry, and (M4) against a Codex public command that
    is not wrapped in ``sh -c``: Codex runs a hook under ``$SHELL -lc``, and a fish login shell
    cannot parse the POSIX prefix that reads the launch record."""
    groups = json.loads(manifest.read_text(encoding="utf-8"))["hooks"].get("SubagentStart")
    assert groups, f"{_label(manifest)} has no SubagentStart entry"
    (group,) = groups
    assert "matcher" not in group, "the start-failure guard prints {}, so no matcher narrows it"
    (hook,) = group["hooks"]
    command = hook["command"]
    assert "sidegraph-subagent-start" in command
    assert command.endswith(_GUARD) and command.count(_GUARD) >= 1
    codex = manifest.parent.name == "codex"
    if "uvx" not in command:  # a development manifest: the checkout the plugin was loaded from
        assert "uv run" in command and "launch-commit" not in command
        return
    assert "launch-commit" in command, "a public SubagentStart entry runs from the hot form"
    if codex:
        assert command.startswith("sh -c '"), command
    else:
        assert not command.startswith("sh -c"), command
