"""OpenSpec flow profile — golden parses, E1/E2/E3/E4 write-path rules, glob discovery.

See design/superpowers/specs/2026-08-06-openspec-profile-design.md §8 for the test
ledger (T1-T15); this module covers T2-T10, T13, T14 (T1 lives in test_flow_profile.py's
registry tests; T11/T12/T15 live in the bootstrap test files, since they need the
planner/apply seam).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from sidegraph.doc_import import import_docs, parse_decision_doc
from sidegraph.engine.reader import GraphifyReader
from sidegraph.profiles import GENERIC_ADR_DIALECT, OPENSPEC_DIALECT, get_profile
from sidegraph.store import Store

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "flows"
_TITLE_PATTERN = get_profile("openspec").title_pattern


def _read(rel: str) -> str:
    return (FIXTURES / rel).read_text(encoding="utf-8")


def _glob_discovers(profile_name: str, tree_rel: str) -> set[str]:
    """Same helper as test_flow_profile.py's — expand a profile's ingest_globs against a
    fixture tree shaped like the real flow's on-disk layout."""
    root = FIXTURES / tree_rel
    found: set[str] = set()
    for pattern in get_profile(profile_name).ingest_globs:
        found |= {str(p.relative_to(root)) for p in root.glob(pattern)}
    return found


def _file_anchor_graph(*doc_paths: Path) -> dict:
    """A minimal graph.json with one "document" node per path in ``doc_paths``, so
    `import_docs` never counts a record `skipped_unanchorable` here regardless of which
    record (parent or child) it is — `file_anchor` is shared across every record from a
    file, unlike a mention anchor (design/superpowers/specs/2026-08-06-openspec-profile-
    design.md is silent on anchoring; this is purely test plumbing, not spec behavior)."""
    nodes = []
    for i, doc_path in enumerate(doc_paths):
        node_path = os.path.relpath(str(doc_path), Path.cwd())
        nodes.append(
            {
                "id": f"doc-{i}",
                "label": doc_path.name,
                "norm_label": doc_path.name.lower(),
                "file_type": "document",
                "source_file": node_path,
                "community": "1",
            }
        )
    return {"built_at_commit": "openspecfixture", "nodes": nodes, "links": []}


def _reader_for(tmp_path: Path, *doc_paths: Path) -> GraphifyReader:
    graph_path = tmp_path / "g.json"
    graph_path.write_text(json.dumps(_file_anchor_graph(*doc_paths)), encoding="utf-8")
    return GraphifyReader(graph_path)


def _write(tmp_path: Path, rel: str, text: str) -> Path:
    path = tmp_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# -- T2 / T2b: E3 rule 1 (echo shape) ---------------------------------------------------


def test_openspec_split_echo_parent_suppressed_children_stand_alone(tmp_path):
    """Spec T2 + T2b: design.md (real upstream skeleton, `## Decisions` with 2 `###`
    children + a `**Rationale:**` label, no H1) imports as CHILDREN ONLY under
    `import_docs` — the parent's `choice` falls through to the deepest-fallback `##
    Context` body, which equals `context` verbatim (E3 rule 1, echo shape).

    Red against: unfixed code (the H1 gate rejects the whole doc -> 0 records, since E1
    isn't wired); a build without E3 (the parent would be written with `choice ==
    context`, so `skipped_degenerate_parent` stays 0 and a 3rd record shows up)."""
    doc = _write(tmp_path, "openspec/changes/add-thing/design.md", _read("openspec/design.md"))
    reader = _reader_for(tmp_path, doc)
    store = Store(tmp_path / "s.db")

    report = import_docs(store, reader, [str(doc)], profile="openspec", any_doc=True)

    assert report.imported == 2  # the two children only — no parent
    assert report.skipped_degenerate_parent == 1
    records = list(store.iter_decisions())
    assert len(records) == 2
    by_ref = {d.provenance.ref: d for d in records}
    assert set(by_ref) == {
        f"{doc}#normalize-the-ref-before-parsing",
        f"{doc}#keep-the-on-disk-path-for-anchor-lookups",
    }
    c1 = by_ref[f"{doc}#normalize-the-ref-before-parsing"]
    assert c1.title == "Normalize the ref before parsing"
    assert "**Rationale:**" in c1.choice
    assert c1.context.rstrip().endswith(f"imported from {doc}#normalize-the-ref-before-parsing")
    assert c1.rejected is None and c1.consequences is None  # doc-level sections stay on the parent


def test_openspec_split_non_echo_fallback_parent_still_written(tmp_path):
    """Spec T2b's second, discriminating fixture: the parent's `choice` comes from the
    BROADER `_deepest_choice_fallback` (a real, non-context `## Summary` section) rather
    than the exact echo predicate — E3 rule 1 must NOT suppress it. Red against rev-2's
    broader "came from `_deepest_choice_fallback`" rule, which deleted this record too."""
    doc = _write(
        tmp_path, "openspec/changes/add-thing/design.md", _read("openspec/design-with-summary.md")
    )
    reader = _reader_for(tmp_path, doc)
    store = Store(tmp_path / "s.db")

    report = import_docs(store, reader, [str(doc)], profile="openspec", any_doc=True)

    assert report.skipped_degenerate_parent == 0
    assert report.imported == 3  # parent + 2 children
    parent = next(d for d in store.iter_decisions() if d.provenance.ref == str(doc))
    assert "short teaser paragraph" in parent.choice


# -- T13: E3 rule 2 (empty shape) -------------------------------------------------------


def test_openspec_split_empty_parent_choice_children_stand_alone(tmp_path):
    """Spec T13 — E3 rule 2 (review Blocker N1): a split target with children and NO
    other section at all leaves the parent's `choice` empty even after every fallback.
    Red against unfixed code (the `:890-891`-era early return discarding both children
    along with the empty parent — 0 records instead of 2)."""
    doc = _write(
        tmp_path, "openspec/changes/add-thing/design.md", _read("openspec/design-empty.md")
    )
    reader = _reader_for(tmp_path, doc)
    store = Store(tmp_path / "s.db")

    report = import_docs(store, reader, [str(doc)], profile="openspec", any_doc=True)

    assert report.imported == 2
    assert report.skipped_degenerate_parent == 1
    refs = {d.provenance.ref for d in store.iter_decisions()}
    assert refs == {f"{doc}#first-decision", f"{doc}#second-decision"}


# -- T14: E4 (split targets are container sections, level <= 2) ------------------------


def test_openspec_h3_split_keyword_skipped_no_bogus_children(tmp_path):
    """Spec T14 — E4 (review Major N2): the fixture's only `split_choice`-matching
    heading, `### Decisions`, is an H3 with H3 siblings (`### Timeline`) — it must be
    skipped as a split target (not itself a container), so the doc falls back to an
    ordinary single-record priority-scan parse. Red against the unfixed H1-H3 selector,
    which would adopt `### Timeline` as a bogus child."""
    doc = _write(
        tmp_path, "openspec/changes/add-thing/design.md", _read("openspec/design-h3-split.md")
    )
    reader = _reader_for(tmp_path, doc)
    store = Store(tmp_path / "s.db")

    report = import_docs(store, reader, [str(doc)], profile="openspec", any_doc=True)

    assert report.imported == 1
    assert report.skipped_degenerate_parent == 0
    record = next(store.iter_decisions())
    assert record.provenance.ref == str(doc)  # no #fragment — no split fired
    assert "ship the thin slice first" in record.choice


# -- T3: proposal.md golden parse (no split, no E3) -------------------------------------


def test_openspec_proposal_golden_parse_single_record(tmp_path):
    """Spec T3: proposal.md fixture (Why / What Changes / Impact, no H1) -> 1 record, all
    three fields; no split fires (`split_choice` has no "what changes"-shaped keyword), so
    E3 never triggers and the parent IS the record. Also pins E1's synthesized title (the
    registry -> parser wiring `import_docs` must carry). Red against unfixed code (the H1
    gate rejects the whole doc)."""
    doc = _write(tmp_path, "openspec/changes/add-thing/proposal.md", _read("openspec/proposal.md"))
    reader = _reader_for(tmp_path, doc)
    store = Store(tmp_path / "s.db")

    report = import_docs(store, reader, [str(doc)], profile="openspec", any_doc=True)

    assert report.imported == 1
    assert report.skipped_degenerate_parent == 0
    record = next(store.iter_decisions())
    assert record.title == "add-thing (proposal)"
    assert "duplicates every archived proposal" in record.context
    assert "ref normalization" in record.choice
    assert record.consequences is not None and "importer" in record.consequences


