"""The graph refresh git hook: install it, remove it, and tell whether it is there.

Sidegraph answers from ``graphify-out/graph.json``, and a graph nobody rebuilds goes stale. This
module installs a small helper script into the repository's common git directory
(``<common>/hooks/sidegraph-graph-refresh``) and a marked three-line block into the
``post-commit``, ``post-merge`` and ``post-checkout`` hooks that calls it. The helper rebuilds
the graph in the background, in the main checkout only (a linked worktree reads the main
checkout's graph), one rebuild at a time, and never misses a request that arrives during one.

The block is inserted right after the hook's shebang line, never replacing anything: a foreign
hook ends up byte-identical to its pre-install state once the block is removed. Hooks that are
not safe to edit (a symlink, non-executable, CRLF, not a shell script, damaged markers) are
refused and left untouched, and the line to add by hand is reported instead.

Portable core: plain git and files, no Graphify or Claude Code specifics beyond the command the
helper runs. Every git call runs through ``gitenv.git_env()`` from the repository the caller
names, so a ``GIT_DIR`` inherited from a hook never redirects it.
# see design/superpowers/specs/2026-10-02-graph-refresh-hook-design.md (D1-D6)
"""

from __future__ import annotations

import contextlib
import os
import shlex
import shutil
import stat
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .config import main_checkout_root
from .gitenv import git_env

HOOKS: tuple[str, ...] = ("post-commit", "post-merge", "post-checkout")
HELPER_NAME = "sidegraph-graph-refresh"

#: The git config key that records the human's answer: ``false`` is "no, stop asking".
CONFIG_KEY = "sidegraph.graphRefresh"

START_MARKER = "# >>> sidegraph graph refresh >>>"
END_MARKER = "# <<< sidegraph graph refresh <<<"
CREATED_HEADER = "#!/bin/sh\n# Created by sidegraph-init.\n"
DAMAGED_MESSAGE = "Sidegraph's block markers are damaged; fix or remove them by hand"

# Markers Graphify's own hooks carry (D6).
GRAPHIFY_MARKERS = ("# graphify-hook-start", "# graphify-checkout-hook-start")

_BLOCK_DESCRIPTION = (
    "# Rebuilds the code graph in the background, main checkout only. "
    "Managed by sidegraph-init; remove with `sidegraph-init --remove-hooks`."
)
_SHELLS = frozenset({"sh", "bash", "dash", "zsh", "ksh"})
_GIT_TIMEOUT = 10.0
# A lock with no pid file, or an empty one, is a job that died (or could not write) before it
# wrote its pid; only its age says so. The helper breaks one after this long (``-mmin +10``).
_STALE_LOCK_SECONDS = 600

