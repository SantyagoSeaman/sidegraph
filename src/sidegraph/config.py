"""Shared store-path resolution (design §5: Configuration & wiring) and the default graph
path (``graph_path_for_store``, ``default_graph_path``) that pairs a store with its own
project's graph.

The ONE place ``cli.py``, ``server.py``, and ``host/hooks.py`` resolve a bare
``Store(...)`` path from, so all three agree on env-var precedence and the
``SIDEGRAPH_DB`` legacy-dispatch rule. Deliberately neutral and dependency-free
(stdlib only) so every caller can import it cheaply: ``host/hooks.py`` needs it without
dragging in ``server.py``'s FastMCP app, and ``server.py`` needs it without dragging in
``cli.py``'s argparse machinery.

Precedence (an explicit path wins outright — nothing below it is even consulted)::

    explicit arg > $SIDEGRAPH_DIR > $SIDEGRAPH_DB (deprecated) > existing ``.sidegraph/``
    > default ``.sidegraph`` (first-creation warning)

The host surfaces (the hooks and the MCP server) add one step to a RELATIVE ``$SIDEGRAPH_DIR``
or the default: when it does not exist where it anchors, they look for it in the ancestors,
inside the repository (``resolve_store_location(search_ancestors=True)``). The CLI never does.

See ``docs/reference/configuration.md`` for the full precedence and path-resolution rules.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Literal, NamedTuple

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
    - a NONEXISTENT path whose leaf ends in ``.db`` (case-insensitive) and sits inside a
      directory (e.g. ``.sidegraph/decisions.db``) -> use its PARENT directory instead. This rescues
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
    - a symlink that resolves to nothing (dangling), whatever its suffix, -> returned as
      given, never its parent: ``is_file()``/``is_dir()`` are both false for it, so without
      this a ``.db``-suffixed dangling link would be rescued to the project root.
      ``Store`` then raises instead of creating anything.
    - any other NONEXISTENT path (``/proj/.sidegraph`` not created yet) -> returned as
      given, never its parent: rescuing it would scaffold a store into the project root.
      See design/superpowers/specs/2026-09-29-cli-graph-and-store-paths-design.md D6.

    ``anchor`` is the same relative-path anchoring callable ``resolve_store_path`` builds
    (identity when no ``root`` was given) — reused both to resolve ``value`` against the
    filesystem and, in the bare-filename case, to anchor the ``DEFAULT_STORE_DIR`` fallback
    consistently.
    """
    anchored = anchor(value)
    path = Path(anchored)
    if path_is_file(path) or path_is_dir(path):
        return anchored
    if path_is_symlink(path):
        return anchored
    if Path(value).parent == Path("."):
        return anchor(DEFAULT_STORE_DIR)
    if Path(value).suffix.lower() != ".db":
        return anchored
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
# timestamp because `meta` does not expire on its own (the PreToolUse nudge prefixes are
# the exception: SessionStart prunes them, but this key is not one of them; this repo's live
# store once held 34 stale nudge keys), and a key that outlived its session would attribute
# every later CLI or pytest retrieval to it. Defined here, not in host/hooks.py, so server.py
# (core) can read it without importing the host seam — host/hooks.py re-exports it for its
# own callers/tests.
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


class StoreLocation(NamedTuple):
    """A resolved store path and the directory a relative value was anchored to.

    ``base`` is that directory AFTER the ancestor lookup (the ancestor the store was found
    in, else the anchor), and ``None`` for an absolute value, an explicit path, or a relative
    value whose anchor cannot be read. The hooks root their touch paths at it.
    see design/superpowers/specs/2026-10-01-subdirectory-launch-design.md (D1, D2)
    """

    path: str
    base: str | None


def _present_by_lstat(path: str) -> bool:
    """``os.lstat`` finds ``path``: a symlink counts, even a dangling one (the rule
    ``_dispatch_sidegraph_db`` follows). Only "no such entry" reads as absent, so a path under
    an unreadable directory counts as present: nothing is looked up around what cannot be seen."""
    try:
        os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        return False
    except OSError:
        return True
    return True


