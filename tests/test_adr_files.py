"""Default ADR diffs and explicit writes preserve source records and human edits."""

import stat

import pytest

from sidegraph.profiles import AdrExport, get_profile
from sidegraph.schema import Decision, Provenance
from sidegraph.store import Store
from tests.test_adr import NOW, bytes_under, seed


def get_change(plan, rid):
    return next(change for change in plan.changes if change.record_id == rid)


def make_plan(db, out, config=None):
    from sidegraph.adr import read_snapshot
    from sidegraph.adr_files import plan_export

    return plan_export(read_snapshot(db), out, config or get_profile("generic-adr").adr_export)


def test_default_preview_and_repeat_are_read_only(tmp_path):
    from sidegraph.adr_files import apply_plan

    db = seed(tmp_path)
    out = tmp_path / "adr"
    source_before = bytes_under(db)
    plan = make_plan(db, out)
    assert not out.exists()
    assert get_change(plan, "ADR").action == "add"
    assert "+Choice for ADR." in get_change(plan, "ADR").diff
    apply_plan(plan)
    assert (out / "ADR.md").is_file()
    assert all(c.action == "unchanged" for c in make_plan(db, out).changes)
    assert bytes_under(db) == source_before


def test_updates_preserve_manual_suffix_and_file_mode(tmp_path):
    from sidegraph.adr_files import apply_plan

    db = seed(tmp_path)
    out = tmp_path / "adr"
    apply_plan(make_plan(db, out))
    file = out / "ADR.md"
    suffix = b"\nHuman scope: only two workers.\r\nDo not generalize.\r\n"
    file.write_bytes(file.read_bytes() + suffix)
    file.chmod(0o640)
    with Store(db) as store:
        store.add_decision(
            Decision(
                id="NEW",
                title="Change the architecture",
                kind="adr",
                status="accepted",
                context="New evidence.",
                choice="Use the successor.",
                valid_from=NOW,
                supersedes="ADR",
                provenance=Provenance(source="manual"),
            )
        )
    plan = make_plan(db, out)
    assert get_change(plan, "ADR").action == "update"
    apply_plan(plan)
    assert file.read_bytes().endswith(suffix)
    assert b"superseded" in file.read_bytes() and b"NEW.md" in file.read_bytes()
    assert stat.S_IMODE(file.stat().st_mode) == 0o640


@pytest.mark.parametrize(
    "before,after",
    [
        (b"Choice for ADR.", b"Human changed this."),
        (b"id=ADR ", b"id=OTHER "),
        (b"number=1 ", b"number=99 "),
    ],
)
def test_body_and_ownership_edits_conflict_without_any_write(tmp_path, before, after):
    from sidegraph.adr_files import apply_plan

    db = seed(tmp_path)
    out = tmp_path / "adr"
    apply_plan(make_plan(db, out))
    file = out / "ADR.md"
    original = file.read_bytes()
    assert before in original
    file.write_bytes(original.replace(before, after, 1))
    changed = bytes_under(out)
    plan = make_plan(db, out)
    assert any(c.action == "conflict" for c in plan.changes)
    with pytest.raises(ValueError):
        apply_plan(plan)
    assert bytes_under(out) == changed


def test_crlf_and_bom_checkout_is_a_noop(tmp_path):
    from sidegraph.adr_files import apply_plan

    db = seed(tmp_path)
    out = tmp_path / "adr"
    apply_plan(make_plan(db, out))
    file = out / "ADR.md"
    file.write_bytes(b"\xef\xbb\xbf" + file.read_bytes().replace(b"\n", b"\r\n"))
    before = bytes_under(out)
    plan = make_plan(db, out)
    assert all(c.action == "unchanged" for c in plan.changes)
    apply_plan(plan)
    assert bytes_under(out) == before


def test_numbered_names_do_not_renumber_when_an_older_id_is_added(tmp_path):
    from sidegraph.adr_files import apply_plan

    db = seed(tmp_path)
    out = tmp_path / "adr"
    config = AdrExport(filename="{number:04d}-{id}.md")
    apply_plan(make_plan(db, out, config))
    with Store(db) as store:
        store.add_decision(
            Decision(
                id="AAA",
                title="An older sorted ID",
                kind="adr",
                status="accepted",
                context="Synthetic.",
                choice="Keep stable names.",
                valid_from=NOW,
                provenance=Provenance(source="manual"),
            )
        )
    plan = make_plan(db, out, config)
    assert get_change(plan, "ADR").path == "0001-ADR.md"
    assert get_change(plan, "AAA").path == "0003-AAA.md"
    apply_plan(plan)
    assert (out / "0001-ADR.md").is_file()


def test_casefolded_human_destination_is_not_overwritten(tmp_path):
    from sidegraph.adr_files import apply_plan

    db = seed(tmp_path)
    out = tmp_path / "adr"
    out.mkdir()
    (out / "adr.md").write_text("Human architecture document.\n")
    before = bytes_under(out)
    plan = make_plan(db, out)
    assert get_change(plan, "ADR").action == "conflict"
    with pytest.raises(ValueError):
        apply_plan(plan)
    assert bytes_under(out) == before


def test_output_cannot_follow_an_ancestor_symlink_or_overlap_the_store(tmp_path):
    db = seed(tmp_path)
    target = tmp_path / "real"
    target.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(target, target_is_directory=True)
    for out in (alias / "adr", db / "adr"):
        with pytest.raises(ValueError):
            make_plan(db, out)
    assert not (target / "adr").exists() and not (db / "adr").exists()