# The helper, verbatim from the reviewed design (D5): the "want" flag, ``take``, the ``.gate``
# stale-breaker and ``me=$(exec sh -c 'echo $PPID')`` are the tested locking and are not to be
# edited. One review amendment: a lock with no pid (its owner died, or could not write, between
# ``mkdir`` and ``echo``) is broken once it is ten minutes old, the age checked again under the
# gate; before, it blocked every rebuild for good. ``@GRAPHIFY@`` is the one substitution: the
# path found at install time, quoted, or ''.
_HELPER_TEMPLATE = r"""#!/bin/sh
# Installed by sidegraph-init: keeps graphify-out/graph.json current for Sidegraph.
# Main checkout only: a linked worktree reads the main checkout's graph. The rebuild runs
# in the background so git never waits. A "want" flag plus a lock directory run one rebuild
# at a time and never miss a request that arrives during one.
hook=$1; shift
[ "$(git rev-parse --path-format=absolute --git-dir 2>/dev/null)" = \
  "$(git rev-parse --path-format=absolute --git-common-dir 2>/dev/null)" ] || exit 0
case $hook in post-checkout) [ "$3" = 1 ] && [ "$1" != "$2" ] || exit 0 ;; esac
top=$(git rev-parse --show-toplevel 2>/dev/null) || exit 0
common=$(git rev-parse --path-format=absolute --git-common-dir 2>/dev/null) || exit 0
graphify=$(command -v graphify 2>/dev/null) || graphify=@GRAPHIFY@
[ -n "$graphify" ] && [ -x "$graphify" ] || exit 0
lock=$common/sidegraph-graph-refresh.lock
want=$common/sidegraph-graph-refresh.want
log=$common/sidegraph-graph-refresh.log
: > "$want" 2>/dev/null || exit 0
(
  me=$(exec sh -c 'echo $PPID')
  take() { mkdir "$lock" 2>/dev/null && echo "$me" > "$lock/pid"; }
  if ! take; then
    pid=$(cat "$lock/pid" 2>/dev/null)
    if [ -n "$pid" ]; then
      kill -0 "$pid" 2>/dev/null && exit 0      # a runner is alive; it will see $want
    else                                        # the owner is mid-write, or died there
      [ -n "$(find "$lock" -maxdepth 0 -mmin +10 2>/dev/null)" ] || exit 0
    fi
    mkdir "$lock.gate" 2>/dev/null || exit 0    # one stale-breaker at a time
    [ "$(cat "$lock/pid" 2>/dev/null)" = "$pid" ] &&
      { [ -n "$pid" ] || [ -n "$(find "$lock" -maxdepth 0 -mmin +10 2>/dev/null)" ]; } &&
      rm -rf "$lock"
    rmdir "$lock.gate"; take || exit 0
  fi
  cd "$top" || { rm -rf "$lock"; exit 0; }
  while :; do
    while [ -e "$want" ]; do
      rm -f "$want"
      PYTHONHASHSEED=0 "$graphify" update . > "$log" 2>&1
    done
    rm -rf "$lock"; [ -e "$want" ] || break; take || break
  done
) </dev/null >/dev/null 2>&1 &
exit 0
"""


@dataclass(frozen=True)
class RepoInfo:
    """Where a repository keeps what the hook touches, as git reports it (absolute paths)."""

    top: Path  # the working tree's root
    common: Path  # the common git directory, shared by every linked worktree
    is_main: bool  # this working tree's git directory is the common one (not a linked worktree)
    hooks_dir: Path  # where git looks for hooks: ``core.hooksPath`` when set
    hooks_path_set: bool  # ``core.hooksPath`` points elsewhere than ``<common>/hooks``

    @property
    def own_hooks_dir(self) -> Path:
        """``<common>/hooks``: where the helper always lives, and where Sidegraph's blocks are
        written when ``core.hooksPath`` is unset."""
        return self.common / "hooks"

    @property
    def helper(self) -> Path:
        return self.own_hooks_dir / HELPER_NAME


@dataclass(frozen=True)
class HookResult:
    """What happened to one hook file. ``refused`` and ``damaged`` leave it untouched and carry
    the ``reason``; ``removed`` kept the file, ``deleted`` removed a file Sidegraph created."""

    hook: str
    action: Literal[
        "created", "inserted", "replaced", "unchanged", "refused", "damaged", "removed", "deleted"
    ]
    reason: str | None = None


@dataclass(frozen=True)
class InstallReport:
    hooks: tuple[HookResult, ...]  # empty when ``core.hooksPath`` is set: nothing was written
    hooks_path_set: bool
    graphify_hook: bool  # Graphify's own hook is also installed (D6)


@dataclass(frozen=True)
class RemoveReport:
    hooks: tuple[HookResult, ...]
    helper_removed: bool


@dataclass(frozen=True)
class Status:
    helper_present: bool
    well_formed: tuple[str, ...]  # hooks that hold one well-formed block, in ``own_hooks_dir``
    wired: bool  # the helper exists and each hook in ``hooks_dir`` is executable and calls it (D7)
    hooks_path_set: bool
    graphify_hook: bool

    @property
    def installed(self) -> bool:
        """Sidegraph's own state: the helper and all three blocks, where git runs hooks from."""
        return self.helper_present and not self.hooks_path_set and len(self.well_formed) == 3