def _is_home_or_above(directory: str) -> bool:
    """``directory`` is the user's home directory or one of its ancestors. ``False`` when no
    home can be determined (``Path.home()`` raises): no special case then."""
    try:
        home = os.path.realpath(Path.home())
    except (RuntimeError, OSError):
        return False
    real = os.path.realpath(directory)
    return home == real or home.startswith(real.rstrip(os.sep) + os.sep)


def _repository_boundary(base: str) -> str | None:
    """The nearest directory at or above ``base`` that holds a ``.git`` entry: a directory,
    or a file as in a linked worktree or a submodule. ``None`` outside a repository.

    A ``.git`` entry at the user's home directory, or above it, does not count: a dotfiles
    repository there would otherwise make every project beneath ``~`` without a store of its
    own share ``~/.sidegraph``. With no other boundary found, nothing is looked up.

    ``lstat`` calls on the way up, no subprocess: the hooks run on every tool call
    (spec R3)."""
    current = base
    while True:
        if _present_by_lstat(os.path.join(current, ".git")) and not _is_home_or_above(current):
            return current
        parent = os.path.dirname(current)
        if parent == current:
            return None
        current = parent


def ancestor_store(base: str, value: str) -> StoreLocation | None:
    """The first store ``value`` names in an ancestor of ``base``, inside the repository.

    Visits the parents of ``base``, nearest first, up to and including the repository
    boundary (:func:`_repository_boundary`), and returns the first ``<ancestor>/<value>`` that
    exists by ``os.lstat`` (a directory, a file for a legacy store, or a symlink, even a
    dangling one: the rule at the anchor) with ``base`` set to that ancestor.
    ``None`` when there is no repository, ``base`` is itself the boundary, nothing is found,
    or ``value`` is absolute or has a ``..`` part (a store outside the tree it is named from
    is not a lookup target). Backs :func:`resolve_store_location` and the SessionStart line
    that names a stray store.
    see design/superpowers/specs/2026-10-01-subdirectory-launch-design.md (D1, D4)
    """
    if os.path.isabs(value) or ".." in Path(value).parts:
        return None
    current = os.path.abspath(base)
    boundary = _repository_boundary(current)
    if boundary is None:
        return None
    while current != boundary:
        current = os.path.dirname(current)
        candidate = os.path.join(current, value)
        try:
            os.lstat(candidate)
        except OSError:
            continue
        return StoreLocation(candidate, current)
    return None


