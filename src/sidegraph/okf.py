"""One-way OKF v0.1 bundle projection of the decision store.

Maps the owned store onto Open Knowledge Format concepts — a directory of markdown files
with YAML frontmatter whose cross-links form a graph (spec:
https://github.com/GoogleCloudPlatform/knowledge-catalog/tree/main/okf). Portable core:
reads only :class:`~sidegraph.store.Store` — no engine reader, no host specifics.
``build_bundle`` is pure (store -> in-memory ``{path: content}``, deterministic);
``write_bundle`` owns the out-directory safety rules (a symlinked out dir is refused; a
previous export is replaced only once the new bundle is fully written, except when the
write goes in place: a mount point, ``--out`` being the current directory, an unwritable,
missing or non-directory parent, or a failed rename aside). Strictly one-way:
the bundle is a derived artifact and the store stays the source of truth; nothing here
writes to the store.

# see design/superpowers/specs/2026-07-23-okf-export-design.md and
# design/superpowers/specs/2026-09-29-okf-export-safe-write-design.md
"""

from __future__ import annotations

import contextlib
import errno
import json
import os
import shutil
import stat
import sys
import tempfile
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
    is_safe_record_id,
    slugify,
)
from .store import Store

#: Spec version this exporter targets, declared in the root ``index.md`` frontmatter.
OKF_VERSION = "0.1"

#: Root-index frontmatter line marking a directory as a previous export. ``write_bundle``
#: only ever replaces a directory carrying it (or an empty one) — see the spec's out-dir
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


def _warn_unsafe_id(kind: str, record_id: object) -> None:
    print(
        f"sidegraph: WARNING okf export skips {kind} {record_id!r}: its id is not a safe "
        "filename, so it cannot name a bundle file.",
        file=sys.stderr,
    )


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
    # An id that is not a safe filename would name a path outside the bundle (design/
    # superpowers/specs/2026-09-29-record-identity-design.md D6): such a record is skipped
    # with a warning, and links to it render as plain ids through ``_record_ref``.
    decisions = sorted(
        (d for d in store.iter_decisions() if d.status is not DecisionStatus.PROPOSED),
        key=lambda d: d.id,
    )
    facts = sorted(
        (f for f in store.iter_facts() if f.status is not DecisionStatus.PROPOSED),
        key=lambda f: f.id,
    )
    for d in decisions:
        if not is_safe_record_id(d.id):
            _warn_unsafe_id("decision", d.id)
    for f in facts:
        if not is_safe_record_id(f.id):
            _warn_unsafe_id("fact", f.id)
    decisions = [d for d in decisions if is_safe_record_id(d.id)]
    facts = [f for f in facts if is_safe_record_id(f.id)]
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
    warned_entities: set[str] = set()
    for rid in [*dec_path, *fact_path]:
        kept: list[AnchorBinding] = []
        for b in store.bindings_for_record(rid):
            if not is_safe_record_id(b.entity_id):
                # as if the entity were missing: filtering the entity list later would
                # leave this binding pointing at a path that does not exist (KeyError)
                if b.entity_id not in warned_entities:
                    warned_entities.add(b.entity_id)
                    _warn_unsafe_id("entity", b.entity_id)
                continue
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


def _emit_tree(bundle: dict[str, str], root: Path) -> None:
    """Write ``bundle`` under ``root``. A directory it creates is opened to owner write and
    search while it fills (a restrictive umask would make ``mkdir`` hide both), then given
    back the mode ``mkdir`` chose. On a failure the open directories stay removable."""
    made: list[tuple[Path, int]] = []

    def ensure_dir(directory: Path) -> None:
        if directory.is_dir():
            return
        ensure_dir(directory.parent)
        directory.mkdir()
        mode = stat.S_IMODE(directory.stat().st_mode)
        if mode & 0o300 != 0o300:
            directory.chmod(mode | 0o300)
            made.append((directory, mode))

    for rel in sorted(bundle):
        target = root / rel
        ensure_dir(target.parent)
        target.write_text(bundle[rel], encoding="utf-8")
    for directory, mode in reversed(made):
        directory.chmod(mode)


