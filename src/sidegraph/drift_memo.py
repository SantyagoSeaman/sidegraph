"""The drift scan's memo: remember each ``git diff <C>..<H> -- <P>`` whose inputs did not change.

The hook-path drift refresh runs one ``git diff --name-only`` per distinct baseline commit (a
capture commit, or a graph build commit, which runs a ``git log`` instead), and nearly every
record has its own commit, so a start with nothing changed still pays one
subprocess per record. Commit objects are immutable, so the diff is a pure function of
``(C, H, P, flags)`` and the diff-relevant git configuration. This module keeps the result of
each batch next to the drift cache and reuses it while those inputs hold.

Exactness (the reason the output stays byte-identical):

- same ``H``: a batch with the same key ``(C, sorted(P), exclude_deletions)`` is the same
  question, so its stored answer is reused;
- ``H`` moved from ``H1`` to ``H2``: one ``git diff --name-only -z --no-renames H1 H2`` lists
  every path whose blob or mode differs between them. For a batch none of whose pathspecs
  names such a path, or a directory holding one, every path it asks about is identical at
  ``H1`` and ``H2``, and ``C`` is fixed, so the pathspec-filtered diff, rename pairing
  included, is the same. Any other batch runs again, against ``H2``;
- ``H1`` unreachable, the diff-relevant configuration changed (any ``diff.*`` or
  ``submodule.*`` value: rename limits, submodule ignore rules), the changed paths include a
  ``.gitmodules`` (it carries submodule ignore rules too), the work-tree ``.gitmodules`` itself
  differs from the one the memo was built with (git reads it, edited or not, for submodule ignore
  rules), or the memo unreadable: nothing is reused;
- a graph batch (``git log``, mode ``"log"``: every file touched since the graph's build commit)
  is reused only at the same ``H``, because a path touched and restored is identical at ``H1`` and
  ``H2`` yet still listed, so the shortcut above does not hold for it. When ``H`` moved forward
  (``H1`` is an ancestor) the stored answer is extended exactly instead: ``B..H2`` is
  ``B..H1`` plus ``H1..H2``, so it is the stored paths plus the batch's paths among the files
  one shared ``git log H1..H2`` lists (:meth:`extend`);
- a capture commit or a memo head that is not a full hex object id (a tag, a branch, ``HEAD``,
  an option-like string) names something that can move, so it is never reused and never
  handed to git as a revision.

A pathspec that is not a plain normalized path (glob or magic characters, ``.`` or ``..``
segments) is never reused, and path comparison folds case and Unicode form, so a filesystem or
``core.ignorecase`` that matches more than the literal bytes only makes the memo rerun more.
A failed batch is never stored, and no memo error can fail or change a batch: the batch just
runs uncached. The CLI doctor passes no memo and stays the uncached reference.

Not covered: ``git replace`` refs and ``core.useReplaceRefs`` can change git's answer at the same
inputs, and the memo does not track them. ``sidegraph-doctor`` (uncached) is the reference.
# see design/superpowers/specs/2026-11-15-sessionstart-drift-memo-design.md
# and design/superpowers/specs/2026-11-16-drift-graph-commit-fallback-design.md (D2)
"""

from __future__ import annotations

import hashlib
import json
import os
import posixpath
import re
import subprocess
import unicodedata
from collections.abc import Callable, Iterable

MEMO_VERSION = 1

# Characters a git pathspec may read as a pattern or a magic signature: such a pathspec
# can match files its literal text does not name, so it is rerun every time.
_PATHSPEC_MAGIC = frozenset("*?[\\:!^")

# A full object id: the only revision text that names one immutable commit.
_OBJECT_ID = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")

# Every git configuration value a name-only diff may read: rename detection and its limits,
# submodule ignore rules. One call fingerprints them all.
_CONFIG_PATTERN = r"^(diff|submodule)\."

GitRunner = Callable[[list[str]], "subprocess.CompletedProcess[bytes]"]


def is_object_id(text: object) -> bool:
    """True when ``text`` is a full hex object id (40 or 64 characters)."""
    return isinstance(text, str) and _OBJECT_ID.fullmatch(text) is not None


def gitmodules_digest(repo_root: str | os.PathLike[str]) -> str | None:
    """The sha256 of the work-tree ``.gitmodules``, or ``None`` when it is absent or unreadable.

    Git takes submodule ignore rules from this file as it is on disk, committed or not, so it
    is part of the memo's fingerprint. One file read, no subprocess."""
    try:
        with open(os.path.join(repo_root, ".gitmodules"), "rb") as handle:
            return hashlib.sha256(handle.read()).hexdigest()
    except OSError:
        return None


