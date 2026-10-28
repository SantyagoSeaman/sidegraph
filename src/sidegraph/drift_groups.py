"""Group per-record ``code-drift`` findings by the drifted file (grouped drift triage).

Pure core: reads the scan ``doctor.curate`` already ran and names, per (record, file), the blob
the scan compared against. Nothing is written or fetched: every git call here is one-shot and
runs with ``GIT_NO_LAZY_FETCH=1``, under one total deadline.
# see design/superpowers/specs/2026-10-27-grouped-drift-triage-design.md
"""

from __future__ import annotations

import ast
import os
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import drift_checks

if TYPE_CHECKING:
    from .doctor import CodeDriftScan

# Chosen ceilings, not measurements (spec §4.2): 0.88 s warm for 129 batches on this repository.
GROUP_DEADLINE_SECONDS = 10.0
MAX_BLOB_BYTES = 1024 * 1024
MAX_TOTAL_BYTES = 8 * 1024 * 1024

_MISTAKES = frozenset({"gotcha", "lesson", "constraint"})
_OID = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_REGULAR = ("100644", "100755")
_TYPE_NAMES = {
    "file": "regular file",
    "symlink": "symlink",
    "submodule": "submodule",
    "tree": "directory",
}


def is_mistake(kind: object) -> bool:
    """A ``gotcha``/``lesson``/``constraint``. A raw ``kind`` may be any JSON value, so a bare
    set-membership test would raise ``TypeError`` on a list; a non-string is a non-mistake."""
    return isinstance(kind, str) and kind in _MISTAKES


def _kind_of_mode(mode: str) -> str:
    return {"120000": "symlink", "160000": "submodule", "040000": "tree"}.get(mode, "file")


@dataclass(frozen=True)
class DriftRecordEntry:
    """One record's entry in one file group (spec §4.3)."""

    record_id: str
    kind: str | None
    title: str
    files: list[str]
    basis: str
    reason: str | None
    check_id: str | None
    checked_commit: str | None
    capture_commit: str | None
    diff: dict[str, Any]
    hint: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "record_id": self.record_id,
            "kind": self.kind,
            "title": self.title,
            "files": list(self.files),
            "basis": self.basis,
            "reason": self.reason,
            "check_id": self.check_id,
            "checked_commit": self.checked_commit,
            "capture_commit": self.capture_commit,
            "diff": dict(self.diff),
            "hint": self.hint,
        }


@dataclass(frozen=True)
class DriftGroup:
    """The records whose drifted files include ``file``."""

    file: str
    records: list[DriftRecordEntry] = field(default_factory=list)


@dataclass(frozen=True)
class DriftGroups:
    """Everything grouped triage renders, from one scan. ``partial`` is true when some pair
    could not be compared (deadline, git failure); no record is ever dropped."""

    partial: bool
    notes: list[str]
    groups: list[DriftGroup]

    def to_dict(self) -> dict[str, Any]:
        """The ``drift_groups`` JSON key (spec §5)."""
        return {
            "partial": self.partial,
            "notes": list(self.notes),
            "groups": [
                {"file": g.file, "records": [r.to_dict() for r in g.records]} for g in self.groups
            ],
        }


