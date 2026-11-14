"""Git's repository-local environment variables, and the environment to run git with.

Any of ``GIT_LOCAL_ENV_VARS`` in the inherited environment overrides repository discovery
from ``cwd``, so a subprocess that means "the repository this directory is in" must run
with them dropped. Stdlib-only and portable: it reads ``os.environ`` and nothing else.
# see design/superpowers/specs/2026-09-30-initiative-from-store-repo-design.md §6 (D5)

``run_git`` is the one function that starts git: it refuses a directory whose discovery would
adopt a git directory without a ``.git`` entry, and adds two protective settings to the
environment, so configuration a checkout brings is never read.
# see design/superpowers/specs/2026-11-12-trust-boundary-clone-content-design.md (D1 to D3)
"""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Callable, Mapping, Sequence
from typing import Any

# Git's repository-local variables: the list `git rev-parse --local-env-vars` prints, minus
# the two config-injection variables `GIT_CONFIG_PARAMETERS` and `GIT_CONFIG_COUNT`. Any of
# these in the inherited environment overrides discovery from `cwd`. The two config ones
# select no repository, and containers and CI pass `safe.directory` through them, so they
# stay. `GIT_CEILING_DIRECTORIES` is not on git's list: it bounds discovery and must survive.
GIT_LOCAL_ENV_VARS = frozenset(
    {
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_CONFIG",
        "GIT_OBJECT_DIRECTORY",
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_IMPLICIT_WORK_TREE",
        "GIT_GRAFT_FILE",
        "GIT_INDEX_FILE",
        "GIT_NO_REPLACE_OBJECTS",
        "GIT_REPLACE_REF_BASE",
        "GIT_PREFIX",
        "GIT_SHALLOW_FILE",
        "GIT_COMMON_DIR",
    }
)


def git_env() -> dict[str, str]:
    """The inherited environment minus git's repository-local variables (``GIT_DIR`` and
    friends), so git discovers the repository from ``cwd``.
    """
    return {k: v for k, v in os.environ.items() if k not in GIT_LOCAL_ENV_VARS}


# The fsmonitor value is EMPTY on purpose: git 2.31 to 2.35 run any other value, "false"
# included, as a hook command; the empty value disables fsmonitor in every version.
PROTECTIVE_CONFIG: tuple[tuple[str, str], ...] = (
    ("safe.bareRepository", "explicit"),
    ("core.fsmonitor", ""),
)


class UnsafeGitDirectory(OSError):
    """Git was not started: discovery from the directory would adopt a git directory that
    has no ``.git`` entry, so its configuration could come from the checkout itself."""


def _inside_git_directory(path: str) -> bool:
    """Whether ``path`` is a ``.git`` directory or lies below one that is not part of a
    checkout. A ``.git`` component only exempts the path when no directory between it and
    ``path`` holds a ``.git`` entry: otherwise ``path`` is a checkout that was placed below a
    ``.git``-named folder, and nothing there was written by git."""
    parts = path.split(os.sep)
    named = [i for i, part in enumerate(parts) if part.lower() == ".git"]
    if not named:
        return False
    return not any(
        os.path.lexists(os.path.join(os.sep.join(parts[: i + 1]), ".git"))
        for i in range(named[-1] + 1, len(parts))
    )


def embedded_git_dir(start: str | os.PathLike[str] | None) -> str | None:
    """The directory git's discovery from ``start`` would adopt without a ``.git`` entry.

    Walks like git: a ``.git`` entry ends the walk (git uses it); a directory holding
    ``HEAD`` and ``objects`` or ``commondir`` is the answer, unless it is ``.git`` or lies
    below one (git accepts those, and a checkout cannot create that name) and no directory
    between that ``.git`` and it is a checkout of its own. ``None`` when discovery is safe,
    and for a path that cannot be resolved (a NUL byte, bytes, a loop). ``lexists`` only, no
    subprocess.
    # see design/superpowers/specs/2026-11-12-trust-boundary-clone-content-design.md (D2)
    """
    try:
        current = os.path.realpath(os.getcwd() if start is None else start)
        while True:
            if os.path.lexists(os.path.join(current, ".git")):
                return None
            if (
                os.path.lexists(os.path.join(current, "HEAD"))
                and (
                    os.path.lexists(os.path.join(current, "objects"))
                    or os.path.lexists(os.path.join(current, "commondir"))
                )
                and not _inside_git_directory(current)
            ):
                return current
            parent = os.path.dirname(current)
            if parent == current:
                return None
            current = parent
    except (OSError, ValueError, TypeError):
        return None


def protected_env(
    extra: Mapping[str, str] | None = None, *, keep_repository_env: bool = False
) -> dict[str, str]:
    """The environment every git call runs with: ``git_env()`` (or the whole inherited
    environment), then ``extra``, then ``PROTECTIVE_CONFIG`` appended after the pairs an
    inherited ``GIT_CONFIG_COUNT`` declares. A count that is not a plain number is left
    alone: git refuses to run with it.
    # see design/superpowers/specs/2026-11-12-trust-boundary-clone-content-design.md (D3)
    """
    env = dict(os.environ) if keep_repository_env else git_env()
    if extra:
        env.update(extra)
    raw = env.get("GIT_CONFIG_COUNT", "")
    # git reads the count with strtoul: leading whitespace and one "+" are part of a number
    if raw and not re.fullmatch(r"[ \t\n\v\f\r]*\+?[0-9]+", raw):
        return env
    count = int(raw) if raw else 0
    for offset, (key, value) in enumerate(PROTECTIVE_CONFIG):
        env[f"GIT_CONFIG_KEY_{count + offset}"] = key
        env[f"GIT_CONFIG_VALUE_{count + offset}"] = value
    env["GIT_CONFIG_COUNT"] = str(count + len(PROTECTIVE_CONFIG))
    return env


def run_git(
    args: Sequence[str],
    cwd: str | os.PathLike[str] | None,
    *,
    dash_c: bool = False,
    keep_repository_env: bool = False,
    env: Mapping[str, str] | None = None,
    run: Callable[..., Any] | None = None,
    **kwargs: Any,
) -> subprocess.CompletedProcess[Any]:
    """Start git. The only place in the package that does.

    ``cwd`` is where git starts looking for the repository (``None``: the working
    directory); ``dash_c`` hands it to git as ``-C`` instead of to the launcher. Raises
    :class:`UnsafeGitDirectory` (an ``OSError``) without starting git when
    :func:`embedded_git_dir` finds one. ``env`` goes on top of ``git_env()``;
    ``keep_repository_env`` keeps ``GIT_DIR`` and friends, for code git itself starts as a
    hook. Other keywords go to ``subprocess.run`` (or ``run``) unchanged.
    # see design/superpowers/specs/2026-11-12-trust-boundary-clone-content-design.md (D1)
    """
    unsafe = embedded_git_dir(cwd)
    if unsafe is not None:
        raise UnsafeGitDirectory(
            f"git not started: {unsafe} holds the files of a git directory but no .git entry"
        )
    argv = ["git", *args]
    if dash_c:
        if cwd is not None:
            argv = ["git", "-C", str(cwd), *args]
    else:
        kwargs["cwd"] = cwd
    launch = subprocess.run if run is None else run
    return launch(argv, env=protected_env(env, keep_repository_env=keep_repository_env), **kwargs)