@dataclass(frozen=True)
class Choice:
    """The recorded answer to "keep the graph fresh?": ``declined`` is a (boolean) ``false``,
    and ``scope`` is the git config level it was read from (``local``, ``global``, ...)."""

    declined: bool
    scope: str


# -- git ---------------------------------------------------------------------------------


def _git(args: list[str], cwd: Path, timeout: float) -> subprocess.CompletedProcess[str] | None:
    """One git call from ``cwd``, never raising; ``None`` when git cannot run."""
    try:
        return subprocess.run(
            ["git", *args],
            cwd=cwd,
            env=git_env(),
            capture_output=True,
            text=True,
            timeout=max(timeout, 0.05),
        )
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def repo_info(cwd: Path, *, timeout: float = _GIT_TIMEOUT) -> RepoInfo | None:
    """The repository ``cwd`` is in, or ``None`` outside one (or without git). One git call.
    # see design/superpowers/specs/2026-10-02-graph-refresh-hook-design.md (D1, D2)"""
    done = _git(
        [
            "rev-parse",
            "--path-format=absolute",
            "--show-toplevel",
            "--git-dir",
            "--git-common-dir",
            "--git-path",
            "hooks",
        ],
        cwd,
        timeout,
    )
    if done is None or done.returncode != 0:
        return None
    lines = done.stdout.split("\n")
    if len(lines) != 5 or lines[4] != "":  # a path holding a newline cannot be told apart
        return None
    top, git_dir, common, hooks = (Path(line) for line in lines[:4])
    return RepoInfo(
        top=top,
        common=common,
        is_main=git_dir == common,
        hooks_dir=hooks,
        hooks_path_set=hooks != common / "hooks",
    )


def rebuild_target(info: RepoInfo, store_path: str | Path) -> Path | None:
    """The one graph the helper rebuilds for this store's repository:
    ``<main checkout>/graphify-out/graph.json``, since the helper runs in the main checkout only
    (a linked worktree reads that graph). ``None`` when there is no main checkout for it to run
    in: a bare repository with worktrees (the ``.bare`` layout) has only linked ones.
    see design/superpowers/specs/2026-10-02-graph-refresh-hook-design.md (D5, D7)"""
    main = info.top if info.is_main else main_checkout_root(store_path)
    return None if main is None else main / "graphify-out" / "graph.json"


def read_choice(cwd: Path, *, timeout: float = _GIT_TIMEOUT) -> Choice | None:
    """The recorded choice, or ``None`` when ``sidegraph.graphRefresh`` is unset (or not a
    boolean). Read as a boolean, so a raw ``no`` reads ``false``; a global ``false`` counts.
    # see design/superpowers/specs/2026-10-02-graph-refresh-hook-design.md (D4)"""
    done = _git(["config", "--show-scope", "--type=bool", "--get", CONFIG_KEY], cwd, timeout)
    if done is None or done.returncode != 0:
        return None
    scope, _, value = done.stdout.strip().partition("\t")
    if not value:
        return None
    return Choice(declined=value == "false", scope=scope)


def record_choice(cwd: Path, *, declined: bool, timeout: float = _GIT_TIMEOUT) -> bool:
    """Write ``false`` to the local config (the common ``config``, even from a linked
    worktree), or unset the local value. A global ``false`` survives the unset. ``True`` when
    git did what was asked; an absent key is a fine thing to unset.
    # see design/superpowers/specs/2026-10-02-graph-refresh-hook-design.md (D4)"""
    if declined:
        done = _git(["config", "--local", CONFIG_KEY, "false"], cwd, timeout)
        return done is not None and done.returncode == 0
    done = _git(["config", "--local", "--unset", CONFIG_KEY], cwd, timeout)
    return done is not None and done.returncode in (0, 5)  # 5: the key was not set


