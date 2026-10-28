"""Pure auxiliary check loading, versioned subject identity and bounded Git snapshots."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import NamedTuple

from .gitenv import git_env
from .schema import CheckedCodeFile, Decision, DriftCheck, Entity, is_safe_record_id
from .store_layout import VERIFICATIONS_SUBDIR, is_regular_file, stat_entry

# Fixed v1 vocabulary: future defaulted model fields do not invalidate existing checks.
_DECISION_FIELDS = (
    "id",
    "title",
    "kind",
    "context",
    "choice",
    "rejected",
    "consequences",
    "valid_from",
    "supersedes",
    "scope",
    "layer",
)
_PROVENANCE_FIELDS = ("source", "ref", "author", "session_id", "graph_version", "commit")
_CHECK_ID = re.compile(r"[0-7][0-9A-HJKMNP-TV-Z]{25}\Z")
_OID = re.compile(rb"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")

# Text caps for declared-section scope (design/superpowers/specs/
# 2026-10-27-declared-section-drift-checks-design.md, section 5.5). Callers read them at
# call time, so tests can patch them.
FILE_MAX_BYTES = 2 * 1024 * 1024
FILE_MAX_LINES = 20_000
SCAN_MAX_BYTES = 8 * 1024 * 1024
SCAN_MAX_LINES = 60_000


class SelectedCheck(NamedTuple):
    path: Path
    raw: dict

    def validated(self) -> DriftCheck:
        check = DriftCheck.model_validate(self.raw)
        if check.id != self.path.stem:
            raise ValueError("check id does not match its file")
        return check


def latest_checks(store_dir: Path) -> dict[str, SelectedCheck]:
    """Select a bound highest-id capsule BEFORE full validation, never an older valid one."""
    folder = store_dir / VERIFICATIONS_SUBDIR
    if folder.is_symlink():
        raise ValueError("verification directory is a symlink")
    out: dict[str, SelectedCheck] = {}
    for path in sorted(folder.glob("*.json")):
        if not _CHECK_ID.fullmatch(path.stem):
            continue
        try:
            if path.is_symlink() or not is_regular_file(stat_entry(path)):
                continue
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, RecursionError):
            continue  # strict snapshot lint reports malformed/unbound files
        if not isinstance(raw, dict) or not is_safe_record_id(raw.get("decision_id")):
            continue
        out[raw["decision_id"]] = SelectedCheck(path, raw)
    return out


def subject_digest(decision: dict, leaf_entities: list[dict]) -> str:
    """Version1 immutable decision + durable leaf descriptor identity, with fixed defaults."""
    payload = Decision.model_validate(decision).model_dump(mode="json")
    subject = {field: payload[field] for field in _DECISION_FIELDS}
    subject["provenance"] = {field: payload["provenance"][field] for field in _PROVENANCE_FIELDS}
    leaves = []
    for raw in leaf_entities:
        entity = Entity.model_validate(raw)
        if entity.descriptor is None:
            raise ValueError("concrete leaf descriptor unavailable")
        leaves.append((entity.entity_id, entity.descriptor.name, entity.descriptor.file_path))
    data = {"decision": subject, "leaf_descriptors": sorted(set(leaves))}
    return hashlib.sha256(
        json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def scope_leaves(
    record_id: str,
    bindings: list[dict],
    entities: dict[str, dict],
    statuses: dict[tuple[str, str], str],
) -> tuple[list[dict], set[str]]:
    """All durable leaves for identity; non-orphan candidate paths for required coverage."""
    leaves: list[dict] = []
    paths: set[str] = set()
    for binding in bindings:
        eid = binding.get("entity_id")
        if binding.get("tier") != 2 or not isinstance(eid, str):
            continue
        raw = entities.get(eid)
        if raw is None:
            raise ValueError("leaf entity unavailable")
        leaves.append(raw)
        descriptor = raw.get("descriptor")
        path = descriptor.get("file_path") if isinstance(descriptor, dict) else None
        if path and statuses.get((record_id, eid)) != "orphaned":
            if not isinstance(path, str):
                raise ValueError("invalid leaf path")
            # The blob/mode are temporary validation placeholders, never persisted here.
            CheckedCodeFile(path=path, blob_id="0" * 40, mode="100644")
            paths.add(path)
    return leaves, paths


def _env(no_lazy_fetch: bool) -> dict[str, str]:
    """``git_env()`` unchanged, plus ``GIT_NO_LAZY_FETCH=1`` when the caller must not fetch."""
    env = git_env()
    return {**env, "GIT_NO_LAZY_FETCH": "1"} if no_lazy_fetch else env


def _run(
    root: Path,
    args: list[str],
    timeout: float | None,
    *,
    stdin: int | None,
    data: bytes | None,
    no_lazy_fetch: bool,
) -> bytes:
    """One isolated Git call; the error mapping shared by ``git_bytes`` and ``git_bytes_input``."""
    if timeout is not None and timeout <= 0:
        raise ValueError("Git check deadline exhausted")
    kwargs: dict[str, object] = {"input": data} if data is not None else {"stdin": stdin}
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=root,
            capture_output=True,
            env=_env(no_lazy_fetch),
            timeout=timeout,
            **kwargs,  # type: ignore[call-overload]
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ValueError("Git check unavailable") from error
    if result.returncode:
        raise ValueError("Git check unavailable")
    return result.stdout


def git_bytes(
    root: Path, args: list[str], timeout: float | None, *, no_lazy_fetch: bool = False
) -> bytes:
    """Isolated read-only Git call with EOF stdin, bounded by the caller's remaining budget."""
    return _run(
        root, args, timeout, stdin=subprocess.DEVNULL, data=None, no_lazy_fetch=no_lazy_fetch
    )