def test_human_creation_after_plan_is_not_overwritten(tmp_path):
    from sidegraph.adr_files import apply_plan

    db = seed(tmp_path)
    out = tmp_path / "adr"
    plan = make_plan(db, out)
    out.mkdir()
    (out / "ADR.md").write_text("Human won the race.\n")
    before = bytes_under(out)
    with pytest.raises((ValueError, FileExistsError)):
        apply_plan(plan)
    assert bytes_under(out) == before


@pytest.mark.parametrize("source", ["doc-import", "doc", "import", "rationale", "graphify"])
def test_doc_imported_human_source_is_linked_without_copying_or_rewriting(tmp_path, source):
    from sidegraph.adr_files import apply_plan

    db = seed(tmp_path)
    out = tmp_path / "adr"
    out.mkdir()
    human = out / "0007-human.md"
    original = b"# A hand-written decision\n\n## Decision\n\nKeep the original document.\n"
    human.write_bytes(original)
    with Store(db) as store:
        store.add_decision(
            Decision(
                id="HUMAN",
                title="An existing human ADR",
                kind="adr",
                status="accepted",
                context="Human source.",
                choice="Read it.",
                valid_from=NOW,
                provenance=Provenance(source=source, ref="adr/0007-human.md"),
            )
        )
    plan = make_plan(db, out)
    assert get_change(plan, "HUMAN").action == "human-source"
    apply_plan(plan)
    assert human.read_bytes() == original
    assert not (out / "HUMAN.md").exists()
    assert b"0007-human.md" in (out / "index.md").read_bytes()


def test_out_of_population_projection_is_retained_and_not_presented_as_current(tmp_path):
    from sidegraph.adr_files import apply_plan

    db = seed(tmp_path)
    out = tmp_path / "adr"
    apply_plan(make_plan(db, out))
    old = (out / "ADR.md").read_bytes()
    # A later projection can intentionally select a smaller published population.
    from dataclasses import replace

    from sidegraph.adr import read_snapshot
    from sidegraph.adr_files import plan_export

    snapshot = read_snapshot(db)
    snapshot = replace(
        snapshot, decisions={rid: d for rid, d in snapshot.decisions.items() if rid != "ADR"}
    )
    plan = plan_export(snapshot, out, get_profile("generic-adr").adr_export)
    assert get_change(plan, "ADR").action == "retained"
    assert any("manual review" in w for w in plan.warnings)
    apply_plan(plan)
    assert (out / "ADR.md").read_bytes() == old
    assert b"](ADR.md)" not in (out / "index.md").read_bytes()


@pytest.mark.parametrize("filename", ["{id}.md", "{number:04d}-{id}.md"])
def test_later_human_number_overlap_warns_without_blocking_or_renumbering(tmp_path, filename):
    from sidegraph.adr_files import apply_plan

    db = seed(tmp_path)
    out = tmp_path / "adr"
    config = AdrExport(filename=filename)
    apply_plan(make_plan(db, out, config))
    human = out / "0001-human.md"
    human.write_bytes(b"# Human ADR written after the first export.\n")
    before = bytes_under(out)
    plan = make_plan(db, out, config)
    assert all(c.action == "unchanged" for c in plan.changes)
    assert any("human ADR number" in warning for warning in plan.warnings)
    apply_plan(plan)
    assert bytes_under(out) == before


@pytest.mark.parametrize(
    "name,number",
    [
        ("1729234567-note.md", 1),
        ("999x.md", 1),
        ("0007-human.md", 8),
        ("20261018-date.md", 20261019),
    ],
)
def test_only_bounded_delimited_human_prefixes_reserve_numbers(tmp_path, name, number):
    from sidegraph.adr_files import apply_plan

    db = seed(tmp_path)
    out = tmp_path / "adr"
    out.mkdir()
    human = out / name
    original = b"# Existing human document.\n"
    human.write_bytes(original)
    plan = make_plan(db, out)
    assert f"number={number} ".encode() in get_change(plan, "ADR").after
    apply_plan(plan)
    assert human.read_bytes() == original
    assert all(c.action == "unchanged" for c in make_plan(db, out).changes)


def test_exhausted_sequence_is_a_clear_error_before_writes(tmp_path):
    db = seed(tmp_path)
    out = tmp_path / "adr"
    out.mkdir()
    (out / "99999999-human.md").write_bytes(b"# Last possible human sequence.\n")
    before = bytes_under(out)
    with pytest.raises(ValueError, match="ADR sequence exhausted"):
        make_plan(db, out)
    assert bytes_under(out) == before


@pytest.mark.parametrize("kind", ["duplicate", "relocated", "malformed", "symlink-leaf"])
def test_ambiguous_or_unsafe_existing_projection_blocks_the_whole_plan(tmp_path, kind):
    from sidegraph.adr_files import apply_plan

    db = seed(tmp_path)
    out = tmp_path / "adr"
    apply_plan(make_plan(db, out))
    target = out / "ADR.md"
    original = target.read_bytes()
    if kind == "duplicate":
        (out / "copy.md").write_bytes(original)
    elif kind == "relocated":
        target.write_bytes(b"# Manual preface\n" + original)
    elif kind == "malformed":
        target.write_bytes(original.replace(b"sidegraph-adr:v1", b"sidegraph-adr:unknown", 1))
    else:
        human = tmp_path / "human.md"
        human.write_bytes(b"# External human document.\n")
        target.unlink()
        target.symlink_to(human)
    before = bytes_under(out)
    plan = make_plan(db, out)
    assert any(c.action == "conflict" for c in plan.changes)
    with pytest.raises(ValueError, match="conflicts"):
        apply_plan(plan)
    assert bytes_under(out) == before