# -- texts -------------------------------------------------------------------------------


def manual_line(hook: str) -> str:
    """The one command line of the block, which a human adds by hand to a hook Sidegraph will
    not edit, or to a ``core.hooksPath`` directory."""
    return (
        'g="$(git rev-parse --path-format=absolute --git-common-dir 2>/dev/null)'
        f'/hooks/{HELPER_NAME}"; [ ! -x "$g" ] || "$g" {hook} "$@" || true'
    )


def block(hook: str) -> str:
    """The marked block for ``hook``, every line ending in a newline (D2)."""
    return f"{START_MARKER}\n{_BLOCK_DESCRIPTION}\n{manual_line(hook)}\n{END_MARKER}\n"


def helper_script(graphify: str | None) -> str:
    """The helper's text. ``graphify`` is the path found at install time, the fallback for a git
    client whose PATH lacks it: quoted with ``shlex.quote``, and not recorded at all when it is
    empty or holds a newline or a NUL (the script then relies on ``command -v`` alone).
    # see design/superpowers/specs/2026-10-02-graph-refresh-hook-design.md (D5)"""
    recorded = "''"
    if graphify and "\n" not in graphify and "\0" not in graphify:
        recorded = shlex.quote(graphify)
    return _HELPER_TEMPLATE.replace("@GRAPHIFY@", recorded)


# -- files -------------------------------------------------------------------------------


def _publish_file(path: Path, data: bytes, mode: int) -> None:
    """Publish ``data`` at ``path`` with ``mode`` by writing a sibling and renaming over it, so
    that a git process (or a running helper) never reads a half-written file."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _decode(data: bytes) -> str:
    return data.decode("utf-8", "surrogateescape")


def _encode(text: str) -> bytes:
    return text.encode("utf-8", "surrogateescape")


def _markers(text: str) -> tuple[Literal["none", "well-formed", "damaged"], int, int]:
    """Where Sidegraph's block sits in ``text``: ``("well-formed", start, end)`` for exactly one
    start marker line before exactly one end marker line (``end`` is past the end marker's
    newline), ``("none", 0, 0)`` without either, and ``damaged`` for anything else (a lone
    marker, reversed markers, two pairs)."""
    starts: list[int] = []
    ends: list[int] = []
    pos = 0
    for line in text.split("\n"):
        after = min(pos + len(line) + 1, len(text))
        if line == START_MARKER:
            starts.append(pos)
        elif line == END_MARKER:
            ends.append(after)
        pos += len(line) + 1
    if not starts and not ends:
        return "none", 0, 0
    if len(starts) == 1 and len(ends) == 1 and starts[0] < ends[0]:
        return "well-formed", starts[0], ends[0]
    return "damaged", 0, 0


def _is_shell_shebang(first_line: str) -> bool:
    """A shebang for ``sh``, ``bash``, ``dash``, ``zsh`` or ``ksh``: a direct path or
    ``/usr/bin/env <name>``, with any trailing arguments."""
    if not first_line.startswith("#!"):
        return False
    tokens = first_line[2:].split()
    if not tokens:
        return False
    name = tokens[0].rsplit("/", 1)[-1]
    if name == "env":
        name = tokens[1] if len(tokens) > 1 else ""
    return name in _SHELLS


def _refusal(path: Path, data: bytes) -> str | None:
    """Why Sidegraph will not edit this existing hook, or ``None`` when it may. A symlink is
    checked by the caller, which holds the ``lstat``."""
    if not os.access(path, os.X_OK):
        return (
            "it is not executable (git ignores it, and making it executable would re-enable "
            "code its owner disabled)"
        )
    if b"\r\n" in data:
        return "it has CRLF line endings"
    newline = data.find(b"\n")
    if newline < 0 or not _is_shell_shebang(_decode(data[:newline])):
        return (
            "its first line is not a shebang for sh, bash, dash, zsh or ksh "
            "(or the file has no newline after it)"
        )
    return None


def _place(path: Path, hook: str) -> HookResult:
    """Put the block in one hook file: create the file, insert right after the shebang, or
    replace an earlier block in place. Anything unsafe is refused and left untouched."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        _publish_file(path, _encode(CREATED_HEADER + block(hook)), 0o755)
        return HookResult(hook, "created")
    if stat.S_ISLNK(info.st_mode):
        return HookResult(hook, "refused", "it is a symlink")
    if not stat.S_ISREG(info.st_mode):
        return HookResult(hook, "refused", "it is not a regular file")
    data = path.read_bytes()
    why = _refusal(path, data)
    if why is not None:
        return HookResult(hook, "refused", why)
    text = _decode(data)
    state, start, end = _markers(text)
    if state == "damaged":
        return HookResult(hook, "damaged", DAMAGED_MESSAGE)
    new_block = block(hook)
    if state == "well-formed":
        updated = text[:start] + new_block + text[end:]
        action: Literal["replaced", "unchanged", "inserted"] = (
            "unchanged" if updated == text else "replaced"
        )
    else:
        cut = text.index("\n") + 1  # _refusal guarantees a first line
        updated = text[:cut] + new_block + text[cut:]
        action = "inserted"
    if action != "unchanged":
        _publish_file(path, _encode(updated), stat.S_IMODE(info.st_mode))
    return HookResult(hook, action)