def git_bytes_input(root: Path, args: list[str], data: bytes, timeout: float | None) -> bytes:
    """``git_bytes`` that feeds ``data`` on stdin and never lazy-fetches (grouped drift triage).

    One-shot ``subprocess.run(input=...)``, so the caller's deadline bounds it.
    # see design/superpowers/specs/2026-10-27-grouped-drift-triage-design.md §4.2
    """
    return _run(root, args, timeout, stdin=None, data=data, no_lazy_fetch=True)


class BlobRead(NamedTuple):
    """One blob's text, or why it could not be read (``reason`` is None when ``text`` is set)."""

    text: str | None
    reason: str | None  # too_large | too_many_lines | scope_cap | not_utf8 | unavailable


def read_blob_texts(
    root: Path,
    oids: Iterable[str],
    timeout: float | None,
    *,
    file_max_bytes: int,
    file_max_lines: int,
    total_max_bytes: int,
    total_max_lines: int,
) -> dict[str, BlobRead]:
    """Read distinct blobs with two Git calls (``--batch-check``, then ``--batch``).

    Blobs are taken in sorted-oid order so the scan caps give a deterministic result.
    Raises ``ValueError`` when the budget is exhausted or Git fails as a whole; a single
    bad blob is reported through its ``reason`` instead. Also raises on a partial clone with
    Git older than 2.45, which ignores ``GIT_NO_LAZY_FETCH`` and could fetch a missing blob
    (the same gate grouped triage applies before its object reads).
    # see design/superpowers/specs/2026-10-27-declared-section-drift-checks-design.md (5.5)
    """
    from .md_sections import count_lines

    ordered = sorted(set(oids))
    if not ordered:
        return {}
    expires = None if timeout is None else time.monotonic() + timeout

    def left() -> float | None:
        return None if expires is None else expires - time.monotonic()

    if partial_clone_configured(root, left()):
        version = git_version(root, left())
        if version is None or version < (2, 45):
            raise ValueError("partial clone on Git older than 2.45: blob reads could fetch")

    request = "".join(f"{oid}\n" for oid in ordered).encode("ascii")
    sizes: dict[str, int] = {}
    out: dict[str, BlobRead] = {}
    for oid, line in zip(
        ordered,
        git_bytes_input(root, ["cat-file", "--batch-check"], request, left()).splitlines(),
        strict=False,
    ):
        parts = line.split(b" ")
        if len(parts) == 3 and parts[1] == b"blob" and parts[2].isdigit():
            sizes[oid] = int(parts[2])
        else:
            out[oid] = BlobRead(None, "unavailable")
    for oid in ordered:
        if oid not in sizes and oid not in out:
            out[oid] = BlobRead(None, "unavailable")
    admitted: list[str] = []
    total_bytes = 0
    for oid in ordered:
        if oid not in sizes:
            continue
        if sizes[oid] > file_max_bytes:
            out[oid] = BlobRead(None, "too_large")
        elif total_bytes + sizes[oid] > total_max_bytes:
            out[oid] = BlobRead(None, "scope_cap")
        else:
            total_bytes += sizes[oid]
            admitted.append(oid)
    bodies: dict[str, bytes] = {}
    if admitted:
        stream = git_bytes_input(
            root,
            ["cat-file", "--batch"],
            "".join(f"{oid}\n" for oid in admitted).encode("ascii"),
            left(),
        )
        pos = 0
        for oid in admitted:
            end = stream.find(b"\n", pos)
            header = stream[pos:end].split(b" ") if end >= 0 else []
            if len(header) != 3 or header[0].decode("ascii", "replace") != oid:
                break
            size = int(header[2])
            bodies[oid] = stream[end + 1 : end + 1 + size]
            pos = end + 1 + size + 1
    total_lines = 0
    for oid in admitted:
        body = bodies.get(oid)
        if body is None or len(body) != sizes[oid]:
            out[oid] = BlobRead(None, "unavailable")
            continue
        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError:
            out[oid] = BlobRead(None, "not_utf8")
            continue
        lines = count_lines(text)
        if lines > file_max_lines:
            out[oid] = BlobRead(None, "too_many_lines")
        elif total_lines + lines > total_max_lines:
            out[oid] = BlobRead(None, "scope_cap")
        else:
            total_lines += lines
            out[oid] = BlobRead(text, None)
    return out


