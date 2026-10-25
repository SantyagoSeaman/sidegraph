"""The settled clock and the dirty set that maintenance of a format-2 store reads from git.

Stdlib-only and pydantic-free. Every path returned is root-relative and POSIX; the one
conversion to a store-relative path is made by the caller (``Eligibility.eligible``).
Every git call runs as ``git -C <work-tree root>`` with a root-relative pathspec, so neither
the process's cwd nor a ``--db`` elsewhere changes what is listed, and with git's
repository-local environment dropped (:func:`sidegraph.gitenv.git_env`).
# see design/superpowers/specs/2026-10-20-packed-store-format-design.md (§6.2)
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from sidegraph.gitenv import git_env

# A clock pass walks the whole first-parent history of the store path; give it room.
_GIT_TIMEOUT_SECONDS = 300.0

_EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
_HEADER = re.compile(rb"\x01[0-9]+")


class ClockError(Exception):
    """The settled clock or the dirty set cannot be computed; maintenance must not guess."""


@dataclass(frozen=True)
class WorkTree:
    """The git work tree a store directory lives in.

    ``store_rel`` is the store path relative to ``root`` (POSIX, never empty: a store at the
    root is refused); ``head`` is ``HEAD`` when resolved (``""`` on an unborn branch).
    """

    root: Path
    store_rel: str
    head: str
    shallow: bool

    @property
    def pathspec(self) -> str:
        return self.store_rel


def _run(
    root: Path | None, args: list[str], pre: tuple[str, ...] = ()
) -> subprocess.CompletedProcess[bytes]:
    """Run ``git [pre] [-C root --literal-pathspecs] args``; never interpret a failure.

    ``--literal-pathspecs`` makes every pathspec a literal path, so a store directory named
    ``:(literal)x``, ``a*`` or ``[a]`` cannot select a sibling. ``LC_ALL=C`` keeps git's
    messages in English for the two outcomes callers tell apart. A missing git or a timeout
    raises :class:`ClockError`.
    """
    cmd = ["git", *pre]
    if root is not None:
        cmd += ["-C", str(root), "--literal-pathspecs"]
    try:
        return subprocess.run(
            [*cmd, *args],
            capture_output=True,
            stdin=subprocess.DEVNULL,
            env={**git_env(), "LC_ALL": "C"},
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ClockError(f"git {' '.join(args[:2])} could not run: {exc}") from exc


def _fail(args: list[str], proc: subprocess.CompletedProcess[bytes]) -> ClockError:
    err = proc.stderr.decode("utf-8", "replace").strip()
    return ClockError(f"git {' '.join(args[:2])} failed ({proc.returncode}): {err}")


def _git(root: Path, args: list[str], pre: tuple[str, ...] = ()) -> bytes:
    """Stdout of a git call that must succeed; :class:`ClockError` otherwise."""
    proc = _run(root, args, pre)
    if proc.returncode != 0:
        raise _fail(args, proc)
    return proc.stdout


def git_version() -> str:
    """``git --version``, for the diagnostics of a clock test run (``""`` if git is absent)."""
    try:
        proc = _run(None, ["--version"])
    except ClockError:
        return ""
    return proc.stdout.decode("utf-8", "replace").strip() if proc.returncode == 0 else ""


def resolve_work_tree(store_dir: Path) -> WorkTree | None:
    """The work tree containing ``store_dir``, resolved from the directory, not the cwd.

    ``None`` only when git says the directory is not inside a repository work tree (no
    clock). Every other failure (dubious ownership, timeout, missing git) raises
    :class:`ClockError`, so a broken git never selects the no-clock rule. Also
    ``ClockError`` for a store path relative to the root that holds a control character
    (it could be mistaken for a log header) and for a store that is the root itself (the
    caller's ``store_rel + "/"`` conversion and the header guard both need a subdirectory).
    # see design/superpowers/specs/2026-10-20-packed-store-format-design.md (§6.2)
    """
    store = Path(os.path.realpath(store_dir))
    if not store.is_dir():
        return None
    args = ["rev-parse", "--show-toplevel"]
    proc = _run(store, args)
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "replace")
        if "not a git repository" in err or "must be run in a work tree" in err:
            return None
        raise _fail(args, proc)
    out = proc.stdout
    if out.endswith(b"\n"):
        out = out[:-1]  # exactly one: a root directory name may itself end in a newline
    root = Path(os.path.realpath(os.fsdecode(out)))
    try:
        rel = store.relative_to(root).as_posix()
    except ValueError:
        return None
    if rel == ".":
        raise ClockError("the store must be a subdirectory of the work tree root")
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in rel):
        raise ClockError(f"store path {rel!r} contains a control character")
    shallow = _git(store, ["rev-parse", "--is-shallow-repository"]).strip() == b"true"
    wt = WorkTree(root=root, store_rel=rel, head="", shallow=shallow)
    return WorkTree(root=root, store_rel=rel, head=read_head(wt), shallow=shallow)


def read_head(wt: WorkTree) -> str:
    """The current ``HEAD`` commit id; ``""`` only for an unborn branch."""
    args = ["rev-parse", "--verify", "-q", "HEAD"]
    proc = _run(wt.root, args)
    if proc.returncode == 0:
        return proc.stdout.decode("ascii", "replace").strip()
    if proc.returncode == 1 and not proc.stderr.strip():
        return ""
    raise _fail(args, proc)


def parse_log_z(stream: bytes) -> dict[str, int]:
    """Root-relative path -> committer time of its newest first-parent commit.

    Exact parse: split into NUL-terminated tokens, strip one leading ``\\n`` from each, a token
    of ``\\x01`` + ASCII digits is a header, every other non-empty token a path. ``git log``
    lists newest first, so the first time seen for a path wins, whatever the dates say.
    # see design/superpowers/specs/2026-10-20-packed-store-format-design.md (§6.2)
    """
    times: dict[str, int] = {}
    current: int | None = None
    for token in stream.split(b"\x00"):
        if token.startswith(b"\n"):
            token = token[1:]
        if not token:
            continue
        if _HEADER.fullmatch(token):
            current = int(token[1:])
            continue
        if current is None:
            continue
        path = token.decode("utf-8", "surrogateescape")
        times.setdefault(path, current)
    return times


def landing_times(wt: WorkTree) -> dict[str, int]:
    """Landing time of every path under the store, from one first-parent log pass."""
    if not read_head(wt):
        return {}  # an unborn branch has no history, so nothing has landed
    out = _git(
        wt.root,
        [
            "log",
            "--first-parent",
            "--diff-merges=first-parent",
            "--no-show-signature",
            "-z",
            "--format=%x01%ct",
            "--name-only",
            "--",
            wt.pathspec,
        ],
        pre=("-c", "log.diffMerges=first-parent"),
    )
    return parse_log_z(out)


def _names(stream: bytes) -> list[str]:
    return [t.decode("utf-8", "surrogateescape") for t in stream.split(b"\x00") if t]


def dirty_paths(wt: WorkTree, recorded_head: str) -> frozenset[str]:
    """Paths that differ from ``recorded_head``, are untracked, or are hidden from ``git diff``
    by assume-unchanged (lower-case tag) or skip-worktree (``S``). Root-relative.
    # see design/superpowers/specs/2026-10-20-packed-store-format-design.md (§6.2)
    """
    dirty: set[str] = set()
    base = recorded_head or _EMPTY_TREE  # unborn branch: every tracked path differs
    dirty.update(_names(_git(wt.root, ["diff", "--name-only", "-z", base, "--", wt.pathspec])))
    dirty.update(_names(_git(wt.root, ["ls-files", "--others", "-z", "--", wt.pathspec])))
    for entry in _names(_git(wt.root, ["ls-files", "-v", "-z", "--", wt.pathspec])):
        tag, path = entry[:1], entry[2:]
        if tag.islower() or tag == "S":
            dirty.add(path)
    return frozenset(dirty)


def uncommitted_paths(wt: WorkTree) -> list[str]:
    """Root-relative paths under the store that ``git status`` shows as changed: staged,
    unstaged and untracked ones that are not ignored (``index.db*`` is). A rename lists both
    names. Tracked paths flagged assume-unchanged or skip-worktree are listed too: ``git status``
    hides an edit to them, and a migration would leave that file as it is.
    # see design/superpowers/specs/2026-10-20-packed-store-format-design.md (§6.1)
    """
    tokens = _names(
        _git(
            wt.root, ["status", "--porcelain=v1", "-z", "--untracked-files=all", "--", wt.pathspec]
        )
    )
    paths: list[str] = []
    renamed = False
    for token in tokens:
        if renamed:  # the origin of the rename or copy the previous entry named
            paths.append(token)
            renamed = False
            continue
        paths.append(token[3:])
        renamed = "R" in token[:2] or "C" in token[:2]
    for entry in _names(_git(wt.root, ["ls-files", "-v", "-z", "--", wt.pathspec])):
        if entry[:1].islower() or entry[:1] == "S":
            paths.append(entry[2:])
    return list(dict.fromkeys(paths))


def head_blob(wt: WorkTree, store_relative: str) -> bytes | None:
    """The bytes of ``<store>/<store_relative>`` as ``HEAD`` records them, ``None`` when there
    is no commit yet or ``HEAD`` has no such file.
    # see design/superpowers/specs/2026-10-20-packed-store-format-design.md (§6.1)
    """
    if not read_head(wt):
        return None
    listing = _git(wt.root, ["ls-tree", "-z", "HEAD", "--", f"{wt.store_rel}/{store_relative}"])
    if not listing:
        return None
    meta = listing.split(b"\t", 1)[0].split()
    if len(meta) != 3 or meta[1] != b"blob":
        return None
    return _git(wt.root, ["cat-file", "blob", meta[2].decode("ascii")])


def git_ls_files_shards(wt: WorkTree) -> list[str]:
    """Names git tracks under ``<store>/shards``, in git's own case, root-relative."""
    prefix = f"{wt.store_rel}/shards"
    return _names(_git(wt.root, ["ls-files", "-z", "--", prefix]))


def current_branch(wt: WorkTree) -> str | None:
    """The checked-out branch name (``None`` on a detached ``HEAD``; an unborn branch has one).
    Read as the full ref and stripped of ``refs/heads/``, since ``--short`` prints
    ``heads/main`` when a tag has the branch's name."""
    args = ["symbolic-ref", "-q", "HEAD"]
    proc = _run(wt.root, args)
    if proc.returncode == 0:
        ref = proc.stdout.decode("utf-8", "surrogateescape").strip()
        prefix = "refs/heads/"
        return ref[len(prefix) :] if ref.startswith(prefix) and len(ref) > len(prefix) else None
    if proc.returncode == 1 and not proc.stderr.strip():
        return None
    raise _fail(args, proc)


def default_branch_names(wt: WorkTree) -> tuple[str, ...]:
    """The names that count as the repository's default branch: the target of
    ``refs/remotes/origin/HEAD`` when the clone has one, else ``main`` and ``master``. Only
    git's plain "no such ref" answer (exit 1, no message) selects the fallback; any other
    failure is a :class:`ClockError`.
    # see design/superpowers/specs/2026-10-20-packed-store-format-design.md (§6.2)
    """
    args = ["symbolic-ref", "-q", "refs/remotes/origin/HEAD"]
    proc = _run(wt.root, args)
    if proc.returncode == 0:
        ref = proc.stdout.decode("utf-8", "surrogateescape").strip()
        prefix = "refs/remotes/origin/"
        if ref.startswith(prefix) and len(ref) > len(prefix):
            return (ref[len(prefix) :],)
    elif not (proc.returncode == 1 and not proc.stderr.strip()):
        raise _fail(args, proc)
    return ("main", "master")


def branch_refusal(wt: WorkTree) -> str | None:
    """Why maintenance may not run on this checkout's branch, or ``None`` when it is the
    default branch. The caller skips this for ``--any-branch`` and outside git.
    # see design/superpowers/specs/2026-10-20-packed-store-format-design.md (§6.2)
    """
    names = default_branch_names(wt)
    branch = current_branch(wt)
    if branch is not None and branch in names:
        return None
    where = f"branch {branch!r}" if branch is not None else "a detached HEAD"
    return f"{where} is not the default branch ({' or '.join(names)}); use --any-branch to override"
