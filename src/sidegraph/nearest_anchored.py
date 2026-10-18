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
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Any, Protocol

from . import worktree_seeds
from .retrieval import (
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

    # One entry per seed, at most three: the path, its nearby anchored files, and the open
    # records bound to the path that nothing shown so far delivers (only for a path the graph
    # does not hold).
    entries: list[tuple[str, list[Nearby], frozenset[str]]] = field(default_factory=list)
    more: int = 0  # seeds past the third that would have had a sentence
    files: list[str] = field(default_factory=list)  # the files the sentences name, once each
    floor_paths: frozenset[str] = frozenset()

    @property
    def sentences(self) -> list[str]:
        return self.lines_for(())

    def lines_for(self, shown: Iterable[str], *, at_least: bool = False) -> list[str]:
        """The sentences, leaving out of each bound-record count the records in ``shown`` (the
        second retrieval's records are known only after the plan); a sentence left with nothing
        to say is dropped. ``at_least`` says "at least N" for a path some of whose bound records
        were taken off as ``shown``: that happened before the hard cap, which may cut some of
        them from the text, so the count there can only be a floor."""
        gone = set(shown)
        out = [
            _sentence(
                path,
                near,
                bound - gone,
                at_least=path in self.floor_paths or (at_least and bool(bound & gone)),
            )
            for path, near, bound in self.entries
        ]
        return [line for line in out if line]

    def lines(self, shown: Iterable[str] = (), *, at_least: bool = False) -> list[str]:
        """The sentences, then a count of the seeds left out."""
        tail = [f"… and {self.more} more seeds with no anchored record"] if self.more else []
        return [*self.lines_for(shown, at_least=at_least), *tail]


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


def bound_records(
    store: Store, held: frozenset[str], paths: Iterable[str], skip: Iterable[str] = ()
) -> dict[str, frozenset[str]]:
    """``path -> open records`` bound, through a binding of any status, to an entity whose
    descriptor names that path, for the ``paths`` the graph does not hold, leaving out the
    records in ``skip`` (already shown). A file only a worktree holds has no graph node, so a
    code-anchor read does not reach its records; ``retrieve_decisions(files=[...])`` matches them
    by that descriptor path.
    # see design/superpowers/specs/2026-10-11-worktree-only-seeds-design.md (D6)"""
    wanted = {p for p in paths if p not in held}
    if not wanted:
        return {}
    skipped = set(skip)
    open_cache: dict[str, bool] = {}
    per_path: dict[str, set[str]] = {}
    for entity in store.iter_concrete_entities():
        path = entity.descriptor.file_path if entity.descriptor is not None else None
        if path not in wanted:
            continue
        for binding in store.bindings_for_entity(entity.entity_id):
            if binding.record_id in skipped:
                continue
            if binding.record_id not in open_cache:
                open_cache[binding.record_id] = _is_open(store, binding.record_id)
            if open_cache[binding.record_id]:
                per_path.setdefault(path, set()).add(binding.record_id)
    return {path: frozenset(ids) for path, ids in per_path.items()}


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
    return (
        not pure.is_absolute()
        and not worktree_seeds.has_drive(p)
        and ".." not in pure.parts
        and posixpath.normpath(p) == p
    )


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


def _sentence(
    path: str, near: list[Nearby], bound: frozenset[str] = frozenset(), *, at_least: bool = False
) -> str:
    named = ", ".join(f"`{n.path}` ({n.how}, {_count(n.records)})" for n in near)
    if not bound:
        return (
            f"No current record is anchored to `{path}`. Nearest anchored: {named}." if near else ""
        )
    lead = (
        f"`{path}` has {'at least ' if at_least else ''}{len(bound)} more record(s) bound to it "
        "through anchors the graph "
        f'cannot resolve: retrieve_decisions(files=["{path}"]) lists decisions; '
        f'list_facts(files=["{path}"]) lists facts.'
    )
    return f"{lead} Nearest anchored: {named}." if near else lead


def _covered(store: Store, held: frozenset[str], shown_ids: Iterable[str]) -> set[str]:
    """The files the shown records already anchor: for each open record among ``shown_ids``,
    the file of every declared entity. Graph-held files require a live/degraded binding;
    missing files may already have been delivered as unverified file applicability. Reads
    a handful of records, where :func:`anchored_files` reads the store."""
    out: set[str] = set()
    for record_id in dict.fromkeys(shown_ids):
        if not _is_open(store, record_id):
            continue
        for binding in store.bindings_for_record(record_id):
            entity = store.get_entity(binding.entity_id)
            path = entity.descriptor.file_path if entity and entity.descriptor else None
            if path is not None and (path not in held or binding.status in _REACHED):
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
    covered = _covered(store, held, shown_ids) if paths else set()
    if paths:
        paths = [p for p in paths if p not in covered or p not in held]
    if not paths:
        return Plan()
    anchored = anchored_files(store, held)
    bound = bound_records(store, held, paths, shown_ids)
    if not anchored and not bound:
        return Plan()
    said: list[tuple[str, list[Nearby], frozenset[str]]] = []
    for path in paths:
        if path in anchored or (path in covered and path not in bound):
            continue
        near = nearest(path, anchored, reader, in_graph=path in held) if anchored else []
        if near or path in bound:
            said.append((path, near, bound.get(path, frozenset())))
    named = said[:_SENTENCES]
    return Plan(
        entries=named,
        # A main record subtracted before the hard cap might be cut later. A remainder
        # is therefore a floor, as for subtraction from the second retrieval below.
        floor_paths=frozenset(covered - held),
        more=len(said) - len(named),
        files=list(dict.fromkeys(n.path for _path, near, _b in named for n in near)),
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
    body, legend, hint = ctx.render_parts(include_structure=False, heading="###")
    text = "\n\n".join(part for part in (body, legend) if part)
    text = text.removeprefix(MEMORY_GUARD_LINE + "\n\n")
    ids_shown = hint is not None
    lead = "" if guarded else MEMORY_GUARD_LINE + "\n\n"
    return f"{RECORDS_HEADING}\n{lead}{text}", ids_shown


def combine(first: TaskContext, second: TaskContext) -> TaskContext:
    """``first`` with ``second``'s shown ids appended and its render counters added, for one
    telemetry row that accounts for both retrievals of a call (spec D12).

    ``dataclasses.replace`` preserves the first context's original requested-seed evidence;
    the internal nearest-file lookup contributes record populations, not new caller seeds.
    """
    return dataclasses.replace(
        first,
        shown_ids=[*first.shown_ids, *second.shown_ids],
        line_ids={
            key: [*first.line_ids.get(key, []), *second.line_ids.get(key, [])]
            for key in (*first.line_ids, *(k for k in second.line_ids if k not in first.line_ids))
        },
        delivery_atoms={
            key: [*first.delivery_atoms.get(key, []), *second.delivery_atoms.get(key, [])]
            for key in (*first.delivery_atoms, *second.delivery_atoms)
        },
        delivery_placements=[*first.delivery_placements, *second.delivery_placements],
        delivery_considered=first.delivery_considered | second.delivery_considered,
        delivery_placed=first.delivery_placed | second.delivery_placed,
        delivery_degraded=first.delivery_degraded | second.delivery_degraded,
        selected=first.selected + second.selected,
        emitted=first.emitted + second.emitted,
        degraded=first.degraded + second.degraded,
        degraded_ids=[*first.degraded_ids, *second.degraded_ids],
        dropped_for_budget=first.dropped_for_budget + second.dropped_for_budget,
        chars_used=first.chars_used + second.chars_used,
    )
