"""Pure conversion of bounded scan results into immutable Bootstrap candidates."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import TYPE_CHECKING

from sidegraph.bootstrap.catalog import CanonicalCatalog, fingerprint_catalog
from sidegraph.bootstrap.model import (
    AnchorPlan,
    BootstrapCandidate,
    BootstrapPlan,
    EditableCandidate,
    PlanIssue,
    ScanResult,
    SourceFingerprint,
    WarningCode,
)
from sidegraph.capture import redact
from sidegraph.doc_import import (
    ParsedDoc,
    _is_draft_like_status,
    _is_live_tree_path,
    _is_rejected_status,
    parse_decision_docs,
)
from sidegraph.profiles import FlowProfile
from sidegraph.schema import DecisionKind, DecisionStatus, Descriptor

if TYPE_CHECKING:
    from sidegraph.engine.reader import GraphifyReader


_CURRENT_STATE_PREFIXES = (
    "currently ",
    "today ",
    "the system is ",
    "the system has ",
    "the system uses ",
)
_DECISION_VERBS = ("choose", "decide", "adopt", "require", "must", "will", "use ")
_MENTION_RE = re.compile(r"`([^`\n]+)`")


def _quality_warnings(parsed: ParsedDoc) -> tuple[WarningCode, ...]:
    warnings: list[WarningCode] = []
    choice = parsed.choice.strip().lower()
    if not parsed.choice.strip():
        warnings.append(WarningCode.MISSING_CHOICE)
    if not (parsed.rejected or "").strip():
        warnings.append(WarningCode.MISSING_REJECTED)
    if choice.startswith(_CURRENT_STATE_PREFIXES) and not any(
        verb in choice for verb in _DECISION_VERBS
    ):
        warnings.append(WarningCode.CURRENT_STATE)
    return tuple(warnings)


def _default_status(parsed: ParsedDoc) -> DecisionStatus:
    if _is_rejected_status(parsed.frontmatter_status):
        return DecisionStatus.REJECTED
    if _is_draft_like_status(parsed.frontmatter_status):
        return DecisionStatus.PROPOSED
    return DecisionStatus.ACCEPTED


def _anchor_text(parsed: ParsedDoc) -> str:
    return "\n".join(
        value
        for value in (
            parsed.title,
            parsed.context,
            parsed.choice,
            parsed.rejected,
            parsed.consequences,
        )
        if value
    )


def _context_body(parsed: ParsedDoc, ref: str) -> str:
    suffix = f"\n\nimported from {ref}"
    return parsed.context.removesuffix(suffix).strip()


def _choice_is_context_fallback(parsed: ParsedDoc, ref: str) -> bool:
    """Reject the parser's last-resort context echo as a Bootstrap decision choice."""
    context = _context_body(parsed, ref)
    return bool(context) and parsed.choice.strip() == context


def _candidate(parsed: ParsedDoc, file_path: str, ref: str, source_hash: str) -> BootstrapCandidate:
    """``file_path`` stays the ON-DISK path (anchor lookups, file I/O); ``ref`` is derived
    by the caller from the NORMALIZED path (E2, design note §6) — one field cannot serve
    both after an archive-style move (review M3)."""
    return BootstrapCandidate.from_fields(
        file_path=file_path,
        ref=ref,
        fragment=parsed.fragment,
        source_hash=source_hash,
        title=parsed.title,
        context=parsed.context,
        choice=parsed.choice,
        rejected=parsed.rejected,
        consequences=parsed.consequences,
        kind=parsed.suggested_kind or DecisionKind.ADR,
        default_status=_default_status(parsed),
        redacted_anchor_text=_anchor_text(parsed),
        anchor_intents=(),
        file_anchor_intent=None,
        anchors=(),
        warnings=_quality_warnings(parsed),
    )


def _content_signature(candidate: BootstrapCandidate) -> str:
    provenance = f"imported from {candidate.ref}"
    context = (
        ""
        if candidate.context == provenance
        else candidate.context.removesuffix(f"\n\n{provenance}")
    )
    material = "\0".join(
        str(value or "")
        for value in (
            candidate.title,
            context,
            candidate.choice,
            candidate.rejected,
            candidate.consequences,
            candidate.kind,
        )
    )
    return hashlib.sha256(material.encode()).hexdigest()