def _fold(path: str) -> str:
    """A comparison form that over-matches: NFC and case-folded."""
    return unicodedata.normalize("NFC", path).casefold()


def _plain(path: str) -> bool:
    """True when ``path`` is a literal, normalized, relative pathspec."""
    return (
        bool(path)
        and not path.startswith("/")
        and not (_PATHSPEC_MAGIC & set(path))
        and posixpath.normpath(path) == path
        and path != ".."
        and not path.startswith("../")
    )


def _closure(moved: Iterable[str]) -> tuple[set[str], set[str]]:
    """The changed paths, folded, and the same plus each directory above one: a pathspec naming
    a directory is affected by a change anywhere below it."""
    files: set[str] = set()
    names: set[str] = set()
    for path in moved:
        folded = _fold(path)
        files.add(folded)
        names.add(folded)
        parent = posixpath.dirname(folded)
        while parent and parent not in names:
            names.add(parent)
            parent = posixpath.dirname(parent)
    return files, names


def _affected(path: str, files: set[str], names: set[str]) -> bool:
    """True when a change names ``path``, lies below it, or replaced a directory above it."""
    folded = _fold(path)
    if folded in names:
        return True
    parent = posixpath.dirname(folded)
    while parent:
        if parent in files:
            return True
        parent = posixpath.dirname(parent)
    return False


def entry_key(commit: str, paths: list[str], exclude_deletions: bool, mode: str = "diff") -> str:
    """The stable key of one batch: its baseline commit, sorted pathspecs and deletion mode.

    ``mode`` names the git question: ``"diff"`` (two trees, a capture batch) keeps the key it
    always had; ``"log"`` (every file touched since a graph build) is a different question about
    the same commit and paths, so it gets its own key.

    ``ensure_ascii`` escapes every character, a lone surrogate included, so any string a
    record can hold serializes."""
    parts: list[object] = [commit, sorted(paths), exclude_deletions]
    if mode != "diff":
        parts.append(mode)
    blob = json.dumps(parts)
    return hashlib.sha256(blob.encode("ascii")).hexdigest()


def _is_paths(value: object) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


