"""Plan ADR diffs without side effects, then apply only explicit conflict-free writes."""

from __future__ import annotations

import difflib
import hashlib
import os
import re
import stat
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .adr import DOC_DERIVED_SOURCES, AdrSnapshot, local_source, render_index, render_record
from .adr_identity import END_MARKER, generated_projection, reserved_lines
from .profiles import AdrExport

_HEADER = re.compile(
    rb"<!-- sidegraph-adr:v1 id=([A-Za-z0-9][A-Za-z0-9._-]{0,127}|@index) "
    rb"number=(0|[1-9][0-9]{0,7}) sha256=([0-9a-f]{64}) -->"
)
_END = re.compile(rb"^<!-- /sidegraph-adr -->(?:\r?\n|$)", re.MULTILINE)
_BOM = b"\xef\xbb\xbf"
_INDEX = "@index"
_MAX_NUMBER = 99_999_999


@dataclass(frozen=True)
class AdrChange:
    path: str
    record_id: str
    action: str
    diff: str = ""
    before: bytes | None = field(default=None, repr=False)
    after: bytes | None = field(default=None, repr=False)
    message: str | None = None


@dataclass(frozen=True)
class AdrPlan:
    out: Path
    changes: list[AdrChange]
    warnings: list[str]


@dataclass(frozen=True)
class _Region:
    record_id: str
    number: int
    body: bytes
    tail: bytes
    bom: bytes


def _lf(data: bytes) -> bytes:
    return data.replace(b"\r\n", b"\n")


def _checksum(record_id: str, number: int, body: bytes) -> str:
    identity = f"v1\n{record_id}\n{number}\n".encode()
    return hashlib.sha256(identity + _lf(body)).hexdigest()


def _parse(data: bytes) -> _Region | None:
    bom = _BOM if data.startswith(_BOM) else b""
    value = data.removeprefix(_BOM)
    header, separator, rest = value.partition(b"\n")
    match = _HEADER.fullmatch(header.removesuffix(b"\r"))
    if match is None:
        # Never adopt an unclaimed human file. A damaged/relocated claim is a conflict.
        if generated_projection(value.decode("utf-8", errors="replace")):
            raise ValueError("malformed or relocated ownership header")
        return None
    if not separator:
        raise ValueError("missing managed region")
    rid, number, digest = match.group(1).decode(), int(match.group(2)), match.group(3).decode()
    if (rid == _INDEX) != (number == 0):
        raise ValueError("invalid projection sequence number")
    closing = _END.search(rest)
    if closing is None:
        raise ValueError("missing managed closing marker")
    body = _lf(rest[: closing.start()])
    tail = rest[closing.end() :]
    if reserved_lines(body.decode("utf-8")) or generated_projection(
        tail.decode("utf-8", errors="replace")
    ):
        raise ValueError("multiple or nested projection claims")
    if _checksum(rid, number, body) != digest:
        raise ValueError("managed body or ownership header was edited")
    return _Region(rid, number, body, tail, bom)


def _wrap(rid: str, number: int, text: str, old: _Region | None = None) -> bytes:
    body = _lf(text.encode("utf-8"))
    if not body.endswith(b"\n"):
        body += b"\n"
    digest = _checksum(rid, number, body)
    header = f"<!-- sidegraph-adr:v1 id={rid} number={number} sha256={digest} -->\n".encode()
    tail = old.tail if old else b"\n## Local notes\n\n"
    # Preserve authored bytes, appending only the repository-required final newline.
    if tail and not tail.endswith(b"\n"):
        tail += b"\n"
    return (old.bom if old else b"") + header + body + END_MARKER.encode() + b"\n" + tail


def _directory(path: Path) -> Path:
    """Reject symlinks before resolving lexical ancestors; never traverse a target file."""
    absolute = path.absolute()
    for component in reversed((absolute, *absolute.parents)):
        if component.is_symlink():
            raise ValueError(f"ADR output path is symlinked: {component}")
        if component.exists() and not component.is_dir():
            raise ValueError(f"ADR output parent is not a directory: {component}")
    return absolute.resolve()


def _read_target(path: Path) -> bytes | None:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(mode):
        raise ValueError(f"ADR target is not a regular file: {path.name}")
    return path.read_bytes()


def _diff(path: str, before: bytes | None, after: bytes) -> str:
    return "".join(
        difflib.unified_diff(
            (before or b"").decode("utf-8", errors="replace").splitlines(keepends=True),
            after.decode("utf-8", errors="replace").splitlines(keepends=True),
            fromfile=path if before is not None else "/dev/null",
            tofile=path,
        )
    )