def _plan_fingerprint(
    profile: str,
    sources: list[SourceFingerprint],
    candidates: list[BootstrapCandidate],
    catalog_fingerprint: str,
    graph_fingerprint: str,
) -> str:
    material = {
        "profile": profile,
        "sources": [(item.path, item.sha256) for item in sources],
        "candidate_keys": [candidate.key for candidate in candidates],
        "catalog_fingerprint": catalog_fingerprint,
        "graph_fingerprint": graph_fingerprint,
    }
    encoded = json.dumps(material, ensure_ascii=False, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


_DUPLICATE_STATUSES = (
    DecisionStatus.ACCEPTED,
    DecisionStatus.PROPOSED,
    DecisionStatus.REJECTED,
)


def _mention_descriptors(text: str) -> tuple[Descriptor, ...]:
    seen: set[str] = set()
    descriptors: list[Descriptor] = []
    for match in _MENTION_RE.finditer(text):
        name = match.group(1).strip()
        if name and name not in seen:
            seen.add(name)
            descriptors.append(Descriptor(name=name))
    return tuple(descriptors)


def _anchor_plan(descriptor: Descriptor, reader: GraphifyReader) -> AnchorPlan:
    result = reader.resolve(descriptor)
    if result.status == "resolved":
        return AnchorPlan(descriptor=descriptor, status="resolved", tier=2)
    if result.status == "ambiguous":
        return AnchorPlan(
            descriptor=descriptor,
            status="ambiguous",
            candidates=tuple(result.candidates),
            tier=1 if result.community is not None else None,
        )
    return AnchorPlan(descriptor=descriptor, status="unresolved", tier=2)


def _enrich_candidate(
    candidate: BootstrapCandidate,
    *,
    catalog: CanonicalCatalog | None,
    reader: GraphifyReader | None,
) -> BootstrapCandidate:
    warnings = list(candidate.warnings)
    if catalog is not None and catalog.find_by_ref(
        "doc-import", candidate.ref, _DUPLICATE_STATUSES
    ):
        warnings.append(WarningCode.DUPLICATE_CANONICAL)

    anchor_intents: tuple[Descriptor, ...] = ()
    file_anchor_intent: Descriptor | None = None
    anchors: tuple[AnchorPlan, ...] = ()
    if reader is not None:
        anchor_intents = _mention_descriptors(candidate.redacted_anchor_text)
        displayed = [_anchor_plan(descriptor, reader) for descriptor in anchor_intents]

        document_nodes = sorted(
            (
                node
                for node in reader.nodes_in_file(candidate.file_path)
                if node.file_type == "document"
            ),
            key=lambda node: (node.node_id, node.name),
        )
        if document_nodes:
            chosen = document_nodes[0]
            file_anchor_intent = Descriptor(
                name=chosen.name,
                file_path=candidate.file_path,
            )
            displayed.append(_anchor_plan(file_anchor_intent, reader))

        anchors = tuple(displayed)
        if any(anchor.status == "ambiguous" for anchor in anchors):
            warnings.append(WarningCode.AMBIGUOUS_ANCHOR)
        if any(anchor.status == "unresolved" for anchor in anchors):
            warnings.append(WarningCode.UNRESOLVED_ANCHOR)

    return candidate.model_copy(
        update={
            "anchor_intents": anchor_intents,
            "file_anchor_intent": file_anchor_intent,
            "anchors": anchors,
            "warnings": tuple(warnings),
        }
    )


def plan_sources(
    root: Path,
    scan: ScanResult,
    profile: FlowProfile,
    *,
    catalog: CanonicalCatalog | None = None,
    reader: GraphifyReader | None = None,
) -> BootstrapPlan:
    """Plan redacted candidates without constructing a graph reader or persistent Store."""
    candidates: list[BootstrapCandidate] = []
    issues: list[PlanIssue] = []
    source_fingerprints: list[SourceFingerprint] = []
    files_read: list[str] = []
    content_signatures: set[str] = set()

    for file_path in scan.files:
        source_bytes = (root / file_path).read_bytes()
        source_hash = hashlib.sha256(source_bytes).hexdigest()
        source_fingerprints.append(SourceFingerprint(path=file_path, sha256=source_hash))
        files_read.append(file_path)
        # E2 (design note §6, review M3): the NORMALIZED path is what gets parsed — that
        # call is what stamps `context`'s "imported from …" and each child's
        # `effective_ref`, so the stamp and fragments come out normalized at the source,
        # matching what `sidegraph-import` stamps for the same doc (§6 point 2). E1: the
        # profile's `title_pattern` rides along, threaded exactly like `dialect`.
        normalized_path = profile.normalized_rel_path(file_path)
        parsed_docs, reason = parse_decision_docs(
            source_bytes.decode("utf-8"),
            normalized_path,
            dialect=profile.dialect,
            title_pattern=profile.title_pattern,
        )
        if not parsed_docs:
            if reason == "unparseable":
                issues.append(
                    PlanIssue(
                        file_path=file_path,
                        ref=normalized_path,
                        warning=WarningCode.MISSING_CHOICE,
                        detail="Decision-shaped document has no usable choice.",
                    )
                )
            continue

        for parsed in parsed_docs:
            # `ref` is derived from the SAME normalized path the stamp used (E2) — keeps
            # `_context_body`'s `removesuffix` matching, so `_choice_is_context_fallback`
            # (the echo-refusal check) fires exactly like it does pre-E2 (review T15).
            ref = (
                normalized_path
                if parsed.fragment is None
                else f"{normalized_path}#{parsed.fragment}"
            )
            if _choice_is_context_fallback(parsed, ref):
                issues.append(
                    PlanIssue(
                        file_path=file_path,
                        ref=ref,
                        warning=WarningCode.MISSING_CHOICE,
                        detail="Decision choice only repeats the context fallback.",
                    )
                )
                continue
            # I2 (R1 improvement wave §2, Blocker 1 — decide-then-stamp): the in-flight
            # note is appended to `parsed.context` only AFTER the echo-refusal decision
            # above has run against the note-free context — mirrors `import_docs`'s own
            # sequencing (`doc_import._is_live_tree_path` is the shared trigger predicate
            # both write paths use). Stamping any earlier would corrupt
            # `_choice_is_context_fallback`'s own strip-and-compare the same way it would
            # corrupt `_choice_is_context_echo`'s.
            if profile.in_flight_note and _is_live_tree_path(file_path, profile):
                parsed = parsed.model_copy(
                    update={"context": f"{parsed.context}\n\n{profile.in_flight_note}"}
                )
            candidate = _candidate(parsed, file_path, ref, source_hash)
            content_signature = _content_signature(candidate)
            if content_signature in content_signatures:
                issues.append(
                    PlanIssue(
                        file_path=file_path,
                        ref=candidate.ref,
                        warning=WarningCode.DUPLICATE_PLAN,
                        detail=f"Duplicate candidate at {candidate.ref} was omitted.",
                    )
                )
                continue
            content_signatures.add(content_signature)
            candidates.append(_enrich_candidate(candidate, catalog=catalog, reader=reader))

    catalog_digest = fingerprint_catalog(catalog) if catalog is not None else ""
    graph_version = reader.graph_version() if reader is not None else None
    fingerprint = _plan_fingerprint(
        profile.name,
        source_fingerprints,
        candidates,
        catalog_digest,
        graph_version or "",
    )
    return BootstrapPlan(
        root=root.resolve().as_posix(),
        profile=profile.name,
        fingerprint=fingerprint,
        source_fingerprints=tuple(source_fingerprints),
        catalog_fingerprint=catalog_digest,
        graph_version=graph_version,
        candidates=tuple(candidates),
        issues=tuple(issues),
        files_read=tuple(files_read),
        exclusions=scan.exclusions,
    )


def _redacted_edit(editable: EditableCandidate) -> ParsedDoc:
    title, _ = redact(editable.title)
    context, _ = redact(editable.context)
    choice, _ = redact(editable.choice)
    rejected = None
    if editable.rejected is not None:
        rejected, _ = redact(editable.rejected)
    consequences = None
    if editable.consequences is not None:
        consequences, _ = redact(editable.consequences)
    return ParsedDoc(
        title=title,
        context=context,
        choice=choice,
        rejected=rejected,
        consequences=consequences,
        suggested_kind=editable.kind,
    )


def replan_edited_candidate(
    candidate: BootstrapCandidate,
    editable: EditableCandidate,
    *,
    reader: GraphifyReader | None,
    catalog: CanonicalCatalog | None,
) -> BootstrapCandidate:
    """Rebuild an edited candidate from redacted fields, resetting unresolved enrichment."""
    parsed = _redacted_edit(editable)
    replanned = BootstrapCandidate.from_fields(
        file_path=candidate.file_path,
        ref=candidate.ref,
        fragment=candidate.fragment,
        source_hash=candidate.source_hash,
        title=parsed.title,
        context=parsed.context,
        choice=parsed.choice,
        rejected=parsed.rejected,
        consequences=parsed.consequences,
        kind=editable.kind,
        default_status=candidate.default_status,
        redacted_anchor_text=_anchor_text(parsed),
        anchor_intents=(),
        file_anchor_intent=None,
        anchors=(),
        warnings=_quality_warnings(parsed),
    )
    return _enrich_candidate(replanned, catalog=catalog, reader=reader)
