"""The seed ladder: read a wrong seed the way the agent meant it, or say that it cannot be.

``get_task_context`` used to take every seed verbatim, so ``./pkg/n.py``, ``app/View.swift`` for
``src/ui/View.swift``, or ``Type.member`` with no file all came back as "No context found.".
:func:`tolerate` rewrites such a seed through a short ladder and leaves one :class:`SeedNote` per
rewrite or refusal, so the reply can say how each seed was read. A rewrite is a guess and is never
silent; an ambiguous seed is reported and never expanded.

Portable core: no engine imports. The reader is duck-typed (:class:`_Reader`): it must offer
``source_files()``, ``resolve()``, ``resolve_member_name()`` and ``get_node()``, which
``GraphifyReader`` does. :func:`tolerate` holds no state between calls.
# see design/superpowers/specs/2026-10-02-tolerant-seeds-design.md (D1, D4, D10)
"""

from __future__ import annotations

import bisect
import os
import posixpath
import re
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Literal, Protocol

from .retrieval import Seed
from .schema import Descriptor

# A directory seed reads at most this many files (shallowest first); a directory of more than
# `_DIR_GATE` files is listed instead of read: ordering a large directory by anything else
# would surface its hottest files, not the area the agent asked about.
_DIR_CAP = 8
_DIR_GATE = 3 * _DIR_CAP
# An ambiguous seed lists this many candidates; a too-large directory this many subdirectories.
_CANDIDATES_LISTED = 5
_SUBDIRS_LISTED = 5
# The block names at most this many seeds, then counts the rest.
_NOTE_LINES = 10

# `file.py:12` and `file.py:12:3`, the way editors and stack traces write a location.
_LOCATION_RE = re.compile(r":\d+(?::\d+)?$")

READ_BLOCK_HEADING = "## How your seeds were read"

Outcome = Literal[
    "normalised",
    "case",
    "suffix",
    "basename",
    "directory",
    "directory-too-large",
    "name",
    "ambiguous",
    "unresolved-name",
]

# The outcomes that rewrite a seed to one other file path.
_SINGLE_FILE = ("normalised", "case", "suffix", "basename")

FileOutcome = Literal["exact", "normalised", "case", "suffix", "basename", "unresolved"]


class _Reader(Protocol):
    """What the ladder asks of the engine reader (``GraphifyReader`` answers all four)."""

    def source_files(self) -> frozenset[str]: ...

    def resolve(self, desc: Descriptor) -> Any: ...

    def resolve_member_name(self, name: str) -> Any: ...

    def get_node(self, node_id: str) -> Any: ...


@dataclass(frozen=True)
class SeedNote:
    """What happened to one seed that was not read as given."""

    given: str  # the seed as passed: "path", "name" or "name (file)"
    outcome: Outcome
    read_as: tuple[str, ...]  # resolved path(s), or the candidates / subdirectories listed
    total: int = 0  # directory: files under it; ambiguous: candidates
    name: str | None = None  # an entity seed's name; None for a file seed
    # directory-too-large: ``read_as`` lists files (the directory has no subdirectories), not
    # "subdirectory count" pairs.
    of_files: bool = False
    # directory-too-large: ``read_as`` names every file under the directory, so nothing is
    # left out of the listing.
    complete: bool = False


@dataclass(frozen=True)
class FileSeed:
    """One file seed that names a single file: the file it was read as, or, when nothing could
    read it, its normalised spelling. The nearest-anchored lookup asks about these only: a
    directory and an ambiguous seed name no single file."""

    path: str
    outcome: FileOutcome