def resolve_store_location(
    explicit: str | None = None,
    *,
    root: str | None = None,
    warn_on_create: bool = True,
    search_ancestors: bool = False,
) -> StoreLocation:
    """Resolve the store a caller should pass to ``Store(...)``, and what a relative value
    was anchored to (see :class:`StoreLocation`). :func:`resolve_store_path` is its ``.path``.

    ``explicit`` is an already-parsed ``--db`` CLI flag (``None`` if the flag was
    omitted). When given, it wins outright — no env var or default is even consulted,
    matching every existing CLI's convention.

    ``root`` anchors a RELATIVE result to a base directory — plugin hooks may run with an
    arbitrary cwd (``$CLAUDE_PROJECT_DIR``); ``None`` (the default) leaves relative
    results as-is, resolved against cwd like every non-host caller.

    ``warn_on_create`` gates the "creating new store" stderr notice emitted when nothing
    resolved and the default ``.sidegraph`` doesn't exist yet — ``sidegraph-init`` passes
    ``False`` since it already announces the same fact on stdout.

    ``search_ancestors`` is for the host surfaces (the hooks and the MCP server) only. A
    session started in ``R/sub`` anchors a relative store to ``R/sub``; where that path is
    absent, the lookup returns the first ``<ancestor>/<value>`` inside the repository
    (:func:`ancestor_store`) rather than the path a second, empty store would be created at.
    It applies to a relative ``$SIDEGRAPH_DIR`` or the default ``.sidegraph``, never to
    ``--db``, ``$SIDEGRAPH_DB`` or its ``.sidegraph`` fallback, and not when the anchored path
    exists by ``os.lstat`` (a symlink, even a dangling one, counts). The CLI leaves it off.
    see design/superpowers/specs/2026-10-01-subdirectory-launch-design.md (D1)
    """

    def _anchor(value: str) -> str:
        if root and not os.path.isabs(value):
            return os.path.join(root, value)
        return value

    def _base(value: str) -> str | None:
        """What a relative ``value`` anchors to: ``root``, else the cwd (None if unreadable)."""
        if os.path.isabs(value):
            return None
        if root:
            return root
        try:
            return os.getcwd()
        except OSError:
            return None

    def _located(value: str) -> StoreLocation:
        anchored = _anchor(value)
        base = _base(value)
        if search_ancestors and base is not None and not _present_by_lstat(anchored):
            found = ancestor_store(base, value)
            if found is not None:
                return found
        return StoreLocation(anchored, base)

    if explicit is not None:
        return StoreLocation(_anchor(explicit), None)

    dir_env = os.environ.get("SIDEGRAPH_DIR")
    if dir_env:
        return _located(dir_env)

    db_env = os.environ.get("SIDEGRAPH_DB")
    if db_env:
        # SIDEGRAPH_DIR unset (or empty) is what makes SIDEGRAPH_DB "actually used" —
        # the deprecation note fires here, never when SIDEGRAPH_DIR already won above.
        _warn_deprecated_sidegraph_db(db_env)
        return StoreLocation(_dispatch_sidegraph_db(db_env, _anchor), _base(db_env))

    default = _located(DEFAULT_STORE_DIR)
    if path_exists(Path(default.path)):
        return default

    if warn_on_create:
        print(
            f"warning: creating new store at {default.path}; pass --db or set "
            "SIDEGRAPH_DIR if this is not the store you meant",
            file=sys.stderr,
        )
    return default


def resolve_store_path(
    explicit: str | None = None,
    *,
    root: str | None = None,
    warn_on_create: bool = True,
) -> str:
    """Resolve the store path a caller should pass to ``Store(...)``: the ``.path`` of
    :func:`resolve_store_location` without the ancestor lookup, so every caller that does not
    opt in (the CLI, ``sidegraph-init``, ``sidegraph-bootstrap``) resolves as it always has.
    See that function for the arguments."""
    return resolve_store_location(explicit, root=root, warn_on_create=warn_on_create).path


#: Graphify's default output, relative to the project that owns the store.
DEFAULT_GRAPH = "graphify-out/graph.json"


def _probe(path: Path, name: str) -> bool:
    """``path.is_file()`` / ``exists()`` / ``is_dir()`` with one answer on every Python: an
    ``OSError`` (``EACCES`` on an unreadable directory) means "not there". Python 3.13 re-raises
    it from these predicates and 3.14 swallows it."""
    try:
        return bool(getattr(path, name)())
    except OSError:
        return False


def path_is_file(path: Path) -> bool:
    return _probe(path, "is_file")


def path_exists(path: Path) -> bool:
    return _probe(path, "exists")


def path_is_dir(path: Path) -> bool:
    return _probe(path, "is_dir")


def path_is_symlink(path: Path) -> bool:
    return _probe(path, "is_symlink")


def path_state(path: Path) -> Literal["present", "missing", "unknown"]:
    """Whether ``path`` exists, without mistaking "cannot look" for "not there": ``os.stat`` raises
    ``PermissionError`` for a path under an unreadable directory on every Python, where
    ``Path.is_file`` swallows it on 3.14. ``"unknown"`` is any ``OSError`` other than
    not-found / not-a-directory; callers that would advise on a missing file stay silent on it."""
    try:
        os.stat(path)
    except (FileNotFoundError, NotADirectoryError):
        return "missing"
    except OSError:
        return "unknown"
    return "present"


