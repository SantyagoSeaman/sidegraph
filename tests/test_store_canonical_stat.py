"""The ``canonical_stat`` table (digest-integrity design, Task 1/2): a derived, gitignored
record of every canonical file's ``(size, mtime_ns)`` AS THIS INDEX WROTE OR LOADED IT --
written beside the file, in the SAME transaction, by every one of the seven canonical
writers named in the spec (six via ``_atomic_write_json`` + tmp/``os.replace``, plus
``_write_archive_segment``'s exclusive ``os.link`` publish). ``_touch_digest`` (Task 3)
compares this table against a fresh filesystem walk before stamping the freshness digest;
this file only pins the table's own bookkeeping -- capture, reload, and compact -- not that
downstream refusal (see ``test_digest_integrity.py``).

These tests cannot run at all against the pre-Task-1 code: ``canonical_stat`` does not
exist yet, so every query against it raises ``sqlite3.OperationalError: no such table``.
That is the red-first evidence for this file -- reported as "cannot run", not as a
behavioural red (mirrors ``test_store_mutation_immediate.py``'s own framing for the same
reason)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from sidegraph.schema import (
    AnchorBinding,
    Decision,
    DecisionKind,
    Domain,
    Entity,
    Fact,
    Initiative,
    Provenance,
)
from sidegraph.store import Store, _atomic_write_json


def _decision(title: str = "an adr", **overrides) -> Decision:
    base = dict(
        title=title,
        kind=DecisionKind.ADR,
        context="ctx",
        choice="choice",
        valid_from=datetime.now(UTC),
        provenance=Provenance(source="manual"),
    )
    base.update(overrides)
    return Decision(**base)


def _fact(statement: str = "a fact", **overrides) -> Fact:
    base = dict(
        statement=statement,
        source="observed",
        valid_from=datetime.now(UTC),
        provenance=Provenance(source="manual"),
    )
    base.update(overrides)
    return Fact(**base)


def _domain(slug: str = "a-domain", **overrides) -> Domain:
    base = dict(
        slug=slug,
        title=slug,
        summary="A domain used for canonical-stat testing.",
        provenance=Provenance(source="manual"),
    )
    base.update(overrides)
    return Domain(**base)


def _walk_actual_stat(store: Store, subdir: str, stem: str) -> tuple[int, int]:
    ext = "jsonl" if subdir == "archive" else "json"
    st = (store.path / subdir / f"{stem}.{ext}").stat()
    return st.st_size, st.st_mtime_ns


def _stat_row(store: Store, subdir: str, stem: str) -> tuple[int, int] | None:
    row = store._conn.execute(
        "SELECT size, mtime_ns FROM canonical_stat WHERE subdir = ? AND stem = ?",
        (subdir, stem),
    ).fetchone()
    return (row["size"], row["mtime_ns"]) if row else None


# -- Step 2 test 1: every one of the seven writers leaves a matching row --------------------


def _case_decision(store: Store) -> tuple[str, str]:
    d = store.add_decision(_decision("case-decision"))
    return "decisions", d.id


def _case_fact(store: Store) -> tuple[str, str]:
    f = store.add_fact(_fact("case-fact"))
    return "facts", f.id


def _case_domain(store: Store) -> tuple[str, str]:
    dom = store.add_domain(_domain("case-domain"))
    return "domains", dom.domain_id


def _case_entity(store: Store) -> tuple[str, str]:
    e = store.upsert_entity(Entity(canonical_name="case-entity"))
    return "entities", e.entity_id


def _case_bindings(store: Store) -> tuple[str, str]:
    e = store.upsert_entity(Entity(canonical_name="case-binding-entity"))
    d = store.add_decision(_decision("case-binding-target"))
    store.add_binding(AnchorBinding(record_id=d.id, entity_id=e.entity_id, tier=2))
    return "bindings", d.id


def _case_initiative(store: Store) -> tuple[str, str]:
    i = store.upsert_initiative(Initiative(name="case-initiative"))
    return "initiatives", i.id


def _case_archive_segment(store: Store) -> tuple[str, str]:
    old = store.add_decision(_decision("case-archive-old"))
    store.add_decision(_decision("case-archive-new", supersedes=old.id))  # closes `old`
    report = store.compact()
    assert report.segment_path is not None
    return "archive", Path(report.segment_path).stem


_STAT_CASES = {
    "decision": _case_decision,
    "fact": _case_fact,
    "domain": _case_domain,
    "entity": _case_entity,
    "bindings": _case_bindings,
    "initiative": _case_initiative,
    "archive_segment": _case_archive_segment,
}


@pytest.mark.parametrize("case_name", sorted(_STAT_CASES))
def test_every_canonical_writer_records_a_matching_stat_row(tmp_path, case_name):
    """One of the seven canonical writers per case -- not one sample generalised to all
    seven, which is exactly the mistake D4a footnotes (an earlier draft's "each
    `_write_*_canonical`" phrasing silently excluded the bindings writer and the archive
    segment writer)."""
    store = Store(tmp_path / "s")
    subdir, stem = _STAT_CASES[case_name](store)
    row = _stat_row(store, subdir, stem)
    assert row is not None, f"{subdir}/{stem} has no canonical_stat row"
    assert row == _walk_actual_stat(store, subdir, stem)


# -- Step 2 test 2: a rolled-back write leaves the file but no row --------------------------


def test_a_rolled_back_write_leaves_the_canonical_file_but_no_stat_row(tmp_path, monkeypatch):
    """``os.replace`` cannot be undone -- a canonical file published just before a LATER
    step in the same ``_mutation()`` fails stays on disk. The ``canonical_stat`` row for it
    is inserted in the SAME (still-uncommitted) transaction, so it rolls back with
    everything else -- which is exactly the state ``_touch_digest``'s refusal (Task 3)
    needs to see: a file present with no matching row, so the next open reloads instead of
    a later stamp certifying content this process never actually indexed."""
    store = Store(tmp_path / "s")

    def _boom(self, decision):
        raise RuntimeError("index write failed")

    monkeypatch.setattr(Store, "_index_write_decision", _boom)
    decision = _decision("orphan")
    with pytest.raises(RuntimeError):
        store.add_decision(decision)

    assert (store.path / "decisions" / f"{decision.id}.json").is_file()
    assert _stat_row(store, "decisions", decision.id) is None
    assert store._conn.in_transaction is False


# == Task 2: reload rebuilds the table, compact removes rows ================================


def _all_canonical_files(store: Store) -> list[tuple[str, str]]:
    """Every ``(subdir, stem)`` this store's canonical directories currently hold, hot
    records plus archive segments -- mirrors the reload's own set of directories."""
    out: list[tuple[str, str]] = []
    for sub in ("decisions", "facts", "domains", "entities", "bindings", "initiatives"):
        d = store.path / sub
        if d.is_dir():
            out.extend((sub, f.stem) for f in d.glob("*.json"))
    archive_dir = store.path / "archive"
    if archive_dir.is_dir():
        out.extend(("archive", f.stem) for f in archive_dir.glob("*.jsonl"))
    return out


