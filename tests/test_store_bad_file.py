"""One unparseable record file no longer switches off the whole store.

Before this, a canonical file that did not parse (a merge left conflict markers in it) or that
the models rejected made the index rebuild raise, so the store could not be opened at all. Now
the reload skips such a file, lists it in the index's ``skipped_canonical_files`` meta key and
carries on; an archive line that does not parse is skipped the same way in the reload only; and
no canonical writer overwrites a file the reload left out.
see design/superpowers/specs/2026-10-03-store-survives-a-bad-file-design.md (D1-D7; T1-T12, T15)
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import NamedTuple

import pytest
from ulid import ULID

import sidegraph.store as store_module
from sidegraph import verify
from sidegraph.schema import (
    AnchorBinding,
    Decision,
    DecisionKind,
    DecisionStatus,
    Descriptor,
    Domain,
    Entity,
    Fact,
    Initiative,
    Provenance,
)
from sidegraph.store import Store

SKIP_KEY = "skipped_canonical_files"
CONFLICT = '<<<<<<< HEAD\n{"id": "x"}\n=======\n{"id": "y"}\n>>>>>>> branch\n'
SEGMENT = "2020-01-01-1-deadbeef0000.jsonl"


def _prov() -> Provenance:
    return Provenance(source="manual")


def _decision(title: str = "an adr", **over) -> Decision:
    base: dict = dict(
        title=title,
        kind=DecisionKind.ADR,
        context="c",
        choice="ch",
        valid_from=datetime.now(UTC),
        provenance=_prov(),
    )
    base.update(over)
    return Decision(**base)


def _fact(**over) -> Fact:
    base: dict = dict(
        statement="httpx retries idempotent requests by default.",
        source="httpx docs",
        valid_from=datetime.now(UTC),
        provenance=_prov(),
    )
    base.update(over)
    return Fact(**base)


class Seed(NamedTuple):
    db: Path
    decisions: list[Decision]
    entity: Entity
    fact: Fact


def _seed(tmp_path) -> Seed:
    """A closed store with three decisions, one entity bound to all three, and one fact."""
    db = tmp_path / ".sidegraph"
    with Store(db) as s:
        entity = s.upsert_entity(
            Entity(canonical_name="widget", descriptor=Descriptor(name="widget", file_path="a.py"))
        )
        decisions = [s.add_decision(_decision(f"decision {i}")) for i in range(3)]
        for d in decisions:
            s.add_binding(AnchorBinding(record_id=d.id, entity_id=entity.entity_id, tier=2))
        fact = s.add_fact(_fact())
    return Seed(db, decisions, entity, fact)


def _put(db: Path, rel: str, text: str) -> Path:
    path = db / rel
    path.write_text(text, encoding="utf-8")
    return path


def _put_json(db: Path, rel: str, data: object) -> Path:
    return _put(db, rel, json.dumps(data))


def _read(db: Path, rel: str) -> dict:
    return json.loads((db / rel).read_text(encoding="utf-8"))


def _skip_list(s: Store) -> list[dict]:
    row = s._conn.execute("SELECT value FROM meta WHERE key = ?", (SKIP_KEY,)).fetchone()
    return json.loads(row["value"]) if row else []


def _skipped(s: Store) -> list[tuple[str, str]]:
    return [(e["path"], e["reason"]) for e in _skip_list(s)]


def _binding_rows(s: Store, record_id: str) -> int:
    return s._conn.execute(
        "SELECT COUNT(*) FROM anchor_bindings WHERE record_id = ?", (record_id,)
    ).fetchone()[0]


def _segment(db: Path, lines: list[str | bytes], name: str = SEGMENT) -> Path:
    (db / "archive").mkdir(exist_ok=True)
    body = b"\n".join(x if isinstance(x, bytes) else x.encode("utf-8") for x in lines) + b"\n"
    path = db / "archive" / name
    path.write_bytes(body)
    return path


def _archived(**over) -> Decision:
    now = datetime.now(UTC)
    return _decision(
        "archived", status=DecisionStatus.SUPERSEDED, valid_from=now, valid_to=now, **over
    )


def _line(d: Decision) -> str:
    return store_module._archive_record_line("decision", d.model_dump(mode="json"))


class _ReloadCounter:
    def __init__(self, monkeypatch) -> None:
        self.n = 0
        orig = Store._reload_index_from_canonical
        counter = self

        def counting(self_, digest):
            counter.n += 1
            return orig(self_, digest)

        monkeypatch.setattr(Store, "_reload_index_from_canonical", counting)


# -- D1: a record file that does not parse is skipped, not fatal --------------------------


def test_t1_a_decision_file_with_conflict_markers_is_skipped(tmp_path):
    seed = _seed(tmp_path)
    bad, *others = seed.decisions
    _put(seed.db, f"decisions/{bad.id}.json", CONFLICT)

    with Store(seed.db) as s:
        assert s.get_decision(bad.id) is None
        assert all(s.get_decision(d.id) is not None for d in others)
        found = s.valid_decisions_for_entity(seed.entity.entity_id)
        assert {d.id for d in found} == {d.id for d in others}
        assert _skipped(s) == [(f"decisions/{bad.id}.json", "parse-error")]


def test_t2_an_entity_file_with_conflict_markers_is_skipped(tmp_path):
    seed = _seed(tmp_path)
    _put(seed.db, f"entities/{seed.entity.entity_id}.json", CONFLICT)

    with Store(seed.db) as s:
        assert s.get_entity(seed.entity.entity_id) is None
        # The bindings file is intact, so the rows still point at the missing entity.
        found = s.valid_decisions_for_entity(seed.entity.entity_id)
        assert {d.id for d in found} == {d.id for d in seed.decisions}
        assert all(s.get_decision(d.id) is not None for d in seed.decisions)
        assert _skipped(s) == [(f"entities/{seed.entity.entity_id}.json", "parse-error")]


def test_t3_a_bindings_file_with_conflict_markers_is_skipped(tmp_path):
    seed = _seed(tmp_path)
    bad, *others = seed.decisions
    _put(seed.db, f"bindings/{bad.id}.json", CONFLICT)

    with Store(seed.db) as s:
        assert s.get_decision(bad.id) is not None
        assert s.bindings_for_record(bad.id) == []
        assert all(len(s.bindings_for_record(d.id)) == 1 for d in others)
        assert _skipped(s) == [(f"bindings/{bad.id}.json", "parse-error")]


def test_t4_a_fact_file_with_conflict_markers_is_skipped(tmp_path):
    seed = _seed(tmp_path)
    _put(seed.db, f"facts/{seed.fact.id}.json", CONFLICT)

    with Store(seed.db) as s:
        assert s.get_fact(seed.fact.id) is None
        assert all(s.get_decision(d.id) is not None for d in seed.decisions)
        assert _skipped(s) == [(f"facts/{seed.fact.id}.json", "parse-error")]


def test_t5_a_decision_that_parses_but_does_not_validate_is_skipped(tmp_path):
    seed = _seed(tmp_path)
    bad = seed.decisions[0]
    _put_json(
        seed.db,
        f"decisions/{bad.id}.json",
        {**_read(seed.db, f"decisions/{bad.id}.json"), "status": "bogus"},
    )

    with Store(seed.db) as s:
        assert s.get_decision(bad.id) is None
        assert _skipped(s) == [(f"decisions/{bad.id}.json", "parse-error")]


def test_t5b_a_title_that_cannot_be_serialised_is_skipped(tmp_path):
    """A lone surrogate parses and validates, then fails ``model_dump_json``: unfixed, inside
    the index write."""
    seed = _seed(tmp_path)
    bad = seed.decisions[0]
    data = {**_read(seed.db, f"decisions/{bad.id}.json"), "title": "\ud800"}
    _put(seed.db, f"decisions/{bad.id}.json", json.dumps(data))

    with Store(seed.db) as s:
        assert s.get_decision(bad.id) is None
        assert _skipped(s) == [(f"decisions/{bad.id}.json", "parse-error")]


def test_t5c_a_file_nested_past_the_recursion_limit_is_skipped(tmp_path):
    seed = _seed(tmp_path)
    bad = seed.decisions[0]
    _put(seed.db, f"decisions/{bad.id}.json", "[" * 200_000 + "]" * 200_000)

    with Store(seed.db) as s:
        assert s.get_decision(bad.id) is None
        assert _skipped(s) == [(f"decisions/{bad.id}.json", "parse-error")]


# -- D3: a bindings file is skipped as a unit ----------------------------------------------


def _bad_bindings(seed: Seed, kind: str) -> object:
    ok = {"entity_id": seed.entity.entity_id, "tier": 2, "relation": "affects", "weight": 1.0}
    return {
        "object": {"a": 1},
        "empty-object": {},
        "invalid-item": [ok, {"entity_id": seed.entity.entity_id, "tier": 9}],
        "non-object-item": [ok, 1],
    }[kind]


@pytest.mark.parametrize("kind", ["object", "empty-object", "invalid-item", "non-object-item"])
def test_t6_a_bindings_file_of_the_wrong_shape_is_skipped_whole(tmp_path, kind):
    seed = _seed(tmp_path)
    bad = seed.decisions[0]
    _put_json(seed.db, f"bindings/{bad.id}.json", _bad_bindings(seed, kind))

    with Store(seed.db) as s:
        assert _binding_rows(s, bad.id) == 0
        assert s.get_decision(bad.id) is not None
        assert _skipped(s) == [(f"bindings/{bad.id}.json", "parse-error")]


# -- D4: an archive line that does not parse is skipped, in the reload only ------------------


def test_t7_an_archive_segment_with_a_conflict_line_loads_its_other_lines(tmp_path):
    seed = _seed(tmp_path)
    good = _archived()
    _segment(seed.db, [_line(good), "<<<<<<< HEAD"])

    with Store(seed.db) as s:
        assert s.get_decision(good.id) is not None
        assert _skipped(s) == [(f"archive/{SEGMENT}", "bad-archive-segment")]


def test_t7b_an_archive_line_whose_payload_does_not_validate_is_skipped(tmp_path):
    seed = _seed(tmp_path)
    good = _archived()
    bad = {**json.loads(_line(_archived())), "status": "bogus"}
    _segment(seed.db, [_line(good), json.dumps(bad)])

    with Store(seed.db) as s:
        assert s.get_decision(good.id) is not None
        assert s.get_decision(bad["id"]) is None
        assert _skipped(s) == [(f"archive/{SEGMENT}", "bad-archive-segment")]


def test_t7c_an_archive_line_of_invalid_utf8_costs_only_that_line(tmp_path):
    seed = _seed(tmp_path)
    good = _archived()
    _segment(seed.db, [_line(good), b'{"record_type": "decision", "title": "\xff\xfe"}'])

    with Store(seed.db) as s:
        assert s.get_decision(good.id) is not None
        assert _skipped(s) == [(f"archive/{SEGMENT}", "bad-archive-segment")]


def test_t7d_a_hot_file_that_fails_validation_does_not_hide_its_archived_copy(tmp_path):
    seed = _seed(tmp_path)
    hot = seed.decisions[0]
    _segment(seed.db, [_line(hot)])
    _put_json(
        seed.db,
        f"decisions/{hot.id}.json",
        {**_read(seed.db, f"decisions/{hot.id}.json"), "status": "bogus"},
    )

    with Store(seed.db) as s:
        assert s.get_decision(hot.id) is not None
        assert _skipped(s) == [(f"decisions/{hot.id}.json", "parse-error")]


@pytest.mark.parametrize("char", ["\u2028", "\u0085"])
def test_a_compacted_decision_whose_title_holds_a_unicode_line_separator_reopens(tmp_path, char):
    """``str.splitlines`` splits a segment line at U+2028 and U+0085, which a title can hold and
    ``ensure_ascii=False`` writes raw. Before the strict reader split bytes, such a segment made
    the reload raise "corrupt archive segment", so the store the compaction left would not open.
    A fresh index forces the reload, as on a fresh clone."""
    db = tmp_path / ".sidegraph"
    title = f"before{char}after"
    with Store(db) as s:
        old = s.add_decision(_decision(title))
        s.add_decision(_decision("successor", supersedes=old.id))
        assert s.compact().decisions_compacted == 1
    for stale in db.glob("index.db*"):
        stale.unlink()

    with Store(db) as s:
        archived = s.get_decision(old.id)
        assert archived is not None
        assert archived.title == title
        assert _skip_list(s) == []


# -- D1/D2: the skip is certified, and it heals ---------------------------------------------


def test_t8_a_write_after_a_skip_still_certifies_the_digest(tmp_path, monkeypatch):
    """Without the skipped file's ``canonical_stat`` row the write's ``_touch_digest`` cannot
    certify the walk, and the third open rebuilds."""
    seed = _seed(tmp_path)
    _put(seed.db, f"decisions/{seed.decisions[0].id}.json", CONFLICT)
    with Store(seed.db) as s:  # the reload that skips
        s.add_decision(_decision("a record from the second session"))
    counter = _ReloadCounter(monkeypatch)

    with Store(seed.db) as s:
        assert _skipped(s) == [(f"decisions/{seed.decisions[0].id}.json", "parse-error")]

    assert counter.n == 0


def test_t10_restoring_the_file_heals_the_store(tmp_path):
    seed = _seed(tmp_path)
    bad = seed.decisions[0]
    path = seed.db / "decisions" / f"{bad.id}.json"
    original = path.read_text(encoding="utf-8")
    path.write_text(CONFLICT, encoding="utf-8")
    Store(seed.db).close()

    path.write_text(original, encoding="utf-8")

    with Store(seed.db) as s:
        assert s.get_decision(bad.id) is not None
        assert _skip_list(s) == []


# -- D5: no canonical writer overwrites a skipped file --------------------------------------


def test_t9_a_binding_write_over_a_skipped_bindings_file_is_refused(tmp_path):
    seed = _seed(tmp_path)
    bad = seed.decisions[0]
    path = _put(seed.db, f"bindings/{bad.id}.json", CONFLICT)
    before = path.read_bytes()

    with Store(seed.db) as s:
        with pytest.raises(ValueError, match=f"bindings/{bad.id}.json"):
            s.add_binding(AnchorBinding(record_id=bad.id, entity_id=seed.entity.entity_id, tier=1))
        assert _binding_rows(s, bad.id) == 0
    assert path.read_bytes() == before


def _writers() -> dict:
    prov = _prov()
    return {
        "entities": lambda s, i: s._write_entity_canonical(Entity(entity_id=i, canonical_name="x")),
        "decisions": lambda s, i: s._write_decision_canonical(_decision(id=i)),
        "facts": lambda s, i: s._write_fact_canonical(_fact(id=i)),
        "domains": lambda s, i: s._write_domain_canonical(
            Domain(domain_id=i, slug="d", title="D", summary="s", provenance=prov)
        ),
        "initiatives": lambda s, i: s._write_initiative_canonical(Initiative(id=i, name="n")),
        "bindings": lambda s, i: s._write_bindings_file(i, []),
    }


@pytest.mark.parametrize("subdir", list(_writers()))
def test_t9b_every_canonical_writer_refuses_a_skipped_file(tmp_path, subdir):
    db = tmp_path / ".sidegraph"
    Store(db).close()
    rid = str(ULID())
    path = _put(db, f"{subdir}/{rid}.json", CONFLICT)
    before = path.read_bytes()

    with Store(db) as s:
        assert _skipped(s) == [(f"{subdir}/{rid}.json", "parse-error")]
        with pytest.raises(ValueError, match=f"{subdir}/{rid}.json"):
            _writers()[subdir](s, rid)
    assert path.read_bytes() == before


def test_is_skipped_reads_the_skip_list_and_follows_a_heal(tmp_path):
    seed = _seed(tmp_path)
    bad, other, _ = seed.decisions
    original = (seed.db / "decisions" / f"{bad.id}.json").read_text(encoding="utf-8")
    _put(seed.db, f"decisions/{bad.id}.json", CONFLICT)

    with Store(seed.db) as s:
        assert s.is_skipped("decisions", bad.id)
        assert not s.is_skipped("decisions", other.id)
        assert not s.is_skipped("facts", bad.id)  # same stem, another directory

    _put(seed.db, f"decisions/{bad.id}.json", original)
    with Store(seed.db) as s:
        assert not s.is_skipped("decisions", bad.id)


def test_the_skip_reasons_are_the_words_verify_uses():
    assert store_module.SKIP_PARSE_ERROR == verify.PARSE_ERROR
    assert store_module.SKIP_BAD_ARCHIVE_SEGMENT == verify.BAD_ARCHIVE_SEGMENT


# -- D4/D6: compact -------------------------------------------------------------------------


def test_t11b_a_dry_run_compact_ignores_a_skipped_hot_file_with_an_archived_copy(tmp_path):
    seed = _seed(tmp_path)
    hot = seed.decisions[0]
    _segment(seed.db, [_line(hot)])
    path = _put(seed.db, f"decisions/{hot.id}.json", CONFLICT)

    with Store(seed.db) as s:
        report = s.compact(dry_run=True)
        assert report.cleaned_up_hot_files == 0
    assert path.read_text(encoding="utf-8") == CONFLICT


# -- D1: an index write that fails still aborts the rebuild ---------------------------------


def test_t12_an_index_write_error_still_aborts_the_rebuild(tmp_path, monkeypatch):
    seed = _seed(tmp_path)
    _put(seed.db, f"decisions/{seed.decisions[0].id}.json", CONFLICT)  # forces a reload

    def boom(self, decision):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(Store, "_index_write_decision", boom)
    with pytest.raises(sqlite3.OperationalError, match="disk I/O error"):
        Store(seed.db)

    conn = sqlite3.connect(str(seed.db / "index.db"))
    try:
        assert conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == 3
        assert conn.execute("SELECT 1 FROM meta WHERE key = ?", (SKIP_KEY,)).fetchone() is None
    finally:
        conn.close()


# -- D7: the warning never says "remove" ----------------------------------------------------


def test_t15_the_record_file_warning_says_restore_and_never_remove(tmp_path, capsys):
    seed = _seed(tmp_path)
    _put(seed.db, f"decisions/{seed.decisions[0].id}.json", CONFLICT)
    capsys.readouterr()

    Store(seed.db).close()
    err = capsys.readouterr().err

    assert "left out of the index" in err
    assert "Restore them with git" in err
    assert "remove" not in err.lower()


def test_t15_the_archive_warning_says_never_delete_a_segment(tmp_path, capsys):
    seed = _seed(tmp_path)
    _segment(seed.db, [_line(_archived()), "<<<<<<< HEAD"])
    capsys.readouterr()

    Store(seed.db).close()
    err = capsys.readouterr().err

    assert f"archive segment {SEGMENT} has lines that could not be read" in err
    assert "never delete a segment" in err
    assert "remove" not in err.lower()


# -- verify names every file the reload lists ------------------------------------------------
# The notice tells the person to run ``sidegraph-verify`` to find the listed files, so verify must
# report a violation on each of them.


def _rejected() -> Decision:
    """An archivable decision that verify has no complaint about (a superseded one without a
    successor would put a finding on the segment, masking a missing one on its bad line)."""
    now = datetime.now(UTC)
    return _decision("archived", status=DecisionStatus.REJECTED, valid_from=now, valid_to=now)


def _break(seed: Seed, shape: str) -> None:
    db, (bad, *_), entity = seed.db, seed.decisions, seed.entity
    decision = f"decisions/{bad.id}.json"
    ok = {"entity_id": entity.entity_id, "tier": 2, "relation": "affects", "weight": 1.0}
    archived = json.loads(_line(_rejected()))
    match shape:
        case "decision-conflict":
            _put(db, decision, CONFLICT)
        case "decision-invalid-utf8":
            (db / decision).write_bytes(b'{"id": "\xff\xfe"}')
        case "decision-not-an-object":
            _put(db, decision, "[1]")
        case "decision-too-deep":
            _put(db, decision, "[" * 200_000 + "]" * 200_000)
        case "decision-bad-schema":
            _put_json(db, decision, {**_read(db, decision), "status": "bogus"})
        case "decision-lone-surrogate":
            _put_json(db, decision, {**_read(db, decision), "title": "\ud800"})
        case "decision-wrong-id":
            _put_json(db, decision, {**_read(db, decision), "id": "someone-else"})
        case "entity-conflict":
            _put(db, f"entities/{entity.entity_id}.json", CONFLICT)
        case "fact-conflict":
            _put(db, f"facts/{seed.fact.id}.json", CONFLICT)
        case "bindings-conflict":
            _put(db, f"bindings/{bad.id}.json", CONFLICT)
        case "bindings-object":
            _put_json(db, f"bindings/{bad.id}.json", {"a": 1})
        case "bindings-invalid-item":
            _put_json(db, f"bindings/{bad.id}.json", [ok, {**ok, "tier": 9}])
        case "bindings-lone-surrogate":
            _put_json(db, f"bindings/{bad.id}.json", [{**ok, "status": "\ud800"}])
        case "archive-conflict-line":
            _segment(db, [_line(_rejected()), "<<<<<<< HEAD"])
        case "archive-invalid-status":
            _segment(db, [_line(_rejected()), json.dumps({**archived, "status": "bogus"})])
        case "archive-lone-surrogate":
            _segment(db, [_line(_rejected()), json.dumps({**archived, "title": "\ud800"})])
        case "archive-invalid-utf8-line":
            _segment(db, [_line(_rejected()), b'{"record_type": "decision", "title": "\xff"}'])
        case _:
            raise AssertionError(shape)


@pytest.mark.parametrize(
    "shape",
    [
        "decision-conflict",
        "decision-invalid-utf8",
        "decision-not-an-object",
        "decision-too-deep",
        "decision-bad-schema",
        "decision-lone-surrogate",
        "decision-wrong-id",
        "entity-conflict",
        "fact-conflict",
        "bindings-conflict",
        "bindings-object",
        "bindings-invalid-item",
        "bindings-lone-surrogate",
        "archive-conflict-line",
        "archive-invalid-status",
        "archive-lone-surrogate",
        "archive-invalid-utf8-line",
    ],
)
def test_verify_reports_every_file_the_reload_lists(tmp_path, shape):
    seed = _seed(tmp_path)
    _break(seed, shape)

    with Store(seed.db) as s:
        listed = {seed.db / e["path"] for e in _skip_list(s)}
    reported = {Path(v.path.split("#")[0]) for v in verify.verify_snapshot(seed.db)}

    assert listed, "the shape must make the reload list a file"
    assert listed <= reported