# -- T4: title synthesis unit (direct parser call) --------------------------------------


def test_openspec_title_synthesized_from_path_live_and_archived_match():
    """Spec T4 (direct parser call): archived path -> "project-config (design)"; the
    live-path counterpart of the SAME doc -> the identical title (E1's title_pattern
    strips the archive prefix via its own optional group); a non-matching H1-less path ->
    rejected (the pattern doubles as a scope guard). Red against unfixed code (no
    synthesis at all -> both paths rejected for lack of an H1)."""
    text = "## Why\n\nSomething worth deciding.\n\n## What Changes\n\nDo the thing.\n"
    live = parse_decision_doc(
        text,
        "openspec/changes/project-config/design.md",
        dialect=OPENSPEC_DIALECT,
        title_pattern=_TITLE_PATTERN,
    )
    archived = parse_decision_doc(
        text,
        "openspec/changes/archive/2026-02-17-project-config/design.md",
        dialect=OPENSPEC_DIALECT,
        title_pattern=_TITLE_PATTERN,
    )
    assert live is not None and live.title == "project-config (design)"
    assert archived is not None and archived.title == "project-config (design)"

    other = parse_decision_doc(
        text, "docs/other/design.md", dialect=OPENSPEC_DIALECT, title_pattern=_TITLE_PATTERN
    )
    assert other is None


def test_openspec_h1_precedence_keeps_real_title():
    """Spec T5 (declared exception — passes before AND after E1): a doc WITH a real H1
    keeps it; synthesis is a fallback, never an override. Guards E1 over-reach."""
    text = "# Real title from the doc\n\n## Why\n\nSomething.\n\n## What Changes\n\nDo it.\n"
    parsed = parse_decision_doc(
        text,
        "openspec/changes/project-config/proposal.md",
        dialect=OPENSPEC_DIALECT,
        title_pattern=_TITLE_PATTERN,
    )
    assert parsed is not None
    assert parsed.title == "Real title from the doc"


def test_openspec_fixture_rejected_under_generic_adr_dialect_no_title_pattern():
    """Spec T9 (review Minor 9 — at `import_docs` level the glob gate fires first and a
    global synthesis bug would stay hidden): direct parser call, GENERIC_ADR_DIALECT, no
    `title_pattern` — the openspec fixture (no H1, openspec-only headings) is rejected.
    Red against a global (non-profile-gated) synthesis implementation."""
    parsed = parse_decision_doc(
        _read("openspec/design.md"), "openspec/changes/x/design.md", dialect=GENERIC_ADR_DIALECT
    )
    assert parsed is None


# -- T10: verified-non-empty `rejected` (R1) ---------------------------------------------


def test_openspec_alternatives_heading_lands_in_rejected():
    """Spec T10: a pre-canonical-era `## Alternatives` proposal section (R1, design note
    §3) lands in `rejected`. Red against a verified-empty `rejected` tuple."""
    parsed = parse_decision_doc(
        _read("openspec/proposal-alternatives.md"),
        "openspec/changes/reject-flag/proposal.md",
        dialect=OPENSPEC_DIALECT,
        title_pattern=_TITLE_PATTERN,
    )
    assert parsed is not None
    assert parsed.rejected is not None
    assert "commit messages" in parsed.rejected


# -- T6: glob discovery over a decoy-laden tree ------------------------------------------


def test_openspec_globs_discover_changes_only_not_specs_or_tasks():
    """Spec T6: exact-set assertion over a fixture tree carrying every §2 non-goal decoy
    (main capability spec, tasks.md, a delta spec, config.yaml) alongside a real
    live+archived proposal/design pair. Red against a wrong-glob profile."""
    found = _glob_discovers("openspec", "openspec/tree")
    assert found == {
        "openspec/changes/add-thing/proposal.md",
        "openspec/changes/add-thing/design.md",
        "openspec/changes/archive/2026-01-01-add-thing/proposal.md",
        "openspec/changes/archive/2026-01-01-add-thing/design.md",
    }
    assert "openspec/specs/cap/spec.md" not in found  # main capability spec (R3)
    assert "openspec/changes/add-thing/tasks.md" not in found  # checklist
    assert "openspec/changes/add-thing/specs/cap/spec.md" not in found  # delta spec (R3)
    assert "openspec/config.yaml" not in found  # not decision-shaped


# -- T7 / T8: E2 ref normalization across the archive move -------------------------------


def test_openspec_archive_move_reimport_skips_existing_stamp_matches(tmp_path):
    """Spec T7: import at the live path; re-import the SAME bytes at the archived path ->
    `skipped_existing`, store count unchanged, and the stored record's `context` stamp is
    byte-identical across the move (the Blocker-1 assertion). Red against unfixed code
    (duplicate: 2 records) AND a ref-only normalization (superseded+successor pair, the
    rev-1 design — `context` differs across the move so `_content_matches` never agrees)."""
    text = _read("openspec/proposal.md")
    live = _write(tmp_path, "openspec/changes/add-thing/proposal.md", text)
    archived = _write(tmp_path, "openspec/changes/archive/2026-01-01-add-thing/proposal.md", text)
    reader = _reader_for(tmp_path, live, archived)
    store = Store(tmp_path / "s.db")

    report1 = import_docs(store, reader, [str(live)], profile="openspec", any_doc=True)
    assert report1.imported == 1
    before = next(store.iter_decisions())

    report2 = import_docs(store, reader, [str(archived)], profile="openspec", any_doc=True)
    assert report2.imported == 0
    assert report2.superseded == 0
    assert report2.skipped_existing == 1
    records = list(store.iter_decisions())
    assert len(records) == 1
    assert records[0].context == before.context


def test_openspec_archive_move_with_edit_supersedes(tmp_path):
    """Spec T8 (coupled to T7 — Minor 10): an archived copy with ONE edited section
    supersedes the live-path record; the successor carries `supersedes`. Red against a
    dedup-that-skips-instead-of-superseding implementation."""
    text = _read("openspec/proposal.md")
    live = _write(tmp_path, "openspec/changes/add-thing/proposal.md", text)
    edited = text.replace(
        "The current importer duplicates every archived proposal on re-import.",
        "The current importer duplicates every archived proposal on re-import; now fixed.",
    )
    archived = _write(tmp_path, "openspec/changes/archive/2026-01-01-add-thing/proposal.md", edited)
    reader = _reader_for(tmp_path, live, archived)
    store = Store(tmp_path / "s.db")

    report1 = import_docs(store, reader, [str(live)], profile="openspec", any_doc=True)
    assert report1.imported == 1
    original = next(store.iter_decisions())

    report2 = import_docs(store, reader, [str(archived)], profile="openspec", any_doc=True)
    assert report2.superseded == 1
    open_now = [d for d in store.iter_decisions() if d.valid_to is None]
    assert len(open_now) == 1
    successor = open_now[0]
    assert successor.supersedes == original.id
    assert "now fixed." in successor.context


# -- I2 (R1 improvement wave §2): in-flight note for live-tree imports -------------------
#
# Unlike the tests above, these need `import_docs`' OWN D7.1 glob-enforcement path (not
# `any_doc=True`) to exercise the I2 trigger predicate's glob-gate clause honestly, and the
# predicate resolves the on-disk path against the REAL repo root (`_glob_repo_root`) --
# `monkeypatch.chdir(tmp_path)` makes the (non-git) tmp_path itself the fallback root, same
# trick T12 (test_bootstrap_apply.py) already relies on.


def test_i2a_live_import_stamps_every_record_parent_and_children(tmp_path, monkeypatch):
    """T-I2a (first half): live-path import -> every record's context ends with the note,
    parent AND children alike. Red against unfixed code (no in_flight_note wiring)."""
    monkeypatch.chdir(tmp_path)
    rel = "openspec/changes/add-thing/design.md"
    doc = _write(tmp_path, rel, _read("openspec/design-with-summary.md"))
    reader = _reader_for(tmp_path, doc)
    store = Store(tmp_path / "s.db")

    report = import_docs(store, reader, [rel], profile="openspec")

    assert report.imported == 3  # parent + 2 children
    note = get_profile("openspec").in_flight_note
    for d in store.iter_decisions():
        assert d.context.rstrip().endswith(note)


def test_i2a_archived_import_has_no_note_anywhere(tmp_path, monkeypatch):
    """T-I2a (second half): archived-path import -> no note anywhere."""
    monkeypatch.chdir(tmp_path)
    rel = "openspec/changes/archive/2026-01-01-add-thing/design.md"
    doc = _write(tmp_path, rel, _read("openspec/design-with-summary.md"))
    reader = _reader_for(tmp_path, doc)
    store = Store(tmp_path / "s.db")

    report = import_docs(store, reader, [rel], profile="openspec")

    assert report.imported == 3
    note = get_profile("openspec").in_flight_note
    for d in store.iter_decisions():
        assert note not in d.context


def test_i2b_live_then_archived_reimport_supersedes_once_per_record(tmp_path, monkeypatch):
    """T-I2b: import live, then re-import the SAME bytes at the archived path -> each
    record superseded once, successors carry no note, `supersedes` links each pair, and
    children's `#fragment` refs are unchanged by the note. Red against a note-in-both
    implementation (which would dedup as skipped_existing instead of superseding, since
    the note would be present -- and identical -- on both sides)."""
    monkeypatch.chdir(tmp_path)
    text = _read("openspec/design-with-summary.md")
    live_rel = "openspec/changes/add-thing/design.md"
    archived_rel = "openspec/changes/archive/2026-01-01-add-thing/design.md"
    live = _write(tmp_path, live_rel, text)
    archived = _write(tmp_path, archived_rel, text)
    reader = _reader_for(tmp_path, live, archived)
    store = Store(tmp_path / "s.db")

    report1 = import_docs(store, reader, [live_rel], profile="openspec")
    assert report1.imported == 3
    old_by_fragment = {}
    for d in store.iter_decisions():
        fragment = d.provenance.ref.split("#", 1)[1] if "#" in d.provenance.ref else None
        old_by_fragment[fragment] = d
    old_ids = {d.id for d in old_by_fragment.values()}

    report2 = import_docs(store, reader, [archived_rel], profile="openspec")
    assert report2.imported == 0
    assert report2.superseded == 3
    assert report2.skipped_existing == 0

    note = get_profile("openspec").in_flight_note
    successors = [d for d in store.iter_decisions() if d.id not in old_ids]
    assert len(successors) == 3
    for successor in successors:
        assert note not in successor.context
        fragment = (
            successor.provenance.ref.split("#", 1)[1] if "#" in successor.provenance.ref else None
        )
        old = old_by_fragment[fragment]  # same #fragment -- unchanged by the note
        assert successor.supersedes == old.id


def test_i2c_already_archived_doc_reimported_twice_dedups(tmp_path, monkeypatch):
    """T-I2c (E2 regression pin, declared exception -- passes before AND after): an
    already-archived doc imported twice dedups exactly as before (`skipped_existing`, no
    pairs) -- I2 must not reopen Blocker 1."""
    monkeypatch.chdir(tmp_path)
    rel = "openspec/changes/archive/2026-01-01-add-thing/design.md"
    doc = _write(tmp_path, rel, _read("openspec/design-with-summary.md"))
    reader = _reader_for(tmp_path, doc)
    store = Store(tmp_path / "s.db")

    report1 = import_docs(store, reader, [rel], profile="openspec")
    assert report1.imported == 3

    report2 = import_docs(store, reader, [rel], profile="openspec")
    assert report2.imported == 0
    assert report2.superseded == 0
    assert report2.skipped_existing == 3


def test_i2d_live_echo_parent_still_suppressed_children_carry_note(tmp_path, monkeypatch):
    """T-I2d (Blocker-1 pin): a LIVE doc whose parent is the echo shape -> parent still
    suppressed (`skipped_degenerate_parent == 1`), children written WITH the note. Red
    against a stamp-during-construction implementation (the spec-rev-1 design): stamping
    before the echo decision runs would corrupt `_choice_is_context_echo`'s own
    strip-and-compare, flipping the suppression off (3 records instead of 2)."""
    monkeypatch.chdir(tmp_path)
    rel = "openspec/changes/add-thing/design.md"
    doc = _write(tmp_path, rel, _read("openspec/design.md"))
    reader = _reader_for(tmp_path, doc)
    store = Store(tmp_path / "s.db")

    report = import_docs(store, reader, [rel], profile="openspec")

    assert report.skipped_degenerate_parent == 1
    assert report.imported == 2
    note = get_profile("openspec").in_flight_note
    for d in store.iter_decisions():
        assert d.context.rstrip().endswith(note)
