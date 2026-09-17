"""One-way OKF v0.1 bundle projection of the decision store.

Maps the owned store onto Open Knowledge Format concepts — a directory of markdown files
with YAML frontmatter whose cross-links form a graph (spec:
https://github.com/GoogleCloudPlatform/knowledge-catalog/tree/main/okf). Portable core:
reads only :class:`~sidegraph.store.Store` — no engine reader, no host specifics.
``build_bundle`` is pure (store -> in-memory ``{path: content}``, deterministic);
``write_bundle`` owns the out-directory safety rules. Strictly one-way: the bundle is a
derived artifact and the store stays the source of truth; nothing here writes to the
store.

# see design/superpowers/specs/2026-07-23-okf-export-design.md
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from ulid import ULID

from .schema import (
    AnchorBinding,
    Decision,
    DecisionStatus,
    Domain,
    DomainStatus,
    Entity,
    EntityKind,
    Fact,
    Provenance,
    slugify,
)
from .store import Store

#: Spec version this exporter targets, declared in the root ``index.md`` frontmatter.
OKF_VERSION = "0.1"

#: Root-index frontmatter line marking a directory as a previous export. ``write_bundle``
#: only ever clears a directory carrying it (or an empty one) — see the spec's out-dir
#: safety rules.
GENERATOR_LINE = "generator: sidegraph"

_SLUG_LIMIT = 60
_FACT_TITLE_LIMIT = 80
_DESCRIPTION_LIMIT = 160

_DOMAIN_PREFIX = "domain:"
_COMMUNITY_PREFIX = "community:"


def _first_line(text: str, limit: int) -> str:
    stripped = text.strip()
    line = stripped.splitlines()[0] if stripped else ""
    return line if len(line) <= limit else line[: limit - 1] + "…"


def _slug(text: str, fallback: str) -> str:
    s = slugify(text)[:_SLUG_LIMIT].rstrip("-")
    return s or fallback


def _label(text: str) -> str:
    """Escape a markdown link label (square brackets only — the rest is legal there)."""
    return text.replace("[", "\\[").replace("]", "\\]")


def _yaml_str(value: str) -> str:
    """A JSON-quoted scalar — valid YAML; handles quotes/colons/newlines exactly."""
    return json.dumps(value, ensure_ascii=False)


def _frontmatter(pairs: list[tuple[str, str]]) -> str:
    return "\n".join(["---", *[f"{k}: {v}" for k, v in pairs], "---"])


def build_bundle(store: Store) -> dict[str, str]:
    """Project ``store`` into an OKF bundle: bundle-relative posix path -> file content.

    Pure — reads the store, writes nothing. Deterministic: same store state yields an
    identical dict (all iteration orders pinned, no wall-clock, no randomness).
    """
    # Proposed (unratified) drafts are never published: the store owner ratifies before a
    # record enters the shared memory, so the bundle carries accepted/superseded/rejected/
    # deprecated only (spec Decisions §1). Only `proposed` is dropped — superseded history
    # is deliberately retained ("tried before, abandoned…" is the product). Everything
    # downstream (paths, bindings, entities, indexes, log) derives from these lists, so
    # filtering here is the single, complete cut. Facts reuse DecisionStatus.
    decisions = sorted(
        (d for d in store.iter_decisions() if d.status is not DecisionStatus.PROPOSED),
        key=lambda d: d.id,
    )
    facts = sorted(
        (f for f in store.iter_facts() if f.status is not DecisionStatus.PROPOSED),
        key=lambda f: f.id,
    )
    domain_rows = [r for r in store.iter_domains() if r.status is not DomainStatus.PROPOSED]

    # Domains collapse to one concept per slug: greatest domain_id (ULID = creation time)
    # wins and its status is what the page shows (spec decision 3).
    winning: dict[str, Domain] = {}
    for row in domain_rows:
        cur = winning.get(row.slug)
        if cur is None or row.domain_id > cur.domain_id:
            winning[row.slug] = row
    slug_by_row_id = {row.domain_id: row.slug for row in domain_rows}

    dec_path = {d.id: f"decisions/{_slug(d.title, 'record')}-{d.id.lower()}.md" for d in decisions}
    fact_title = {f.id: _first_line(f.statement, _FACT_TITLE_LIMIT) for f in facts}
    fact_path = {
        f.id: f"facts/{_slug(_first_line(f.statement, 200), 'record')}-{f.id.lower()}.md"
        for f in facts
    }
    domain_path = {slug: f"domains/{slug}.md" for slug in winning}
    record_path = dec_path | fact_path
    record_title = {d.id: d.title for d in decisions} | fact_title
    # A Decision and a Fact sharing one ULID is store corruption (each add-path enforces
    # id uniqueness only within its own type) — the merged maps above would silently
    # resolve such an id to the fact. Treat it as ambiguous: drop the id from the link
    # maps so every reference renders as a plain id, never a wrong link. The two concept
    # files themselves still export (their per-type paths never clash).
    for rid in dec_path.keys() & fact_path.keys():
        del record_path[rid]
        del record_title[rid]

    # Bindings carry identity only (statuses are volatile and index-only — see the doctor
    # spec); community:* targets are skipped outright (renumberable snapshot labels).
    entity_by_id: dict[str, Entity] = {}
    kept_bindings: dict[str, list[AnchorBinding]] = {}
    records_by_entity: dict[str, list[tuple[str, str]]] = {}  # entity_id -> [(rid, relation)]
    records_by_domain_slug: dict[str, list[str]] = {}
    for rid in [*dec_path, *fact_path]:
        kept: list[AnchorBinding] = []
        for b in store.bindings_for_record(rid):
            ent = entity_by_id.get(b.entity_id) or store.get_entity(b.entity_id)
            if ent is None or ent.canonical_name.startswith(_COMMUNITY_PREFIX):
                continue
            entity_by_id[ent.entity_id] = ent
            kept.append(b)
            cname = ent.canonical_name
            if cname.startswith(_DOMAIN_PREFIX) and cname[len(_DOMAIN_PREFIX) :] in domain_path:
                records_by_domain_slug.setdefault(cname[len(_DOMAIN_PREFIX) :], []).append(rid)
            else:
                records_by_entity.setdefault(ent.entity_id, []).append((rid, b.relation))
        kept_bindings[rid] = kept

    # Entities export only when referenced; domain:* entities with a domain page are
    # represented by that page instead (spec decision 4).
    entities = sorted(
        (
            e
            for e in entity_by_id.values()
            if not (
                e.canonical_name.startswith(_DOMAIN_PREFIX)
                and e.canonical_name[len(_DOMAIN_PREFIX) :] in domain_path
            )
        ),
        key=lambda e: (e.canonical_name, e.entity_id),
    )
    ent_path = {
        e.entity_id: f"entities/{_slug(e.canonical_name, 'entity')}-{e.entity_id.lower()}.md"
        for e in entities
    }

    superseded_by: dict[str, list[str]] = {}
    all_records: list[Decision | Fact] = [*decisions, *facts]
    for rec in all_records:
        if rec.supersedes:
            superseded_by.setdefault(rec.supersedes, []).append(rec.id)

    def _record_ref(rid: str) -> str:
        # Defensive: a dirty store may hold a dangling id — plain text, never a broken
        # link (spec §Concept mapping). Unreachable through Store's own write guards.
        path = record_path.get(rid)
        if path is None:
            return rid
        return f"[{_label(record_title[rid])}](/{path})"

    def _record_order(rid: str) -> tuple[int, str]:
        return (0 if rid in dec_path else 1, rid)  # decisions first, then facts, by id

    def _anchor_lines(rid: str) -> list[str]:
        rows: list[tuple[int, str, str]] = []
        for b in kept_bindings[rid]:
            ent = entity_by_id[b.entity_id]
            cname = ent.canonical_name
            if cname.startswith(_DOMAIN_PREFIX) and cname[len(_DOMAIN_PREFIX) :] in domain_path:
                slug = cname[len(_DOMAIN_PREFIX) :]
                label, target = winning[slug].title, f"/{domain_path[slug]}"
            else:
                label = ent.descriptor.name if ent.descriptor else cname
                target = f"/{ent_path[b.entity_id]}"
            line = f"- [{_label(label)}]({target}) — {b.relation}, tier {b.tier}"
            rows.append((b.tier, label, line))
        if not rows:
            return []
        rows.sort()
        return ["# Anchors", *[line for _, _, line in rows]]

    def _history_lines(rid: str, supersedes: str | None) -> list[str]:
        lines: list[str] = []
        if supersedes:
            lines.append(f"- Supersedes {_record_ref(supersedes)}")
        lines += [f"- Superseded by {_record_ref(s)}" for s in sorted(superseded_by.get(rid, []))]
        return ["# History", *lines] if lines else []

    def _provenance_lines(p: Provenance) -> list[str]:
        lines = ["# Provenance", f"- source: {p.source}"]
        if p.ref:
            lines.append(f"- ref: {p.ref}")
        if p.author:
            lines.append(f"- author: {p.author}")
        return lines

    def _render_decision(d: Decision) -> str:
        fm: list[tuple[str, str]] = [
            ("type", "decision"),
            ("title", _yaml_str(d.title)),
            ("description", _yaml_str(_first_line(d.choice, _DESCRIPTION_LIMIT))),
            ("timestamp", d.valid_from.isoformat()),
            ("id", d.id.lower()),
            ("kind", d.kind.value),
            ("status", d.status.value),
            ("scope", d.scope.value),
        ]
        if d.layer:
            fm.append(("layer", d.layer))
        fm.append(("valid_from", d.valid_from.isoformat()))
        if d.valid_to:
            fm.append(("valid_to", d.valid_to.isoformat()))
        if d.supersedes:
            fm.append(("supersedes", d.supersedes.lower()))
        sections: list[str] = []
        for heading, text in (
            ("# Context", d.context),
            ("# Choice", d.choice),
            ("# Rejected", d.rejected or ""),
            ("# Consequences", d.consequences or ""),
        ):
            if text.strip():
                sections.append(f"{heading}\n{text.strip()}")
        for block in (_anchor_lines(d.id), _history_lines(d.id, d.supersedes)):
            if block:
                sections.append("\n".join(block))
        sections.append("\n".join(_provenance_lines(d.provenance)))
        return _frontmatter(fm) + "\n\n" + "\n\n".join(sections) + "\n"

    def _render_fact(f: Fact) -> str:
        fm: list[tuple[str, str]] = [
            ("type", "fact"),
            ("title", _yaml_str(fact_title[f.id])),
            ("timestamp", f.valid_from.isoformat()),
            ("id", f.id.lower()),
            ("status", f.status.value),
            ("valid_from", f.valid_from.isoformat()),
        ]
        if f.valid_to:
            fm.append(("valid_to", f.valid_to.isoformat()))
        if f.supersedes:
            fm.append(("supersedes", f.supersedes.lower()))
        sections = [f.statement.strip(), f"# Source\n{f.source.strip()}"]
        if f.supports:
            sections.append(
                "\n".join(["# Supports", *[f"- {_record_ref(s)}" for s in sorted(f.supports)]])
            )
        for block in (_anchor_lines(f.id), _history_lines(f.id, f.supersedes)):
            if block:
                sections.append("\n".join(block))
        sections.append("\n".join(_provenance_lines(f.provenance)))
        return _frontmatter(fm) + "\n\n" + "\n\n".join(sections) + "\n"

    def _render_domain(dm: Domain) -> str:
        fm: list[tuple[str, str]] = [
            ("type", "domain"),
            ("title", _yaml_str(dm.title)),
            ("description", _yaml_str(_first_line(dm.summary, _DESCRIPTION_LIMIT))),
            ("timestamp", ULID.from_str(dm.domain_id).datetime.isoformat()),
            ("id", dm.domain_id.lower()),
            ("slug", dm.slug),
            ("status", dm.status.value),
        ]
        sections = [dm.summary.strip()]
        parent_slug = slug_by_row_id.get(dm.parent_id) if dm.parent_id else None
        if parent_slug and parent_slug != dm.slug and parent_slug in domain_path:
            parent = winning[parent_slug]
            sections.append(
                f"# Hierarchy\n- Parent: [{_label(parent.title)}](/{domain_path[parent_slug]})"
            )
        bound = sorted(set(records_by_domain_slug.get(dm.slug, [])), key=_record_order)
        if bound:
            sections.append(
                "\n".join(["# Anchored records", *[f"- {_record_ref(r)}" for r in bound]])
            )
        sections.append("\n".join(_provenance_lines(dm.provenance)))
        return _frontmatter(fm) + "\n\n" + "\n\n".join(sections) + "\n"

    def _render_entity(e: Entity) -> str:
        label = e.descriptor.name if e.descriptor else e.canonical_name
        fm: list[tuple[str, str]] = [
            ("type", "code-entity" if e.kind is EntityKind.CONCRETE else "concept"),
            ("title", _yaml_str(label)),
            ("id", e.entity_id.lower()),
            ("name", _yaml_str(e.canonical_name)),
        ]
        if e.descriptor and e.descriptor.file_path:
            fm.append(("file_path", _yaml_str(e.descriptor.file_path)))
        refs = sorted(
            set(records_by_entity.get(e.entity_id, [])), key=lambda t: _record_order(t[0])
        )
        lines = ["# Anchored records", *[f"- {_record_ref(rid)} — {rel}" for rid, rel in refs]]
        return _frontmatter(fm) + "\n\n" + "\n".join(lines) + "\n"

    bundle: dict[str, str] = {}
    for d in decisions:
        bundle[dec_path[d.id]] = _render_decision(d)
    for f in facts:
        bundle[fact_path[f.id]] = _render_fact(f)
    for slug in sorted(winning):
        bundle[domain_path[slug]] = _render_domain(winning[slug])
    for e in entities:
        bundle[ent_path[e.entity_id]] = _render_entity(e)

    # --- section indexes (reserved files: no frontmatter) ---
    def _section_index(heading: str, entries: list[tuple[str, str, str]]) -> str:
        lines = [heading, *[f"- [{_label(t)}](/{p}) — {desc}" for t, p, desc in entries]]
        return "\n".join(lines) + "\n"

    if decisions:
        bundle["decisions/index.md"] = _section_index(
            "# Decisions",
            [
                (d.title, dec_path[d.id], _first_line(d.choice, _DESCRIPTION_LIMIT))
                for d in decisions
            ],
        )
    if facts:
        bundle["facts/index.md"] = (
            "\n".join(
                ["# Facts", *[f"- [{_label(fact_title[f.id])}](/{fact_path[f.id]})" for f in facts]]
            )
            + "\n"
        )
    if winning:
        bundle["domains/index.md"] = _section_index(
            "# Domains",
            [
                (
                    winning[s].title,
                    domain_path[s],
                    _first_line(winning[s].summary, _DESCRIPTION_LIMIT),
                )
                for s in sorted(winning)
            ],
        )
    if entities:
        bundle["entities/index.md"] = _section_index(
            "# Entities",
            [
                (
                    e.descriptor.name if e.descriptor else e.canonical_name,
                    ent_path[e.entity_id],
                    e.descriptor.file_path
                    if e.descriptor and e.descriptor.file_path
                    else e.canonical_name,
                )
                for e in entities
            ],
        )

    # --- root log.md: chronology of the memory (decisions + facts, newest date first) ---
    by_date: dict[str, list[tuple[str, str]]] = {}
    for d in decisions:
        by_date.setdefault(d.valid_from.date().isoformat(), []).append(
            (d.id, f"- decision ({d.kind.value}): {_record_ref(d.id)}")
        )
    for f in facts:
        by_date.setdefault(f.valid_from.date().isoformat(), []).append(
            (f.id, f"- fact: {_record_ref(f.id)}")
        )
    if by_date:
        log_lines = ["# Log"]
        for date in sorted(by_date, reverse=True):
            log_lines += ["", f"## {date}", *[line for _, line in sorted(by_date[date])]]
        bundle["log.md"] = "\n".join(log_lines) + "\n"

    # --- root index.md: the ONE reserved file allowed frontmatter (okf_version there) ---
    root = [
        "---",
        f'okf_version: "{OKF_VERSION}"',
        GENERATOR_LINE,
        "---",
        "",
        "# Sidegraph decision memory",
        "",
        "One-way OKF projection of a Sidegraph decision store — append-only decision",
        "memory with full supersession history. The store, not this bundle, is the",
        "source of truth.",
    ]
    contents: list[str] = []
    if decisions:
        contents.append(
            f"- [decisions/](/decisions/index.md) — {len(decisions)} decision records "
            "(ADRs, lessons, constraints, gotchas)"
        )
    if facts:
        contents.append(
            f"- [facts/](/facts/index.md) — {len(facts)} facts "
            "(non-derivable knowledge that informed decisions)"
        )
    if winning:
        contents.append(
            f"- [domains/](/domains/index.md) — {len(winning)} domains (named areas of the system)"
        )
    if entities:
        contents.append(
            f"- [entities/](/entities/index.md) — {len(entities)} code entities decisions anchor to"
        )
    if contents:
        root += ["", "## Contents", *contents]
    bundle["index.md"] = "\n".join(root) + "\n"
    return bundle


def _is_previous_export(out_dir: Path) -> bool:
    """True iff ``out_dir/index.md``'s FIRST frontmatter block carries the literal
    ``generator: sidegraph`` line. A deliberate string check, not YAML parsing — the
    exporter has no runtime YAML dependency (spec decision 8)."""
    index = out_dir / "index.md"
    if not index.is_file():
        return False
    try:
        lines = index.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return False
    if not lines or lines[0] != "---":
        return False
    for line in lines[1:]:
        if line == "---":
            return False
        if line == GENERATOR_LINE:
            return True
    return False


def write_bundle(bundle: dict[str, str], out_dir: Path) -> None:
    """Write ``bundle`` as an exact snapshot of ``out_dir``.

    Safety rules (spec §CLI contract): create the directory if missing; use it if empty;
    clear and rewrite it ONLY when its root ``index.md`` marks it as a previous sidegraph
    export. Anything else raises ``ValueError`` — clearing a directory the user pointed
    at by mistake would be destructive.
    """
    if out_dir.exists():
        if not out_dir.is_dir():
            raise ValueError(f"refusing to overwrite {out_dir}: not a directory")
        children = sorted(out_dir.iterdir())
        if children and not _is_previous_export(out_dir):
            raise ValueError(
                f"refusing to overwrite {out_dir}: not an empty directory or a "
                "sidegraph-generated bundle"
            )
        for child in children:
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink()
    for rel in sorted(bundle):
        target = out_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(bundle[rel], encoding="utf-8")
