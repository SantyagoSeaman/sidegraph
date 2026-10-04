"""The launch-commit record: the commit SessionStart resolved for ``@main``.

Every public hook command used to launch ``uvx --from git+…@main``, and uv re-resolves a
branch ref on every call. SessionStart keeps that (once per session, and it is what carries a
release to a running machine); the hot-path hooks (Stop, PreToolUse) read the commit it
recorded here and launch from that exact commit, which uv serves from its cache. Only a
fallback on ``CANONICAL_REF`` takes the record: a plugin pinned to a tag (a snapshot cut
with ``tools/release-public.sh --ref``) keeps its own ref, so the hot prefix compares the
fallback's ref before it repoints.

The record is user-level and host-neutral, ``${XDG_CACHE_HOME:-$HOME/.cache}/sidegraph/
launch-commit``, and holds exactly the 40 hex bytes of a commit. The shell prefix that reads
it lives in the public hook manifests (``plugin/sidegraph/hooks/hooks.public.json``,
``plugin/sidegraph/codex/hooks.public.json``); this module is the writer.

# see design/superpowers/specs/2026-10-03-launch-from-session-commit-design.md (D1-D4)
"""

from __future__ import annotations

import contextlib
import importlib.metadata
import json
import os
import re
from pathlib import Path

# The branch the public manifests install from. tools/release-public.sh declares the same
# value; tests/test_public_hook_launch_release.py pins the two equal.
CANONICAL_REF = "main"
CANONICAL_URL = "https://github.com/SantyagoSeaman/sidegraph.git"

_COMMIT = re.compile(r"[0-9a-f]{40}")


def launch_commit_path() -> Path:
    """Where the record lives: ``$XDG_CACHE_HOME/sidegraph/launch-commit``, falling back to
    ``~/.cache`` when the variable is unset or empty (the shell's ``${…:-…}`` treats empty as
    unset, and the hot commands read the same path).

    # see design/superpowers/specs/2026-10-03-launch-from-session-commit-design.md (D2)
    """
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "sidegraph" / "launch-commit"


def _resolved_main_commit() -> str | None:
    """The commit uv resolved for this install, only when this is an ``@main`` install of the
    canonical URL: a tag or SHA pin, an editable or file install, a PyPI install (no
    ``direct_url.json``) or another URL yields ``None``."""
    text = importlib.metadata.distribution("sidegraph").read_text("direct_url.json")
    if not text:
        return None
    info = json.loads(text)
    if not isinstance(info, dict) or info.get("url") != CANONICAL_URL:
        return None
    vcs = info.get("vcs_info")
    if not isinstance(vcs, dict) or vcs.get("vcs") != "git":
        return None
    if vcs.get("requested_revision") != CANONICAL_REF:
        return None
    commit = vcs.get("commit_id")
    if not isinstance(commit, str) or not _COMMIT.fullmatch(commit):
        return None
    return commit


def _record_main_commit() -> None:
    commit = _resolved_main_commit()
    if commit is None:
        return
    data = commit.encode("ascii")
    path = launch_commit_path()
    # Read the existing value only when it can be the one being written: a regular file of
    # exactly 40 bytes (``stat`` follows a symlink). A FIFO would block ``open`` forever, and
    # this runs first in SessionStart; a huge file would be read whole. Anything else counts as
    # "differs" and goes to the atomic write, which replaces a FIFO and a symlink and fails
    # quietly over a directory.
    with contextlib.suppress(OSError):
        if path.is_file() and path.stat().st_size == len(data) and path.read_bytes() == data:
            return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_bytes(data)
        os.replace(tmp, path)
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink()


def record_launch_commit() -> None:
    """Record the commit of an ``@main`` install for the hot-path hooks to launch from.

    Writes only for an install that asked for ``@main`` (``requested_revision``): the record
    is shared by every project on the machine, so a pinned install used for CI or a pilot must
    never repoint the plugin's hot hooks. The file holds the 40 hex bytes with no newline, is
    replaced atomically, and is left untouched when it already holds the same value. The existing
    value is read only from a regular file of exactly 40 bytes, so a FIFO or an oversized file at
    the path never blocks or slows SessionStart. Never raises and never prints: a failure leaves
    the hot hooks on the ``@main`` fallback, which is today's behaviour.

    # see design/superpowers/specs/2026-10-03-launch-from-session-commit-design.md (D2)
    """
    with contextlib.suppress(Exception):
        _record_main_commit()