def _strip(path: Path, hook: str) -> HookResult:
    """Take the block out of one hook file, byte for byte. A file that is then exactly the
    header Sidegraph created is deleted; anything else keeps its remainder and mode."""
    try:
        info = path.lstat()
    except FileNotFoundError:
        return HookResult(hook, "unchanged")
    if stat.S_ISLNK(info.st_mode):
        return HookResult(hook, "refused", "it is a symlink, left untouched")
    if not stat.S_ISREG(info.st_mode):
        return HookResult(hook, "unchanged")
    text = _decode(path.read_bytes())
    state, start, end = _markers(text)
    if state == "none":
        return HookResult(hook, "unchanged")
    if state == "damaged":
        return HookResult(hook, "damaged", DAMAGED_MESSAGE)
    remainder = text[:start] + text[end:]
    if remainder == CREATED_HEADER:
        path.unlink()
        return HookResult(hook, "deleted")
    _publish_file(path, _encode(remainder), stat.S_IMODE(info.st_mode))
    return HookResult(hook, "removed")


def _has_graphify_hook(info: RepoInfo) -> bool:
    """Whether any hook the repository runs holds one of Graphify's own markers."""
    for hook in HOOKS:
        try:
            text = _decode((info.hooks_dir / hook).read_bytes())
        except OSError:
            continue
        if any(marker in text for marker in GRAPHIFY_MARKERS):
            return True
    return False


def _lock_is_dead(lock: Path) -> bool:
    """Whether the helper's lock directory belongs to a job that is gone: its pid no longer runs,
    or it has no usable pid and is over ten minutes old. A pid that runs, even another user's, and
    a recent pid-less lock (an owner still writing) are live. A pid can be reused by a live
    process after a reboot, which this cannot tell from the runner."""
    try:
        age = time.time() - lock.stat().st_mtime
    except OSError:
        return False
    try:
        text = (lock / "pid").read_text().strip()
    except OSError:
        text = ""
    pid = int(text) if text.isdigit() else 0
    if pid <= 0:
        return age > _STALE_LOCK_SECONDS
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except OSError:  # PermissionError: the process exists
        return False
    return False


# -- install, remove, status -----------------------------------------------------------------


