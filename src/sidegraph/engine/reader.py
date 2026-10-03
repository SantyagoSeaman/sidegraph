"""``GraphifyReader`` — the engine seam: the ONLY module that parses Graphify's graph.json.

graph.json-only (no Graphify MCP): resolve/neighbors/communities/subgraph are computed
locally from the node-link graph. Read-only — never writes graph.json. Version is never
mtime: it's the embedded ``built_at_commit`` field with a content hash always folded in
(``f"{commit}:{hash}"``), or just the content hash (``f"content:{hash}"``) when
``built_at_commit`` is absent — folding the hash in even when a commit is present is what
catches dirty-tree rebuilds that change graph.json without changing the commit. See
``docs/integrations/graphify.md``.

``community_labels()`` additionally reads the engine's optional ``.graphify_labels.json``
sidecar (written by Graphify's cluster-only/label commands, sibling to ``graph.json``) —
this module is the ONLY place allowed to know that file's name/shape (see
``docs/concepts/mind-model.md#domain-lifecycle``).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import subprocess
import time
from collections import deque
from pathlib import Path, PurePosixPath

from pydantic import BaseModel

from ..config import borrowed_graph_path, main_checkout_root
from ..freshness import GraphFreshness
from ..gitenv import git_env
from ..schema import Descriptor, canonicalize, strip_decoration

# Node file types that can carry anchors. Concepts (LLM-extracted thematic entities) and
# rationale nodes (recorded reasoning, code AST or LLM prose) anchor exactly like code
# symbols / doc headings: name+file descriptor, Tier-2 leaf + Tier-1 community. `image`
# stays out — not name-anchorable — never-guess extends to file types.
ANCHORABLE_FILE_TYPES = frozenset({"code", "document", "concept", "rationale"})

# Sidecar written by Graphify's cluster-only/label commands, sibling to graph.json:
# {"<community_id>": "<human label>", ...}. Optional — absent on graphs built without a
# labeling pass (verified empirically: present on code/adr/doc corpora rebuilt with
# `graphify update --cluster-only`/label commands, absent on a plain semantic-only build).
_LABELS_FILENAME = ".graphify_labels.json"

# detect_subdir_mismatch: minimum distinct anchorable source_file values before judging a
# mismatch at all -- a graph with fewer is too small a corpus (a fixture, a tiny doc-only
# build) to tell "ran from a subdirectory" apart from "small/partial" safely.
_SUBDIR_MISMATCH_MIN_SAMPLE = 8

# detect_subdir_mismatch: fraction of the sample that must be missing relative to the repo
# root before even considering a subdir-run mismatch. 90%, not 100% -- a real repo can have
# a coincidental repo-root-relative collision (a vendored copy, a symlink) for one sampled
# path, and demanding unanimous absence would let that one collision defeat the whole check.
_SUBDIR_MISMATCH_THRESHOLD = 0.9

# freshness(): the only build-commit values worth handing to git. A full SHA-1 or SHA-256 id;
# anything shorter or ref-like (`abc123`, `v1`, `-x`) could resolve to a different commit, or be
# read by git as an option, so it reads as "unknown" instead.
_FULL_COMMIT_RE = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")

# Sibling of graph.json that Graphify rewrites on every `update`, including one that finds the
# topology unchanged and leaves graph.json itself untouched. Only freshness() reads it, for
# its mtime; like the labels sidecar, this module is the one place that knows its name.
_MANIFEST_FILENAME = "manifest.json"

# freshness(): how many of the changed paths a stale verdict names (GraphFreshness.sample).
_FRESHNESS_SAMPLE = 5

# Bumped whenever ``resolve()`` starts answering differently for the SAME graph, so a store
# stamped by an older resolver reruns its sync once and heals what the old rules left orphaned.
# Folded into :meth:`GraphifyReader.sync_stamp`, never into ``graph_version()`` (which is
# provenance). 2: ``Type.member`` falls back to the member's own node.
# see design/superpowers/specs/2026-10-01-member-anchor-names-design.md (D2)
RESOLVER_REVISION = 2

# A stamp's resolver-revision suffix (:meth:`GraphifyReader.sync_stamp`). The content hash is hex,
# so an ``r`` after the last colon can only be the revision.
_REVISION_SUFFIX_RE = re.compile(r":r\d+$")

# The two edge relations by which Graphify links a type to what it owns: ``method`` (type ->
# method) and ``case_of`` (enum -> case). Source is the owner, target the member.
_OWNER_RELATIONS = frozenset({"method", "case_of"})

# ``Type.member``, ``Type::member`` and ``Type#member`` are the three spellings of "member of".
_MEMBER_SEPARATOR_RE = re.compile(r"::|[.#]")


class NodeRef(BaseModel):
    node_id: str
    name: str
    norm_name: str
    file_type: str
    file_path: str | None = None
    line: str | None = None
    community: str | None = None


class Community(BaseModel):
    community_id: str
    members: list[str]
    god_node: str | None = None


class Subgraph(BaseModel):
    nodes: list[NodeRef]
    edges: list[tuple[str, str, str]]


class ResolveResult(BaseModel):
    status: str  # "resolved" | "ambiguous" | "unresolved"
    node_id: str | None = None
    candidates: list[str] = []
    community: str | None = None


class _Unknown(Exception):
    """Internal to ``GraphifyReader.freshness``: a step could not decide; carries the reason."""


class RationaleNode(BaseModel):
    node_id: str
    text: str  # the node's label — engine truncates ~80 chars, that's fine
    file_path: str | None = None
    community: str | None = None
    targets: list[NodeRef] = []  # what this rationale explains


def _strip_generics(name: str) -> str:
    """Drop every balanced ``<...>`` group (``Stack<Element>`` -> ``Stack``). An unclosed
    ``<`` swallows the rest, which leaves the name without a member and so unresolved."""
    out: list[str] = []
    depth = 0
    for ch in name:
        if ch == "<":
            depth += 1
        elif ch == ">" and depth:
            depth -= 1
        elif not depth:
            out.append(ch)
    return "".join(out)


def _split_member(name: str) -> tuple[str, str] | None:
    """``(owner, member)`` of a qualified name, or ``None`` for a bare or degenerate one.

    Generic parameters and the trailing call decoration go first, then the name is split at its
    last separator; the owner is narrowed to its own last segment (``Outer.Inner.m`` -> owner
    ``Inner``, member ``m``). Both sides must be non-empty.
    """
    parts = _MEMBER_SEPARATOR_RE.split(strip_decoration(_strip_generics(name)))
    if len(parts) < 2:
        return None
    owner, member = parts[-2].strip(), parts[-1].strip()
    return (owner, member) if owner and member else None


def _to_node(raw: dict) -> NodeRef:
    community = raw.get("community")
    return NodeRef(
        node_id=str(raw.get("id")),
        name=str(raw.get("label", "")),
        norm_name=str(raw.get("norm_label", raw.get("label", ""))),
        file_type=str(raw.get("file_type", "")),
        file_path=raw.get("source_file"),
        line=raw.get("source_location"),
        community=None if community is None else str(community),
    )


class GraphifyReader:
    """Reads ``graphify-out/graph.json`` read-only and answers graph queries locally."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._raw = self._load()
        self._nodes = [_to_node(n) for n in self._raw.get("nodes", [])]
        self._by_id = {n.node_id: n for n in self._nodes}
        self._links = self._raw.get("links", [])
        # Lazy, memoized indices — each is built at most once, from a single O(V)/O(E) pass,
        # the first time its owning method is called (not in __init__: a reader that never
        # calls e.g. resolve() shouldn't pay for an index it never needs). See neighbors(),
        # resolve(), nodes_in_file() below — each used to re-scan self._links/self._nodes in
        # full on EVERY call, which is fine for a single lookup but catastrophic for callers
        # that loop over many nodes (e.g. rationale_nodes() calling neighbors() once per
        # rationale node, or import_rationales() calling resolve() per anchor candidate): on
        # a 113K-node/232K-edge graph with 23K rationale nodes this was billions of
        # comparisons in pure Python and never returned. Building the index once turns each
        # of those into O(degree)/O(matches) per call instead of O(E)/O(V).
        self._adjacency: dict[str, list[tuple[str, str]]] | None = None
        self._resolve_idx: dict[str, list[NodeRef]] | None = None
        self._file_idx: dict[str, list[NodeRef]] | None = None
        self._owner_idx: dict[str, list[str]] | None = None
        self._source_files: frozenset[str] | None = None

    def _load(self, retries: int = 3) -> dict:
        last: Exception | None = None
        for _ in range(max(1, retries)):
            text = self.path.read_text()
            try:
                raw = json.loads(text)
            except json.JSONDecodeError as e:  # graph.json write is non-atomic
                last = e
                time.sleep(0.2)
                continue
            self._content_hash = hashlib.sha256(text.encode()).hexdigest()[:12]
            return raw
        raise last  # type: ignore[misc]

    def graph_version(self) -> str:
        """``f"{commit}:{content_hash}"`` when the engine stamped a ``built_at_commit``,
        else ``f"content:{content_hash}"``. The content hash is folded in even when a commit
        is present: Graphify rewrites graph.json without bumping built_at_commit whenever the
        working tree is dirty (e.g. an uncommitted rename), and a bare commit would then miss
        that the content changed and let sync report "up to date" while serving stale anchors.
        One-time upgrade note: stores written before this change stamped
        ``last_synced_graph_version`` in the old bare-commit format, so the first sync after
        upgrading sees a differing version string and reruns once — self-healing, and intended.
        """
        commit = self._raw.get("built_at_commit")
        if commit:
            return f"{commit}:{self._content_hash}"
        return f"content:{self._content_hash}"

    def sync_stamp(self) -> str:
        """What ``sync`` stamps and compares: :meth:`graph_version` plus the resolver revision.

        ``graph_version()`` stays the provenance value records carry; this one also changes
        when ``resolve()`` learns to answer differently about the same graph, so the first
        sync after such an upgrade reruns once and heals what the older rules left orphaned.
        The stamp lives in the gitignored index, so the store format is unchanged.
        # see design/superpowers/specs/2026-10-01-member-anchor-names-design.md (D2)
        """
        return f"{self.graph_version()}:r{RESOLVER_REVISION}"

    @staticmethod
    def without_revision(stamp: str | None) -> str | None:
        """``stamp`` without its resolver-revision suffix: the plain graph version, which is
        what a sync report shows on both sides. ``None`` and a stamp written before the
        revision existed come back unchanged."""
        return None if stamp is None else _REVISION_SUFFIX_RE.sub("", stamp)

    def built_at_commit(self) -> str | None:
        """The raw ``built_at_commit`` field: the HEAD Graphify ran at. ``None`` when it is
        absent or not a non-empty string. Engine-seam accessor: nothing outside ``engine/``
        reads the field.
        # see design/superpowers/specs/2026-10-01-stale-graph-visible-design.md (D1)
        """
        commit = self._raw.get("built_at_commit")
        return commit if isinstance(commit, str) and commit else None

    def source_files(self) -> frozenset[str]:
        """Every node's ``source_file``, of every file type (not only anchorable ones: an
        image or a config file the graph holds is still a file the graph holds), built once
        (memoized). Backs :meth:`freshness` and the "not in the code graph" answer.
        # see design/superpowers/specs/2026-10-01-stale-graph-visible-design.md (D1)
        """
        if self._source_files is None:
            self._source_files = frozenset(n.file_path for n in self._nodes if n.file_path)
        return self._source_files

    def list_nodes(self) -> list[NodeRef]:
        return list(self._nodes)

    def get_node(self, node_id: str) -> NodeRef | None:
        return self._by_id.get(node_id)

    def _file_index(self) -> dict[str, list[NodeRef]]:
        """``file_path -> anchorable nodes`` in that file, built once from a single pass
        over ``self._nodes`` (memoized). Backs ``nodes_in_file()``."""
        if self._file_idx is None:
            idx: dict[str, list[NodeRef]] = {}
            for n in self._nodes:
                if n.file_type in ANCHORABLE_FILE_TYPES and n.file_path is not None:
                    idx.setdefault(n.file_path, []).append(n)
            self._file_idx = idx
        return self._file_idx

    def nodes_in_file(self, file_path: str) -> list[NodeRef]:
        """All anchorable nodes (code symbols / doc headings) whose source_file matches."""
        return list(self._file_index().get(file_path, []))

    def _resolve_index(self) -> dict[str, list[NodeRef]]:
        """``canonicalize(name) -> anchorable nodes`` with that canonical name, built once
        from a single pass over ``self._nodes`` (memoized). Backs ``resolve()``."""
        if self._resolve_idx is None:
            idx: dict[str, list[NodeRef]] = {}
            for n in self._nodes:
                if n.file_type in ANCHORABLE_FILE_TYPES:
                    idx.setdefault(canonicalize(n.name), []).append(n)
            self._resolve_idx = idx
        return self._resolve_idx

    def resolve(self, desc: Descriptor) -> ResolveResult:
        target = canonicalize(desc.name)
        matches = list(self._resolve_index().get(target, []))
        if desc.file_path is not None:
            matches = [n for n in matches if n.file_path == desc.file_path]
        if len(matches) == 1:
            m = matches[0]
            return ResolveResult(status="resolved", node_id=m.node_id, community=m.community)
        if not matches:
            return self._resolve_member(desc)
        comms = {m.community for m in matches if m.community is not None}
        shared = comms.pop() if len(comms) == 1 else None
        return ResolveResult(
            status="ambiguous",
            candidates=[m.node_id for m in matches],
            community=shared,
        )

    def _owner_index(self) -> dict[str, list[str]]:
        """``member node_id -> owner node_ids`` over the ``method`` / ``case_of`` edges, read in
        the engine's direction (source owns target), built once (memoized). Backs
        :meth:`_resolve_member`; deliberately directed, because a type that has methods must
        not read as owned by them."""
        if self._owner_idx is None:
            idx: dict[str, list[str]] = {}
            for link in self._links:
                if self._relation(link) in _OWNER_RELATIONS:
                    idx.setdefault(str(link.get("target")), []).append(str(link.get("source")))
            self._owner_idx = idx
        return self._owner_idx

    def _resolve_member(self, desc: Descriptor) -> ResolveResult:
        """Fallback for a name with no exact match: ``Type.member`` -> the member's own node.

        Graphify labels a member ``.playClip()`` and links it to its type; agents write
        ``AudioPlayback.playClip``. It is for code identifiers only: the owner's last segment
        and the member must both be identifiers, and a candidate must be a ``code`` node, so
        prose with a dot in it, a numbered heading (``8. Testing``) or a file name
        (``verify-release.sh``) never splits into a "member" of something. Needs a file
        (without one the member name alone matches across the whole repository, which is a
        guess). Candidates are the nodes in that file whose label, decoration stripped and
        case KEPT, is the member. A node whose label differs from the member only by case (a
        struct ``Message`` beside a method ``.message()``) makes the whole answer
        ``unresolved``: the store dedups an anchor by its lowercased name, so the two spellings
        share one entity and binding either node would put the other's record on it. A
        candidate an owner edge ties to a type other than the named one (compared with case
        kept) is rejected, even when it is alone; of several, only those tied to the named owner
        stay. Whatever does not narrow to exactly one is ``unresolved``, never ``ambiguous``:
        that keeps the write path's orphaned, healable leaf.
        # see design/superpowers/specs/2026-10-01-member-anchor-names-design.md (D1)
        """
        unresolved = ResolveResult(status="unresolved")
        if not desc.file_path:
            return unresolved
        split = _split_member(desc.name)
        if split is None:
            return unresolved
        owner, member = split
        if not (owner.isidentifier() and member.isidentifier()):
            return unresolved
        in_file = self._file_index().get(desc.file_path, [])
        for n in in_file:
            label = strip_decoration(n.name)
            if label != member and label.lower() == member.lower():
                return unresolved
        owners = self._owner_index()
        kept: list[NodeRef] = []
        tied: list[NodeRef] = []  # kept, and tied by an owner edge to `owner` in this file
        for n in in_file:
            if n.file_type != "code" or strip_decoration(n.name) != member:
                continue
            owner_nodes = [o for oid in owners.get(n.node_id, []) if (o := self._by_id.get(oid))]
            if any(strip_decoration(o.name) != owner for o in owner_nodes):
                continue
            kept.append(n)
            if any(o.file_path == desc.file_path for o in owner_nodes):
                tied.append(n)
        chosen = kept if len(kept) == 1 else tied
        if len(chosen) != 1:
            return unresolved
        return ResolveResult(
            status="resolved", node_id=chosen[0].node_id, community=chosen[0].community
        )

    def resolve_member_name(self, name: str) -> ResolveResult:
        """``Type.member`` with no file: the member's own node(s), found through the owner edge.

        For a seed that names a member and nothing else (:meth:`_resolve_member` needs a file).
        Member forms only (``Type.member``, ``Type::member``, ``Type#member``, generics and call
        decoration stripped): a plain name already resolves through ``resolve(Descriptor(name,
        None))``. A candidate is a ``code`` node whose label, decoration stripped and case KEPT,
        is the member, and which an owner edge ties to a node labelled (case kept) the named
        type; the owner and the member must both be identifiers. A candidate whose own file
        holds a node that differs from the member only by case is dropped: the store dedups an
        anchor by its lowercased name, so binding either spelling would put the other's record
        on it. The rule is per candidate file, so a twin elsewhere changes nothing.
        One survivor is ``resolved``, several are ``ambiguous``, none is ``unresolved``.
        ``resolve()`` and anchoring are unchanged, so ``RESOLVER_REVISION`` does not move.
        # see design/superpowers/specs/2026-10-02-tolerant-seeds-design.md (D2)
        """
        unresolved = ResolveResult(status="unresolved")
        split = _split_member(name)
        if split is None:
            return unresolved
        owner, member = split
        if not (owner.isidentifier() and member.isidentifier()):
            return unresolved
        same_name = self._resolve_index().get(member.lower(), [])
        twin_files = {n.file_path for n in same_name if strip_decoration(n.name) != member}
        owners = self._owner_index()
        found: list[NodeRef] = []
        for n in same_name:
            if n.file_type != "code" or strip_decoration(n.name) != member:
                continue
            if not n.file_path or n.file_path in twin_files:
                continue
            owner_nodes = [o for oid in owners.get(n.node_id, []) if (o := self._by_id.get(oid))]
            if any(strip_decoration(o.name) == owner for o in owner_nodes):
                found.append(n)
        if not found:
            return unresolved
        if len(found) == 1:
            return ResolveResult(
                status="resolved", node_id=found[0].node_id, community=found[0].community
            )
        comms = {n.community for n in found if n.community is not None}
        return ResolveResult(
            status="ambiguous",
            candidates=[n.node_id for n in found],
            community=comms.pop() if len(comms) == 1 else None,
        )

    def _relation(self, link: dict) -> str:
        return str(link.get("relation") or link.get("type") or "")

    def _adjacency_index(self) -> dict[str, list[tuple[str, str]]]:
        """``node_id -> [(other_node_id, relation), ...]`` across both edge endpoints,
        built once from a single pass over ``self._links`` (memoized). Backs ``neighbors()``.

        Mirrors the per-node-id "which end is the other one" derivation the old per-call
        scan used, so that iterating ``adjacency[node_id]`` yields exactly the (other,
        relation) pairs a full scan filtered to ``node_id`` would have yielded, in the same
        link order — including a self-loop edge (source == target) contributing exactly one
        entry, same as before.
        """
        if self._adjacency is None:
            adj: dict[str, list[tuple[str, str]]] = {}
            for link in self._links:
                rel = self._relation(link)
                s, t = str(link.get("source")), str(link.get("target"))
                adj.setdefault(s, []).append((t, rel))
                if t != s:
                    adj.setdefault(t, []).append((s, rel))
            self._adjacency = adj
        return self._adjacency

    def neighbors(self, node_id: str, relations: list[str] | None = None) -> list[NodeRef]:
        out: list[NodeRef] = []
        seen: set[str] = set()
        for other, rel in self._adjacency_index().get(node_id, []):
            if relations is not None and rel not in relations:
                continue
            if other in seen:
                continue
            node = self._by_id.get(other)
            if node is not None:
                seen.add(other)
                out.append(node)
        return out

    def rationale_nodes(self) -> list[RationaleNode]:
        """All ``rationale`` nodes (recorded reasoning), with targets resolved.

        This is the ONLY place that knows the ``rationale_for`` (code AST pass: rationale
        --rationale_for--> code, verified empirically 492/492 edges on a real corpus) and
        ``references`` (LLM prose pass, same direction) relation names. ``neighbors()``
        already resolves "the other end of the edge" regardless of source/target order, so
        this stays defensive to either orientation without extra bookkeeping. Falls back to
        ``references`` only when a node has zero ``rationale_for`` edges; non-anchorable
        targets (e.g. ``image``) are dropped from the result either way.
        """
        out: list[RationaleNode] = []
        for n in self._nodes:
            if n.file_type != "rationale":
                continue
            targets = self.neighbors(n.node_id, relations=["rationale_for"])
            if not targets:
                targets = self.neighbors(n.node_id, relations=["references"])
            targets = [t for t in targets if t.file_type in ANCHORABLE_FILE_TYPES]
            out.append(
                RationaleNode(
                    node_id=n.node_id,
                    text=n.name,
                    file_path=n.file_path,
                    community=n.community,
                    targets=targets,
                )
            )
        return out

    def containing(self, node_id: str) -> str | None:
        node = self._by_id.get(node_id)
        return node.community if node is not None else None

    def communities(self) -> list[Community]:
        degree: dict[str, int] = {}
        for link in self._links:
            for end in (str(link.get("source")), str(link.get("target"))):
                degree[end] = degree.get(end, 0) + 1
        groups: dict[str, list[str]] = {}
        for n in self._nodes:
            if n.community is not None:
                groups.setdefault(n.community, []).append(n.node_id)
        out: list[Community] = []
        for cid, members in groups.items():
            god = max(members, key=lambda m: degree.get(m, 0)) if members else None
            out.append(Community(community_id=cid, members=members, god_node=god))
        return out

    def community_labels(self) -> dict[str, str]:
        """Community id -> human label/summary, from the optional ``.graphify_labels.json``
        sidecar next to ``graph.json`` (see module docstring). Purely additive: bootstrap
        falls back to a god-node-derived name when a community has no entry here, or when
        this method returns ``{}`` outright (no labeling pass run on this corpus).

        Defensive by construction — a missing file, unreadable/malformed JSON, or a
        non-dict payload all yield ``{}`` rather than raising, since this is best-effort
        engine metadata, not a required input (unlike ``graph.json`` itself, whose read
        failures ARE fatal in ``__init__``). Non-string values are dropped rather than
        coerced, since a label is display text, not just anything JSON allows here.
        Whitespace-only strings are dropped too: a blank label is worthless as a title
        source and, left in, crashes a caller like ``bootstrap_domains`` downstream
        (``Domain.title`` rejects blank text) — better every caller sees "no label here"
        and falls back cleanly.
        """
        label_path = self.path.parent / _LABELS_FILENAME
        if not label_path.exists():
            return {}
        try:
            raw = json.loads(label_path.read_text())
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(raw, dict):
            return {}
        return {str(k): v for k, v in raw.items() if isinstance(v, str) and v.strip()}

    def subgraph(self, seed_ids: list[str], budget: int) -> Subgraph:
        adj: dict[str, list[tuple[str, str]]] = {}
        for link in self._links:
            s, t = str(link.get("source")), str(link.get("target"))
            rel = self._relation(link)
            adj.setdefault(s, []).append((t, rel))
            adj.setdefault(t, []).append((s, rel))
        picked: dict[str, NodeRef] = {}
        q: deque[str] = deque()
        for sid in seed_ids:
            if sid in self._by_id and sid not in picked:
                picked[sid] = self._by_id[sid]
                q.append(sid)
                if len(picked) >= budget:
                    break
        while q and len(picked) < budget:
            cur = q.popleft()
            for other, _rel in adj.get(cur, []):
                if other not in picked and other in self._by_id:
                    picked[other] = self._by_id[other]
                    q.append(other)
                    if len(picked) >= budget:
                        break
        keep = set(picked)
        edges = [
            (str(link.get("source")), str(link.get("target")), self._relation(link))
            for link in self._links
            if str(link.get("source")) in keep and str(link.get("target")) in keep
        ]
        return Subgraph(nodes=list(picked.values()), edges=edges)

    def changed_files(self, since: str | None = None) -> list[str]:
        """Repo-relative paths changed in the last commit (git diff HEAD~1 HEAD).

        Uses the repo containing graph.json. Best-effort: returns [] on any git error.
        """
        repo = self.path.parent.parent
        try:
            out = subprocess.run(
                ["git", "-C", str(repo), "diff", "--name-only", "HEAD~1", "HEAD"],
                capture_output=True,
                text=True,
            )
            return [p for p in out.stdout.split("\n") if p] if out.returncode == 0 else []
        except (OSError, subprocess.SubprocessError):
            return []

    def repo_root(self, timeout: float = 5.0) -> Path | None:
        """Best-effort git worktree root containing THIS reader's graph.json —
        ``git rev-parse --show-toplevel`` run from ``self.path.parent``, never raising.
        ``None`` when graph.json sits outside any git working tree, or git itself is
        unavailable (or slower than ``timeout`` seconds, which :meth:`freshness` passes
        as the time left on its one deadline).

        Deliberately keyed off graph.json's OWN location, not any other artifact (e.g. a
        decision store, which is routinely copied elsewhere for safe inspection and would
        silently report "not a git repo" for the copy even when the real checkout is right
        there) — every caller needing a git-relative fact about this reader's node
        ``file_path``s (``sync.py``'s moved-rung dirty-tree guard, ``detect_subdir_mismatch``
        below) resolves its repo root through this one method rather than inventing a
        second lookup.
        """
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--show-toplevel"],
                cwd=self.path.parent,
                env=git_env(),
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if result.returncode != 0:
            return None
        return Path(result.stdout.strip()).resolve()

    def freshness(self, deadline: float = 3.0, root: Path | None = None) -> GraphFreshness:
        """Is the graph current? ``built_at_commit`` set against HEAD, never raising.

        ``stale`` means a file the graph should reflect changed since the build and the
        graph does not reflect it; ``commits_behind`` is reported and never decides that (a
        commit touching only file types the graph does not hold must not nag). ``unknown``
        (with a ``reason``) when the build commit is absent or not a full id, graph.json is
        outside a git repository, git cannot run or outruns ``deadline`` (one shared budget
        for every git call here), or the repository lacks the build commit. ``root`` is the
        repository root when the caller already holds it (from :meth:`repo_root`), which
        saves that one git call.

        Every git call runs from graph.json's own directory, so the paths ``git diff`` prints
        (``--no-relative``: repository-root relative whatever the graph's depth) are directly
        comparable with node ``source_file`` values. Graphify stamps the HEAD it ran at and
        indexes the working tree, so a changed file the graph holds whose working-tree copy
        is no newer than the build is already reflected and does not count. The build time
        is the later of graph.json's mtime and the sibling ``manifest.json``'s: a rebuild
        that finds the topology unchanged leaves graph.json (and its stamp) untouched but
        still rewrites the manifest.
        # see design/superpowers/specs/2026-10-01-stale-graph-visible-design.md (D2)
        """
        try:
            return self._freshness(deadline, root)
        except _Unknown as e:
            return GraphFreshness(state="unknown", reason=str(e))
        except Exception:
            return GraphFreshness(state="unknown", reason="the comparison with HEAD failed")

    def _freshness(self, deadline: float, root: Path | None) -> GraphFreshness:
        """The body of :meth:`freshness`; raises :class:`_Unknown` where it cannot decide."""
        end = time.monotonic() + deadline

        def git(*args: str) -> subprocess.CompletedProcess[bytes]:
            left = end - time.monotonic()
            if left <= 0:
                raise _Unknown("git timed out")
            try:
                return subprocess.run(
                    ["git", *args],
                    cwd=self.path.parent,
                    env=git_env(),
                    capture_output=True,
                    timeout=left,
                )
            except subprocess.TimeoutExpired:
                raise _Unknown("git timed out") from None
            except OSError:
                raise _Unknown("git could not be run") from None

        built = self.built_at_commit()
        if built is None:
            raise _Unknown("graph.json records no build commit")
        if not _FULL_COMMIT_RE.fullmatch(built):
            raise _Unknown("graph.json's build commit is not a full commit id")

        if root is None:
            left = end - time.monotonic()
            root = self.repo_root(timeout=left) if left > 0 else None
        if root is None:
            if end <= time.monotonic():
                raise _Unknown("git timed out")
            raise _Unknown("graph.json is not inside a git repository")

        head_run = git("rev-parse", "HEAD")
        head = head_run.stdout.decode().strip()
        if head_run.returncode != 0 or not head:
            raise _Unknown("the repository has no commits")
        if built == head:
            return GraphFreshness(
                state="fresh", built_at=built, head=head, in_history=True, commits_behind=0
            )
        if git("cat-file", "-e", f"{built}^{{commit}}").returncode != 0:
            raise _Unknown(f"built at {built[:7]}, a commit this repository does not have")

        ancestor = git("merge-base", "--is-ancestor", built, "HEAD").returncode
        commits_behind: int | None = None
        if ancestor == 0:
            count = git("rev-list", "--count", f"{built}..HEAD")
            if count.returncode != 0:
                raise _Unknown("git could not count the commits since the build")
            commits_behind = int(count.stdout.decode().strip())
        elif ancestor != 1:
            raise _Unknown("git could not relate the build commit to HEAD")

        diff = git(
            "diff", "--name-only", "--no-relative", "--no-renames", "-z", built, "HEAD", "--"
        )
        if diff.returncode != 0:
            raise _Unknown("git diff failed")
        changed = [os.fsdecode(p) for p in diff.stdout.split(b"\0") if p]

        held = self.source_files()
        # No extension is not an extension: an extensionless file in the graph (a Makefile)
        # must not make every extensionless change look indexable.
        extensions = {PurePosixPath(p).suffix for p in held} - {""}
        build_time = self.path.stat().st_mtime_ns
        # No manifest beside graph.json (OSError): graph.json's own mtime is the build time.
        with contextlib.suppress(OSError):
            build_time = max(build_time, (self.path.parent / _MANIFEST_FILENAME).stat().st_mtime_ns)

        def counts(p: str) -> bool:
            in_graph = p in held
            # Graphify skips dot-directories, so a new file there is not one it should hold.
            indexable = in_graph or (
                PurePosixPath(p).suffix in extensions
                and not any(seg.startswith(".") for seg in p.split("/")[:-1])
            )
            if not indexable:
                return False
            target = root / p
            try:
                if not target.is_file():
                    return in_graph  # deleted, or now a directory: the graph still holds it
                if not in_graph:
                    return True  # a new file
                return target.stat().st_mtime_ns > build_time  # edited after the build
            except OSError:
                return True

        counting = [p for p in changed if counts(p)]
        ordered = sorted(p for p in counting if p in held) + sorted(
            p for p in counting if p not in held
        )
        return GraphFreshness(
            state="stale" if counting else "fresh",
            built_at=built,
            head=head,
            in_history=ancestor == 0,
            commits_behind=commits_behind,
            changed=len(counting),
            sample=ordered[:_FRESHNESS_SAMPLE],
        )

    def detect_subdir_mismatch(
        self, repo_root: Path | None = None, sample_size: int = 25
    ) -> dict | None:
        """Best-effort diagnostic for the "ran `graphify update` from a subdirectory
        instead of the repo root" footgun: when every ``source_file`` Graphify recorded is
        relative to the directory it was invoked FROM rather than the repository root,
        every descriptor this reader resolves stops matching the real repo-relative
        anchors the store expects, and a person sees mass, unexplained ``orphaned``
        findings with nothing pointing at the real cause.

        ``repo_root`` defaults to :meth:`repo_root` (the graph's own git worktree root);
        ``None`` (no resolvable repo, or an explicit ``None`` passed by a caller with none)
        means there is nothing to check existence against, so this always returns ``None``.

        Samples up to ``sample_size`` DISTINCT anchorable ``source_file`` values (code/
        document/concept/rationale — the same universe :data:`ANCHORABLE_FILE_TYPES`
        anchors against, in node order — deterministic) and checks each for existence
        relative to ``repo_root``. Returns ``None`` (no finding, abstain) when ANY of:

        - the sample has fewer than :data:`_SUBDIR_MISMATCH_MIN_SAMPLE` distinct paths —
          too small a corpus to judge safely (a tiny fixture, a doc-only build);
        - fewer than :data:`_SUBDIR_MISMATCH_THRESHOLD` (90%) of the sample is missing
          relative to ``repo_root`` — a LEGITIMATELY PARTIAL graph (an ``--exclude``d
          corpus, a doc-only build) still has its INCLUDED files sitting at their real
          repo-relative paths, so it never trips this ratio;
        - no SINGLE immediate subdirectory of ``repo_root`` resolves EVERY missing sampled
          path — the positive half of the signal. A graph gone stale after files were
          deleted or moved for an unrelated reason can also leave most of a sample missing,
          but it is vanishingly unlikely for ALL of them to coincidentally resolve under
          one specific subdirectory; only an actual subdirectory-relative build produces
          that pattern, so requiring it is what keeps a stale graph from being
          misdiagnosed as a subdir-run one.

        Returns ``{"sampled": N, "missing": M, "likely_run_from": "<subdir>"}`` when a
        single candidate subdirectory explains the whole missing sample — callers use this
        to print an actionable "rerun graphify from the repo root" diagnostic instead of
        letting every one of those paths surface as a separate, unexplained orphaned
        anchor.
        """
        root = repo_root if repo_root is not None else self.repo_root()
        if root is None:
            return None
        sampled: list[str] = []
        seen: set[str] = set()
        for n in self._nodes:
            if n.file_type in ANCHORABLE_FILE_TYPES and n.file_path and n.file_path not in seen:
                seen.add(n.file_path)
                sampled.append(n.file_path)
            if len(sampled) >= sample_size:
                break
        if len(sampled) < _SUBDIR_MISMATCH_MIN_SAMPLE:
            return None
        missing = [p for p in sampled if not (root / p).exists()]
        if len(missing) / len(sampled) < _SUBDIR_MISMATCH_THRESHOLD:
            return None
        try:
            subdirs = sorted(
                d.name for d in root.iterdir() if d.is_dir() and not d.name.startswith(".")
            )
        except OSError:
            return None
        for sub in subdirs:
            if all((root / sub / p).exists() for p in missing):
                return {"sampled": len(sampled), "missing": len(missing), "likely_run_from": sub}
        return None


def open_borrowed_reader(store_path: str | Path) -> tuple[GraphifyReader, Path] | None:
    """A reader over the main checkout's graph for a linked worktree whose store has no graph of
    its own, with the main checkout root; ``None`` when nothing is borrowable or the graph cannot
    be read. Never raises. The read tools and SessionStart call it after the store's own graph
    failed to open, and sync the result index-only (``canonical_writes=False``): the worktree's
    index is derived from the borrowed graph and no tracked file is rewritten. No write path
    borrows.
    see design/superpowers/specs/2026-10-01-worktree-borrowed-graph-design.md (D2)"""
    try:
        graph, main = borrowed_graph_path(store_path), main_checkout_root(store_path)
        if graph is None or main is None:
            return None
        return GraphifyReader(graph), main
    except Exception:
        return None