def _must_emit_in_place(target: Path) -> bool:
    """True when swapping ``target`` for a staged directory is impossible or harmful: a
    mount point (a rename there fails), the working directory itself (the swap would delete
    it), or a parent the process cannot write to, that does not exist yet, or that is not
    a directory (a regular file: ``os.access`` says writable, ``mkdtemp`` cannot stage in it)."""
    if target.exists() and os.path.ismount(target):
        return True
    if Path.cwd().resolve() == target:
        return True
    if not target.parent.is_dir():
        return True
    return not os.access(target.parent, os.W_OK)


def _remove_tree(path: Path) -> None:
    """Remove the directory tree ``path`` even when directories inside it are read-only.
    On a permission error the handler grants owner write+search on the entry's parent (and
    on the entry itself when it is a directory) inside the tree, then retries once."""

    def grant(directory: Path) -> None:
        if directory.is_dir() and not directory.is_symlink():
            mode = stat.S_IMODE(directory.stat().st_mode)
            directory.chmod(mode | stat.S_IWUSR | stat.S_IXUSR | stat.S_IRUSR)

    def onexc(func: object, failed: str, exc: BaseException) -> None:
        if not isinstance(exc, PermissionError):
            raise exc
        entry = Path(failed)
        for directory in (entry.parent, entry):
            if directory == path or path in directory.parents:
                grant(directory)
        if func is os.scandir or func is os.open:
            _remove_tree(entry)  # the walk skipped an unreadable directory: redo it
        else:
            func(failed)  # type: ignore[operator]

    shutil.rmtree(path, onexc=onexc)


def _remove_tree_quietly(path: Path) -> None:
    """``_remove_tree`` for a cleanup that must not mask the error being handled."""
    with contextlib.suppress(OSError):
        _remove_tree(path)


def _writable_tree(target: Path) -> bool:
    """True when every directory under ``target`` (itself included) is writable and
    searchable, so an in-place clear cannot stop half way. Symlinks are not followed."""
    unreadable: list[OSError] = []
    for dirpath, _dirnames, _filenames in os.walk(target, onerror=unreadable.append):
        if not os.access(dirpath, os.W_OK | os.X_OK):
            return False
    return not unreadable


def _emit_in_place(
    bundle: dict[str, str], target: Path, children: list[Path], out_dir: Path
) -> None:
    """Clear ``children`` and write ``bundle`` into ``target``, the validated resolved path.
    An ``OSError`` is re-raised naming ``out_dir``, the spelling the caller used. Nothing is
    cleared unless the directory is writable, so a refusal never leaves a partial export."""
    try:
        if target.exists() and not _writable_tree(target):
            raise OSError(errno.EACCES, os.strerror(errno.EACCES))
        for child in children:
            if child.is_dir() and not child.is_symlink():
                _remove_tree(child)
            else:
                child.unlink()
        _emit_tree(bundle, target)
    except OSError as e:
        raise OSError(e.errno, e.strerror, str(out_dir)) from e


