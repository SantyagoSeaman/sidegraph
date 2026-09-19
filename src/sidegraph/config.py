"""Shared store-path resolution (design §5: Configuration & wiring).

The ONE place ``cli.py``, ``server.py``, and ``host/hooks.py`` resolve a bare
``Store(...)`` path from, so all three agree on env-var precedence and the
``SIDEGRAPH_DB`` legacy-dispatch rule. Deliberately neutral and dependency-free
(stdlib only) so every caller can import it cheaply: ``host/hooks.py`` needs it without
dragging in ``server.py``'s FastMCP app, and ``server.py`` needs it without dragging in
``cli.py``'s argparse machinery.

Precedence (an explicit path wins outright — nothing below it is even consulted)::

    explicit arg > $SIDEGRAPH_DIR > $SIDEGRAPH_DB (deprecated) > existing ``.sidegraph/``
    > default ``.sidegraph`` (first-creation warning)

See ``docs/reference/configuration.md`` for the full precedence and path-resolution rules.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

#: The canonical, directory-based store convention (design §1) — the primary default.
DEFAULT_STORE_DIR = ".sidegraph"

# Emitted at most once per PROCESS (module-level flag, not per-call): a long-lived server
# or a hook that re-resolves the path on every request must not spam stderr repeatedly.
# Tests reset this via ``monkeypatch.setattr(config, "_deprecation_warned", False)``.
_deprecation_warned = False


def _dispatch_sidegraph_db(value: str, anchor) -> str:
    """Apply the ``SIDEGRAPH_DB`` legacy-dispatch rule (pinned from review):

    - an existing legacy ``*.db`` FILE -> pass through unchanged; ``Store`` migrates it
      on open (see ``Store._migrate_legacy``).
    - an existing DIRECTORY (canonical layout or not — ``Store`` already knows how to
      open or populate either shape) -> use it directly, never its parent.
    - a NONEXISTENT path that looks like a file sitting inside a directory (e.g.
      ``.sidegraph/decisions.db``) -> use its PARENT directory instead. This rescues
      every old config snippet in the wild (``SIDEGRAPH_DB=.sidegraph/decisions.db``) by
      pointing ``Store`` at the ``.sidegraph`` directory, rather than creating a fresh
      canonical layout literally named ``decisions.db``.
    - a NONEXISTENT BARE filename with no directory component at all (e.g. the historic
      bare-file default ``SIDEGRAPH_DB=sidegraph.db``) -> fall through to
      ``DEFAULT_STORE_DIR`` instead. "Its parent" for a bare filename is the CURRENT
      DIRECTORY itself (or ``$CLAUDE_PROJECT_DIR`` under a host anchor) — rescuing to
      that would make the entire root the store: canonical layout, ``.gitignore``, and
      the root tmp-sweep (see ``Store._sweep_stale_tmp_files``) all operating on
      whatever else happens to live there. Checked against the RAW (pre-anchor) value:
      anchoring a bare filename still doesn't give it a real "directory it lives in" to
      rescue to, it just moves the ambiguity from cwd onto the anchor root instead.

    ``anchor`` is the same relative-path anchoring callable ``resolve_store_path`` builds
    (identity when no ``root`` was given) — reused both to resolve ``value`` against the
    filesystem and, in the bare-filename case, to anchor the ``DEFAULT_STORE_DIR`` fallback
    consistently.
    """
    anchored = anchor(value)
    path = Path(anchored)
    if path.is_file() or path.is_dir():
        return anchored
    if Path(value).parent == Path("."):
        return anchor(DEFAULT_STORE_DIR)
    return str(path.parent)


def _warn_deprecated_sidegraph_db(value: str) -> None:
    global _deprecation_warned
    if _deprecation_warned:
        return
    _deprecation_warned = True
    print(
        f"sidegraph: $SIDEGRAPH_DB is deprecated (was {value!r}); set $SIDEGRAPH_DIR to "
        "the store directory instead",
        file=sys.stderr,
    )


# The bridge between processes (D2): the PreToolUse hook knows the host's session id, the
# MCP server does not. SessionStart publishes it here — index-only key/value, the same
# mechanism host/hooks.py's `_PRETOOL_NUDGE_KEY_PREFIX` uses. The value carries a write
# timestamp because `meta` never expires on its own (this repo's live store holds 34 stale
# nudge keys), and a key that outlived its session would attribute every later CLI or pytest
# retrieval to it. Defined here, not in host/hooks.py, so server.py (core) can read it
# without importing the host seam — host/hooks.py re-exports it for its own callers/tests.
TELEMETRY_SESSION_KEY = "telemetry:session"

# The workspace session a recorded session sits beneath, when the host has one (Codex: its
# `session_id` is an umbrella that spans days, survives resume and covers every thread under
# it — see host/hooks._session_identity). Written only when it differs from the recorded key,
# so a Claude Code store never carries it: there, the two are the same string. Kept because it
# is the only link between sibling threads, which is what separates "how many tasks" from
# "how many agents under one task" on that host.
TELEMETRY_SESSION_GROUP_KEY = "telemetry:session_group"


def telemetry_enabled() -> bool:
    """True unless ``SIDEGRAPH_TELEMETRY=off``. Opt-out, read at point of use and never
    cached at import — one definition, two consumers (the MCP server and the PreToolUse
    hook), because two copies of an env check drift."""
    return os.environ.get("SIDEGRAPH_TELEMETRY", "on").strip().lower() != "off"


def resolve_store_path(
    explicit: str | None = None,
    *,
    root: str | None = None,
    warn_on_create: bool = True,
) -> str:
    """Resolve the store path a caller should pass to ``Store(...)``.

    ``explicit`` is an already-parsed ``--db`` CLI flag (``None`` if the flag was
    omitted). When given, it wins outright — no env var or default is even consulted,
    matching every existing CLI's convention.

    ``root`` anchors a RELATIVE result to a base directory — plugin hooks may run with an
    arbitrary cwd (see ``host.hooks._env_path``); ``None`` (the default) leaves relative
    results as-is, resolved against cwd like every non-host caller.

    ``warn_on_create`` gates the "creating new store" stderr notice emitted when nothing
    resolved and the default ``.sidegraph`` doesn't exist yet — ``sidegraph-init`` passes
    ``False`` since it already announces the same fact on stdout.
    """

    def _anchor(value: str) -> str:
        if root and not os.path.isabs(value):
            return os.path.join(root, value)
        return value

    if explicit is not None:
        return _anchor(explicit)

    dir_env = os.environ.get("SIDEGRAPH_DIR")
    if dir_env:
        return _anchor(dir_env)

    db_env = os.environ.get("SIDEGRAPH_DB")
    if db_env:
        # SIDEGRAPH_DIR unset (or empty) is what makes SIDEGRAPH_DB "actually used" —
        # the deprecation note fires here, never when SIDEGRAPH_DIR already won above.
        _warn_deprecated_sidegraph_db(db_env)
        return _dispatch_sidegraph_db(db_env, _anchor)

    default_path = _anchor(DEFAULT_STORE_DIR)
    if Path(default_path).exists():
        return default_path

    if warn_on_create:
        print(
            f"warning: creating new store at {default_path}; pass --db or set "
            "SIDEGRAPH_DIR if this is not the store you meant",
            file=sys.stderr,
        )
    return default_path
