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

import hashlib
import json
import subprocess
import time
from collections import deque
from pathlib import Path

from pydantic import BaseModel

from ..schema import Descriptor, canonicalize

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


class RationaleNode(BaseModel):
    node_id: str
    text: str  # the node's label — engine truncates ~80 chars, that's fine
    file_path: str | None = None
    community: str | None = None
    targets: list[NodeRef] = []  # what this rationale explains


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
            return ResolveResult(status="unresolved")
        comms = {m.community for m in matches if m.community is not None}
        shared = comms.pop() if len(comms) == 1 else None
        return ResolveResult(
            status="ambiguous",
            candidates=[m.node_id for m in matches],
            community=shared,
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

    def repo_root(self) -> Path | None:
        """Best-effort git worktree root containing THIS reader's graph.json —
        ``git rev-parse --show-toplevel`` run from ``self.path.parent``, never raising.
        ``None`` when graph.json sits outside any git working tree, or git itself is
        unavailable.

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
                capture_output=True,
                text=True,
                timeout=5.0,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if result.returncode != 0:
            return None
        return Path(result.stdout.strip()).resolve()

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