def write_bundle(bundle: dict[str, str], out_dir: Path) -> None:
    """Write ``bundle`` as an exact snapshot of ``out_dir``.

    Safety rules (spec §CLI contract): refuse a symlinked ``out_dir``; create the directory
    if missing; use it if empty; replace it ONLY when its root ``index.md`` marks it as a
    previous sidegraph export. Anything else raises ``ValueError`` — replacing a directory
    the user pointed at by mistake would be destructive.

    The bundle is staged beside ``out_dir`` and swapped in by rename, so a failed write
    leaves a previous export intact. A mount point, the working directory itself, an
    unwritable or non-directory parent, or a previous export whose rename-aside fails is
    written in place instead (not atomic: a later write failure can leave a partial export).
    The in-place clear and write use the resolved, validated ``target``.

    # see design/superpowers/specs/2026-09-29-okf-export-safe-write-design.md
    """
    if out_dir.is_symlink():
        raise ValueError(f"refusing to write {out_dir}: it is a symlink")
    target = out_dir.resolve()
    children: list[Path] = []
    old_mode: int | None = None
    if target.exists():
        if not target.is_dir():
            raise ValueError(f"refusing to overwrite {out_dir}: not a directory")
        try:
            children = sorted(target.iterdir())
        except OSError as e:
            raise OSError(e.errno, e.strerror, str(out_dir)) from e
        if children and not _is_previous_export(target):
            raise ValueError(
                f"refusing to overwrite {out_dir}: not an empty directory or a "
                "sidegraph-generated bundle"
            )
        old_mode = stat.S_IMODE(target.stat().st_mode)
    if _must_emit_in_place(target):
        _emit_in_place(bundle, target, children, out_dir)
        return

    # keep the staging name inside NAME_MAX (a byte limit)
    name64 = target.name.encode("utf-8")[:64].decode("utf-8", "ignore")
    staging_root = Path(tempfile.mkdtemp(dir=target.parent, prefix=f".{name64}.okf-new-"))
    old: Path | None = None
    swapped = in_place = widened = False
    try:
        if stat.S_IMODE(staging_root.stat().st_mode) & 0o300 != 0o300:
            staging_root.chmod(0o700)  # mkdtemp's 0o700 is masked by a restrictive umask
        bundle_dir = staging_root / "bundle"
        bundle_dir.mkdir()  # the kernel applies the umask, as for a fresh mkdir
        publish_mode = stat.S_IMODE(bundle_dir.stat().st_mode)
        if publish_mode & 0o300 != 0o300:
            # A restrictive umask: the dir must stay writable to fill and searchable to
            # rename across directories; the post-swap chmod gives back the mode mkdir chose.
            bundle_dir.chmod(publish_mode | 0o300)
        if old_mode is not None:
            publish_mode = old_mode
        _emit_tree(bundle, bundle_dir)
        if old_mode is not None:
            old = Path(tempfile.mkdtemp(dir=target.parent, prefix=f".{name64}.okf-old-"))
            if stat.S_IMODE(old.stat().st_mode) & 0o300 != 0o300:
                old.chmod(0o700)  # mkdtemp's 0o700 is masked by a restrictive umask
            try:
                target.rename(old)
            except OSError:
                # A read-only previous export (APFS refuses to rename it): grant owner write
                # and retry once; the post-swap chmod gives the published copy its mode back.
                # Otherwise (e.g. a bind mount `ismount` cannot see, or a sticky target)
                # write in place, which refuses to clear a directory it cannot write.
                in_place = True
                if not old_mode & stat.S_IWUSR:
                    try:
                        target.chmod(old_mode | stat.S_IWUSR)
                        widened = True
                        try:
                            target.rename(old)
                            in_place = False
                        except OSError:
                            target.chmod(old_mode)
                            widened = False
                    except OSError:
                        pass
        if not in_place:
            bundle_dir.rename(target)
            swapped = True
    except BaseException:
        try:
            if old is not None and not swapped:
                # Decide from the filesystem, not the flags: an interrupt can land
                # between the rename and the swap.
                try:
                    if not target.exists():
                        old.rename(target)
                        if widened and old_mode is not None:
                            target.chmod(old_mode)
                    else:
                        old.rmdir()
                except OSError:
                    print(f"previous export is preserved at {old}", file=sys.stderr)
        finally:
            _remove_tree_quietly(staging_root)
        raise
    if in_place:
        assert old is not None
        try:
            old.rmdir()
        except OSError as e:
            print(f"warning: could not remove {old}: {e}", file=sys.stderr)
        _remove_tree_quietly(staging_root)
        _emit_in_place(bundle, target, children, out_dir)
        return
    # After the swap: chmod before it would break the cross-parent rename of a 0o555 dir.
    # The read sits in the same `try`: the export is published, so a failed stat must
    # still reach the leftover cleanup below.
    try:
        if stat.S_IMODE(target.stat().st_mode) != publish_mode:
            target.chmod(publish_mode)
    except OSError as e:
        print(f"warning: could not restore mode on {out_dir}: {e}", file=sys.stderr)
    for leftover, clean in (
        (staging_root, staging_root.rmdir),
        (old, (lambda: _remove_tree(old)) if old is not None else None),
    ):
        if leftover is None or clean is None:
            continue
        try:
            clean()
        except OSError as e:
            print(f"warning: could not remove {leftover}: {e}", file=sys.stderr)
