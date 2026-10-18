"""Pure auxiliary check loading, versioned subject identity and bounded Git snapshots."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import time
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


def git_bytes(root: Path, args: list[str], timeout: float | None) -> bytes:
    """Isolated read-only Git call with EOF stdin, bounded by the caller's remaining budget."""
    if timeout is not None and timeout <= 0:
        raise ValueError("Git check deadline exhausted")
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=root,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            env=git_env(),
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ValueError("Git check unavailable") from error
    if result.returncode:
        raise ValueError("Git check unavailable")
    return result.stdout


def head_revision(root: Path, timeout: float | None) -> str:
    value = git_bytes(root, ["rev-parse", "--verify", "HEAD^{commit}"], timeout).strip()
    if not _OID.fullmatch(value):
        raise ValueError("HEAD unavailable")
    return value.decode("ascii")


def tree_entries(
    root: Path, head: str, paths: set[str], timeout: float | None
) -> dict[str, tuple[str, str]]:
    """One NUL-safe tree lookup, including absence and mode; no historical object needed."""
    if not paths:
        return {}
    args = ["--literal-pathspecs", "ls-tree", "-r", "-z", "--full-tree", head]
    # Avoid argv limits on large review histories. This still reads tree metadata once,
    # then filters locally; it never reads source bodies or spawns per-record Git.
    if sum(len(os.fsencode(path)) + 1 for path in paths) <= 16_384:
        args += ["--", *sorted(paths)]
    output = git_bytes(root, args, timeout)
    out: dict[str, tuple[str, str]] = {}
    for atom in output.split(b"\0"):
        if not atom:
            continue
        metadata, name = atom.split(b"\t", 1)
        mode, kind, oid = metadata.split(b" ")
        path = os.fsdecode(name)
        if (
            path in paths
            and kind == b"blob"
            and mode in (b"100644", b"100755")
            and _OID.fullmatch(oid)
        ):
            out[path] = (oid.decode("ascii"), mode.decode("ascii"))
    return out


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
