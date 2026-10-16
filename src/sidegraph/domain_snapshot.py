"""The committed domain-membership snapshot, ``.sidegraph/domain-snapshot.json``.

One store-level file that freezes which accepted domains each node belonged to when it was
taken, so an engine upgrade that regroups communities cannot move domain membership. Portable
core: it reads nodes as ``NodeRef`` and takes Graphify's label conventions from the reader
(``is_file_node``, ``UNSTABLE_LABEL_FILE_TYPES``). ``sync`` turns it into the membership
index; this module owns the format, its validation and the matching rules.

Matching a current node (D5.2):

1. In a listed file: its entry by engine node id; else by ``canonicalize(name)`` when that key
   is unique among the file's current nodes and among all its entries, and that entry's id
   matched no current node (never for a node whose label is unstable); else the file default.
   With no file: the ``stubs`` entry of its canonical name and file type, either the one set
   every such node had, or, for an entry kept per node, the node's own set by engine id.
2. Anywhere else (a file new since the snapshot, a path-less node no entry covers): the
   snapshot domains held by more than half of the listed-file nodes in its community
   (``sync`` adds the domains whose path prefixes cover its file).

Engine node ids are opaque hint keys here, backed by the name and the file default. Entries
belong to the covered domain's record and its ``supersedes`` lineage, never to a slug.
# see design/superpowers/specs/2026-10-14-graphify-upgrade-design.md (D5)
"""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    ValidationError,
    field_validator,
    model_validator,
)

from .engine.reader import UNSTABLE_LABEL_FILE_TYPES, NodeRef, is_file_node
from .schema import Domain, canonicalize
from .store import write_domain_snapshot_file
from .store_layout import DOMAIN_SNAPSHOT_NAME, read_snapshot_bytes, snapshot_signature_of

SNAPSHOT_FORMAT: Literal[1] = 1

# The first release that reads snapshots. Taking one raises the store's requirement to it, so
# an older Sidegraph refuses the store instead of showing the absorbed domains. A constant,
# not ``__version__``: a later release taking a snapshot must not lock out 0.14.x teammates.
SNAPSHOT_MIN_SIDEGRAPH = "0.14.0"

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


class SnapshotError(ValueError):
    """The snapshot file exists but cannot be read, or is not a valid format-1 snapshot.
    ``signature`` is that of the bytes that failed (``"unreadable"`` when none could be read):
    a membership pass records its failure under it."""

    def __init__(self, message: str, *, signature: str = "unknown") -> None:
        super().__init__(message)
        self.signature = signature


def _check_slugs(slugs: list[str]) -> list[str]:
    bad = [s for s in slugs if not _SLUG_RE.match(s)]
    if bad:
        raise ValueError(f"not a domain slug: {bad[0]!r}")
    if slugs != sorted(set(slugs)):
        raise ValueError("slugs must be sorted and unique")
    return slugs


class SnapshotNode(BaseModel):
    """A node whose domains differ from its file's default."""

    model_config = ConfigDict(extra="forbid")
    id: str
    name: str
    domains: list[str]

    @field_validator("domains")
    @classmethod
    def _slugs(cls, v: list[str]) -> list[str]:
        return _check_slugs(v)


class SnapshotFile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    domains: list[str]
    nodes: list[SnapshotNode] = []

    @field_validator("domains")
    @classmethod
    def _slugs(cls, v: list[str]) -> list[str]:
        return _check_slugs(v)

    @model_validator(mode="after")
    def _unique_ids(self) -> SnapshotFile:
        ids = [n.id for n in self.nodes]
        if len(ids) != len(set(ids)):
            raise ValueError("an entry id repeats within a file")
        return self


class SnapshotStubNode(BaseModel):
    """One path-less node of a ``stubs`` entry whose nodes had different sets."""

    model_config = ConfigDict(extra="forbid")
    id: str
    domains: list[str]

    @field_validator("domains")
    @classmethod
    def _slugs(cls, v: list[str]) -> list[str]:
        return _check_slugs(v)


class SnapshotStub(BaseModel):
    """The path-less anchorable nodes sharing one canonical name and file type (external
    stubs; stdlib concepts on newer engines): the one set they all had (``domains``; empty
    means they existed with no domain), or each node's set by engine id (``nodes``)."""

    model_config = ConfigDict(extra="forbid")
    name: str
    file_type: str
    domains: list[str] | None = None
    nodes: list[SnapshotStubNode] | None = None

    @field_validator("domains")
    @classmethod
    def _slugs(cls, v: list[str] | None) -> list[str] | None:
        return None if v is None else _check_slugs(v)

    @model_validator(mode="after")
    def _one_form(self) -> SnapshotStub:
        if (self.domains is None) == (self.nodes is None):
            raise ValueError("a stubs entry has either domains or nodes, not both")
        ids = [n.id for n in self.nodes or ()]
        if self.nodes is not None and (not ids or len(ids) != len(set(ids))):
            raise ValueError("a stubs entry lists each node once")
        return self

    def used_slugs(self) -> set[str]:
        if self.domains is not None:
            return set(self.domains)
        return set().union(*(n.domains for n in self.nodes or ()))


