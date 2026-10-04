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

The second half of the module reads the settings that decide whether the plugin runs in a
session (`plugin_reach`, the `plugin-off-in-subdirectories` check's evidence); it never writes.
see design/superpowers/specs/2026-10-03-host-wiring-checks-design.md (D3)
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from ..config import _repository_boundary
from ..gitenv import git_env
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


# -- which directories run Sidegraph (the `plugin-off-in-subdirectories` evidence) ------------

PLUGIN_NAME = "sidegraph"
LOCAL_SETTINGS_RELATIVE_PATH = Path(".claude") / "settings.local.json"
_USER_SETTINGS_RELATIVE_PATH = Path(".claude") / "settings.json"

# One `git ls-files` lists every directory that can hold a launch; this is its budget, seconds.
_LS_FILES_TIMEOUT = 5.0


@dataclass(frozen=True)
class PluginReach:
    """Where Sidegraph runs in the repository at ``root``, by Claude Code's settings model.

    ``project_only`` (case a): the plugin is on for a launch at the root, but only through the
    root's project settings, which no launch below the root reads. ``off`` (case b): nested
    directories with a settings file of their own that sets ``enabledPlugins`` and whose merge
    leaves the plugin off, sorted; a nested file without it cannot change the merge, so case (a)
    already covers its directory.
    see design/superpowers/specs/2026-10-03-host-wiring-checks-design.md (D3)
    """

    root: Path
    project_only: bool
    off: tuple[Path, ...]


def _settings_map(path: Path) -> dict[str, object] | None:
    """``enabledPlugins`` of the settings file at ``path``; ``None`` when the file does not
    mention it: there is no such file, or it is not JSON, not an object, or holds no object under
    the key. Such a file cannot change a merge, so a caller treats ``None`` as "no say"."""
    try:
        if not path.is_file():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    plugins = data.get("enabledPlugins") if isinstance(data, dict) else None
    return dict(plugins) if isinstance(plugins, dict) else None


def _on(*maps: dict[str, object] | None) -> bool:
    """Sidegraph is on under the key-wise merge of ``maps`` (later ones win): some key whose
    plugin part, before ``@``, is ``sidegraph`` holds ``true``.
    see design/superpowers/specs/2026-10-03-host-wiring-checks-design.md (D3)"""
    merged: dict[str, object] = {}
    for one in maps:
        merged.update(one or {})
    return any(
        key.partition("@")[0] == PLUGIN_NAME and value is True for key, value in merged.items()
    )


def _user_map() -> dict[str, object] | None:
    """The user's ``enabledPlugins``: ``$CLAUDE_CONFIG_DIR/settings.json`` when the variable is
    set and non-empty, else ``~/.claude/settings.json`` through ``Path.home()`` (``HOME``), which
    is the directory Claude Code reads its user settings from."""
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    if config_dir:
        return _settings_map(Path(config_dir) / "settings.json")
    try:
        return _settings_map(Path.home() / _USER_SETTINGS_RELATIVE_PATH)
    except RuntimeError:  # no home directory
        return None


def _listed_directories(listing: bytes) -> list[str]:
    """The directories (relative, root excluded) that hold at least one path of a ``-z`` listing,
    with every ancestor of one, sorted."""
    seen: set[str] = set()
    for raw in listing.split(b"\0"):
        directory = os.path.dirname(os.fsdecode(raw))
        while directory and directory not in seen:
            seen.add(directory)
            directory = os.path.dirname(directory)
    return sorted(seen)


def plugin_reach(project: Path) -> PluginReach | None:
    """Whether Sidegraph runs in every directory of the repository that holds ``project``, or
    ``None`` when that cannot be told (no repository, git missing or slow).

    The model, verified against Claude Code 2.1.288: a session launched in ``D`` reads the user
    settings, ``D/.claude/settings.json``, ``D/.claude/settings.local.json`` and the git root's
    ``.claude/settings.local.json``, and ``enabledPlugins`` merges key by key in that order. The
    root's project settings are therefore read by root launches only.

    The repository root is the nearest ancestor with a ``.git`` entry (no subprocess). The
    candidate directories are the parents of every path ``git ls-files --cached`` reports (the
    index alone: ``--others`` scanned the whole work tree and cost most of the walk), each checked
    directly for its two settings files: the files themselves are never taken from the listing,
    because a repository may ignore ``.claude/`` and Claude Code's global ignore hides every
    ``settings.local.json``. That is the only subprocess. A plugin that is off at the root too is
    not reported: nothing was switched off.
    see design/superpowers/specs/2026-10-03-host-wiring-checks-design.md (D3)
    """
    boundary = _repository_boundary(os.path.abspath(project))
    if boundary is None:
        return None
    root = Path(boundary)
    user = _user_map()
    root_project = _settings_map(root / SETTINGS_RELATIVE_PATH)
    root_local = _settings_map(root / LOCAL_SETTINGS_RELATIVE_PATH)
    if not _on(user, root_project, root_local):
        return PluginReach(root, project_only=False, off=())
    project_only = not _on(user, root_local)
    try:
        done = subprocess.run(
            ["git", "ls-files", "--cached", "-z"],
            cwd=root,
            env=git_env(),
            capture_output=True,
            timeout=_LS_FILES_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    if done.returncode != 0:
        return None
    off: list[Path] = []
    for relative in _listed_directories(done.stdout):
        directory = root / relative
        own_project = _settings_map(directory / SETTINGS_RELATIVE_PATH)
        own_local = _settings_map(directory / LOCAL_SETTINGS_RELATIVE_PATH)
        if own_project is None and own_local is None:  # neither file sets `enabledPlugins`
            continue
        if not _on(user, own_project, own_local, root_local):
            off.append(directory)
    return PluginReach(root, project_only=project_only, off=tuple(off))
