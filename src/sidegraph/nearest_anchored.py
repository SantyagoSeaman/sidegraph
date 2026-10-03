"""The nearest anchored records: say where the memory is when a file seed has none of its own.

A file seed that no graph read could place, or that resolved with no open record anchored to
it, is not an answer of "nothing": the files around it often carry the records the agent needs.
:func:`plan` finds, per such seed, the nearest files that do (graph neighbours first, then the
nearest directory) and says so in one sentence. :func:`records_block` renders the records of
those files for a main answer that had no decision memory at all, and :func:`combine` folds that
second retrieval into the first for the telemetry.

Portable core: no engine imports. The reader is duck-typed (:class:`_Reader`).
# see design/superpowers/specs/2026-10-02-tolerant-seeds-design.md (D10-D12)
"""

from __future__ import annotations

import dataclasses
import posixpath
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Any, Protocol

from .retrieval import (
    _STANDING_SUPERSEDE_HINT,
    MEMORY_GUARD_LINE,
    Seed,
    TaskContext,
    proposal_surfaces,
)
from .schema import DecisionStatus
from .seed_ladder import Ladder
from .store import Store

# Per seed: the closest files by graph edge, by directory, and the most of both together.
_PER_KIND = 2
_FILES_PER_SEED = 3
# An ancestor directory holding more than this share of all anchored files is the repository's
# hub, not the area a seed belongs to. A store with a handful of anchored files has no hub: the
# share only applies above a floor of twice the files a seed is given from one directory.
_HUB_SHARE = 0.25
_HUB_FLOOR = 2 * _PER_KIND
# At most this many seeds get a sentence; the rest are counted.
_SENTENCES = 3

NEAREST_HEADING = "## Nearest anchored"
RECORDS_HEADING = "## Nearest anchored records (not anchored to your files)"

# The binding statuses through which retrieval reaches a record
# (``Store.valid_decisions_for_entity``).
_REACHED = ("live", "degraded")

_LINKED = "linked in the code graph"
_SAME_DIRECTORY = "same directory"

_HEADING_RE = re.compile(r"(?m)^## ")


class _Reader(Protocol):
    """What this module asks of the engine reader (``GraphifyReader`` answers all three)."""

    def source_files(self) -> frozenset[str]: ...

    def nodes_in_file(self, file_path: str) -> list[Any]: ...

    def neighbors(self, node_id: str, relations: list[str] | None = None) -> list[Any]: ...


@dataclass(frozen=True)
class Nearby:
    """One anchored file near a seed: how it is near, and how many open records it carries."""

    path: str
    records: int
    how: str  # "linked in the code graph", "same directory" or "under `dir`"


@dataclass(frozen=True)
class Plan:
    """What the call says about the seeds that have no record of their own."""

    sentences: list[str] = field(default_factory=list)  # one per seed, at most three
    more: int = 0  # seeds past the third that would have had a sentence
    files: list[str] = field(default_factory=list)  # the files the sentences name, once each

    def lines(self) -> list[str]:
        """The sentences, then a count of the seeds left out."""
        tail = [f"… and {self.more} more seeds with no anchored record"] if self.more else []
        return [*self.sentences, *tail]


def _is_open(store: Store, record_id: str) -> bool:
    """Whether the record is memory a retrieval may show right now: accepted, or proposed and
    still inside the proposal window, and not past its ``valid_to`` (the rule of
    ``retrieval.partition_by_trust`` and ``Store.valid_decisions_for_entity``)."""
    record = store.get_decision(record_id) or store.get_fact(record_id)
    if record is None:
        return False
    if record.status != DecisionStatus.ACCEPTED and not (
        record.status == DecisionStatus.PROPOSED and proposal_surfaces(record)
    ):
        return False
    return record.valid_to is None or record.valid_to > datetime.now(UTC)