@dataclass(frozen=True)
class Ladder:
    # Seeds that resolved as given, and the entity seeds that stay as given: the original of a
    # rewritten one, one that matches nothing, one with an ambiguous name.
    exact: list[Seed]
    guessed: list[Seed]  # what the ladder rewrote a seed to (a directory is its files)
    unresolved: list[str]  # file seeds nothing could read, in their normalised spelling
    notes: list[SeedNote]
    # The file path each given seed adds to telemetry, in input order (spec D7): what was read
    # for a single-file rewrite, the directory's own key for a directory, the spelling as given
    # for the rest.
    seed_paths: list[str] = field(default_factory=list)
    # The file seeds that name a single file, in input order (spec D10): resolved as given,
    # rewritten to one file, or unresolved. Duplicates are kept; a directory, an ambiguous seed
    # and an entity seed are not listed.
    file_seeds: list[FileSeed] = field(default_factory=list)


@dataclass
class _FileRead:
    """The ladder's verdict on one file path."""

    outcome: str  # exact, unresolved, ambiguous, or one of the notes' file outcomes
    paths: tuple[str, ...] = ()  # the files it reads as
    shown: str = ""  # the spelling to report when nothing read it
    listed: tuple[str, ...] = ()  # candidates, or subdirectories with their counts
    total: int = 0
    of_files: bool = False  # `listed` holds files because the directory has no subdirectories
    complete: bool = False  # `listed` names everything under the directory
    on_disk: bool = False  # unresolved because the path is a file on disk the graph lacks


class _Files:
    """The graph's file set with the indices the rungs need, built on first use."""

    def __init__(self, files: frozenset[str], root: Path | Callable[[], Path | None] | None):
        self.held = files
        self._root_arg = root
        self._root_done = not callable(root)
        self._root: Path | None = None if callable(root) else root
        self._sorted: list[str] | None = None
        self._by_base: dict[str, list[str]] | None = None
        self._folded: dict[str, list[str]] | None = None

    @property
    def root(self) -> Path | None:
        if not self._root_done:
            assert callable(self._root_arg)
            self._root = self._root_arg()
            self._root_done = True
        return self._root

    @property
    def sorted(self) -> list[str]:
        if self._sorted is None:
            self._sorted = sorted(self.held)
        return self._sorted

    def under(self, prefix: str) -> list[str]:
        """The files whose path starts with ``prefix``, in path order."""
        files = self.sorted
        out: list[str] = []
        for i in range(bisect.bisect_left(files, prefix), len(files)):
            if not files[i].startswith(prefix):
                break
            out.append(files[i])
        return out

    def folded(self, path: str) -> list[str]:
        """The files whose path equals ``path`` once letter case is ignored, in path order."""
        if self._folded is None:
            folded: dict[str, list[str]] = {}
            for f in self.sorted:
                folded.setdefault(f.casefold(), []).append(f)
            self._folded = folded
        return self._folded.get(path.casefold(), [])

    def ending(self, tail: str) -> list[str]:
        """The files equal to ``tail`` or ending in ``/`` + ``tail``, in path order."""
        if self._by_base is None:
            by_base: dict[str, list[str]] = {}
            for f in self.sorted:
                by_base.setdefault(f.rsplit("/", 1)[-1], []).append(f)
            self._by_base = by_base
        base = tail.rsplit("/", 1)[-1]
        return [f for f in self._by_base.get(base, []) if f == tail or f.endswith("/" + tail)]


def _under_root(path: str, root: Path) -> str | None:
    """``path`` relative to ``root``, or ``None`` when it is not under it. Either spelling of
    each side counts: an absolute path or a root may go through a symlink."""
    bases = dict.fromkeys((root.as_posix(), os.path.realpath(root)))
    for candidate in dict.fromkeys((path, os.path.realpath(path))):
        for base in bases:
            try:
                return PurePosixPath(candidate).relative_to(PurePosixPath(base)).as_posix()
            except ValueError:
                continue
    return None


def is_file_exact(root: Path, rel: str) -> bool:
    """Whether ``rel`` is a regular file under ``root`` spelled with exactly its letter case.
    ``(root / rel).is_file()`` alone is true for ``Scripts/x.sh`` on a case-insensitive
    filesystem that holds ``scripts/x.sh``, so every component is looked up in its directory."""
    if not (root / rel).is_file():
        return False
    cur = root
    for part in PurePosixPath(rel).parts:
        try:
            if part not in os.listdir(cur):
                return False
        except OSError:
            return False
        cur = cur / part
    return True