def graph_path_for_store(graph_path: str | Path, store_path: str | Path) -> Path:
    """``graph_path`` as an absolute path: a RELATIVE one is resolved against the STORE's own
    project root, not the process CWD. Backs :func:`_resolve_cli_graph` (used by every
    command that takes a store and a ``--graph``) and :func:`_ratify_reader`: a store in
    another directory would otherwise pick up whatever ``graphify-out/graph.json`` happened
    to sit beside the shell, pairing one project's records with a different project's graph.

    The project root is the store path's PARENT. Only a legacy single-FILE store living
    inside a store directory needs one more level up: an existing file, a path ending in
    ``.db`` that is not created yet, or an existing directory ending in ``.db`` (what such a
    file becomes when the first open migrates it, so the hop must survive that). An absent
    path without that suffix (a new store directory such as ``.sidegraph/nested``) is a
    directory-to-be, not a legacy file.

    A name check on ".sidegraph" was tried and rejected (review R2-4): ``--db mystore`` is a
    supported invocation, and a name check resolved such a store's root to itself, silently
    disabling this for anyone not on the default directory name — the exact "dead while
    looking alive" failure this helper exists to avoid.

    See design/superpowers/specs/2026-09-30-graph-path-one-rule-design.md."""
    path = Path(graph_path)
    if path.is_absolute():
        return path
    return _store_project_root(store_path) / path


def _store_project_root(store_path: str | Path) -> Path:
    """The project a store belongs to, the directory a relative graph value is resolved
    against: the store path's PARENT, one level higher for a legacy single-file store (see
    :func:`graph_path_for_store`). Lexical, never ``resolve()``d: a symlinked ``.sidegraph``
    belongs to the project that holds the link, not to the directory the link points into."""
    store = Path(os.path.abspath(store_path))
    root = store.parent
    legacy_file = (
        path_is_file(store)
        or (not path_exists(store) and store.suffix.lower() == ".db")
        # a migrated legacy store is a DIRECTORY that keeps its `.db` name
        or (path_is_dir(store) and store.suffix.lower() == ".db")
    )
    if legacy_file and (root.name == ".sidegraph" or path_is_file(root / "format")):
        root = root.parent
    return root


def default_graph_path(store_path: str | Path) -> Path:
    """The graph a process reads when no path was typed: ``$SIDEGRAPH_GRAPH`` (an empty value
    counts as unset) or :data:`DEFAULT_GRAPH`, a relative value resolved against the store's
    project."""
    return graph_path_for_store(os.environ.get("SIDEGRAPH_GRAPH") or DEFAULT_GRAPH, store_path)


def repository_root(store_path: str | Path) -> Path | None:
    """The root of the repository (or linked worktree) the store's project sits in: the nearest
    directory at or above the project that holds a ``.git`` entry (:func:`_repository_boundary`).
    ``None`` outside a repository. Lexical, like the store path itself."""
    boundary = _repository_boundary(str(_store_project_root(store_path)))
    return None if boundary is None else Path(boundary)


def _read_first_line(path: str, limit: int = 4096) -> str | None:
    """The first line of a small text file, or ``None`` when it cannot be read."""
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return handle.readline(limit).strip()
    except OSError:
        return None


def _is_bare_repository(common: str) -> bool:
    """``<common>/config`` sets ``core.bare`` to a true value, or cannot be read. A directory
    named ``.git`` is not enough to call its parent a checkout: ``git clone --bare R x/.git``
    makes one that is bare. Unreadable counts as bare: what cannot be verified is not borrowed
    from. ``include`` files are not followed."""
    try:
        with open(os.path.join(common, "config"), encoding="utf-8", errors="replace") as handle:
            text = handle.read(1 << 20)
    except OSError:
        return True
    section = ""
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("["):
            section = line.split("]", 1)[0].lstrip("[").strip().lower()
            continue
        key, _, value = line.partition("=")
        if section == "core" and key.strip().lower() == "bare":
            return value.split("#", 1)[0].split(";", 1)[0].strip().lower() in {
                "true",
                "yes",
                "on",
                "1",
            }
    return False