def head_revision(root: Path, timeout: float | None, *, no_lazy_fetch: bool = False) -> str:
    value = git_bytes(
        root, ["rev-parse", "--verify", "HEAD^{commit}"], timeout, no_lazy_fetch=no_lazy_fetch
    ).strip()
    if not _OID.fullmatch(value):
        raise ValueError("HEAD unavailable")
    return value.decode("ascii")


def _ls_tree_args(head: str, paths: set[str], *, recursive: bool) -> list[str]:
    args = [
        "--literal-pathspecs",
        "ls-tree",
        *(["-r"] if recursive else []),
        "-z",
        "--full-tree",
        head,
    ]
    # Avoid argv limits on large review histories. This still reads tree metadata once,
    # then filters locally; it never reads source bodies or spawns per-record Git.
    if sum(len(os.fsencode(path)) + 1 for path in paths) <= 16_384:
        args += ["--", *sorted(paths)]
    return args


def _parse_tree(output: bytes, paths: set[str]) -> dict[str, tuple[str, str, str]]:
    """``ls-tree -z`` output as path -> (oid, mode, kind), restricted to ``paths``."""
    out: dict[str, tuple[str, str, str]] = {}
    for atom in output.split(b"\0"):
        if not atom:
            continue
        metadata, name = atom.split(b"\t", 1)
        mode, kind, oid = metadata.split(b" ")
        path = os.fsdecode(name)
        if path in paths and _OID.fullmatch(oid):
            out[path] = (oid.decode("ascii"), mode.decode("ascii"), kind.decode("ascii"))
    return out


def tree_entries(
    root: Path, head: str, paths: set[str], timeout: float | None
) -> dict[str, tuple[str, str]]:
    """One NUL-safe tree lookup, including absence and mode; no historical object needed."""
    if not paths:
        return {}
    output = git_bytes(root, _ls_tree_args(head, paths, recursive=True), timeout)
    return {
        path: (oid, mode)
        for path, (oid, mode, kind) in _parse_tree(output, paths).items()
        if kind == "blob" and mode in ("100644", "100755")
    }


def _chunks(paths: set[str]) -> list[set[str]]:
    """``paths`` split so each chunk's pathspec stays within the argv budget (sorted, stable)."""
    chunks: list[set[str]] = []
    current: set[str] = set()
    size = 0
    for path in sorted(paths):
        cost = len(os.fsencode(path)) + 1
        if current and size + cost > 16_384:
            chunks.append(current)
            current, size = set(), 0
        current.add(path)
        size += cost
    if current:
        chunks.append(current)
    return chunks


def _layers(paths: set[str]) -> list[set[str]]:
    """``paths`` in layers where no path is an ancestor directory of another in its layer."""
    layers: list[set[str]] = []
    for path in sorted(paths):
        for layer in layers:
            if not any(
                other.startswith(path + "/") or path.startswith(other + "/") for other in layer
            ):
                layer.add(path)
                break
        else:
            layers.append({path})
    return layers


