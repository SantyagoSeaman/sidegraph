"""Claude Code `.claude/settings.json` integration (host seam, Task 1).

`sidegraph-init` can write ``SIDEGRAPH_RATIFY_POLICY`` into the project's
`.claude/settings.json` `env` block rather than flipping the library default or hiding the
setting in the plugin manifests, so that a repo's auto-ratification policy is visible,
committed, and reviewable in the project itself, not a silent behavior change. This is
Claude-Code-specific file format knowledge, so it lives in the host seam (`host/`), not in
the portable core. `cli.py` (the wiring/orchestration layer, not the core store/retrieval/MCP
surface `host/__init__.py` isolates from this seam) calls it from `init_main`, which decides
*what value* to write (an explicit flag, an interactive answer, or nothing at all in a
non-interactive run) -- this module only knows how to read and merge the file safely.

Behavior (owner-specified):
- Absent file: create it (and `.claude/`) with just the `env` block, set to the given value.
- Present, valid JSON object: merge in, preserving every other key and the file's own
  shape.
- `SIDEGRAPH_RATIFY_POLICY` already set, to any value: never touch it. The project chose.
- Present but not valid JSON, or not an object (top level or the `env` value itself): never
  write. The caller is expected to print the line the person should add by hand.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from ..verify import _find_repo_root

RATIFY_POLICY_ENV_VAR = "SIDEGRAPH_RATIFY_POLICY"
RATIFY_POLICY_DEFAULT = "auto-low-risk"
SETTINGS_RELATIVE_PATH = Path(".claude") / "settings.json"


@dataclass(frozen=True)
class RatifyPolicySettingsResult:
    """Outcome of one `current_ratify_policy`/`ensure_ratify_policy_setting` call.

    ``outcome`` is one of ``"written"`` (file created or merged into -- only from
    ``ensure_ratify_policy_setting``), ``"already_set"`` (the env var was already present,
    at ``existing_value``, and nothing changed), ``"unset"`` (the file is absent, or present
    and valid with the key absent -- safe to write into; only from
    ``current_ratify_policy``), or ``"skipped"`` (the file exists but is unsafe to touch,
    see ``skip_reason``).
    """

    outcome: str
    path: Path
    existing_value: str | None = None
    skip_reason: str | None = None  # "invalid_json" | "not_an_object" | "env_not_an_object"


def repo_root_for_settings() -> Path:
    """The repo root `.claude/settings.json` is resolved against. Same never-raise
    fallback as `doc_import._glob_repo_root`: `git rev-parse --show-toplevel` from cwd,
    falling back to the bare (resolved) cwd when there's no repo or git isn't available.
    A settings-file problem must never be the reason `sidegraph-init` fails."""
    try:
        return _find_repo_root(Path.cwd())
    except (ValueError, OSError):
        return Path.cwd().resolve()


def current_ratify_policy(repo_root: Path) -> RatifyPolicySettingsResult:
    """Read-only peek at `<repo_root>/.claude/settings.json`'s ratify-policy state --
    never writes. Lets a caller (`init_main`) decide whether it is even worth asking a
    question or resolving a flag: `"already_set"` and `"skipped"` mean any write attempt
    would be a no-op anyway, so those states are reported directly without prompting.
    `"unset"` means the file is absent, or present and a valid mergeable object with the
    key absent -- the safe-to-write case `ensure_ratify_policy_setting` will act on.
    """
    settings_path = repo_root / SETTINGS_RELATIVE_PATH

    if not settings_path.exists():
        return RatifyPolicySettingsResult(outcome="unset", path=settings_path)

    try:
        data = json.loads(settings_path.read_text())
    except (json.JSONDecodeError, UnicodeDecodeError, OSError):
        return RatifyPolicySettingsResult(
            outcome="skipped", path=settings_path, skip_reason="invalid_json"
        )

    if not isinstance(data, dict):
        return RatifyPolicySettingsResult(
            outcome="skipped", path=settings_path, skip_reason="not_an_object"
        )

    env = data.get("env")
    if env is not None and not isinstance(env, dict):
        return RatifyPolicySettingsResult(
            outcome="skipped", path=settings_path, skip_reason="env_not_an_object"
        )

    if isinstance(env, dict) and RATIFY_POLICY_ENV_VAR in env:
        return RatifyPolicySettingsResult(
            outcome="already_set",
            path=settings_path,
            existing_value=str(env[RATIFY_POLICY_ENV_VAR]),
        )

    return RatifyPolicySettingsResult(outcome="unset", path=settings_path)


def ensure_ratify_policy_setting(
    repo_root: Path, value: str = RATIFY_POLICY_DEFAULT
) -> RatifyPolicySettingsResult:
    """Ensure `<repo_root>/.claude/settings.json` carries `env.SIDEGRAPH_RATIFY_POLICY=
    <value>`; see module docstring for the exact rules. `value` is whatever the caller
    already decided to write (a flag, an interactive answer, ...) -- this function makes
    no policy choice of its own, only `manual`/`already_set`/`unsafe`-file safety calls."""
    state = current_ratify_policy(repo_root)
    if state.outcome in ("already_set", "skipped"):
        return state

    settings_path = state.path
    if not settings_path.exists():
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        settings_path.write_text(
            json.dumps({"env": {RATIFY_POLICY_ENV_VAR: value}}, indent=2) + "\n"
        )
        return RatifyPolicySettingsResult(outcome="written", path=settings_path)

    # `state.outcome == "unset"` with an existing file means `current_ratify_policy`
    # already proved the file parses as a mergeable JSON object with the key absent.
    data = json.loads(settings_path.read_text())
    data.setdefault("env", {})
    data["env"][RATIFY_POLICY_ENV_VAR] = value
    settings_path.write_text(json.dumps(data, indent=2) + "\n")
    return RatifyPolicySettingsResult(outcome="written", path=settings_path)