def _normalise(p: str, files: _Files, *, location: bool = True) -> str | None:
    """``p`` as a repo-relative path: surrounding whitespace and, with ``location``, a trailing
    ``:12`` or ``:12:3`` dropped, an absolute path under the root made relative, ``./``
    stripped, then ``normpath``. ``None`` when the result is the root itself, climbs out of
    it, or is still absolute."""
    s = p.strip()
    if location:
        s = _LOCATION_RE.sub("", s)
    if posixpath.isabs(s):
        root = files.root
        if root is not None:
            s = _under_root(s, root) or s
    while s.startswith("./"):
        s = s[2:]
    q = posixpath.normpath(s)
    if q in (".", "..") or q.startswith("../") or posixpath.isabs(q):
        return None
    return q


def _read_file(p: str, files: _Files, *, directories: bool) -> _FileRead:
    """The ladder over one file path ``p``: exact, normalised, the on-disk guard, a directory,
    then the shortest path tail that matches. ``directories=False`` is an entity seed's file,
    which is never read as a directory."""
    if p in files.held:
        return _FileRead("exact", (p,), shown=p)
    q = _normalise(p, files)
    if q is None:
        return _FileRead("unresolved", shown=p)
    dir_intent = p.strip().endswith("/")  # a trailing slash tries the directory rung only
    # A file may be named like a location (`notes:12`): the spelling with only `./` and the
    # path normalised comes before the one with the location stripped.
    spellings = list(dict.fromkeys([_normalise(p, files, location=False) or q, q]))
    if not dir_intent:
        for spelling in spellings:
            if spelling != p and spelling in files.held:
                return _FileRead("normalised", (spelling,), shown=spelling)
    # The file exists but the graph lacks it: the not-in-graph block explains that as stale or
    # new. Guessing another file with the same name would be a wrong answer with authority.
    # "Exists" is case-exact: a case-insensitive filesystem says yes to the wrong letter case.
    root = files.root
    if root is not None:
        for spelling in spellings:
            if is_file_exact(root, spelling):
                return _FileRead("unresolved", shown=spelling, on_disk=True)
    if not dir_intent:
        same_letters = files.folded(q)
        if len(same_letters) == 1:
            return _FileRead("case", (same_letters[0],), shown=q)
        if len(same_letters) > 1:
            listed = tuple(same_letters[:_CANDIDATES_LISTED])
            return _FileRead("ambiguous", shown=q, listed=listed, total=len(same_letters))
    if dir_intent and not directories:
        return _FileRead("unresolved", shown=q)
    if directories:
        below = files.under(q + "/")
        if below:
            if len(below) <= _DIR_GATE:
                chosen = sorted(below, key=lambda f: (f.count("/"), f))[:_DIR_CAP]
                return _FileRead("directory", tuple(chosen), shown=q, total=len(below))
            rests = [f[len(q) + 1 :] for f in below]
            counts = Counter(f"{q}/{r.split('/', 1)[0]}" for r in rests if "/" in r)
            top = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:_SUBDIRS_LISTED]
            # Files directly in the directory fill what the subdirectories leave of the five.
            direct = [f for f, r in zip(below, rests, strict=True) if "/" not in r]
            files_listed = direct[: _SUBDIRS_LISTED - len(top)]
            listed = (*(f"{sub} {n}" for sub, n in top), *files_listed)
            covered = sum(n for _sub, n in top) + len(files_listed)
            return _FileRead(
                "directory-too-large",
                shown=q,
                listed=listed,
                total=len(below),
                of_files=not top,
                complete=covered >= len(below),
            )
    if dir_intent:
        return _FileRead("unresolved", shown=q)
    parts = q.split("/")
    for k in range(len(parts)):
        matches = files.ending("/".join(parts[k:]))
        if len(matches) == 1:
            outcome = "basename" if k == len(parts) - 1 else "suffix"
            return _FileRead(outcome, (matches[0],), shown=q)
        if len(matches) > 1:
            # A shorter tail only matches a superset, so the answer stops at the first tail
            # that matches anything.
            listed = tuple(matches[:_CANDIDATES_LISTED])
            return _FileRead("ambiguous", shown=q, listed=listed, total=len(matches))
    return _FileRead("unresolved", shown=q)