class DriftDiffMemo:
    """One scan's view of the memo: what the last scan stored, and what this scan keeps.

    Built from the raw meta value (anything: ``None``, bad JSON, a wrong shape is just an
    empty memo), handed to the scan, which calls :meth:`begin` once HEAD is resolved, asks
    :meth:`lookup` before each batch and tells :meth:`record` what it ran. ``next`` is the
    pruned memo to publish after the scan, or ``None`` when the memo was not in use.
    """

    def __init__(self, raw: str | None = None) -> None:
        self.next: dict | None = None
        self._prev_head: str | None = None
        self._prev_entries: dict[str, list[str]] = {}
        self._prev_config: object = None
        self._prev_gitmodules: object = None
        self._head: str | None = None
        self._config: list[str] | None = None
        self._gitmodules: str | None = None
        self._moved: tuple[set[str], set[str]] | None = None  # None: nothing may be reused
        self._same_head = False  # the last memo was built at this very HEAD and is reusable
        self._git: GitRunner | None = None
        self._forward: bool | None = None  # H1 is an ancestor of this HEAD (asked once, lazily)
        self._entries: dict[str, list[str]] = {}
        self._active = False
        self._load(raw)

    def _load(self, raw: str | None) -> None:
        if raw is None:
            return
        try:
            data = json.loads(raw)
        except (ValueError, RecursionError, TypeError):
            return
        if not isinstance(data, dict) or data.get("v") != MEMO_VERSION:
            return
        head, entries = data.get("head"), data.get("entries")
        if not is_object_id(head) or not isinstance(entries, dict):
            return
        self._prev_head = head
        self._prev_config = data.get("config")
        self._prev_gitmodules = data.get("gitmodules")
        self._prev_entries = {k: v for k, v in entries.items() if isinstance(k, str)}

    def begin(self, head: str, git: GitRunner, gitmodules: str | None = None) -> None:
        """Resolve what the last memo may be reused for, given the HEAD this scan diffs to.

        ``git`` runs one git command from the repository root against the scan's time budget
        and raises ``ValueError``/``OSError``/``TimeoutExpired`` on failure. Never raises: any
        failure here leaves the memo unused, and the scan runs as it did before the memo.
        ``gitmodules`` is :func:`gitmodules_digest` of the work-tree file; a different value than
        the memo's discards it.
        """
        self._active = False
        self._moved = None
        self._same_head = False
        self._git = None
        self._forward = None
        self._entries = {}
        if not is_object_id(head):
            return
        try:
            result = git(["config", "-z", "--get-regexp", _CONFIG_PATTERN])
            if result.returncode == 0:
                self._config = sorted(os.fsdecode(c) for c in result.stdout.split(b"\0") if c)
            elif result.returncode == 1:
                self._config = []  # none of the keys is set
            else:
                return
            self._head = head
            self._gitmodules = gitmodules
            self._active = True
            if (
                self._prev_head is None
                or self._prev_config != self._config
                or self._prev_gitmodules != gitmodules
            ):
                return
            if self._prev_head == head:
                self._moved = (set(), set())
                self._same_head = True
                return
            moved = git(["diff", "--name-only", "-z", "--no-renames", self._prev_head, head])
            if moved.returncode != 0:  # H1 unreachable after gc or a shallow clone
                return
            names = [os.fsdecode(n) for n in moved.stdout.split(b"\0") if n]
            if any(posixpath.basename(_fold(n)) == ".gitmodules" for n in names):
                return  # submodule ignore rules changed: nothing carries over
            self._moved = _closure(names)
            self._git = git
        except Exception:  # the memo is an optimization: no failure here may cost the scan
            self._moved = None

    def forward_head(self) -> str | None:
        """The HEAD the last memo was built at, when this scan's HEAD descends from it and the
        memo is still valid for the diff configuration; else ``None``. One
        ``merge-base --is-ancestor``, asked once and only when a graph batch wants it."""
        if (
            not self._active
            or self._moved is None
            or self._same_head
            or self._prev_head is None
            or self._git is None
            or self._head is None
        ):
            return None
        if self._forward is None:
            try:
                result = self._git(["merge-base", "--is-ancestor", self._prev_head, self._head])
                self._forward = result.returncode == 0
            except Exception:
                self._forward = False
        return self._prev_head if self._forward else None

    def extend(
        self, key: str | None, paths: list[str], touched: Callable[[], set[str] | None]
    ) -> list[str] | None:
        """The exact ``"log"`` answer at this HEAD from the stored one, or ``None`` to rerun.

        For a graph batch built at ``B`` with a stored answer for ``B..H1``, and ``H2`` a
        descendant of ``H1``: ``B..H2`` is ``B..H1`` plus ``H1..H2``, so the answer is the
        stored one plus the batch's paths among ``touched()``, the files any commit in
        ``H1..H2`` touched (one shared log for all batches). ``B`` stays an ancestor of ``H2``.
        Anything that is not a plain literal path, or that a touched file only reaches through a
        directory or a case fold, is rerun rather than guessed."""
        if key is None or key not in self._prev_entries or self.forward_head() is None:
            return None
        try:
            stored = self._prev_entries[key]
            changed = touched()
            if not _is_paths(stored) or changed is None or not all(_plain(p) for p in paths):
                return None
            files, names = _closure(changed)
            exact = set(paths) & changed
            if any(p not in exact and _affected(p, files, names) for p in paths):
                return None
            return sorted(set(stored) | exact)
        except Exception:
            return None

    def batch_key(
        self, commit: str, paths: list[str], exclude_deletions: bool, mode: str = "diff"
    ) -> str | None:
        """The key to reuse and record this batch under, or ``None`` when it must run uncached:
        the memo is not in use, or ``commit`` is not a full object id (a tag or branch can
        move without HEAD moving)."""
        if not self._active or not is_object_id(commit):
            return None
        try:
            return entry_key(commit, paths, exclude_deletions, mode)
        except Exception:
            return None

    def lookup(
        self, key: str | None, paths: list[str], *, same_head_only: bool = False
    ) -> list[str] | None:
        """The stored changed-paths for this batch, or ``None`` when it must run.

        ``same_head_only`` is for a ``"log"`` batch: the H1..H2 shortcut below is argued for
        two-tree diffs (a path identical at both heads is unchanged between them), and a log
        lists commits that touched a path, which a restore leaves identical at both heads. Such
        an answer is reused only at the head it was computed for."""
        if key is None or not self._active or (same_head_only and not self._same_head):
            return None
        try:
            stored = self._prev_entries.get(key)
            if stored is None or self._moved is None or not _is_paths(stored):
                return None
            files, names = self._moved
            if not all(_plain(p) and not _affected(p, files, names) for p in paths):
                return None
            self._entries[key] = list(stored)
            return list(stored)
        except Exception:
            return None

    def record(self, key: str | None, changed: list[str]) -> None:
        """Keep a batch that ran and succeeded. A failed batch is simply never recorded."""
        if key is not None and self._active:
            self._entries[key] = list(changed)

    def finish(self) -> None:
        """Freeze ``next``: this scan's keys only, so the memo cannot grow without bound."""
        if self._active and self._head is not None:
            self.next = {
                "v": MEMO_VERSION,
                "head": self._head,
                "config": self._config,
                "gitmodules": self._gitmodules,
                "entries": self._entries,
            }