def test_reload_rebuilds_canonical_stat_for_every_file_it_read(tmp_path):
    """After ``_reload_index_from_canonical`` (forced here by deleting ``index.db`` so the
    next open has no digest to match and takes the full-reload path), the table describes
    every canonical file the reload actually read -- hot records AND an archived segment
    together, so this also covers Step 1 test 3 (an archive segment absorbed by a reload)."""
    path = tmp_path / "s"
    setup = Store(path)
    setup.upsert_entity(Entity(canonical_name="reload-entity"))
    setup.add_decision(_decision("reload-decision"))
    setup.add_fact(_fact("reload-fact"))
    setup.add_domain(_domain("reload-domain"))
    setup.upsert_initiative(Initiative(name="reload-initiative"))
    old = setup.add_decision(_decision("reload-archive-old"))
    setup.add_decision(_decision("reload-archive-new", supersedes=old.id))
    setup.compact()  # writes an archive segment -- exercised by the reload below too
    setup.close()

    (path / "index.db").unlink()  # simulate a fresh clone: no derived index at all yet
    reopened = Store(path)

    files = _all_canonical_files(reopened)
    assert len(files) >= 6  # every kind represented, including the archive segment
    for subdir, stem in files:
        assert _stat_row(reopened, subdir, stem) == _walk_actual_stat(reopened, subdir, stem), (
            f"{subdir}/{stem} row does not match its on-disk stat after reload"
        )
    reopened.close()


def test_compact_deletes_the_stat_row_of_hot_files_it_removes(tmp_path):
    """``compact`` removes a hot file's canonical_stat row in the same step it unlinks the
    file -- the row must not outlive the file it describes as gone."""
    store = Store(tmp_path / "s")
    old = store.add_decision(_decision("compact-old"))
    store.add_decision(_decision("compact-new", supersedes=old.id))  # closes `old`
    assert _stat_row(store, "decisions", old.id) is not None  # present before compact

    store.compact()

    assert not (store.path / "decisions" / f"{old.id}.json").is_file()
    assert _stat_row(store, "decisions", old.id) is None


def test_compact_keeps_the_stat_row_of_a_hot_file_it_keeps_on_mismatch(tmp_path):
    """Mirrors ``_hot_file_matches``'s own skip (store.py, compact): a hot file whose
    on-disk content diverged from what compact was about to archive is left in place
    (never destroy data) -- and its canonical_stat row is left alone too, since the file it
    describes is still exactly the file on disk."""
    store = Store(tmp_path / "s")
    old = store.add_decision(_decision("compact-tamper-old"))
    store.add_decision(_decision("compact-tamper-new", supersedes=old.id))
    row_before = _stat_row(store, "decisions", old.id)
    assert row_before is not None

    hot_path = store.path / "decisions" / f"{old.id}.json"
    tampered = old.model_copy(update={"context": "changed out from under compact"})
    # Written directly (bypassing the store, so no canonical_stat update happens here
    # either), simulating a hand edit -- the same shape
    # test_store_compact_review.py's own mismatch test uses.
    _atomic_write_json(hot_path, tampered.model_dump(mode="json"))

    store.compact()

    assert hot_path.is_file()  # never deleted -- compact warns instead
    # The row is UNCHANGED (still the pre-tamper stat is wrong now -- compact's own skip
    # means neither the file nor its row was touched by this compact() call at all): what
    # matters here is that the row was not DELETED just because compact chose not to
    # archive-and-remove this file.
    assert _stat_row(store, "decisions", old.id) is not None
