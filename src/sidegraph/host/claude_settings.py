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
- `.claude/` or `settings.json` a symlink (live or dangling): never write through it.
- A merge is published by writing a temp file and replacing, so a reader never sees a
  truncated file; a new file is created with `O_EXCL`. Any `OSError` in the write phase
  gives a `skipped` outcome (`write_failed`), never an exception.
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
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
    # "invalid_json" | "not_an_object" | "env_not_an_object" | "symlink" | "write_failed"
    skip_reason: str | None = None


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

    # `is_symlink` is lstat-based, so it is true for a dangling link too, where `exists()`
    # would say "absent" and the write would follow the link out of the repo.
    # On Python 3.13 `is_symlink` re-raises EACCES (a `.claude` with no search bit), and the
    # peek sits outside the write path's guard: an unsearchable tree is a failed write.
    # Python 3.14 swallows that error (`is_symlink` / `exists` return False), so the
    # unsearchable case is detected explicitly to behave the same on every version.
    claude_dir = repo_root / SETTINGS_RELATIVE_PATH.parent
    try:
        if (
            claude_dir.is_dir()
            and not claude_dir.is_symlink()
            and not os.access(claude_dir, os.X_OK)
        ):
            return RatifyPolicySettingsResult(
                outcome="skipped", path=settings_path, skip_reason="write_failed"
            )
        if _link_in_the_way(repo_root):
            return RatifyPolicySettingsResult(
                outcome="skipped", path=settings_path, skip_reason="symlink"
            )
        if not settings_path.exists():
            return RatifyPolicySettingsResult(outcome="unset", path=settings_path)
    except OSError:
        return RatifyPolicySettingsResult(
            outcome="skipped", path=settings_path, skip_reason="write_failed"
        )

    try:
        data = json.loads(settings_path.read_text())
    except (json.JSONDecodeError, UnicodeDecodeError, OSError):
        return RatifyPolicySettingsResult(
            outcome="skipped", path=settings_path, skip_reason="invalid_json"
        )

    return _classify(data, settings_path) or RatifyPolicySettingsResult(
        outcome="unset", path=settings_path
    )


def _classify(data: object, settings_path: Path) -> RatifyPolicySettingsResult | None:
    """The one classification of parsed settings data, shared by the peek and the write
    path's re-read so the two can never disagree about what is safe to merge into. Returns
    the skip / `already_set` result, or `None` when `data` is a mergeable object."""
    if not isinstance(data, dict):
        return RatifyPolicySettingsResult(
            outcome="skipped", path=settings_path, skip_reason="not_an_object"
        )
    # `"env": null` is not an object either: the merge could not assign into it.
    if "env" in data and not isinstance(data["env"], dict):
        return RatifyPolicySettingsResult(
            outcome="skipped", path=settings_path, skip_reason="env_not_an_object"
        )
    env = data.get("env")
    if isinstance(env, dict) and RATIFY_POLICY_ENV_VAR in env:
        return RatifyPolicySettingsResult(
            outcome="already_set",
            path=settings_path,
            existing_value=str(env[RATIFY_POLICY_ENV_VAR]),
        )
    return None


def _link_in_the_way(repo_root: Path) -> bool:
    """True when `.claude/` or `settings.json` is a symlink (live or dangling). Checked
    again immediately before each write, since the peek's answer can be stale. May raise
    ``OSError`` (an unsearchable directory); both callers catch it."""
    return (repo_root / SETTINGS_RELATIVE_PATH.parent).is_symlink() or (
        repo_root / SETTINGS_RELATIVE_PATH
    ).is_symlink()


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
    symlink = RatifyPolicySettingsResult(
        outcome="skipped", path=settings_path, skip_reason="symlink"
    )
    created: tuple[int, int] | None = None
    try:
        if not settings_path.exists():
            # Re-check right before the write: the peek is stale by now. A swap between
            # this check and the syscalls below is a residual window we accept (see the
            # host-settings spec): closing it needs dir-fd pinning, and whoever can swap
            # `.claude/` inside the repo during init can write its hooks directly anyway.
            if _link_in_the_way(repo_root):
                return symlink
            settings_path.parent.mkdir(parents=True, exist_ok=True)
            # `"x"` is O_EXCL: it refuses a link at the final component instead of
            # following it (the parent is covered by the re-check above), and a file it
            # opened is ours, so a failed write may remove it.
            with open(settings_path, "x") as fh:
                st = os.fstat(fh.fileno())
                created = (st.st_dev, st.st_ino)
                fh.write(json.dumps({"env": {RATIFY_POLICY_ENV_VAR: value}}, indent=2) + "\n")
            return RatifyPolicySettingsResult(outcome="written", path=settings_path)

        if _link_in_the_way(repo_root):
            return symlink
        # A write-protected file is the project's statement; do not replace it. Root passes
        # `os.access` for any file, so the mode bits are checked too.
        if (
            not os.access(settings_path, os.W_OK)
            or stat.S_IMODE(settings_path.stat().st_mode) & 0o222 == 0
        ):
            return RatifyPolicySettingsResult(
                outcome="skipped", path=settings_path, skip_reason="write_failed"
            )
        # Re-read without following a link swapped in since the re-check (ELOOP).
        try:
            fd = os.open(settings_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        except OSError:
            if settings_path.is_symlink():
                return symlink
            raise
        with os.fdopen(fd) as fh:
            data = json.load(fh)
        # The file may have changed since the peek: classify the re-read the same way.
        reread = _classify(data, settings_path)
        if reread is not None:
            return reread
        data.setdefault("env", {})[RATIFY_POLICY_ENV_VAR] = value
        _replace_atomically(settings_path, json.dumps(data, indent=2) + "\n")
    except (OSError, ValueError):
        if created is not None:
            # The identity check narrows the window, it does not close it: concurrent
            # writers are out of scope, the same non-goal as the symlink residual.
            try:
                now = os.lstat(settings_path)
                if (now.st_dev, now.st_ino) == created:
                    settings_path.unlink(missing_ok=True)
            except OSError:
                pass
        return RatifyPolicySettingsResult(
            outcome="skipped", path=settings_path, skip_reason="write_failed"
        )
    return RatifyPolicySettingsResult(outcome="written", path=settings_path)


def _replace_atomically(path: Path, text: str) -> None:
    """Publish `text` at `path` through a sibling temp file and `os.replace`, keeping the
    existing file's mode. The temp file is removed on any failure, so the original is
    never left truncated or next to debris."""
    mode = stat.S_IMODE(path.stat().st_mode)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp_name, mode)
        os.replace(tmp_name, path)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise
