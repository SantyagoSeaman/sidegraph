"""Shared builders and observers for the packed-store (format 2) tests.

Every format-2 test module imports these; keep the signatures stable.
# see design/superpowers/specs/2026-10-20-packed-store-format-design.md (§5, §9)
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from sidegraph import maintenance
from sidegraph.gitenv import git_env as _inherited_git_env
from sidegraph.schema import SCHEMA_VERSION, validate_record_body
from sidegraph.store_format import (
    SHARD_TYPES,
    bucket_name,
    encode_body,
    encode_line,
    inventory_from_disk,
    marker_text,
    parse_marker,
    render_shard,
    split_lines,
)
from sidegraph.store_format import bucket as bucket_of
from sidegraph.store_rules import effective_all

_RECORD_DIRS = {
    "decision": "decisions",
    "fact": "facts",
    "entity": "entities",
    "domain": "domains",
    "bindings": "bindings",
}
_ID_FIELDS = {"decision": "id", "fact": "id", "entity": "entity_id", "domain": "domain_id"}
_WHEN = "2026-01-01T00:00:00Z"
_PROVENANCE = {"source": "manual"}


# -- record bodies (raw JSON dicts, valid against the models) --------------------------------


def decision_body(rid: str, **over: object) -> dict:
    body = {
        "id": rid,
        "title": f"decision {rid}",
        "kind": "adr",
        "status": "proposed",
        "context": "ctx",
        "choice": "choice",
        "valid_from": _WHEN,
        "provenance": dict(_PROVENANCE),
    }
    body.update(over)
    return body


def fact_body(rid: str, **over: object) -> dict:
    body = {
        "id": rid,
        "statement": f"fact {rid}",
        "source": "measured",
        "valid_from": _WHEN,
        "provenance": dict(_PROVENANCE),
    }
    body.update(over)
    return body


def entity_body(rid: str, **over: object) -> dict:
    body = {"entity_id": rid, "canonical_name": f"entity {rid}", "kind": "concrete"}
    body.update(over)
    return body


def domain_body(rid: str, **over: object) -> dict:
    body = {
        "domain_id": rid,
        "slug": f"domain-{rid.lower()}",
        "title": f"Domain {rid}",
        "summary": "why it exists",
        "status": "proposed",
        "provenance": dict(_PROVENANCE),
    }
    body.update(over)
    return body


def bindings_body(*entity_ids: str) -> list[dict]:
    return [{"entity_id": e, "tier": 2} for e in entity_ids]


def validator(record_type: str, body: object) -> str | None:
    """The model check inventories are built with in tests."""
    return validate_record_body(record_type, body)


# -- builders ----------------------------------------------------------------------------------


def make_store(tmp_path: Path, *, fmt: int = 2, n: int = 64) -> Path:
    """A store directory with a format marker: format 1 writes ``sidegraph-store 1.0.0`` and
    never a ``shards/``; format 2 writes the marker of ``n`` shards (and no ``shards/`` yet)."""
    store = tmp_path / ".sidegraph"
    store.mkdir(parents=True, exist_ok=True)
    text = marker_text(1, "0.16.0", None) if fmt == 1 else marker_text(2, "0.17.0", n)
    (store / "format").write_text(text, encoding="utf-8")
    return store


def format1_store(path: Path) -> Path:
    """``path`` as a per-record store (format 1) with the marker a fresh store had before
    format 2: ``sidegraph-store 0.6.0``, no requirement, index stamp ``0.6.0``. A path the
    caller chose, and never over an existing marker. A bare ``Store(path)`` on an empty
    directory is format 2 now, so a test that assumes the per-record layout (archive
    segments, ``Store.compact``, a ``0.6.0`` index stamp) opens ``Store(format1_store(path))``.
    ``make_store(tmp_path, fmt=1)`` is the raised-store form (``1.0.0`` plus a requirement).
    """
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    marker = path / "format"
    if not marker.exists():
        marker.write_text(f"sidegraph-store {SCHEMA_VERSION}\n", encoding="utf-8")
    return path


def legacy_store(path: Path) -> Path:
    """``path`` as a store that predates the format marker: one entity record, no marker. The
    first ``Store(path)`` backfills the major-0 marker (``sidegraph-store 0.6.0``), as it did
    for a fresh store before format 2; an empty directory is format 2 now. For tests that
    exercise the moment the marker is created.
    """
    put_file(Path(path), "entity", entity_body("E0LEGACY"))
    return Path(path)


def put_file(store: Path, t: str, body: object, *, record_id: str | None = None) -> Path:
    """Write a per-record file ``<dir>/<id>.json``. The id is the body's id field; a bindings
    list has none, so pass ``record_id=``."""
    if record_id is None:
        assert isinstance(body, dict), "a bindings file needs record_id="
        record_id = str(body[_ID_FIELDS[t]])
    path = store / _RECORD_DIRS[t] / f"{record_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(body, sort_keys=True, indent=2, ensure_ascii=False) + "\n", "utf-8")
    return path


def _shard_count(store: Path) -> int:
    marker = parse_marker(
        (store / "format").read_text(encoding="utf-8"),
        store / "format",
        malformed=lambda p, d: ValueError(d),
    )
    assert marker.shards is not None, "a shard line needs a format-2 store"
    return marker.shards


def put_shard_line(store: Path, t: str, i: str, body: object, *, bucket: str | None = None) -> Path:
    """Add a line for ``(t, i)`` to a shard file (default: the identity's own bucket) and
    re-render the file sorted. ``bucket`` is a file name such as ``0A.SHARD``."""
    assert t in SHARD_TYPES
    name = bucket if bucket is not None else bucket_name(bucket_of(i, _shard_count(store)))
    path = store / "shards" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    existing: list[tuple[str, str, bytes]] = []
    if path.exists():
        for _, raw in split_lines(path.read_bytes()):
            parsed = json.loads(raw)
            existing.append((parsed["type"], parsed["id"], raw))
    existing.append((t, i, encode_line(t, i, body)))
    path.write_bytes(render_shard(existing))
    return path


def put_archive(store: Path, segment: str, lines: Iterable[dict | bytes | str]) -> Path:
    """Write ``archive/<segment>.jsonl``: dict lines as JSON (with their ``record_type``), bytes
    and str lines as given."""
    path = store / "archive" / (segment if segment.endswith(".jsonl") else segment + ".jsonl")
    path.parent.mkdir(parents=True, exist_ok=True)
    out = b""
    for line in lines:
        if isinstance(line, dict):
            line = json.dumps(line, sort_keys=True, ensure_ascii=False)
        out += (line.encode("utf-8") if isinstance(line, str) else line) + b"\n"
    path.write_bytes(out)
    return path


# -- git -----------------------------------------------------------------------------------------


def git_env() -> dict[str, str]:
    env = _inherited_git_env()
    env.update(
        GIT_AUTHOR_NAME="t",
        GIT_AUTHOR_EMAIL="t@example.com",
        GIT_COMMITTER_NAME="t",
        GIT_COMMITTER_EMAIL="t@example.com",
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_SYSTEM=os.devnull,
    )
    return env


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        env=git_env(),
    ).stdout


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """A fresh repository (``main``, user config, ``gc.auto 0``) with the store directory at
    ``<repo>/.sidegraph`` (not yet created)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.name", "t")
    git(repo, "config", "user.email", "t@example.com")
    git(repo, "config", "gc.auto", "0")
    return repo


def commit_all(repo: Path, msg: str, *, date: str) -> str:
    """Stage everything, commit with author and committer date ``date``, return the sha."""
    git(repo, "add", "-A")
    env = {**git_env(), "GIT_COMMITTER_DATE": date, "GIT_AUTHOR_DATE": date}
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-q", "--allow-empty", "-m", msg],
        check=True,
        capture_output=True,
        stdin=subprocess.DEVNULL,
        env=env,
    )
    return git(repo, "rev-parse", "HEAD").strip()


