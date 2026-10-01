"""Git's repository-local environment variables, and the environment to run git with.

Any of ``GIT_LOCAL_ENV_VARS`` in the inherited environment overrides repository discovery
from ``cwd``, so a subprocess that means "the repository this directory is in" must run
with them dropped. Stdlib-only and portable: it reads ``os.environ`` and nothing else.
# see design/superpowers/specs/2026-09-30-initiative-from-store-repo-design.md §6 (D5)
"""

from __future__ import annotations

import os

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