def plan_export(snapshot: AdrSnapshot, out: Path, config: AdrExport) -> AdrPlan:
    """Read a flat ADR set, retaining ownership/numbering and never deleting a file."""
    out = _directory(out)
    if out.is_relative_to(snapshot.root.resolve()):
        raise ValueError("ADR output must be outside the canonical store")
    changes: list[AdrChange] = []
    warnings: list[str] = []
    existing: dict[str, bytes] = {}
    folded: dict[str, str] = {}
    regions: dict[str, _Region] = {}
    owned: dict[str, str] = {}
    numbers: dict[int, str] = {}
    human_numbers: set[int] = set()
    for path in sorted(out.iterdir()) if out.exists() else []:
        if path.suffix.casefold() != ".md":
            continue
        name = path.name
        folded_name = name.casefold()
        if folded_name in folded:
            changes.append(
                AdrChange(name, name, "conflict", message="case-insensitive filename collision")
            )
        folded[folded_name] = name
        try:
            raw = _read_target(path)
            assert raw is not None
            existing[name] = raw
            region = _parse(raw)
            if region is None:
                prefix = re.match(r"^([0-9]{1,8})[-_]", name)
                if prefix:
                    human_numbers.add(int(prefix.group(1)))
                continue
            regions[name] = region
            if region.record_id in owned or region.number in numbers:
                raise ValueError("duplicate generated identity or sequence number")
            owned[region.record_id] = name
            numbers[region.number] = name
        except (ValueError, UnicodeError) as exc:
            changes.append(AdrChange(name, name, "conflict", message=str(exc)))
    for number in human_numbers & numbers.keys():
        if number:
            warnings.append(
                f"{numbers[number]}: generated number {number} overlaps a human ADR number; "
                "existing filenames and numbers retained"
            )
    highest = max({0, *numbers, *human_numbers})
    paths: dict[str, str] = {}
    sequence: dict[str, int] = {}
    human: set[str] = set()
    for rid, decision in snapshot.exported.items():
        source = local_source(snapshot, decision.provenance.ref)
        if (
            decision.provenance.source in DOC_DERIVED_SOURCES
            and source is not None
            and source.parent.resolve() == out
            and source.name in existing
            and source.name not in regions
        ):
            paths[rid] = source.name
            human.add(rid)
        elif rid in owned:
            paths[rid] = owned[rid]
            sequence[rid] = regions[owned[rid]].number
        else:
            if highest >= _MAX_NUMBER:
                raise ValueError(
                    "ADR sequence exhausted (maximum 99999999); review output numbering"
                )
            highest += 1
            paths[rid] = config.basename(rid, highest)
            sequence[rid] = highest
    desired: list[tuple[str, str, int, str]] = []
    for rid in snapshot.exported:
        if rid in human:
            warnings.append(f"{paths[rid]}: human source for {rid}; review status/content manually")
            changes.append(AdrChange(paths[rid], rid, "human-source"))
        else:
            desired.append(
                (paths[rid], rid, sequence[rid], render_record(snapshot, rid, paths, config, out))
            )
    desired.append((owned.get(_INDEX, "index.md"), _INDEX, 0, render_index(snapshot, paths)))
    planned_names: set[str] = set()
    for name, rid, number, text in desired:
        key = name.casefold()
        if key in planned_names:
            changes.append(AdrChange(name, rid, "conflict", message="duplicate planned filename"))
            continue
        planned_names.add(key)
        other_name = folded.get(key)
        old = regions.get(other_name) if other_name else None
        before = existing.get(other_name) if other_name else None
        if other_name and (old is None or old.record_id != rid):
            changes.append(
                AdrChange(
                    name,
                    rid,
                    "conflict",
                    before=before,
                    message="destination belongs to a human or another record",
                )
            )
            continue
        if old:
            assert other_name is not None
            name = other_name
        after = _wrap(rid, number, text, old)
        # A checkout line-ending conversion is not a source-content change.
        planned_region = _parse(after)
        assert planned_region is not None
        if old and old.body == planned_region.body:
            assert before is not None
            action, after = "unchanged", before
        else:
            action = "add" if before is None else "update"
        assert after is not None
        changes.append(
            AdrChange(
                name,
                rid,
                action,
                _diff(name, before, after) if action != "unchanged" else "",
                before,
                after,
            )
        )
    for rid, name in owned.items():
        if rid != _INDEX and rid not in snapshot.exported:
            warnings.append(
                f"{name}: retained for manual review; {rid} is outside the published ADR population"
            )
            changes.append(AdrChange(name, rid, "retained"))
    return AdrPlan(out, changes, warnings)


def apply_plan(plan: AdrPlan) -> None:
    """Explicitly apply an already reviewed plan; fail on conflicts or changed targets.

    Each existing file is replaced atomically. A later I/O/race error can leave earlier
    writes applied (or a partial newly-created file); no batch transaction is claimed.
    """
    if any(change.action == "conflict" for change in plan.changes):
        raise ValueError("ADR plan contains conflicts; no writes applied")
    _directory(plan.out)
    writes = [change for change in plan.changes if change.action in {"add", "update"}]
    # Check the complete write set before even creating directories.
    for change in writes:
        if _read_target(plan.out / change.path) != change.before:
            raise ValueError(f"ADR target changed after preview: {change.path}")
    if not writes:
        return
    plan.out.mkdir(parents=True, exist_ok=True)
    for change in writes:
        _directory(plan.out)
        target = plan.out / change.path
        if _read_target(target) != change.before:
            raise ValueError(f"ADR target changed after preview: {change.path}")
        assert change.after is not None
        if change.before is None:
            fd = os.open(
                target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o666
            )
            with os.fdopen(fd, "wb") as handle:
                handle.write(change.after)
        else:
            mode = stat.S_IMODE(target.stat().st_mode)
            fd, temporary = tempfile.mkstemp(prefix=".sidegraph-adr-", dir=plan.out)
            try:
                with os.fdopen(fd, "wb") as handle:
                    os.chmod(temporary, mode)
                    handle.write(change.after)
                if _read_target(target) != change.before:
                    raise ValueError(f"ADR target changed during write: {change.path}")
                os.replace(temporary, target)
            finally:
                Path(temporary).unlink(missing_ok=True)