def _linked_checkouts(store_path: str | Path) -> tuple[Path, Path] | None:
    """``(worktree_root, main_checkout)`` when the store's repository is a linked worktree of a
    non-bare main checkout, else ``None``. The filesystem only (a few ``stat`` calls and two
    small file reads, no git subprocess), because the hooks run on every tool call.

    The worktree's ``.git`` is a FILE whose ``gitdir:`` line names ``<common>/worktrees/<name>``;
    that directory's ``commondir`` file names ``<common>``. A relative ``gitdir:`` value is
    resolved against the REAL path of the worktree root, because git writes relative paths from
    real paths and the store may be reached through a symlink. The common directory must be named
    ``.git`` and must not be bare (``core.bare`` in its ``config``): its parent is then the main
    checkout. A bare repository, the ``.bare`` layout and a separate git directory with another
    name fail the first; ``git clone --bare R x/.git`` fails the second; a submodule's gitdir has
    no ``commondir``; a ``.git`` directory is a main checkout or a plain clone. Each gives
    ``None``.
    see design/superpowers/specs/2026-10-01-worktree-borrowed-graph-design.md (D1)
    """
    root = repository_root(store_path)
    if root is None:
        return None
    dot_git = os.path.join(root, ".git")
    if not path_is_file(Path(dot_git)):
        return None
    line = _read_first_line(dot_git)
    if line is None or not line.startswith("gitdir:"):
        return None
    value = line[len("gitdir:") :].strip()
    if not value:
        return None
    if not os.path.isabs(value):
        value = os.path.join(os.path.realpath(root), value)
    gitdir = os.path.realpath(value)
    commondir = _read_first_line(os.path.join(gitdir, "commondir"))
    if not commondir:
        return None
    common = os.path.realpath(os.path.join(gitdir, commondir))
    if os.path.basename(common) != ".git" or not path_is_dir(Path(common)):
        return None
    if _is_bare_repository(common):
        return None
    return root, Path(os.path.dirname(common))


def main_checkout_root(store_path: str | Path) -> Path | None:
    """The main checkout of the linked worktree the store's project sits in, or ``None`` when
    that repository is not a linked worktree of a non-bare main checkout (see
    :func:`_linked_checkouts` for the cases). Backs :func:`borrowed_graph_path`.
    see design/superpowers/specs/2026-10-01-worktree-borrowed-graph-design.md (D1)"""
    linked = _linked_checkouts(store_path)
    return None if linked is None else linked[1]


def borrowed_graph_candidate(store_path: str | Path) -> Path | None:
    """Where a linked worktree's missing graph would sit in its main checkout, whether or not
    a graph is there; ``None`` unless all of these hold:

    - the store's own graph (:func:`default_graph_path`) is missing, not merely unreadable;
    - the graph value is relative (an absolute ``$SIDEGRAPH_GRAPH`` names one graph for every
      checkout);
    - the store's repository is a linked worktree of a main checkout (:func:`main_checkout_root`).

    The path is the main checkout joined with the store project's path below the worktree
    root and the graph value: the project-relative rule of :func:`default_graph_path`, applied
    in the main checkout, so a nested or per-package store maps to its own package's graph.
    see design/superpowers/specs/2026-10-01-worktree-borrowed-graph-design.md (D1, D4)"""
    value = os.environ.get("SIDEGRAPH_GRAPH") or DEFAULT_GRAPH
    if os.path.isabs(value) or path_state(default_graph_path(store_path)) != "missing":
        return None
    linked = _linked_checkouts(store_path)
    if linked is None:
        return None
    worktree, main = linked
    try:
        below = _store_project_root(store_path).relative_to(worktree)
    except ValueError:
        return None
    return main / below / value


def borrowed_graph_path(store_path: str | Path) -> Path | None:
    """The graph a linked worktree reads when it has none of its own: its main checkout's
    (:func:`borrowed_graph_candidate`) when that file exists, else ``None``. Read tools and the
    SessionStart hook use it; no CLI default and no write path does (spec R1).
    see design/superpowers/specs/2026-10-01-worktree-borrowed-graph-design.md (D1, D2)"""
    candidate = borrowed_graph_candidate(store_path)
    return candidate if candidate is not None and path_is_file(candidate) else None