def _listed_candidates(
    name: str, node_ids: list[str], reader: _Reader
) -> tuple[tuple[str, ...], int]:
    """``"name (file)"`` for up to five of the places an ambiguous entity seed could mean, by
    file, and how many nodes there were. A node with no file is not a choice an agent can make
    (several nodes in one file are one choice), so only files are offered, and counted; with
    none, nothing is listed."""
    places = sorted({_file_of(reader, node_id) for node_id in node_ids} - {""})
    labels = tuple(f"{name} ({path})" for path in places)
    return labels[:_CANDIDATES_LISTED], len(labels) if labels else len(node_ids)


class _Build:
    """Accumulates the ladder's output in input order."""

    def __init__(self) -> None:
        self.exact: list[Seed] = []
        self.guessed: list[Seed] = []
        self.unresolved: list[str] = []
        self.notes: list[SeedNote] = []
        self.paths: list[str] = []
        self.file_seeds: list[FileSeed] = []

    def result(self) -> Ladder:
        def key(s: Seed) -> tuple[str | None, str | None]:
            return (s.name, s.file_path)

        taken = {key(s) for s in self.exact}
        guessed: list[Seed] = []
        for s in self.guessed:
            if key(s) not in taken:
                taken.add(key(s))
                guessed.append(s)
        return Ladder(
            exact=self.exact,
            guessed=guessed,
            unresolved=list(dict.fromkeys(self.unresolved)),
            notes=self.notes,
            seed_paths=self.paths,
            file_seeds=self.file_seeds,
        )


def _file_seed(seed: Seed, files: _Files, out: _Build) -> str:
    """Settle a file seed; returns the path it adds to telemetry (spec D7)."""
    p = seed.file_path or ""
    read = _read_file(p, files, directories=True)
    if read.outcome == "exact":
        out.exact.append(seed)
        out.file_seeds.append(FileSeed(p, "exact"))
    elif read.outcome in _SINGLE_FILE:
        out.guessed.append(Seed(file_path=read.paths[0]))
        out.file_seeds.append(FileSeed(read.paths[0], read.outcome))  # type: ignore[arg-type]
        out.notes.append(SeedNote(p, read.outcome, read.paths))  # type: ignore[arg-type]
        return read.paths[0]
    elif read.outcome == "directory":
        out.guessed.extend(Seed(file_path=f) for f in read.paths)
        out.notes.append(SeedNote(p, "directory", read.paths, read.total))
        return read.shown
    elif read.outcome in ("directory-too-large", "ambiguous"):
        out.notes.append(
            SeedNote(
                p,
                read.outcome,  # type: ignore[arg-type]
                read.listed,
                read.total,
                of_files=read.of_files,
                complete=read.complete,
            )
        )
        if read.outcome == "directory-too-large":
            return read.shown
    else:
        out.unresolved.append(read.shown)
        out.file_seeds.append(FileSeed(read.shown, "unresolved"))
    return p


def _file_of(reader: _Reader, node_id: str | None) -> str:
    node = reader.get_node(node_id) if node_id else None
    return (node.file_path if node is not None else None) or ""


def _rewritten(seed: Seed, target: str, outcome: str, given: str, out: _Build) -> None:
    """Record an entity seed rewritten to ``target``: the rewrite is guessed, and the original
    stays an exact seed, so a store descriptor written under it still matches at the tier it
    always had."""
    out.exact.append(seed)
    out.guessed.append(Seed(name=seed.name, file_path=target))
    out.notes.append(SeedNote(given, outcome, (target,), name=seed.name))  # type: ignore[arg-type]


