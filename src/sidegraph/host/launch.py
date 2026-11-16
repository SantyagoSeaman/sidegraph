"""The launch-commit record: the commit SessionStart resolved for ``@main``.

Plugin installs older than 0.25.0 launched every hook with ``uvx --from git+…@main``, and uv
re-resolves a branch ref on every call. Their SessionStart kept ``@main`` (once per session,
and it is what carried a release to a running machine); their hot-path hooks read the commit
it recorded here and launched from that exact commit, which uv serves from its cache: Stop,
PreToolUse and SubagentStart on Claude and Grok, Stop and SubagentStart on Codex, and Muse's
SubagentStart (read in Python by ``muse/hook.py``). Only a fallback on ``CANONICAL_REF``
took the record: a plugin pinned to a tag kept its own ref.

No current manifest reads the record: since 0.25.0 every public manifest launches an exact
PyPI version (``uvx --from sidegraph==X.Y.Z``), so a current install writes nothing here.
This module stays as the writer for a migration window only: an older install, whose
SessionStart now runs the current code, keeps its hot hooks on that same code
(``tests/test_host_launch.py`` guards it with an old manifest's Stop command). It can go once
pre-0.25.0 installs are out of support, together with that test.

The record is user-level and host-neutral, ``${XDG_CACHE_HOME:-$HOME/.cache}/sidegraph/
launch-commit``, and holds exactly the 40 hex bytes of a commit. Only an install whose
``direct_url.json`` names the canonical URL at ``requested_revision`` ``main`` writes it.

# see design/superpowers/specs/2026-10-03-launch-from-session-commit-design.md (D1-D4)
"""

from __future__ import annotations

import contextlib
import importlib.metadata
import json
import os
import re
from collections.abc import Callable
from pathlib import Path

# The branch a pre-0.25.0 install launched (``@main``): only an install of this ref writes the
# record. tests/test_host_launch.py pins the value.
CANONICAL_REF = "main"
CANONICAL_URL = "https://github.com/SantyagoSeaman/sidegraph.git"

_COMMIT = re.compile(r"[0-9a-f]{40}")


def launch_commit_path() -> Path:
    """Where the record lives: ``$XDG_CACHE_HOME/sidegraph/launch-commit``, falling back to
    ``~/.cache`` when the variable is unset or empty (the shell's ``${…:-…}`` treats empty as
    unset, and an older install's hot commands read the same path).

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


def record_launch_commit(
    *,
    on_error: Callable[[Exception], None] | None = None,
) -> None:
    """Record the commit of an ``@main`` install for an older install's hot-path hooks.

    Writes only for an install that asked for ``@main`` (``requested_revision``): the record
    is shared by every project on the machine, so a pinned install used for CI or a pilot must
    never repoint the plugin's hot hooks. The file holds the 40 hex bytes with no newline, is
    replaced atomically, and is left untouched when it already holds the same value. The existing
    value is read only from a regular file of exactly 40 bytes, so a FIFO or an oversized file at
    the path never blocks or slows SessionStart. Never raises and never prints: a failure leaves
    an older install's hot hooks on the ``@main`` fallback.

    # see design/superpowers/specs/2026-10-03-launch-from-session-commit-design.md (D2)
    """
    try:
        _record_main_commit()
    except Exception as error:
        if on_error is not None:
            with contextlib.suppress(Exception):
                on_error(error)