# -- observation -----------------------------------------------------------------------------------


def codes(findings: Iterable[object]) -> set[str]:
    """The ``code`` of each finding."""
    return {str(f.code) for f in findings}


def effective_snapshot(store: Path) -> dict[tuple[str, str], object]:
    """``effective_all`` of the store on disk, mapped to each raw body; quarantined identities
    are included as ``None``."""
    inv = inventory_from_disk(store, validate=validator)
    return {k: e.raw for k, e in effective_all(inv).items()}


def shard_bytes(store: Path) -> dict[str, bytes]:
    shards = store / "shards"
    if not shards.is_dir():
        return {}
    return {p.name: p.read_bytes() for p in sorted(shards.glob("*.shard"))}


def _tree(store: Path) -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    for p in sorted(store.rglob("*")):
        if p.is_file() and not p.name.startswith("index.db") and not p.name.endswith(".tmp"):
            out[p.relative_to(store).as_posix()] = p.read_bytes()
    return out


def canonical_bytes(store: Path) -> dict[str, bytes]:
    """Every file under the store except ``index.db*`` and ``*.tmp``. The skip list is index
    state (``skipped_canonical_files`` in ``index.db``), not a file, so it is not in this map."""
    return _tree(store)


def tree_bytes(store: Path) -> dict[str, bytes]:
    """The same map including the skip list; it is index state, so today equal to
    :func:`canonical_bytes`."""
    return _tree(store)