class SnapshotDomain(BaseModel):
    """A covered domain: its slug, and the id of the record that held it when the snapshot was
    taken. Only that record and its successors (``supersedes``) hold the entries."""

    model_config = ConfigDict(extra="forbid")
    slug: str
    id: str

    @field_validator("slug")
    @classmethod
    def _slug(cls, v: str) -> str:
        _check_slugs([v])
        return v


class DomainSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")
    format: Literal[1]
    taken_at: AwareDatetime
    engine_version: str | None
    engine_generation: str
    graph_version: str
    store_commit: str | None
    domains: list[SnapshotDomain]
    files: dict[str, SnapshotFile]
    stubs: list[SnapshotStub] = []

    @field_validator("format", mode="before")
    @classmethod
    def _integer_format(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("format must be integer 1")
        return value

    @model_validator(mode="after")
    def _consistent(self) -> DomainSnapshot:
        slugs = [d.slug for d in self.domains]
        if slugs != sorted(set(slugs)):
            raise ValueError("domains must be sorted by slug, each slug once")
        ids = [d.id for d in self.domains]
        if len(ids) != len(set(ids)):
            raise ValueError("a domain id repeats")
        covered = set(slugs)
        for path, entry in self.files.items():
            if not path or path.startswith("/") or ".." in PurePosixPath(path).parts:
                raise ValueError(f"not a repository-relative path: {path!r}")
            used = set(entry.domains).union(*(n.domains for n in entry.nodes))
            unknown = sorted(used - covered)
            if unknown:
                raise ValueError(f"{path}: {unknown[0]!r} is not one of the snapshot's domains")
        keys = [(e.name, e.file_type) for e in self.stubs]
        if keys != sorted(set(keys)):
            raise ValueError("stubs must be sorted by name and file type, each key once")
        for e in self.stubs:
            unknown = sorted(e.used_slugs() - covered)
            if unknown:
                raise ValueError(f"stubs: {unknown[0]!r} is not one of the snapshot's domains")
        return self

    def covered_slugs(self) -> list[str]:
        return [d.slug for d in self.domains]

    def covered_ids(self) -> dict[str, str]:
        """Covered record id -> its slug."""
        return {d.id: d.slug for d in self.domains}


def snapshot_path(store_dir: str | Path) -> Path:
    return Path(store_dir) / DOMAIN_SNAPSHOT_NAME


def parse_snapshot(text: str) -> DomainSnapshot:
    """Raises :class:`SnapshotError` naming the first problem."""
    try:
        return DomainSnapshot.model_validate_json(text)
    except ValidationError as e:
        first = e.errors()[0]
        where = ".".join(str(p) for p in first["loc"]) or "snapshot"
        raise SnapshotError(f"{where}: {first['msg']}") from None


def read_snapshot(store_dir: str | Path) -> tuple[DomainSnapshot | None, str]:
    """The store's snapshot and the signature of the very bytes it was parsed from
    (``"absent"`` without one): what a membership pass applies and certifies, read once.
    Raises :class:`SnapshotError`, carrying ``signature``, for a file that cannot be read or
    is not a valid format-1 snapshot."""
    try:
        data = read_snapshot_bytes(store_dir)
    except OSError as e:
        raise SnapshotError(f"not readable: {e}", signature="unreadable") from None
    signature = snapshot_signature_of(data)
    if data is None:
        return None, signature
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as e:
        raise SnapshotError(f"not UTF-8 text: {e}", signature=signature) from None
    try:
        return parse_snapshot(text), signature
    except SnapshotError as e:
        e.signature = signature
        raise


def load_snapshot(store_dir: str | Path) -> DomainSnapshot | None:
    """``None`` when the store has none; :class:`SnapshotError` for one that cannot be read
    or is not valid. For readers that certify nothing (advice, checks)."""
    return read_snapshot(store_dir)[0]


def snapshot_text(snap: DomainSnapshot) -> str:
    data = snap.model_dump(mode="json")
    # A stubs entry is written in its one form: the other field is left out, not null.
    data["stubs"] = [{k: v for k, v in e.items() if v is not None} for e in data["stubs"]]
    return json.dumps(data, sort_keys=True, indent=2, ensure_ascii=False) + "\n"


def stub_key(node: NodeRef) -> tuple[str, str]:
    """A path-less node's key in the ``stubs`` section: its canonical name and file type."""
    return canonicalize(node.name), node.file_type


def write_snapshot(store_dir: str | Path, snap: DomainSnapshot) -> int:
    """Write the snapshot atomically through ``store.write_domain_snapshot_file`` (the canonical
    writers live in ``store.py``); return its size in bytes."""
    return write_domain_snapshot_file(store_dir, snapshot_text(snap))


def _file_default(nodes: Sequence[NodeRef], sets: Mapping[str, frozenset[str]]) -> frozenset[str]:
    """The file node's set, else the most common set among the file's nodes, ties to the
    smallest in sorted order."""
    for n in nodes:
        if is_file_node(n):
            return sets[n.node_id]
    counts = Counter(sets[n.node_id] for n in nodes)
    top = max(counts.values())
    return min((s for s, k in counts.items() if k == top), key=lambda s: tuple(sorted(s)))


def build_snapshot(
    nodes: Iterable[NodeRef],
    members: Mapping[str, Iterable[str]],
    domain_ids: Mapping[str, str],
    *,
    taken_at: datetime,
    engine_version: str | None,
    engine_generation: str,
    graph_version: str,
    store_commit: str | None,
) -> DomainSnapshot:
    """The snapshot of ``members`` (covered slug -> member node ids) over ``nodes`` (the
    graph's anchorable nodes); ``domain_ids`` gives each covered slug's record id. Every file
    with an anchorable node is listed, an empty one too; every node whose set differs from its
    file's default gets an entry; every path-less node is under its ``stubs`` key. Reading it
    back over the same graph gives every node its set."""
    if set(members) != set(domain_ids):
        raise ValueError("every covered slug needs its domain id")
    nodes = list(nodes)
    held: dict[str, set[str]] = {}
    for slug, ids in members.items():
        for nid in ids:
            held.setdefault(nid, set()).add(slug)
    by_file: dict[str, list[NodeRef]] = {}
    for n in nodes:
        if n.file_path:
            by_file.setdefault(n.file_path, []).append(n)
    files: dict[str, SnapshotFile] = {}
    for path, file_nodes in sorted(by_file.items()):
        sets = {n.node_id: frozenset(held.get(n.node_id, ())) for n in file_nodes}
        default = _file_default(file_nodes, sets)
        files[path] = SnapshotFile(
            domains=sorted(default),
            nodes=[
                SnapshotNode(id=n.node_id, name=n.name, domains=sorted(sets[n.node_id]))
                for n in sorted(file_nodes, key=lambda n: n.node_id)
                if sets[n.node_id] != default
            ],
        )
    by_key: dict[tuple[str, str], list[NodeRef]] = {}
    for n in nodes:
        if not n.file_path:
            by_key.setdefault(stub_key(n), []).append(n)
    stubs: list[SnapshotStub] = []
    for (name, file_type), group in sorted(by_key.items()):
        stub_sets = {n.node_id: sorted(held.get(n.node_id, ())) for n in group}
        if len({tuple(v) for v in stub_sets.values()}) == 1:
            stubs.append(
                SnapshotStub(name=name, file_type=file_type, domains=next(iter(stub_sets.values())))
            )
        else:
            stubs.append(
                SnapshotStub(
                    name=name,
                    file_type=file_type,
                    nodes=[SnapshotStubNode(id=i, domains=stub_sets[i]) for i in sorted(stub_sets)],
                )
            )
    return DomainSnapshot(
        format=SNAPSHOT_FORMAT,
        taken_at=taken_at,
        engine_version=engine_version,
        engine_generation=engine_generation,
        graph_version=graph_version,
        store_commit=store_commit,
        domains=[SnapshotDomain(slug=s, id=domain_ids[s]) for s in sorted(members)],
        files=files,
        stubs=stubs,
    )


@dataclass(frozen=True)
class Recorded:
    """Each node's snapshot domains, slugs as recorded (``sync`` maps them to live domains),
    and ``listed``: the nodes matched in a listed file, which are the voters and the nodes rule
    4 never reaches. A path-less node the ``stubs`` section covers has its set but is not
    listed. Every other node got the vote."""

    sets: dict[str, frozenset[str]]
    listed: frozenset[str]


def _match_listed(file_nodes: Sequence[NodeRef], entry: SnapshotFile) -> dict[str, frozenset[str]]:
    by_id = {e.id: e for e in entry.nodes}
    current = {n.node_id for n in file_nodes}
    node_keys = Counter(canonicalize(n.name) for n in file_nodes)
    entry_keys = Counter(canonicalize(e.name) for e in entry.nodes)
    # Eligible for a name match: an entry whose id matched no current node. Uniqueness is
    # counted over ALL entries, so a name two entries share never matches.
    unmatched = {canonicalize(e.name): e for e in entry.nodes if e.id not in current}
    default = frozenset(entry.domains)
    out: dict[str, frozenset[str]] = {}
    for n in file_nodes:
        hit = by_id.get(n.node_id)
        if hit is None and n.file_type not in UNSTABLE_LABEL_FILE_TYPES:
            key = canonicalize(n.name)
            if node_keys[key] == 1 and entry_keys[key] == 1:
                hit = unmatched.get(key)
        out[n.node_id] = frozenset(hit.domains) if hit is not None else default
    return out


def recorded_sets(nodes: Sequence[NodeRef], snap: DomainSnapshot) -> Recorded:
    """D5.2 rules 1 and 2 over ``nodes``, the graph's anchorable nodes, path-less ones
    included. A path-less node takes its ``stubs`` entry's set, or its own set by id from an
    entry kept per node. The vote counts listed-file nodes in the node's (type-split)
    community, empty sets included; a domain wins with more than half of them; no voters, no
    domain."""
    by_file: dict[str, list[NodeRef]] = {}
    for n in nodes:
        if n.file_path and n.file_path in snap.files:
            by_file.setdefault(n.file_path, []).append(n)
    sets: dict[str, frozenset[str]] = {}
    for path, file_nodes in by_file.items():
        sets.update(_match_listed(file_nodes, snap.files[path]))
    listed = frozenset(sets)
    stubs = {(e.name, e.file_type): e for e in snap.stubs}
    for n in nodes:
        entry = None if n.file_path else stubs.get(stub_key(n))
        if entry is None:
            continue
        if entry.domains is not None:
            sets[n.node_id] = frozenset(entry.domains)
        else:
            hit = next((x for x in entry.nodes or () if x.id == n.node_id), None)
            if hit is not None:
                sets[n.node_id] = frozenset(hit.domains)
    voters: dict[str, list[frozenset[str]]] = {}
    for n in nodes:
        if n.node_id in listed and n.community is not None:
            voters.setdefault(n.community, []).append(sets[n.node_id])
    for n in nodes:
        if n.node_id in sets:
            continue
        votes = voters.get(n.community, []) if n.community is not None else []
        tally = Counter(s for v in votes for s in v)
        sets[n.node_id] = frozenset(s for s, k in tally.items() if 2 * k > len(votes))
    return Recorded(sets, listed)


def snapshot_slug_of(
    domain: Domain, covered_ids: Mapping[str, str], get_domain: Callable[[str], Domain | None]
) -> str | None:
    """The covered slug ``domain`` holds entries for: its own record's when covered, else the
    first covered record along its ``supersedes`` chain (a supersede, or a rename). A domain
    that merely reuses a slug holds nothing."""
    seen: set[str] = set()
    current: Domain | None = domain
    while current is not None and current.domain_id not in seen:
        if current.domain_id in covered_ids:
            return covered_ids[current.domain_id]
        seen.add(current.domain_id)
        current = get_domain(current.supersedes) if current.supersedes else None
    return None


def snapshot_heirs(
    snap: DomainSnapshot, accepted: Sequence[Domain], get_domain: Callable[[str], Domain | None]
) -> tuple[dict[str, str], dict[str, list[str]]]:
    """``(domain_id -> covered slug it holds, split slug -> its accepted heirs' ids)``. A slug
    two or more accepted domains descend from is a split: none of them holds it until
    ``snapshot --refresh`` names them."""
    covered_ids = snap.covered_ids()
    reach: dict[str, list[str]] = {}
    for d in accepted:
        slug = snapshot_slug_of(d, covered_ids, get_domain)
        if slug is not None:
            reach.setdefault(slug, []).append(d.domain_id)
    heirs = {ids[0]: slug for slug, ids in reach.items() if len(ids) == 1}
    splits = {slug: sorted(ids) for slug, ids in reach.items() if len(ids) > 1}
    return heirs, splits


def file_counts(snap: DomainSnapshot) -> dict[str, int]:
    """Per covered slug, the files with at least one node in it."""
    counts = dict.fromkeys(snap.covered_slugs(), 0)
    for entry in snap.files.values():
        for slug in set(entry.domains).union(*(n.domains for n in entry.nodes)):
            counts[slug] += 1
    return counts


def supersede_note(
    store_dir: str | Path, old: Domain, new_slug: str, get_domain: Callable[[str], Domain | None]
) -> str | None:
    """The sentence a supersede answer adds when ``old``'s membership is frozen, or None."""
    try:
        snap = load_snapshot(store_dir)
    except (SnapshotError, OSError):
        return None
    if snap is None:
        return None
    slug = snapshot_slug_of(old, snap.covered_ids(), get_domain)
    if slug is None:
        return None
    return (
        f"{old.slug}'s membership is frozen in the domain snapshot (as {slug}) and carries "
        f"over to {new_slug} once it is accepted; its new rules apply only after "
        f"`sidegraph-domains snapshot --refresh --domain {new_slug}`. If this supersede "
        "splits the domain, refresh every part the same way"
    )