def tree_entries_all(
    root: Path,
    head: str,
    paths: set[str],
    timeout: float | None | Callable[[], float | None],
) -> dict[str, tuple[str, str]]:
    """``tree_entries`` for every entry kind (symlink, submodule, directory), never fetching.

    ``ls-tree -r`` lists a directory's children, not the directory, so a path absent from the
    recursive output gets a non-recursive lookup that finds a ``040000 tree`` entry. Path sets
    over the argv budget are split into chunks rather than dropping the pathspec. ``timeout``
    may be a callable returning the time left: every Git call draws on it, so the fallback
    and the chunks share one deadline.
    # see design/superpowers/specs/2026-10-27-grouped-drift-triage-design.md §4.2
    """
    if not paths:
        return {}

    def left() -> float | None:
        return timeout() if callable(timeout) else timeout

    found: dict[str, tuple[str, str, str]] = {}
    for chunk in _chunks(paths):
        output = git_bytes(
            root, _ls_tree_args(head, chunk, recursive=True), left(), no_lazy_fetch=True
        )
        found.update(_parse_tree(output, chunk))
    # One non-recursive call lists a requested path's own entry only if no other requested
    # path lies beneath it, so ancestors and descendants go in separate layers.
    for layer in _layers(paths - set(found)):
        for chunk in _chunks(layer):
            output = git_bytes(
                root, _ls_tree_args(head, chunk, recursive=False), left(), no_lazy_fetch=True
            )
            found.update(_parse_tree(output, chunk))
    return {path: (oid, mode) for path, (oid, mode, _kind) in found.items()}


def _config_lines(root: Path, args: list[str], timeout: float | None) -> list[str] | None:
    """``git config`` output lines, ``None`` when no key matched (exit 1); any other failure,
    including a value Git rejects, raises ``ValueError``."""
    if timeout is not None and timeout <= 0:
        raise ValueError("Git check deadline exhausted")
    try:
        result = subprocess.run(
            ["git", "config", *args],
            cwd=root,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            env=_env(True),
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ValueError("Git check unavailable") from error
    if result.returncode == 1:
        return None
    if result.returncode:
        raise ValueError("Git check unavailable")
    return result.stdout.decode("utf-8", "replace").splitlines()


def partial_clone_configured(root: Path, timeout: float | None) -> bool:
    """Whether the repository is a partial clone: ``extensions.partialClone`` is set or a
    remote is a promisor. Booleans are parsed by Git itself (``--type=bool``: a bare key,
    ``2``, ``yes`` and ``on`` are true). Raises ``ValueError`` when Git cannot answer or
    rejects a value; the caller then assumes a partial clone."""
    extension = _config_lines(root, ["--get", "extensions.partialclone"], timeout)
    if extension is not None and any(line.strip() for line in extension):
        return True
    promisors = _config_lines(
        root, ["--type=bool", "--get-regexp", r"^remote\..*\.promisor$"], timeout
    )
    return any(line.rsplit(" ", 1)[-1] == "true" for line in promisors or [])


def git_version(root: Path, timeout: float | None) -> tuple[int, int] | None:
    """Git's ``(major, minor)``, or ``None`` when it cannot be read."""
    text = git_bytes(root, ["--version"], timeout, no_lazy_fetch=True).decode("ascii", "replace")
    match = re.search(r"(\d+)\.(\d+)", text)
    return (int(match[1]), int(match[2])) if match else None


def clean_inspected_files(
    root: Path, head: str, entries: dict[str, tuple[str, str]], timeout: float
) -> bool:
    """Check index/mode deltas and hash real worktree files even behind index skip flags.

    hash-object without -w does not write objects. Default Git filters retain normal EOL
    conversion semantics: https://git-scm.com/docs/git-hash-object .
    """
    expires = time.monotonic() + timeout
    paths = sorted(entries)
    if not paths or any(
        not (root / path).is_file() or (root / path).is_symlink() for path in paths
    ):
        return False
    args = ["--literal-pathspecs", "diff", "--no-ext-diff", "--no-textconv", "--quiet"]
    try:
        # Git's index skip flags can hide chmod too. Respect core.filemode=false on
        # filesystems (notably Windows) where executable bits are not meaningful.
        tracks_mode = (
            git_bytes(
                root,
                ["config", "--type=bool", "--default=true", "--get", "core.filemode"],
                expires - time.monotonic(),
            ).strip()
            == b"true"
        )
        if tracks_mode and any(
            bool((root / path).stat().st_mode & stat.S_IXUSR) != (entries[path][1] == "100755")
            for path in paths
        ):
            return False
        git_bytes(root, [*args, "--cached", head, "--", *paths], expires - time.monotonic())
        git_bytes(root, [*args, head, "--", *paths], expires - time.monotonic())
        hashes = git_bytes(
            root, ["hash-object", "--", *paths], expires - time.monotonic()
        ).splitlines()
    except (ValueError, OSError):
        return False
    return hashes == [entries[path][0].encode("ascii") for path in paths]