def per_record_file(store: Path, t: str, i: str) -> Path:
    return store / _RECORD_DIRS[t] / f"{i}.json"


def every_record_survives(store: Path, before: dict) -> bool:
    """``effective_snapshot`` keys and raw bodies are a superset of ``before``."""
    now = effective_snapshot(store)
    return all(k in now and now[k] == v for k, v in before.items())


def index_stamp_of(store: Path) -> str:
    """The ``schema_version`` stamp of the store's derived index."""
    import sqlite3

    conn = sqlite3.connect(store / "index.db")
    try:
        row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    finally:
        conn.close()
    return str(row[0])


def grep_src(literal: str) -> set[str]:
    """Repo-relative paths under ``src/`` whose text contains ``literal``."""
    root = Path(__file__).resolve().parent.parent
    return {
        p.relative_to(root).as_posix()
        for p in sorted((root / "src").rglob("*.py"))
        if literal in p.read_text(encoding="utf-8")
    }


# -- maintenance publisher: crash, kill and fsync-order observers --------------------------------
# see design/superpowers/specs/2026-10-20-packed-store-format-design.md (§6.2, §9 T-M1, T-M2)


class Killed(BaseException):
    """Raised by :func:`run_with_kill` at the chosen checkpoint: a process killed there."""


def _maintain(store: Path, *, fold_checks: bool = False) -> object:
    from sidegraph.store import Store

    return Store(store).maintain(
        settled_days=0, dry_run=False, any_branch=True, fold_checks=fold_checks
    )


def record_checkpoints(store: Path, *, fold_checks: bool = False) -> list[str]:
    """Run maintenance on ``store`` with ``maintenance.checkpoint`` recording every label."""
    labels: list[str] = []
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(maintenance, "checkpoint", labels.append)
        _maintain(store, fold_checks=fold_checks)
    return labels


def run_with_kill(store: Path, k: int, *, fold_checks: bool = False) -> None:
    """Run maintenance on ``store`` and raise :class:`Killed` at its ``k``-th (0-based)
    checkpoint, after the durable step the checkpoint follows."""
    count = 0

    def hook(label: str) -> None:
        nonlocal count
        if count == k:
            raise Killed(label)
        count += 1

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(maintenance, "checkpoint", hook)
        _maintain(store, fold_checks=fold_checks)


def _name_of_fd(fd: int, store: Path) -> str:
    """The store-relative name of the file behind ``fd`` (found by inode), ``?`` if unknown."""
    ino = os.fstat(fd).st_ino
    for p in store.rglob("*"):
        try:
            if p.is_file() and p.stat().st_ino == ino:
                return p.relative_to(store).as_posix()
        except OSError:
            continue
    return "?"


def _name_of_dir(path: Path, store: Path) -> str:
    return "store" if Path(path) == store else Path(path).relative_to(store).as_posix()