def _report_ambiguous(
    seed: Seed, given: str, node_ids: list[str], reader: _Reader, out: _Build
) -> int:
    """Report an entity seed whose name matches several places. The seed is not read; if it came
    with a file, the original stays exact, so a record stored under that descriptor is still
    found by the store lookup alone. Returns 0 when reported, or, when no candidate has a file
    (so there is nothing to offer), how many nodes matched, with nothing recorded."""
    name = seed.name or ""
    listed, total = _listed_candidates(name, node_ids, reader)
    if not listed:
        return total
    out.notes.append(SeedNote(given, "ambiguous", listed, total, name=name))
    if (seed.file_path or "").strip():
        out.exact.append(seed)
    return 0


def _entity_seed(seed: Seed, reader: _Reader, files: _Files, out: _Build) -> str | None:
    """Settle an entity seed (spec D1); returns the path it adds to telemetry, if any."""
    name = seed.name or ""
    path = (seed.file_path or "").strip()
    given = f"{name} ({seed.file_path})" if path else name
    found = reader.resolve(Descriptor(name=name, file_path=seed.file_path))
    read: _FileRead | None = None
    if path:
        if found.status != "unresolved":
            out.exact.append(seed)  # resolved as given
            return seed.file_path
        if path in files.held:
            # A real file in the graph that lacks the name: never a guess across files, but
            # never a silent miss either.
            out.exact.append(seed)
            out.notes.append(SeedNote(given, "unresolved-name", (path,), name=name))
            return seed.file_path
        # The file is not in the graph. Read it the way a path is read, and keep the rewrite
        # only when the entity really is in the file that comes out.
        read = _read_file(path, files, directories=False)
        if read.on_disk:
            # A file on disk the graph lacks: the not-in-graph block explains it. The name is
            # not hunted for in other files.
            out.exact.append(seed)
            out.unresolved.append(read.shown)
            return seed.file_path
        if read.outcome in _SINGLE_FILE:
            target = read.paths[0]
            if reader.resolve(Descriptor(name=name, file_path=target)).status != "unresolved":
                _rewritten(seed, target, read.outcome, given, out)
                return target
        # Then by name alone, which is what a seed with no file always did.
        found = reader.resolve(Descriptor(name=name))
    no_file = 0  # nodes that matched the name but have no file to offer
    if found.status == "resolved":
        if not path:
            out.exact.append(seed)
            return None
        target = _file_of(reader, found.node_id)
        if target:
            _rewritten(seed, target, "name", given, out)
            return target
    elif found.status == "ambiguous":
        no_file = _report_ambiguous(seed, given, list(found.candidates), reader, out)
        if not no_file:
            return seed.file_path if path else None
    settled, moved = _by_member(seed, given, reader, out)
    if settled:
        return moved or (seed.file_path if path else None)
    if no_file and read is None:
        # Several nodes answer to the name and none has a file: an ambiguous seed is never
        # expanded, so it is not an exact seed either.
        out.notes.append(SeedNote(given, "unresolved-name", (), no_file, name=name))
        return None
    out.exact.append(seed)
    if read is not None:
        out.unresolved.append(read.shown)
        return seed.file_path
    out.notes.append(SeedNote(given, "unresolved-name", (), no_file, name=name))
    return None


def _by_member(seed: Seed, given: str, reader: _Reader, out: _Build) -> tuple[bool, str | None]:
    """Rungs 3 and 4 for an entity seed: ``Type.member`` found by its owner edge, whatever the
    file. Returns whether the seed was settled (rewritten, or reported ambiguous) and, for a
    rewrite, the file it now reads."""
    found = reader.resolve_member_name(seed.name or "")
    if found.status == "resolved":
        target = _file_of(reader, found.node_id)
        if target:
            _rewritten(seed, target, "name", given, out)
            return True, target
    elif found.status == "ambiguous":
        # A member candidate always has a file (D2), so this always reports.
        if not _report_ambiguous(seed, given, list(found.candidates), reader, out):
            return True, None
    return False, None


