"""Bootstrap decisions from Graphify rationale nodes (portable core).

Reads rationale nodes via the engine seam (``GraphifyReader.rationale_nodes()``) and writes
them through the existing deterministic pipeline (redact -> validate -> anchor -> write) —
same shape as ``capture.py``'s propose path, but for bulk, non-interactive import. See
``docs/reference/cli.md#sidegraph-import``.
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, Field

from .anchoring import resolve_and_bind
from .capture import (
    AutoEligibility,
    RatifyPolicy,
    _anchor_signal,
    _auto_ratify,
    auto_ratify_eligible,
    redact,
)
from .engine.rationale import rationale_labels_agree
from .engine.reader import GraphifyReader, RationaleNode
from .schema import (
    Decision,
    DecisionKind,
    DecisionStatus,
    Descriptor,
    Provenance,
    canonicalize,
    matches_path_prefix,
)
from .store import Store

# Cap per design decision 3: a well-connected rationale node must not fan out into an
# unbounded number of bindings on a bulk import.
_MAX_ANCHORS = 3

_TitleKey = tuple[str, str | None]
_ContextKey = tuple[str | None, str]


class ImportReport(BaseModel):
    """Counts (+ dry-run-only listing) for one ``import_rationales`` run.

    ``imported``/``skipped_existing``/``skipped_unanchorable``/``filtered`` reflect what
    WOULD happen whether or not ``dry_run`` actually wrote anything; ``dry_run`` carries the
    (file_path, title) listing and is populated only when the run was a dry run.
    """

    imported: int = 0
    skipped_existing: int = 0
    skipped_unanchorable: int = 0
    filtered: int = 0
    # Auto-ratification policy (design D2/D6) -- additive/defaulted, same contract as
    # capture.py's ProposeResult pair: incremented/appended by the post-write auto block
    # below, always empty/zero under `manual` or when a written record was ineligible.
    auto_ratified: int = 0
    auto_ratify_failures: list[str] = Field(default_factory=list)  # ["<decision id>: <reason>"]
    # What a real run would auto-ratify, filled ONLY by a dry run (the 2026-10-04 spec, D2):
    # `auto_ratified` keeps meaning "transitions that actually happened", so it stays 0 in a
    # dry run. An eligibility count, not a success guarantee -- a real `_auto_ratify` can
    # still fail (a race, a cascade-guard refusal), which a dry run cannot foresee.
    would_auto_ratify: int = 0
    # [{"file_path", "title", "node_id", "auto_ratify_eligible"}, ...]
    dry_run: list[dict] = Field(default_factory=list)

    def by_file(self) -> dict[str, int]:
        """Per-file breakdown of the dry-run listing, for the CLI's ``--dry-run`` printer."""
        out: dict[str, int] = {}
        for item in self.dry_run:
            fp = item["file_path"] or "<unknown>"
            out[fp] = out.get(fp, 0) + 1
        return out


def _make_title(text: str) -> str:
    """First line of the rationale text, capped at 120 chars (design decision: the rationale
    text IS the recorded reasoning — the title is a lead-in, not a re-summarization).

    ``text`` must already be redacted: redacting the full text first and truncating the
    already-redacted result avoids leaving a partial secret fragment when the 120-char cut
    falls inside what would otherwise be a secret token (redact-then-truncate, never the
    other way — a truncated fragment of a secret usually no longer matches the secret
    pattern, so a later redact pass over the already-cut title would miss it)."""
    lines = text.splitlines()
    first = lines[0] if lines else text
    return first[:120]


def _existing_import_keys(store: Store) -> tuple[set[_TitleKey], dict[_ContextKey, list[str]]]:
    """Read existing title/ref and exact-context/choice keys once for this import."""
    title_keys: set[_TitleKey] = set()
    context_choices: dict[_ContextKey, list[str]] = {}
    for decision in store.decisions_by_source("import"):
        title_keys.add((canonicalize(decision.title), decision.provenance.ref))
        context_choices.setdefault((decision.provenance.ref, decision.context), []).append(
            decision.choice
        )
    return title_keys, context_choices


def _resolved(reader: GraphifyReader, ref: Descriptor) -> bool:
    return reader.resolve(ref, collapse=False).status == "resolved"