class _Unavailable(Exception):
    """A grouping step could not run; ``reason`` is what the affected pairs report."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


_DEADLINE = "grouping deadline reached"
_GIT = "git unavailable"
_NO_BASELINE = "baseline object not in this clone"
_NO_CURRENT = "current object not in this clone"
_OLD_GIT = "diff unavailable (partial clone, Git older than 2.45)"


class _Budget:
    """One total deadline for every git call of a ``build`` (drawn like the scan's)."""

    def __init__(self, seconds: float) -> None:
        self._seconds = seconds
        self._started = time.monotonic()

    def remaining(self) -> float:
        left = self._seconds - (time.monotonic() - self._started)
        if left <= 0:
            raise _Unavailable(_DEADLINE)
        return left


def _diff_dict(
    old: tuple[str, str] | None,
    new: tuple[str, str] | None,
    change: str | None,
    command: str | None,
    unavailable: str | None,
) -> dict[str, Any]:
    return {
        "old_blob": old[0] if old else None,
        "new_blob": new[0] if new else None,
        "old_mode": old[1] if old else None,
        "new_mode": new[1] if new else None,
        "change": change,
        "command": command,
        "unavailable": unavailable,
    }


def _classify(old: tuple[str, str], new: tuple[str, str]) -> str:
    """``content``/``mode``/``type``/``none`` from two (oid, mode) pairs."""
    if _kind_of_mode(old[1]) != _kind_of_mode(new[1]):
        return "type"
    if old[0] == new[0]:
        return "none" if old[1] == new[1] else "mode"
    return "submodule" if _kind_of_mode(old[1]) == "submodule" else "content"


def _partial_clone_blocks_probes(repo: Path, budget: _Budget) -> bool:
    """True when object access is not safe: a partial clone (``extensions.partialClone`` or a
    promisor remote) on Git older than 2.45, which has no ``GIT_NO_LAZY_FETCH``. An unreadable
    config counts as a partial clone, an unreadable version as too old (never as "git
    unavailable" for the diffs)."""
    try:
        partial = drift_checks.partial_clone_configured(repo, budget.remaining())
    except ValueError:
        partial = True
    if not partial:
        return False
    try:
        version = drift_checks.git_version(repo, budget.remaining())
    except ValueError:
        return True
    return version is None or version < (2, 45)


def _batch_check(repo: Path, oids: set[str], budget: _Budget) -> dict[str, int | None]:
    """oid -> size for every object present, ``None`` for a missing one (one call)."""
    valid = sorted(oid for oid in oids if _OID.fullmatch(oid))
    if not valid:
        return {}
    data = ("\n".join(valid) + "\n").encode("ascii")
    out = drift_checks.git_bytes_input(
        repo, ["cat-file", "--batch-check"], data, budget.remaining()
    )
    result: dict[str, int | None] = {}
    for line in out.decode("ascii", "replace").splitlines():
        parts = line.split(" ")
        if len(parts) == 2 and parts[1] == "missing":
            result[parts[0]] = None
        elif len(parts) == 3 and parts[2].isdigit():
            result[parts[0]] = int(parts[2])
    return result


def _read_candidate_blobs(
    repo: Path, sized_ids: dict[str, int], remaining: Callable[[], float | None]
) -> dict[str, bytes]:
    """The bytes of the hint-candidate blobs, within the byte caps (spec §4.2 step 5).

    Ids are taken in sorted object-id order, never input order, so a store always yields the
    same hints: a blob over ``MAX_BLOB_BYTES`` is skipped, and the walk stops at the first id
    that would take the running total past ``MAX_TOTAL_BYTES``. One ``cat-file --batch`` call.
    """
    chosen: list[str] = []
    total = 0
    for oid in sorted(sized_ids):
        size = sized_ids[oid]
        if size > MAX_BLOB_BYTES or not _OID.fullmatch(oid):
            continue
        if total + size > MAX_TOTAL_BYTES:
            break
        chosen.append(oid)
        total += size
    if not chosen:
        return {}
    data = ("\n".join(chosen) + "\n").encode("ascii")
    out = drift_checks.git_bytes_input(repo, ["cat-file", "--batch"], data, remaining())
    blobs: dict[str, bytes] = {}
    pos = 0
    while pos < len(out):
        end = out.index(b"\n", pos)
        header = out[pos:end].decode("ascii", "replace").split(" ")
        pos = end + 1
        if len(header) != 3 or header[1] != "blob" or not header[2].isdigit():
            continue  # "<oid> missing": no body follows
        size = int(header[2])
        blobs[header[0]] = out[pos : pos + size]
        pos += size + 1
    return blobs


@dataclass
class _Pair:
    """One (record, file) comparison in flight."""

    commit: str
    entry: Any  # doctor.DriftEntry
    file: str
    old: tuple[str, str] | None = None
    new: tuple[str, str] | None = None
    unavailable: str | None = None
    change: str | None = None


def build(
    scan: CodeDriftScan,
    decisions: list[tuple[Path, dict]],
    bindings: dict[str, list[dict]],
    entities: list[tuple[Path, dict]],
    store_dir: Path,
    *,
    notes: Sequence[str] = (),
) -> DriftGroups | None:
    """Group ``scan``'s per-record entries by drifted file, each entry naming the blob the
    scan compared against. ``None`` when the scan has no per-record entries. Never drops a
    record: a failed or out-of-budget step marks its pairs unavailable and sets ``partial``.
    # see design/superpowers/specs/2026-10-27-grouped-drift-triage-design.md §4.2, §4.3
    """
    pairs = [
        _Pair(batch.commit, entry, path)
        for batch in scan.batches
        if not batch.failed
        for entry in batch.entries
        for path in entry.files
    ]
    if not pairs:
        return None
    budget = _Budget(GROUP_DEADLINE_SECONDS)
    partial, sizes = _resolve(scan, pairs, budget)
    meta = {raw["id"]: raw for _path, raw in decisions if isinstance(raw.get("id"), str)}
    by_file: dict[str, list[DriftRecordEntry]] = {}
    for pair in pairs:
        raw = meta.get(pair.entry.record_id, {})
        kind = raw.get("kind")
        title = raw.get("title")
        by_file.setdefault(pair.file, []).append(
            DriftRecordEntry(
                record_id=pair.entry.record_id,
                kind=kind if isinstance(kind, str) else None,
                title=title if isinstance(title, str) else "",
                files=sorted(pair.entry.files),
                basis=pair.entry.basis,
                reason=pair.entry.check_reason,
                check_id=pair.entry.check_id,
                checked_commit=pair.entry.checked_commit,
                capture_commit=pair.commit,
                diff=_diff_dict(pair.old, pair.new, pair.change, _command(pair), pair.unavailable),
            )
        )
    partial = _annotate(scan, by_file, bindings, entities, sizes, budget) or partial
    groups = []
    for path, records in by_file.items():
        records.sort(key=lambda r: (0 if is_mistake(r.kind) else 1, r.record_id))
        groups.append(DriftGroup(path, records))
    groups.sort(
        key=lambda g: (
            0 if any(is_mistake(r.kind) for r in g.records) else 1,
            -len(g.records),
            g.file,
        )
    )
    return DriftGroups(partial=partial, notes=list(notes), groups=groups)


def _command(pair: _Pair) -> str | None:
    if (
        pair.change == "content"
        and pair.old
        and pair.new
        and _kind_of_mode(pair.old[1]) in ("file", "symlink")
    ):
        return f"git diff {pair.old[0]} {pair.new[0]}"
    return None


def _resolve(
    scan: CodeDriftScan, pairs: list[_Pair], budget: _Budget
) -> tuple[bool, dict[str, int | None]]:
    """Fill every pair's old/new/change/unavailable; return whether anything was partial and
    the blob sizes the existence probe read (the hint's candidates are chosen from them)."""
    repo = scan.repo_root
    partial = False
    sizes: dict[str, int | None] = {}

    def fail(targets: list[_Pair], reason: str) -> None:
        nonlocal partial
        partial = True
        for pair in targets:
            if pair.unavailable is None and pair.change is None:
                pair.unavailable = reason

    if repo is None:
        fail(pairs, _GIT)
        return partial, sizes
    try:
        # The gate comes first: on an old-Git partial clone no tree or object lookup of ours
        # runs at all (a missing tree could be lazily fetched). The scan's own calls precede
        # grouping and are unchanged.
        if _partial_clone_blocks_probes(repo, budget):
            for pair in pairs:
                pair.unavailable = _OLD_GIT
            return True, sizes
        head = scan.compared_head or drift_checks.head_revision(
            repo, budget.remaining(), no_lazy_fetch=True
        )
        head_tree = drift_checks.tree_entries_all(
            repo, head, {p.file for p in pairs}, budget.remaining
        )
    except _Unavailable as stop:
        fail(pairs, stop.reason)
        return partial, sizes
    except ValueError:
        fail(pairs, _GIT)
        return partial, sizes

    capture_pairs: dict[str, list[_Pair]] = {}
    for pair in pairs:
        if pair.entry.basis == "capture":
            capture_pairs.setdefault(pair.commit, []).append(pair)
        else:
            blobs = pair.entry.check_blobs or {}
            pair.old = blobs.get(pair.file)
            if pair.old is None:
                pair.unavailable = "no stored baseline for this file"
    for commit in sorted(capture_pairs):
        group = capture_pairs[commit]
        try:
            tree = drift_checks.tree_entries_all(
                repo, commit, {p.file for p in group}, budget.remaining
            )
        except _Unavailable as stop:
            fail(group, stop.reason)
            continue
        except ValueError:
            fail(group, _NO_BASELINE)
            continue
        for pair in group:
            pair.old = tree.get(pair.file)
            if pair.old is None:
                pair.unavailable = "file absent at baseline"
    for pair in pairs:
        pair.new = head_tree.get(pair.file)
        if pair.unavailable is None and pair.new is None:
            pair.unavailable = "file deleted at HEAD"

    comparable = [p for p in pairs if p.unavailable is None and p.old and p.new]
    for pair in comparable:
        assert pair.old and pair.new
        pair.change = _classify(pair.old, pair.new)
    for pair in comparable:
        if pair.change == "content" and _kind_of_mode(pair.old[1]) == "tree":  # type: ignore[index]
            pair.change, pair.unavailable = None, "path is a directory at both ends"
    probe = [
        p
        for p in comparable
        if p.change == "content" and _kind_of_mode(p.old[1]) in ("file", "symlink")  # type: ignore[index]
    ]
    if probe:
        try:
            sizes = _batch_check(
                repo,
                {oid for p in probe for oid in (p.old[0], p.new[0])},  # type: ignore[index]
                budget,
            )
            for pair in probe:
                assert pair.old and pair.new
                if sizes.get(pair.old[0], 0) is None:
                    pair.change, pair.unavailable = None, _NO_BASELINE
                elif sizes.get(pair.new[0], 0) is None:
                    pair.change, pair.unavailable = None, _NO_CURRENT
        except (_Unavailable, ValueError) as stop:
            reason = stop.reason if isinstance(stop, _Unavailable) else _GIT
            for pair in probe:
                pair.change = None  # before fail(), which only marks pairs without a change
            fail(probe, reason)
    return partial, sizes


HINT_TEXT = "anchored region unchanged; sibling sections of the file may still matter"
_SCOPE_TEXT = "scope changed; the file may also have changed"


def _diff_key(diff: dict[str, Any]) -> tuple[Any, ...]:
    return (diff["old_blob"], diff["new_blob"], diff["command"], diff["unavailable"])


def _change_text(entry: DriftRecordEntry, numbers: dict[tuple[Any, ...], int]) -> str:
    """What happened to the file, in words, or a ``[diff N]`` reference to read it."""
    diff = entry.diff
    change = diff["change"]
    if change == "mode":
        return f"mode changed {diff['old_mode']}\u2192{diff['new_mode']}, content identical"
    if change == "type":
        old = _TYPE_NAMES[_kind_of_mode(diff["old_mode"])]
        new = _TYPE_NAMES[_kind_of_mode(diff["new_mode"])]
        return f"type changed {old}\u2192{new}"
    if change == "none":
        return "blobs identical"
    if change == "submodule":
        return f"submodule commit changed {diff['old_blob'][:8]}\u2192{diff['new_blob'][:8]}"
    number = numbers.get(_diff_key(diff))
    text = f"[diff {number}]" if number is not None else ""
    if change == "content" and diff["old_mode"] != diff["new_mode"]:
        text += f" mode changed {diff['old_mode']}\u2192{diff['new_mode']}"
    return text.strip()


def render_groups_text(groups: DriftGroups) -> list[str]:
    """The default ``sidegraph-doctor`` block for per-record code-drift (spec §5): a header,
    then per file its distinct diffs and one line per record. Never a verdict.
    # see design/superpowers/specs/2026-10-27-grouped-drift-triage-design.md §5
    """
    ids = {r.record_id for g in groups.groups for r in g.records}
    lines = [f"code-drift: {len(ids)} record(s) in {len(groups.groups)} file group(s)"]
    for index, group in enumerate(groups.groups, 1):
        numbers: dict[tuple[Any, ...], int] = {}
        described: list[str] = []
        for record in group.records:
            diff = record.diff
            if diff["command"] is None and diff["unavailable"] is None:
                continue
            key = _diff_key(diff)
            if key not in numbers:
                numbers[key] = len(numbers) + 1
                described.append(f"diff {numbers[key]}: {diff['command'] or diff['unavailable']}")
        lines.append(
            f" file {index}/{len(groups.groups)}  {group.file}  "
            f"({len(group.records)} record(s), {len(numbers)} distinct diff(s))"
        )
        if described:
            lines.append("   " + "   ".join(described))
        for record in group.records:
            parts = [record.kind or "-", record.record_id, " ".join(record.title.split())]
            tail = []
            if record.reason:
                commit = (record.capture_commit or "")[:8]
                tail.append(
                    f"{'scope advisory' if record.basis == 'scope' else 'check'}: {record.reason} "
                    f"(check {record.check_id}, capture {commit})"
                )
                if record.basis == "scope":
                    tail.append(_SCOPE_TEXT)
            change = _change_text(record, numbers)
            if change:
                tail.append(change)
            if record.hint == "region-unchanged":
                tail.append(HINT_TEXT)
            lines.append("   " + "  ".join(parts) + ("    " + " ".join(tail) if tail else ""))
    return lines


# -- the region hint (spec §4.4): annotation only, fail closed -------------------------------

_HINT_SUFFIXES = {".py": "python", ".pyi": "python", ".md": "markdown", ".markdown": "markdown"}
_Anchor = tuple[object, str | None, object]  # (tier, status, name) of a binding on the file


def _anchors_by_pair(
    scan: CodeDriftScan,
    bindings: dict[str, list[dict]],
    entities: list[tuple[Path, dict]],
) -> dict[tuple[str, str], list[_Anchor]]:
    """``(record_id, file)`` -> the record's bindings whose entity lives in that file."""
    entity_file: dict[str, tuple[str, object]] = {}
    for _path, raw in entities:
        eid = raw.get("entity_id")
        descriptor = raw.get("descriptor")
        if isinstance(eid, str) and isinstance(descriptor, dict):
            file_path = descriptor.get("file_path")
            if isinstance(file_path, str):
                entity_file[eid] = (file_path, descriptor.get("name"))
    statuses = scan.binding_statuses or {}
    out: dict[tuple[str, str], list[_Anchor]] = {}
    for record_id, rows in bindings.items():
        for row in rows:
            eid = row.get("entity_id")
            if not isinstance(eid, str) or eid not in entity_file:
                continue
            file_path, name = entity_file[eid]
            out.setdefault((record_id, file_path), []).append(
                (row.get("tier"), statuses.get((record_id, eid)), name)
            )
    return out


def _normalise_name(name: str) -> str:
    """A stored descriptor name as the source spells it: ``f()`` -> ``f``, ``.m()`` -> ``m``."""
    return name.removesuffix("()").lstrip(".")


_DEFS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
_Def = ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef


def _python_index(source: str) -> tuple[list[_Def], list[str]] | None:
    """The def/class nodes and lines of ``source``, or ``None`` when it does not parse:
    the parse a blob needs once, however many records anchor into it."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError, RecursionError):
        return None
    return [n for n in ast.walk(tree) if isinstance(n, _DEFS)], source.split("\n")


def _python_spans(
    source: str, names: list[str], index: tuple[list[_Def], list[str]] | None = None
) -> dict[str, tuple[int, int]] | None:
    """Line spans ``[start, end)`` (0-based) of the def/class each name resolves to uniquely,
    or ``None`` when any name is missing, ambiguous, or the source does not parse. A span runs
    from the first decorator through ``end_lineno``; an odd decorator makes it unresolvable.
    ``index`` is :func:`_python_index` of ``source`` when the caller already has it.
    # see design/superpowers/specs/2026-10-27-grouped-drift-triage-design.md §4.4
    """
    if index is None:
        index = _python_index(source)
    if index is None:
        return None
    nodes, lines = index
    defs = _DEFS
    spans: dict[str, tuple[int, int]] = {}
    for name in names:
        bare = _normalise_name(name)
        if "." in bare:
            owner, _, member = bare.rpartition(".")
            owners = [n for n in nodes if isinstance(n, ast.ClassDef) and n.name == owner]
            candidates = (
                [m for m in owners[0].body if isinstance(m, defs) and m.name == member]
                if len(owners) == 1
                else []
            )
        else:
            candidates = [n for n in nodes if n.name == bare]
        if len(candidates) != 1 or not bare:
            return None
        node = candidates[0]
        first = node.lineno
        for decorator in node.decorator_list:
            if decorator.end_lineno != decorator.lineno or not lines[
                decorator.lineno - 1
            ].lstrip().startswith("@"):
                return None
            first = min(first, decorator.lineno)
        if node.end_lineno is None:
            return None
        spans[name] = (first - 1, node.end_lineno)
    return spans


def _markdown_index(source: str) -> tuple[list[tuple[int, str, int, int]], int] | None:
    """Every heading of ``source`` as (h level, text, first line, token level), and its line
    count, or ``None`` when the parser is missing: the parse a blob needs once. Block rules
    only (the inline pass is off, as in md_sections): heading text is the block's raw content.
    """
    try:
        from markdown_it import MarkdownIt  # imported here: the hot paths never reach it
    except ImportError:
        return None
    tokens = MarkdownIt("commonmark").disable("inline").parse(source)
    headings: list[tuple[int, str, int, int]] = []
    for index, token in enumerate(tokens):
        if token.type == "heading_open" and token.map and index + 1 < len(tokens):
            headings.append(
                (int(token.tag[1:]), tokens[index + 1].content, token.map[0], token.level)
            )
    return headings, len(source.split("\n"))


def _markdown_spans(
    source: str,
    names: list[str],
    index: tuple[list[tuple[int, str, int, int]], int] | None = None,
) -> dict[str, tuple[int, int]] | None:
    """Line spans ``[start, end)`` (0-based) of the section each name titles, from the
    CommonMark parser's own token maps (so fences in list items, HTML blocks and indented code
    are never mistaken for headings). A name must be the exact text of exactly one heading in
    the file, and that heading a top-level block; a section runs to the next top-level heading
    of the same or higher level. ``None`` when the parser is missing or any name fails.
    ``index`` is :func:`_markdown_index` of ``source`` when the caller already has it.
    # see design/superpowers/specs/2026-10-27-grouped-drift-triage-design.md §4.4
    """
    if index is None:
        index = _markdown_index(source)
    if index is None:
        return None
    headings, total = index
    spans: dict[str, tuple[int, int]] = {}
    for name in names:
        matches = [h for h in headings if h[1] == name]
        if len(matches) != 1 or matches[0][3] != 0:
            return None
        level, _text, start, _depth = matches[0]
        end = next((h[2] for h in headings if h[3] == 0 and h[2] > start and h[0] <= level), total)
        spans[name] = (start, end)
    return spans


def _lone_cr(text: str) -> bool:
    return re.search(r"\r(?!\n)", text) is not None


def _hint_eligible(
    entry: DriftRecordEntry, file: str, anchors: list[_Anchor], statuses_known: bool
) -> bool:
    """H0-H3 (no I/O): the cheap conditions that decide whether a pair's blobs are read."""
    if entry.basis not in ("capture", "check") or entry.diff["change"] != "content":
        return False
    if not statuses_known:
        return False
    if not anchors:
        return False
    if any(tier != 2 or status != "live" for tier, status, _name in anchors):
        return False
    base = os.path.basename(file)
    for _tier, _status, name in anchors:
        if not isinstance(name, str) or name in ("", file, base):
            return False
    return os.path.splitext(file)[1] in _HINT_SUFFIXES