def anchored_files(store: Store, held: frozenset[str]) -> dict[str, int]:
    """``file -> number of open records`` for every file the graph holds that carries at least
    one: a live or degraded binding (the two retrieval reaches a record through) from an open
    record to an entity whose descriptor names that file. Computed once per call (spec D11). A
    record bound twice into one file counts once."""
    files: dict[str, str] = {}
    for entity in store.iter_concrete_entities():
        path = entity.descriptor.file_path if entity.descriptor is not None else None
        if path is not None and path in held:
            files[entity.entity_id] = path
    open_cache: dict[str, bool] = {}
    per_file: dict[str, set[str]] = {}
    for entity_id, path in files.items():
        for binding in store.bindings_for_entity(entity_id):
            if binding.status not in _REACHED:
                continue
            if binding.record_id not in open_cache:
                open_cache[binding.record_id] = _is_open(store, binding.record_id)
            if open_cache[binding.record_id]:
                per_file.setdefault(path, set()).add(binding.record_id)
    return {path: len(ids) for path, ids in per_file.items()}


def _top(paths: set[str], anchored: dict[str, int]) -> list[str]:
    """The ``_PER_KIND`` files with the most records, ties by path."""
    return sorted(paths, key=lambda p: (-anchored[p], p))[:_PER_KIND]


def _neighbours(path: str, anchored: dict[str, int], reader: _Reader) -> list[Nearby]:
    """Anchored files whose nodes sit next to a node of ``path`` in the graph (spec D11, 1)."""
    found: set[str] = set()
    for node in reader.nodes_in_file(path):
        for other in reader.neighbors(node.node_id):
            if other.file_path and other.file_path != path and other.file_path in anchored:
                found.add(other.file_path)
    return [Nearby(p, anchored[p], _LINKED) for p in _top(found, anchored)]


def _by_directory(path: str, anchored: dict[str, int]) -> list[Nearby]:
    """Anchored files under the nearest ancestor directory that has any (spec D11, 2).

    The walk starts at the seed's own directory and never reaches the repository root. An
    ancestor holding more than ``_HUB_SHARE`` of all anchored files, and more than
    ``_HUB_FLOOR`` of them, is skipped."""
    own = posixpath.dirname(path)
    for parent in PurePosixPath(path).parents:
        directory = parent.as_posix()
        if directory in (".", "/"):  # the root is nobody's neighbourhood, and ends the walk
            break
        under = {p for p in anchored if p.startswith(directory + "/")}
        if under and len(under) <= max(_HUB_FLOOR, _HUB_SHARE * len(anchored)):
            return [
                Nearby(
                    p,
                    anchored[p],
                    _SAME_DIRECTORY if posixpath.dirname(p) == own else f"under `{directory}`",
                )
                for p in _top(under, anchored)
            ]
    return []


def nearest(
    path: str, anchored: dict[str, int], reader: _Reader, *, in_graph: bool
) -> list[Nearby]:
    """The anchored files nearest to ``path``: graph neighbours (when the graph holds it), then
    the nearest directory, merged without repeats and cut to ``_FILES_PER_SEED``."""
    found = _neighbours(path, anchored, reader) if in_graph else []
    named = {n.path for n in found}
    found += [n for n in _by_directory(path, anchored) if n.path not in named]
    return found[:_FILES_PER_SEED]


def _is_repo_relative(p: str) -> bool:
    """Whether ``p`` is written the way the graph writes ``source_file``: relative, normalised
    and inside the repository. A path outside it has no neighbourhood here."""
    pure = PurePosixPath(p)
    return not pure.is_absolute() and ".." not in pure.parts and posixpath.normpath(p) == p


def _file_paths(seeds: list[Seed], ladder: Ladder | None, held: frozenset[str]) -> list[str]:
    """The file paths spec D10 looks at, in input order and once each: a file seed read as
    given or as one other file, or one nothing could read that names a path inside the
    repository. Without a ladder every seed resolved as given, so those in the graph count."""
    if ladder is None:
        paths = [s.file_path for s in seeds if not s.name and s.file_path and s.file_path in held]
    else:
        paths = [
            f.path
            for f in ladder.file_seeds
            if f.outcome != "unresolved" or _is_repo_relative(f.path)
        ]
    return list(dict.fromkeys(paths))


def _count(n: int) -> str:
    return f"{n} record" if n == 1 else f"{n} records"


def _sentence(path: str, near: list[Nearby]) -> str:
    named = ", ".join(f"`{n.path}` ({n.how}, {_count(n.records)})" for n in near)
    return f"No current record is anchored to `{path}`. Nearest anchored: {named}."