def _select_anchors(node: RationaleNode, reader: GraphifyReader) -> list[Descriptor]:
    """Up to 3 of the node's targets, each independently confirmed ``resolved`` — never
    guess: ambiguous/unresolved targets are skipped outright, not orphan-bound (unlike the
    interactive capture path, a bulk import must not flood the entity table with junk
    orphans for every AST edge that doesn't line up). Falls back to the source file's own
    node (``Descriptor(name=file_path, file_path=file_path)`` — the file-level anchor
    pattern already used on doc corpora) only when zero targets resolved. An empty return
    means "skip this rationale entirely"; the caller counts it as unanchorable.
    """
    anchors: list[Descriptor] = []
    for target in node.targets:
        if len(anchors) >= _MAX_ANCHORS:
            break
        ref = Descriptor(name=target.name, file_path=target.file_path)
        if _resolved(reader, ref):
            anchors.append(ref)
    if anchors:
        return anchors
    if node.file_path is not None:
        file_ref = Descriptor(name=node.file_path, file_path=node.file_path)
        if _resolved(reader, file_ref):
            anchors.append(file_ref)
    return anchors


def import_rationales(
    store: Store,
    reader: GraphifyReader,
    *,
    kind: str = "adr",
    propose: bool = False,
    dry_run: bool = False,
    limit: int | None = None,
    path_prefixes: list[str] | None = None,
    ratify_policy: RatifyPolicy = RatifyPolicy.MANUAL,
) -> ImportReport:
    """One :class:`Decision` per rationale node (see design doc section 3).

    ``title`` = the rationale text's first line (<=120 chars, derived from the FULL text
    AFTER redaction — see ``_make_title``); ``context`` = "imported from <file_path>
    (<node_id>)", or "imported from <node_id>" when the rationale has no ``file_path``;
    ``choice`` = the rationale text verbatim — it IS the recorded reasoning. Redaction
    (``capture.redact``) runs on the full rationale text first, before title/context are
    derived from it.

    ``provenance.ref`` is stamped with the rationale's ``file_path`` (falling back to its
    ``node_id`` when the rationale has no file_path) — this is also the idempotency key's
    second component (see below).

    ``kind`` defaults to ``adr`` (rationale = recorded reasoning; this also keeps bulk
    imports out of retrieval's ``_MISTAKE_KINDS`` bucket) — override per run. ``propose=True``
    writes ``proposed`` (ratify gate) instead of the default ``accepted``. ``dry_run=True``
    runs the full selection (path filter -> limit -> idempotency -> anchor resolution) but
    writes nothing to the store; counts still reflect what WOULD happen, and
    ``ImportReport.dry_run`` carries the listing. Idempotent: a rerun skips a rationale
    held by a non-superseded ``import`` decision through either its canonicalized title
    and ``provenance.ref``, or its exact reconstructed ``context`` and ``ref`` when
    identity survives redaction, with compatible observed labels. The engine seam compares
    already-redacted labels and receives explicit flags disabling length evidence when
    redaction made it unreliable.
    Compatibility describes observed labels, not every possible edit of the underlying
    docstring. Identical first lines in different files remain distinct memories.
    See design/superpowers/specs/2026-10-14-graphify-upgrade-design.md D3.

    ``ratify_policy`` (default ``RatifyPolicy.MANUAL``): the resolved
    ``SIDEGRAPH_RATIFY_POLICY`` value (design D1), sampled once by the CLI shell
    immediately before this call and passed down unchanged. When a written record is
    ``propose=True`` (not a dry run) and passes the same D3 gate ``capture.propose`` uses,
    its own post-write block stamps it ``auto:<policy>`` via the shared ``_auto_ratify``
    helper — never a second copy of that predicate or stamp construction. Every importer
    write already carries ≥1 live Tier-2 binding (only resolved anchors are ever bound),
    so eligibility here turns on kind/policy/supersedes, same as everywhere else.

    A dry run (``dry_run=True``) fires no transition, so ``auto_ratified`` stays 0; it runs the
    same ``auto_ratify_eligible`` verdict on the signal the write would produce and reports it
    in ``ImportReport.would_auto_ratify`` and, per item, ``"auto_ratify_eligible"``. A key
    repeated within the run counts as ``skipped_existing`` the second time in both modes:
    each anchorable record adds its keys before the write or dry-run listing.
    # see design/superpowers/specs/2026-09-11-auto-ratification-policy-design.md D1/D2/D3
    # see design/superpowers/specs/2026-10-04-import-dry-run-previews-auto-ratification-design.md
    """
    decision_kind = DecisionKind(kind)
    status = DecisionStatus.PROPOSED if propose else DecisionStatus.ACCEPTED
    graph_version = reader.graph_version()

    all_nodes = reader.rationale_nodes()
    # Empty prefixes are dropped, so `--path ""` still matches everything; a non-empty one
    # matches on a directory boundary (or the exact file) via `matches_path_prefix`.
    prefixes = [p for p in (path_prefixes or []) if p]
    if prefixes:
        kept = [
            n
            for n in all_nodes
            if n.file_path is not None
            and any(matches_path_prefix(n.file_path, p) for p in prefixes)
        ]
    else:
        kept = all_nodes
    filtered = len(all_nodes) - len(kept)
    process = kept[:limit] if limit is not None else kept

    report = ImportReport(filtered=filtered)
    # One read of existing imports; each anchorable item adds keys before either run mode.
    title_keys, context_choices = _existing_import_keys(store)

    for node in process:
        ref = node.file_path if node.file_path is not None else node.node_id

        # Redact the FULL text first; title/context are then derived from the already-
        # redacted result (never the reverse — see _make_title).
        choice, _ = redact(node.text)
        title = _make_title(choice)
        if node.file_path is not None:
            context = f"imported from {node.file_path} ({node.node_id})"
        else:
            context = f"imported from {node.node_id}"
        context, context_redactions = redact(context)
        # Redaction can erase the node id, or equal a literal marker in another id.
        # Such a context supplies no identity evidence; the title/ref key still applies.
        context_key: _ContextKey | None = (
            (ref, context) if context_redactions == 0 and "[REDACTED]" not in context else None
        )

        canonical_title = canonicalize(title)
        if (canonical_title, ref) in title_keys or (
            context_key is not None
            and any(
                rationale_labels_agree(
                    prior,
                    choice,
                    stored_length_reliable="[REDACTED]" not in prior,
                    new_length_reliable="[REDACTED]" not in choice,
                )
                for prior in context_choices.get(context_key, ())
            )
        ):
            report.skipped_existing += 1
            continue

        anchors = _select_anchors(node, reader)
        if not anchors:
            report.skipped_unanchorable += 1
            continue

        # One update serves both real writes and dry-run listings. A prior unanchorable
        # item never consumes either key.
        title_keys.add((canonical_title, ref))
        if context_key is not None:
            context_choices.setdefault(context_key, []).append(choice)

        if dry_run:
            # The same verdict the post-write block below reaches, built from the inputs the
            # write uses (the 2026-10-04 spec, D1). Every anchor `_select_anchors` returns is
            # confirmed `resolved`, so each binds as a live Tier-2 leaf: `len(anchors)` is at
            # least 1 here and may be lower than the real `_anchor_signal` (Tier-1 community
            # bindings come on top), which the gate's `>= 1` cannot tell apart. The importer
            # never supersedes.
            eligible = (
                propose
                and ratify_policy != RatifyPolicy.MANUAL
                and auto_ratify_eligible(
                    AutoEligibility(
                        kind=decision_kind.value,
                        live_tier12=len(anchors),
                        ambiguous_or_orphan_only=False,
                        pipeline_clean=True,
                        has_provenance=True,
                        domain_anchored=False,
                        has_supersedes=False,
                    ),
                    ratify_policy,
                )
            )
            report.imported += 1
            if eligible:
                report.would_auto_ratify += 1
            report.dry_run.append(
                {
                    "file_path": node.file_path,
                    "title": title,
                    "node_id": node.node_id,
                    "auto_ratify_eligible": eligible,
                }
            )
            continue

        decision = Decision(
            title=title,
            kind=decision_kind,
            status=status,
            context=context,
            choice=choice,
            valid_from=datetime.now(UTC),
            provenance=Provenance(
                source="import",
                author="sidegraph-import",
                ref=ref,
                graph_version=graph_version,
            ),
        )
        store.add_decision(decision)
        for anchor in anchors:
            resolve_and_bind(decision.id, anchor, reader, store)
        report.imported += 1

        # Auto-ratify (design D2/D3), AFTER the binding loop above — _anchor_signal reads
        # live bindings, which don't exist yet before it runs. `propose` is checked
        # explicitly (not left to auto_ratify_eligible alone) so a decision that already
        # landed accepted through the pre-existing `propose=False` default is never
        # handed to a ratify transition at all.
        if propose and not dry_run and ratify_policy != RatifyPolicy.MANUAL:
            live_tier12, ambiguous_or_orphan_only = _anchor_signal(store, decision.id)
            signal = AutoEligibility(
                kind=decision_kind.value,
                live_tier12=live_tier12,
                ambiguous_or_orphan_only=ambiguous_or_orphan_only,
                pipeline_clean=True,
                has_provenance=True,
                domain_anchored=False,
                has_supersedes=decision.supersedes is not None,
            )
            if auto_ratify_eligible(signal, ratify_policy):
                outcome = _auto_ratify(store, decision.id, signal.kind, ratify_policy)
                if outcome.ratified_by is not None:
                    report.auto_ratified += 1
                if outcome.error is not None:
                    report.auto_ratify_failures.append(f"{decision.id}: {outcome.error}")

    return report