def record_fsyncs(monkeypatch: pytest.MonkeyPatch, store: Path) -> list[tuple[str, str]]:
    """Record ``("file", name)`` for each ``fsync_fd`` of a file (a tmp shows up under its
    ``*.tmp`` name), ``("dir", name)`` for each ``fsync_dir`` (``store`` for the store
    directory) and ``("unlink", name)`` for each ``os.unlink``. Calls still run."""
    calls: list[tuple[str, str]] = []
    real_fd, real_dir, real_unlink = maintenance.fsync_fd, maintenance.fsync_dir, os.unlink

    def fd(handle: int) -> None:
        calls.append(("file", _name_of_fd(handle, store)))
        real_fd(handle)

    def directory(path: Path) -> None:
        calls.append(("dir", _name_of_dir(path, store)))
        real_dir(path)

    def unlink(path: str | os.PathLike[str], *a: object, **kw: object) -> None:
        target = Path(os.fsdecode(path))
        if target.is_absolute() and target.is_relative_to(store):
            calls.append(("unlink", target.relative_to(store).as_posix()))
        real_unlink(path, *a, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(maintenance, "fsync_fd", fd)
    monkeypatch.setattr(maintenance, "fsync_dir", directory)
    monkeypatch.setattr(os, "unlink", unlink)
    return calls


def first_unlink(calls: list[tuple[str, str]]) -> int:
    return next(i for i, c in enumerate(calls) if c[0] == "unlink")


def record_publishes(monkeypatch: pytest.MonkeyPatch, store: Path) -> list[tuple[str, str]]:
    """Record ``("fsync_tmp", target name)`` when a tmp is flushed, ``("replace", name)`` for
    each ``os.replace`` and ``("fsync_dir", name)`` for each directory fsync. Calls still run."""
    calls: list[tuple[str, str]] = []
    real_fd, real_dir, real_replace = maintenance.fsync_fd, maintenance.fsync_dir, os.replace

    def fd(handle: int) -> None:
        name = _name_of_fd(handle, store)
        if name.endswith(".tmp"):  # <target>.<pid>.<hex>.tmp
            head, _, leaf = name.rpartition("/")
            leaf = ".".join(leaf.split(".")[:-3])
            name = f"{head}/{leaf}" if head else leaf
        calls.append(("fsync_tmp", name))
        real_fd(handle)

    def directory(path: Path) -> None:
        calls.append(("fsync_dir", _name_of_dir(path, store)))
        real_dir(path)

    def replace(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        target = Path(os.fsdecode(dst))
        if target.is_absolute() and target.is_relative_to(store):
            calls.append(("replace", target.relative_to(store).as_posix()))
        real_replace(src, dst)

    monkeypatch.setattr(maintenance, "fsync_fd", fd)
    monkeypatch.setattr(maintenance, "fsync_dir", directory)
    monkeypatch.setattr(os, "replace", replace)
    return calls


def first_shard(calls: list[tuple[str, str]]) -> int:
    """Index of the first ``replace`` of a ``shards/*.shard`` file."""
    return next(i for i, c in enumerate(calls) if c[0] == "replace" and c[1].startswith("shards/"))


# -- maintenance fixtures -----------------------------------------------------------------------

_A, _B, _C = 3, 17, 40  # three buckets of a 64-shard store


def ids_in_bucket(b: int, count: int = 1, *, prefix: str = "X", n: int = 64) -> list[str]:
    """``count`` record ids that hash into bucket ``b`` of ``n`` shards."""
    found: list[str] = []
    i = 0
    while len(found) < count:
        candidate = f"{prefix}{i:05d}"
        i += 1
        if bucket_of(candidate, n) == b:
            found.append(candidate)
    return found


def shard_name(b: int) -> str:
    return bucket_name(b)


def archived_decision(rid: str, **over: object) -> dict:
    """An archive line for a terminal decision."""
    body = decision_body(rid, status="rejected", valid_to="2026-02-01T00:00:00Z", **over)
    return {"record_type": "decision", **body}


def build(fixture: str, path: Path) -> Path:
    """The store directory of one of the crash-test fixtures, built under ``path`` (outside
    git, so every file is eligible under ``--settled-days 0``). Every fixture also holds a
    plain per-record file with no shard line, so the insert and the deletion run too."""
    store = make_store(path)
    plain = ids_in_bucket(_C, 1, prefix="PLAIN")[0]
    put_file(store, "decision", decision_body(plain))
    x, w = ids_in_bucket(_A, 2, prefix="XA")
    y, z = ids_in_bucket(_B, 2, prefix="YB")
    c1 = ids_in_bucket(_C, 1, prefix="ZC")[0]

    def line(i: str, name: str, **over: object) -> None:
        put_shard_line(store, "decision", i, decision_body(i, **over), bucket=name)

    if fixture == "swap_two_shards":
        line(x, shard_name(_B))
        line(y, shard_name(_A))
        put_archive(store, "seg1", [archived_decision("ARCH1")])
    elif fixture == "three_shard_cycle":
        line(x, shard_name(_B))
        line(y, shard_name(_C))
        line(c1, shard_name(_A))
    elif fixture == "misplaced_with_eligible_newer_file":
        line(x, shard_name(_B))
        put_file(store, "decision", decision_body(x, status="accepted"))
    elif fixture == "swap_with_phase_c_replacement":
        line(x, shard_name(_B))
        line(y, shard_name(_A))
        line(w, shard_name(_A))
        put_file(store, "decision", decision_body(w, status="accepted"))
    elif fixture == "stray_into_two_buckets":
        line(x, "extra.shard")
        line(y, "extra.shard")
    elif fixture == "stray_skipped_beside_eligible":
        skipped = ids_in_bucket(_C, 1, prefix="SKIP")[0]
        line(skipped, "extra.shard", status="accepted")
        put_file(store, "decision", decision_body(skipped, status="proposed"))
        line(x, "extra.shard")
        put_file(store, "decision", decision_body(x, status="accepted"))
    elif fixture == "bucket_two_equal_lines":
        line(x, shard_name(_A))
        line(x, shard_name(_A))
    elif fixture == "equal_bindings_bodies_one_in_dest_other_misplaced_or_stray":
        r1, r2, r3 = ids_in_bucket(_A, 3, prefix="RB")
        for rid, name in ((r1, shard_name(_A)), (r2, shard_name(_B)), (r3, "extra.shard")):
            put_shard_line(store, "bindings", rid, bindings_body("E1", "E2"), bucket=name)
    else:  # pragma: no cover
        raise ValueError(fixture)
    return store


# -- reading a store through its API ------------------------------------------------------------


def _summary(record_type: str, raw: object) -> object:
    """A comparable digest of one record body: what the Store API must serve for it."""
    if raw is None:
        return None
    assert isinstance(raw, (dict, list))
    if record_type == "decision":
        return (raw["title"], raw["status"])  # type: ignore[index]
    if record_type == "fact":
        return raw["statement"]  # type: ignore[index]
    if record_type == "entity":
        return raw["canonical_name"]  # type: ignore[index]
    if record_type == "domain":
        return (raw["slug"], raw["status"])  # type: ignore[index]
    return sorted(item["entity_id"] for item in raw)  # type: ignore[union-attr]


def disk_summaries(store: Path) -> dict[tuple[str, str], object]:
    """``_summary`` of every effective record read from the files (no index involved)."""
    return {k: _summary(k[0], raw) for k, raw in effective_snapshot(store).items()}


def store_snapshot(store: Path) -> dict[tuple[str, str], object]:
    """The same summaries read through a freshly opened ``Store`` (its index), for every
    identity the files hold. ``None`` where the Store serves no such record."""
    from sidegraph.store import Store

    out: dict[tuple[str, str], object] = {}
    with Store(store) as s:
        for record_type, record_id in effective_snapshot(store):
            got: object = None
            if record_type == "decision":
                d = s.get_decision(record_id)
                got = None if d is None else (d.title, d.status.value)
            elif record_type == "fact":
                f = s.get_fact(record_id)
                got = None if f is None else f.statement
            elif record_type == "entity":
                e = s.get_entity(record_id)
                got = None if e is None else e.canonical_name
            elif record_type == "domain":
                dm = s.get_domain(record_id)
                got = None if dm is None else (dm.slug, dm.status.value)
            else:
                got = sorted(b.entity_id for b in s.bindings_for_record(record_id))
            out[(record_type, record_id)] = got
    return out


# -- the --against fixture DSL ----------------------------------------------------------------
# One case is a REF (ops applied to an empty format-2 store, then committed) and a new tree (ops
# applied on top of REF's tree and left uncommitted: ``verify_against`` reads the work tree).
# see design/superpowers/specs/2026-10-20-packed-store-format-design.md (§7.1, §9 T-V2)


@dataclass(frozen=True)
class File:
    """Write a per-record file (``record_id`` for a bindings list)."""

    t: str
    body: object
    record_id: str | None = None


@dataclass(frozen=True)
class DelFile:
    t: str
    id: str


@dataclass(frozen=True)
class Line:
    """Add a shard line (default bucket: the identity's own)."""

    t: str
    id: str
    body: object
    bucket: str | None = None


@dataclass(frozen=True)
class DelLine:
    """Remove the ``k``-th line (file order) of an identity from a bucket."""

    t: str
    id: str
    bucket: str | None = None
    k: int = 0


@dataclass(frozen=True)
class Segment:
    """Write ``archive/<name>`` from dict lines (with their ``record_type``) or raw bytes."""

    name: str
    lines: list


@dataclass(frozen=True)
class DelSegment:
    name: str


@dataclass(frozen=True)
class Marker:
    """Replace the ``format`` marker text."""

    text: str


@dataclass(frozen=True)
class Raw:
    """Write ``data`` at the store-relative ``path`` (unparseable or conflicted bytes); ``None``
    removes the file."""

    path: str
    data: bytes | None


@dataclass(frozen=True)
class Config:
    """``git config <key> <value>`` before the REF commit (``core.autocrlf``)."""

    key: str
    value: str


Op = File | DelFile | Line | DelLine | Segment | DelSegment | Marker | Raw | Config


@dataclass(frozen=True)
class Case:
    ref: list[Op] = field(default_factory=list)
    new: list[Op] = field(default_factory=list)


def own_bucket(store: Path, record_id: str) -> str:
    """The shard file name that holds ``record_id`` in this store."""
    return bucket_name(bucket_of(record_id, _shard_count(store)))


def other_bucket(store: Path, record_id: str) -> str:
    """A valid shard file name that is not ``record_id``'s own bucket."""
    own = own_bucket(store, record_id)
    return next(bucket_name(b) for b in range(_shard_count(store)) if bucket_name(b) != own)


def _apply(repo: Path, store: Path, op: Op) -> None:
    if isinstance(op, File):
        put_file(store, op.t, op.body, record_id=op.record_id)
    elif isinstance(op, DelFile):
        per_record_file(store, op.t, op.id).unlink()
    elif isinstance(op, Line):
        put_shard_line(store, op.t, op.id, op.body, bucket=op.bucket)
    elif isinstance(op, DelLine):
        name = op.bucket if op.bucket is not None else own_bucket(store, op.id)
        path = store / "shards" / name
        kept: list[tuple[str, str, bytes]] = []
        seen = 0
        for _, raw in split_lines(path.read_bytes()):
            parsed = json.loads(raw)
            if (parsed["type"], parsed["id"]) == (op.t, op.id):
                seen += 1
                if seen - 1 == op.k:
                    continue
            kept.append((parsed["type"], parsed["id"], raw))
        assert seen > op.k, f"no line {op.k} of {op.t} {op.id} in {name}"
        path.write_bytes(render_shard(kept))
    elif isinstance(op, Segment):
        put_archive(store, op.name, op.lines)
    elif isinstance(op, DelSegment):
        (store / "archive" / op.name).unlink()
    elif isinstance(op, Marker):
        (store / "format").write_text(op.text, encoding="utf-8")
    elif isinstance(op, Raw):
        path = store / op.path
        if op.data is None:
            path.unlink()
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(op.data)
    else:
        git(repo, "config", op.key, op.value)


def build_case(repo: Path, case: Case) -> tuple[Path, str]:
    """Build ``case`` in the fresh repository ``repo``: a format-2 store at ``<repo>/.sidegraph``
    with the REF ops, committed; then the new ops, left in the work tree. Returns the store
    directory and REF's sha."""
    store = make_store(repo)
    (repo / ".gitattributes").write_text("*.shard -text\n", encoding="utf-8")
    for op in case.ref:
        _apply(repo, store, op)
    ref = commit_all(repo, "ref", date="2026-02-01T00:00:00Z")
    for op in case.new:
        _apply(repo, store, op)
    return store, ref


# -- the committed format-1 store and the migration stop points (shared by migrate and the
# older-reader tests) -------------------------------------------------------------------------

OLD = "2026-01-01T00:00:00Z"
GITIGNORE = "index.db*\n*.tmp\n"


def format1_committed(repo: Path) -> Path:
    """A committed format-1 store: marker ``1.0.0`` + ``requires-sidegraph >= 0.16.0`` and a
    handful of per-record files of every packable type."""
    store = make_store(repo, fmt=1)
    put_file(store, "decision", decision_body("D1"))
    put_file(store, "decision", decision_body("D2", status="accepted"))
    put_file(store, "fact", fact_body("F1"))
    put_file(store, "entity", entity_body("E1"))
    put_file(store, "domain", domain_body("DM1"))
    put_file(store, "bindings", [{"entity_id": "E1", "tier": 2}], record_id="D2")
    (store / ".gitignore").write_text(GITIGNORE, encoding="utf-8")
    commit_all(repo, "store", date=OLD)
    return store


# Maintenance checkpoints at which a migration is killed (``maintenance.checkpoint`` labels).
STOPS = {
    "format": lambda label: label == "migrate:format",
    "gitattributes": lambda label: label == "prep:.gitattributes",
    "prep": lambda label: label == "prep:fsync",
    "phase_c": lambda label: label.startswith("C:"),
    "deletions": lambda label: label.startswith("delete:"),
}


def _open_and_close_store(path_str: str, barrier, result_queue) -> None:
    """Module-level (picklable under the 'spawn' start method) worker: wait for every
    sibling process to be ready, then race them all into ``Store(path_str)``."""
    try:
        barrier.wait(timeout=10)
        from sidegraph.store import Store

        store = Store(path_str)
        store.close()
        result_queue.put(("ok", None))
    except Exception as e:  # report every failure mode back to the parent, not just crash
        result_queue.put(("error", f"{type(e).__name__}: {e}"))


# -- drift-check files and packed lines ---------------------------------------------------------
# see design/superpowers/specs/2026-10-29-packed-drift-checks-design.md (§3, §4)


def check_body(cid: str, decision_id: str, **over: object) -> dict:
    """A drift-check body that passes ``DriftCheck.model_validate``."""
    commit = "a" * 40
    body: dict = {
        "schema_version": 1,
        "id": cid,
        "decision_id": decision_id,
        "checked_at": _WHEN,
        "checked_commit": commit,
        "provenance": {"source": "agent", "commit": commit},
        "rationale": "Still holds.",
        "required_files": ["code.py"],
        "checked_files": [{"path": "code.py", "blob_id": "b" * 40, "mode": "100644"}],
        "subject_digest": "c" * 64,
    }
    body.update(over)
    return body


def put_check(store: Path, body: dict, *, name: str | None = None) -> Path:
    """Write ``verifications/<name or id>.json`` as the writer does (``indent=2``)."""
    folder = store / "verifications"
    folder.mkdir(exist_ok=True)
    path = folder / f"{name or body['id']}.json"
    path.write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8")
    return path


def put_packed(store: Path, lines: Iterable[dict | bytes]) -> Path:
    """Write ``verifications/packed.shard``: a dict becomes its ``encode_body`` line, bytes
    are taken as a raw line."""
    folder = store / "verifications"
    folder.mkdir(exist_ok=True)
    path = folder / "packed.shard"
    path.write_bytes(
        b"".join(
            (encode_body(line).encode("utf-8") if isinstance(line, dict) else line) + b"\n"
            for line in lines
        )
    )
    return path


@pytest.fixture
def pin_version_0_22(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run as Sidegraph 0.22.0, the release that reads and folds packed drift checks."""
    import sidegraph

    monkeypatch.setattr(sidegraph, "__version__", "0.22.0")