def _covered(store: Store, held: frozenset[str], shown_ids: Iterable[str]) -> set[str]:
    """The files the shown records already anchor: for each open record among ``shown_ids``,
    the file of every entity it is bound to through a live or degraded binding. Reads a handful
    of records, where :func:`anchored_files` reads the store."""
    out: set[str] = set()
    for record_id in dict.fromkeys(shown_ids):
        if not _is_open(store, record_id):
            continue
        for binding in store.bindings_for_record(record_id):
            entity = store.get_entity(binding.entity_id) if binding.status in _REACHED else None
            path = entity.descriptor.file_path if entity and entity.descriptor else None
            if path is not None and path in held:
                out.add(path)
    return out


def plan(
    seeds: list[Seed],
    ladder: Ladder | None,
    store: Store,
    reader: _Reader,
    *,
    shown_ids: Iterable[str] = (),
) -> Plan:
    """One sentence for each file seed with no open record of its own and an anchored file near
    it, at most ``_SENTENCES``, and the files they name (spec D10, D11, D12).

    ``shown_ids`` are the records the main answer showed. A seed one of them is anchored to
    needs nothing said, so it is dropped before the store is read; the anchored files are built
    only when some file seed is left, and then once. A seed with nothing anchored near it has
    nothing to say and is not counted."""
    held = reader.source_files()
    paths = _file_paths(seeds, ladder, held)
    if paths:
        covered = _covered(store, held, shown_ids)
        paths = [p for p in paths if p not in covered]
    if not paths:
        return Plan()
    anchored = anchored_files(store, held)
    if not anchored:
        return Plan()
    said: list[tuple[str, list[Nearby]]] = []
    for path in paths:
        if path in anchored:
            continue
        near = nearest(path, anchored, reader, in_graph=path in held)
        if near:
            said.append((path, near))
    named = said[:_SENTENCES]
    return Plan(
        sentences=[_sentence(path, near) for path, near in named],
        more=len(said) - len(named),
        files=list(dict.fromkeys(n.path for _path, near in named for n in near)),
    )


def has_memory(ctx: TaskContext) -> bool:
    """Whether a context holds any decision memory: a mistake, decision, fact, related or
    unratified line. Its structural map does not count."""
    return bool(ctx.mistakes or ctx.decisions or ctx.facts or ctx.related or ctx.unratified)


def records_block(ctx: TaskContext, *, guarded: bool) -> tuple[str, bool]:
    """The records of the nearest files, as a block under :data:`RECORDS_HEADING`, and whether
    they carried ids (so the reply must end with the supersede hint). ``("", False)`` when the
    second retrieval found nothing to show.

    The block holds ``ctx.render(include_structure=False)`` without its guard line and its
    supersede hint, which the reply carries once, with its ``##`` headings one level lower.
    ``guarded`` says whether the main answer already carries the guard line; when it does not
    (an empty answer), the block brings it, so no record reaches the reader unlabelled."""
    if not has_memory(ctx):
        return "", False
    text = ctx.render(include_structure=False)
    text = text.removeprefix(MEMORY_GUARD_LINE + "\n\n")
    ids_shown = text.endswith("\n\n" + _STANDING_SUPERSEDE_HINT)
    if ids_shown:
        text = text.removesuffix("\n\n" + _STANDING_SUPERSEDE_HINT)
    text = _HEADING_RE.sub("### ", text)
    lead = "" if guarded else MEMORY_GUARD_LINE + "\n\n"
    return f"{RECORDS_HEADING}\n{lead}{text}", ids_shown


def combine(first: TaskContext, second: TaskContext) -> TaskContext:
    """``first`` with ``second``'s shown ids appended and its render counters added, for one
    telemetry row that accounts for both retrievals of a call (spec D12)."""
    return dataclasses.replace(
        first,
        shown_ids=[*first.shown_ids, *second.shown_ids],
        selected=first.selected + second.selected,
        emitted=first.emitted + second.emitted,
        degraded=first.degraded + second.degraded,
        dropped_for_budget=first.dropped_for_budget + second.dropped_for_budget,
        chars_used=first.chars_used + second.chars_used,
    )