def _hint_for(
    entry: DriftRecordEntry,
    file: str,
    anchors: list[_Anchor],
    blobs: dict[str, bytes],
    statuses_known: bool,
    parsed: dict[tuple[str, str], Any] | None = None,
    before_parse: Callable[[], object] | None = None,
) -> bool:
    """True when every anchored symbol/section of ``file`` is textually identical at the
    baseline and at HEAD. Fails closed: any doubt means no hint. A reading aid, never a verdict.
    ``parsed`` caches each blob's parse by (oid, kind) across the records of one build;
    ``before_parse`` runs before each uncached parse (the build's deadline check).
    # see design/superpowers/specs/2026-10-27-grouped-drift-triage-design.md §4.4 (H0-H6)
    """
    if not _hint_eligible(entry, file, anchors, statuses_known):
        return False
    old, new = blobs.get(entry.diff["old_blob"]), blobs.get(entry.diff["new_blob"])
    if old is None or new is None:
        return False
    try:
        old_text, new_text = old.decode("utf-8"), new.decode("utf-8")
    except UnicodeDecodeError:
        return False
    if _lone_cr(old_text) or _lone_cr(new_text):
        return False
    names = [name for _tier, _status, name in anchors if isinstance(name, str)]
    kind = _HINT_SUFFIXES[os.path.splitext(file)[1]]
    indexer, resolver = (
        (_python_index, _python_spans) if kind == "python" else (_markdown_index, _markdown_spans)
    )
    cache = parsed if parsed is not None else {}

    def spans(oid: str, text: str) -> dict[str, tuple[int, int]] | None:
        if (oid, kind) not in cache:
            if before_parse is not None:
                before_parse()
            cache[(oid, kind)] = indexer(text)
        index = cache[(oid, kind)]
        return None if index is None else resolver(text, names, index)

    old_spans = spans(entry.diff["old_blob"], old_text)
    new_spans = spans(entry.diff["new_blob"], new_text)
    if old_spans is None or new_spans is None:
        return False
    old_lines, new_lines = old_text.split("\n"), new_text.split("\n")
    for name in names:
        a, b = old_spans[name], new_spans[name]
        if old_lines[a[0] : a[1]] != new_lines[b[0] : b[1]]:
            return False
    return True