def install_helper(info: RepoInfo, *, graphify: str | None) -> None:
    """Write (or refresh) the helper alone, atomically, and clear what a dead job left: a lock
    whose pid no longer runs, or that never got one and is over ten minutes old, and a stale
    stale-breaker gate. For a repository whose hooks call it by hand.
    # see design/superpowers/specs/2026-10-02-graph-refresh-hook-design.md (D2, D5)"""
    info.own_hooks_dir.mkdir(parents=True, exist_ok=True)
    lock = info.common / f"{HELPER_NAME}.lock"
    if _lock_is_dead(lock):  # re-running --hooks is the way out of a lock a dead job left
        shutil.rmtree(lock, ignore_errors=True)
    _publish_file(info.helper, _encode(helper_script(graphify)), 0o755)
    with contextlib.suppress(OSError):  # a stale stale-breaker's gate (D5)
        (info.common / f"{HELPER_NAME}.lock.gate").rmdir()


def install(info: RepoInfo, *, graphify: str | None) -> InstallReport:
    """Install (or refresh) the helper, and the block in each of the three hooks.

    The helper always goes to ``<common>/hooks``, even with ``core.hooksPath`` set; a lock a dead
    job left (see ``_lock_is_dead``) is removed first. Hook files
    are written only when it is unset: a hooks manager owns and regenerates that other directory,
    so the caller prints ``manual_line`` for each hook instead. A second run changes no byte.
    # see design/superpowers/specs/2026-10-02-graph-refresh-hook-design.md (D2, D5)"""
    install_helper(info, graphify=graphify)
    hooks: tuple[HookResult, ...] = ()
    if not info.hooks_path_set:
        hooks = tuple(_place(info.hooks_dir / hook, hook) for hook in HOOKS)
    return InstallReport(hooks, info.hooks_path_set, _has_graphify_hook(info))


def remove(info: RepoInfo) -> RemoveReport:
    """Take Sidegraph out: the block from each hook, then (once) the helper, the want flag, any
    lock and gate, and the recorded choice. The rebuild log is left for the owner to read.
    # see design/superpowers/specs/2026-10-02-graph-refresh-hook-design.md (D3)"""
    hooks = tuple(_strip(info.own_hooks_dir / hook, hook) for hook in HOOKS)
    helper_removed = info.helper.exists()
    info.helper.unlink(missing_ok=True)
    (info.common / f"{HELPER_NAME}.want").unlink(missing_ok=True)
    for leftover in ("lock", "lock.gate"):
        shutil.rmtree(info.common / f"{HELPER_NAME}.{leftover}", ignore_errors=True)
    record_choice(info.top, declined=False)
    return RemoveReport(hooks, helper_removed)


def _runs_helper(path: Path) -> bool:
    """Whether git would run the hook at ``path`` and the hook calls the helper: the file is
    executable (git ignores one that is not) and the helper's name is on a line whose first
    non-blank character is not ``#``, so a comment that names it, a block of Sidegraph's own
    or a line added by hand are told apart by what the shell would run."""
    try:
        if not os.access(path, os.X_OK):
            return False
        text = _decode(path.read_bytes())
    except OSError:
        return False
    return any(
        HELPER_NAME in line and not line.lstrip(" \t").startswith("#") for line in text.splitlines()
    )


def status(info: RepoInfo) -> Status:
    """What is installed now, read-only: the helper, the hooks that hold a well-formed block,
    whether git would run the helper (a call added by hand counts, see ``_runs_helper``), and
    Graphify's own hook.
    # see design/superpowers/specs/2026-10-02-graph-refresh-hook-design.md (D7)"""
    well_formed = []
    for hook in HOOKS:
        try:
            state = _markers(_decode((info.own_hooks_dir / hook).read_bytes()))[0]
        except OSError:
            continue
        if state == "well-formed":
            well_formed.append(hook)
    helper_present = info.helper.is_file()
    wired = helper_present and all(_runs_helper(info.hooks_dir / hook) for hook in HOOKS)
    return Status(
        helper_present=helper_present,
        well_formed=tuple(well_formed),
        wired=wired,
        hooks_path_set=info.hooks_path_set,
        graphify_hook=_has_graphify_hook(info),
    )