def needs_tolerance(seeds: list[Seed], reader: _Reader) -> bool:
    """Whether any seed needs a rung past exact. A caller that learns it is ``False`` skips
    the repository-root lookup and every note: seeds that resolve as given cost nothing."""
    held = reader.source_files()
    for s in seeds:
        if s.name:
            found = reader.resolve(Descriptor(name=s.name, file_path=s.file_path))
            path = (s.file_path or "").strip()
            if path:
                if found.status == "unresolved":
                    return True
            elif found.status != "resolved":
                return True
        elif s.file_path and s.file_path not in held:
            return True
    return False


def tolerate(
    seeds: list[Seed], reader: _Reader, root: Path | Callable[[], Path | None] | None
) -> Ladder:
    """Read ``seeds`` through the ladder (spec D1) and return what each became.

    ``root`` is the repository root (a ``Path``, or a callable that returns one, called at most
    once and only when a rung needs it; ``None`` when there is none). With a root, an absolute
    path under it is made relative and a file that exists on disk is never guessed to be
    another file.
    """
    files = _Files(reader.source_files(), root)
    out = _Build()
    for seed in seeds:
        recorded: str | None = None
        if seed.name:
            recorded = _entity_seed(seed, reader, files, out)
        elif seed.file_path:
            recorded = _file_seed(seed, files, out)
        else:
            out.exact.append(seed)
        if recorded:
            out.paths.append(recorded)
    return out.result()


def _quoted(items: tuple[str, ...]) -> str:
    return ", ".join(f"`{i}`" for i in items)


def _line(note: SeedNote) -> str:
    given = note.given.replace("\n", " ")
    head = f"- `{given}` → "
    if note.outcome in ("normalised", "case", "suffix", "basename", "name"):
        target = f"`{note.name}` in `{note.read_as[0]}`" if note.name else f"`{note.read_as[0]}`"
        why = {
            "normalised": "normalised",
            "case": "guessed: different letter case",
            "suffix": "guessed: the only file with that path ending",
            "basename": "guessed: the only file with that name",
            "name": (
                "guessed: the given file is not in the code graph"
                if note.given != note.name
                else "guessed: the only member with that name on that type"
            ),
        }[note.outcome]
        return f"{head}read as {target} ({why})"
    if note.outcome == "directory":
        if note.total <= len(note.read_as):
            every = "the 1 file" if note.total == 1 else f"all {note.total} files"
            return f"{head}a directory: read as {every} under it"
        return (
            f"{head}a directory: read as {len(note.read_as)} of {note.total} files under it, "
            "shallowest first"
        )
    if note.outcome == "directory-too-large" and note.of_files:
        return (
            f"{head}a directory of {note.total} files and no subdirectories, too many to read: "
            f"pass a file ({', '.join(note.read_as)}, …)"
        )
    if note.outcome == "directory-too-large":
        more = "" if note.complete else ", …"
        return (
            f"{head}a directory of {note.total} files, too many to read: pass a file or a "
            f"smaller directory ({', '.join(note.read_as)}{more})"
        )
    if note.outcome == "ambiguous":
        cut = note.total > len(note.read_as)
        tail = f", … ({len(note.read_as)} of {note.total})" if cut else ""
        advice = "Pass `file_path` to say which." if note.name else "Pass the repo-relative path."
        return f"{head}ambiguous, did you mean {_quoted(note.read_as)}{tail}? {advice}"
    if note.read_as:
        return f"{head}matches no symbol in `{note.read_as[0]}`"
    if note.total:
        return f"{head}matches {note.total} symbols with no source file in the code graph"
    return f"{head}matches no symbol in the code graph"


def read_block(notes: list[SeedNote]) -> str:
    """The "How your seeds were read" block: one line per note, at most ten, then a count of
    the rest. ``""`` for no notes (spec D4)."""
    if not notes:
        return ""
    lines = [READ_BLOCK_HEADING, *(_line(n) for n in notes[:_NOTE_LINES])]
    if len(notes) > _NOTE_LINES:
        lines.append(f"… and {len(notes) - _NOTE_LINES} more seeds")
    return "\n".join(lines)