def _annotate(
    scan: CodeDriftScan,
    by_file: dict[str, list[DriftRecordEntry]],
    bindings: dict[str, list[dict]],
    entities: list[tuple[Path, dict]],
    sizes: dict[str, int | None],
    budget: _Budget,
) -> bool:
    """Fill ``hint`` in place and say whether the blob step was abandoned (deadline or Git
    failure), which makes the result partial. Blobs are read only for pairs that pass the
    cheap conditions."""
    statuses_known = scan.binding_statuses is not None and not scan.binding_statuses_uncomputed
    anchors = _anchors_by_pair(scan, bindings, entities)
    wanted: dict[str, int] = {}
    for path, records in by_file.items():
        for record in records:
            if _hint_eligible(
                record, path, anchors.get((record.record_id, path), []), statuses_known
            ):
                for oid in (record.diff["old_blob"], record.diff["new_blob"]):
                    size = sizes.get(oid)
                    if size is not None:
                        wanted[oid] = size
    blobs: dict[str, bytes] = {}
    abandoned = False
    if wanted and scan.repo_root is not None:
        try:
            blobs = _read_candidate_blobs(scan.repo_root, wanted, budget.remaining)
        except (_Unavailable, ValueError):
            blobs = {}
            abandoned = True
    parsed: dict[tuple[str, str], Any] = {}
    try:
        # Parsing counts against the same deadline: checked before every uncached parse, and
        # once more at the end, so the overrun is at most one parse and is reported partial.
        for path, records in by_file.items():
            for index, record in enumerate(records):
                if _hint_for(
                    record,
                    path,
                    anchors.get((record.record_id, path), []),
                    blobs,
                    statuses_known,
                    parsed,
                    budget.remaining,
                ):
                    records[index] = replace(record, hint="region-unchanged")
        if parsed:
            budget.remaining()
    except _Unavailable:
        return True
    return abandoned
