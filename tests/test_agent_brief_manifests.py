"""The Claude carriers wire ``Agent|Task`` to the shared PreToolUse command; Codex does not (T8).

A subagent's brief reaches the hook as a PreToolUse call on ``Agent`` (``Task`` on older hosts),
so each place a Claude Code user's hooks come from needs the matcher: the development manifest,
the public manifest in its hot form, and the manual-wiring snippet in the setup guide. The new
group runs the same command as the Read/Grep/Edit/Write group, byte for byte, so a host that runs
both spawns one process. The matcher is an exact alternation, which Claude Code compares with the
tool name as a whole: ``TaskCreate``, ``TaskList`` and ``TaskGet`` do not fire it. The Codex
manifests have no PreToolUse event for it to ride on (that port is deferred).

This file ships, and release verification runs it on the public snapshot, so it globs the
manifests that exist: the ``*.public.json`` twins are renamed onto the base names there.

see design/superpowers/specs/2026-10-03-records-in-subagent-briefs-design.md (D1, T8)
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_PLUGIN = _ROOT / "plugin" / "sidegraph"
_CLAUDE_MANIFESTS = sorted((_PLUGIN / "hooks").glob("hooks*.json"))
_CODEX_MANIFESTS = sorted((_PLUGIN / "codex").glob("hooks*.json"))
_SETUP_GUIDE = _ROOT / "docs" / "getting-started" / "claude-code-setup.md"
_MATCHER = "Agent|Task"


def _snippet() -> dict:
    blocks = re.findall(r"```json\n(.*?)\n```", _SETUP_GUIDE.read_text(encoding="utf-8"), re.DOTALL)
    return json.loads(next(b for b in blocks if "PreToolUse" in b))


def _claude_carriers() -> list[tuple[str, dict]]:
    carriers = [
        (path.name, json.loads(path.read_text(encoding="utf-8"))) for path in _CLAUDE_MANIFESTS
    ]
    return [*carriers, ("claude-code-setup.md", _snippet())]


def test_t8_the_scan_found_the_carriers() -> None:
    assert len(_CLAUDE_MANIFESTS) >= 1 and len(_CODEX_MANIFESTS) >= 1
    assert [label for label, _ in _claude_carriers()][-1] == "claude-code-setup.md"


@pytest.mark.parametrize(
    ("label", "config"), _claude_carriers(), ids=[label for label, _ in _claude_carriers()]
)
def test_t8_a_claude_carrier_wires_agent_and_task_to_the_shared_command(
    label: str, config: dict
) -> None:
    """Red against a carrier without the group, against a matcher that is not the exact
    alternation, and against a group that runs a command of its own: the rule of
    ``_commands`` in test_public_hook_launch is one command per event."""
    groups = config["hooks"]["PreToolUse"]
    agent_groups = [g for g in groups if "Agent" in (g.get("matcher") or "")]
    assert len(agent_groups) == 1, f"{label}: expected one Agent group, found {groups!r}"
    matcher = agent_groups[0]["matcher"]
    # Claude Code treats a matcher made only of letters, digits, underscores and ``|`` as a list
    # of whole tool names, so this fires on those two and on no ``Task*`` tool.
    assert matcher == _MATCHER and re.fullmatch(r"[A-Za-z0-9_|]+", matcher), label
    (hook,) = agent_groups[0]["hooks"]
    assert hook["type"] == "command"
    assert "if" not in hook, f"{label}: the Agent group takes no `if` entry"
    assert hook["command"] == groups[0]["hooks"][0]["command"], label
    assert "sidegraph-pre-tool-use" in hook["command"], label


@pytest.mark.parametrize(
    "manifest", _CODEX_MANIFESTS, ids=lambda p: p.relative_to(_PLUGIN).as_posix()
)
def test_t8_the_codex_manifests_do_not_carry_it(manifest: Path) -> None:
    hooks = json.loads(manifest.read_text(encoding="utf-8"))["hooks"]
    matchers = [g.get("matcher") for g in hooks.get("PreToolUse", [])]
    assert not any(m and ("Agent" in m or "Task" in m) for m in matchers), matchers
