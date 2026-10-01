"""A record's id is a filename, and the store treats it as one (design/superpowers/specs/
2026-09-29-record-identity-design.md): the write path refuses an id that is not a single
path segment (D1, D2), and reload skips a file whose JSON identity does not match its name
(D3-D5)."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from ulid import ULID

import sidegraph.store as store_module
from sidegraph.schema import (
    Decision,
    DecisionKind,
    Domain,
    Entity,
    Fact,
    Initiative,
    Provenance,
)
from sidegraph.store import Store

UNSAFE_IDS = [
    "../escaped",
    "/abs/escaped",
    "sub/nested",
    "..",
    ".hidden",
    "a\\b",
    "abc\n",
    "a" * 129,
]


def _prov() -> Provenance:
    return Provenance(source="manual")


def _decision(did: str | None = None) -> Decision:
    kwargs = {"id": did} if did else {}
    return Decision(
        title="t",
        kind=DecisionKind.ADR,
        context="c",
        choice="ch",
        valid_from=datetime.now(UTC),
        provenance=_prov(),
        **kwargs,
    )


def _files_outside(root, store_dir) -> list[str]:
    """Every file under ``root`` that is not inside the store directory."""
    return sorted(
        str(p.relative_to(root))
        for p in root.rglob("*")
        if p.is_file() and store_dir not in p.parents
    )


@pytest.mark.parametrize("bad", UNSAFE_IDS)
def test_write_refuses_unsafe_id(tmp_path, bad):
    db = tmp_path / "proj" / ".sidegraph"
    s = Store(db)
    with pytest.raises(ValueError, match="unsafe record id"):
        s.add_decision(_decision(bad))
    s.close()
    assert _files_outside(tmp_path, db) == []
    assert not list(db.rglob("*escaped*"))
    assert not (db / "decisions" / "sub").exists()


def test_every_writer_refuses_unsafe_id(tmp_path):
    db = tmp_path / "proj" / ".sidegraph"
    s = Store(db)
    bad = "../escaped"
    with pytest.raises(ValueError, match="unsafe record id"):
        s.upsert_entity(Entity(entity_id=bad, canonical_name="x"))
    with pytest.raises(ValueError, match="unsafe record id"):
        s.add_fact(
            Fact(
                id=bad,
                statement="s",
                source="src",
                valid_from=datetime.now(UTC),
                provenance=_prov(),
            )
        )
    with pytest.raises(ValueError, match="unsafe record id"):
        s.add_domain(Domain(domain_id=bad, slug="d", title="D", summary="sum", provenance=_prov()))
    with pytest.raises(ValueError, match="unsafe record id"):
        s.upsert_initiative(Initiative(id=bad, name="n"))
    s.close()
    assert _files_outside(tmp_path, db) == []


# -- reload: identity of a file (D3-D5, D8) -----------------------------------------------

SKIP_KEY = "skipped_canonical_files"


def _seed(tmp_path) -> tuple:
    """A store directory holding one real decision, closed. Returns ``(db, decision)``."""
    db = tmp_path / "proj" / ".sidegraph"
    s = Store(db)
    d = s.add_decision(_decision())
    s.close()
    return db, d


def _dump(d: Decision) -> dict:
    return json.loads(d.model_dump_json())


def _put(db, subdir: str, name: str, data: object) -> None:
    (db / subdir / name).write_text(json.dumps(data), encoding="utf-8")


def _drop_index(db) -> None:
    for f in db.glob("index.db*"):
        f.unlink()


def _crafted(tmp_path) -> tuple:
    """A store with one real decision plus ``decisions/<ULID>.json`` whose JSON id is
    ``../../victim``. Returns ``(db, real, crafted_stem)``."""
    db, real = _seed(tmp_path)
    stem = str(ULID())
    _put(db, "decisions", f"{stem}.json", {**_dump(real), "id": "../../victim"})
    _drop_index(db)
    return db, real, stem


def _count(s: Store, table: str) -> int:
    return s._conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def _skip_list(s: Store) -> list:
    row = s._conn.execute("SELECT value FROM meta WHERE key = ?", (SKIP_KEY,)).fetchone()
    return json.loads(row["value"]) if row else []


class _ReloadCounter:
    def __init__(self, monkeypatch) -> None:
        self.n = 0
        orig = Store._reload_index_from_canonical
        counter = self

        def counting(self_, digest):
            counter.n += 1
            return orig(self_, digest)

        monkeypatch.setattr(Store, "_reload_index_from_canonical", counting)


def test_reload_skips_crafted_id(tmp_path, capsys):
    db, real, stem = _crafted(tmp_path)
    s = Store(db)
    err = capsys.readouterr().err
    assert s.get_decision("../../victim") is None
    assert s.get_decision(stem) is None
    assert s.get_decision(real.id) is not None
    assert f"decisions/{stem}.json" in err
    with pytest.raises(ValueError, match="is not proposed"):
        s.ratify("../../victim")
    s.close()
    assert not (db.parent / "victim.json").exists()
    assert _files_outside(tmp_path, db) == []


def test_reload_skips_safe_but_mismatched_id(tmp_path, capsys):
    db, real = _seed(tmp_path)
    _put(db, "decisions", "A.json", {**_dump(real), "id": "B"})
    _drop_index(db)
    s = Store(db)
    assert s.get_decision("B") is None
    assert s.get_decision("A") is None
    assert "decisions/A.json" in capsys.readouterr().err
    s.close()


def test_reload_skips_unsafe_id_that_matches_its_filename(tmp_path, capsys):
    db, real = _seed(tmp_path)
    _put(db, "decisions", ".hidden.json", {**_dump(real), "id": ".hidden"})
    _put(db, "bindings", ".x.json", [])
    _drop_index(db)
    s = Store(db)
    err = capsys.readouterr().err
    assert s.get_decision(".hidden") is None
    assert "decisions/.hidden.json" in err
    assert "bindings/.x.json" in err
    s.close()


def test_reload_skips_duplicate_id_file(tmp_path, capsys):
    db, real = _seed(tmp_path)
    (db / "decisions" / f"{real.id}.json").unlink()
    _put(db, "decisions", "A.json", {**_dump(real), "id": "A", "title": "first"})
    _put(db, "decisions", "B.json", {**_dump(real), "id": "A", "title": "second"})
    _drop_index(db)
    s = Store(db)
    got = s.get_decision("A")
    assert got is not None and got.title == "first"
    assert "decisions/B.json" in capsys.readouterr().err
    assert _count(s, "decisions") == 1
    s.close()


@pytest.mark.parametrize("shape", ["no-id", "non-object"])
def test_reload_skips_file_without_id_or_non_object(tmp_path, capsys, shape):
    db, real = _seed(tmp_path)
    stem = str(ULID())
    body = {k: v for k, v in _dump(real).items() if k != "id"} if shape == "no-id" else []
    _put(db, "decisions", f"{stem}.json", body)
    _drop_index(db)
    s = Store(db)
    assert f"decisions/{stem}.json" in capsys.readouterr().err
    assert _count(s, "decisions") == 1  # only the real one: no ULID-keyed ghost
    s.close()


def test_skip_warns_on_every_open_without_reloading(tmp_path, capsys, monkeypatch):
    db, _real, stem = _crafted(tmp_path)
    Store(db).close()
    capsys.readouterr()
    counter = _ReloadCounter(monkeypatch)
    s = Store(db)
    err = capsys.readouterr().err
    s.close()
    assert counter.n == 0
    assert f"decisions/{stem}.json" in err
    assert "sidegraph-verify" in err


def test_fixing_the_file_clears_the_warning(tmp_path, capsys):
    db, _real, stem = _crafted(tmp_path)
    Store(db).close()
    (db / "decisions" / f"{stem}.json").unlink()
    Store(db).close()  # reloads: the digest moved
    capsys.readouterr()
    s = Store(db)
    assert capsys.readouterr().err == ""
    assert _skip_list(s) == []
    s.close()


def test_removal_while_a_long_lived_store_writes(tmp_path, capsys):
    db, _real, stem = _crafted(tmp_path)
    live = Store(db)
    (db / "decisions" / f"{stem}.json").unlink()
    live.add_decision(_decision())
    live.close()
    capsys.readouterr()
    s = Store(db)
    assert capsys.readouterr().err == ""
    assert _skip_list(s) == []
    s.close()


def test_skipped_file_row_keeps_writes_certified(tmp_path, monkeypatch):
    db, _real, _stem = _crafted(tmp_path)
    s = Store(db)
    s.add_decision(_decision())
    s.close()
    counter = _ReloadCounter(monkeypatch)
    Store(db).close()
    assert counter.n == 0


def test_archived_copy_of_skipped_hot_id_loads(tmp_path, capsys):
    db, real = _seed(tmp_path)
    payload = _dump(real)
    seg = db / "archive"
    seg.mkdir(exist_ok=True)
    (seg / "2026-01-01-1.jsonl").write_text(
        json.dumps({"record_type": "decision", **payload}) + "\n", encoding="utf-8"
    )
    # a hot file under the archived id whose JSON id differs: skipped, so the archive wins
    _put(db, "decisions", f"{real.id}.json", {**payload, "id": "OTHER"})
    _drop_index(db)
    s = Store(db)
    assert s.get_decision(real.id) is not None
    assert s.get_decision("OTHER") is None
    s.close()


@pytest.mark.parametrize("bad", ["unsafe", "list-id", "non-object"])
def test_unsafe_or_nonstring_archived_id_is_skipped(tmp_path, capsys, bad):
    db, real = _seed(tmp_path)
    good = _dump(real)
    lines: list[object] = [{"record_type": "decision", **good, "id": str(ULID())}]
    if bad == "unsafe":
        lines.append({"record_type": "decision", **good, "id": "../x"})
    elif bad == "list-id":
        lines.append({"record_type": "decision", **good, "id": ["a"]})
    else:
        lines.append([])
    seg = db / "archive"
    seg.mkdir(exist_ok=True)
    (seg / "2026-01-01-1.jsonl").write_text(
        "".join(json.dumps(x) + "\n" for x in lines), encoding="utf-8"
    )
    _drop_index(db)
    s = Store(db)
    assert "archive" in capsys.readouterr().err
    assert _count(s, "decisions") == 2  # the hot one plus the segment's one good line
    s.compact(dry_run=True)
    s.compact()
    s.close()
    assert _files_outside(tmp_path, db) == []


@pytest.mark.parametrize("record_type", [["decision"], {"a": 1}, 7, None])
def test_archive_line_with_a_non_string_record_type_is_skipped_silently(
    tmp_path, capsys, record_type
):
    """An unknown ``record_type`` is skipped without a warning (forward-compat); a list or
    dict value must not raise ``TypeError: unhashable type`` and wedge every open (D8)."""
    db, real = _seed(tmp_path)
    seg = db / "archive"
    seg.mkdir(exist_ok=True)
    line = json.dumps({**_dump(real), "id": str(ULID()), "record_type": record_type})
    (seg / "2026-01-01-1.jsonl").write_text(line + "\n", encoding="utf-8")
    _drop_index(db)
    s = Store(db)
    assert _count(s, "decisions") == 1
    s.close()
    assert capsys.readouterr().err == ""


def test_every_record_kind_gets_the_identity_check(tmp_path, capsys):
    db = tmp_path / "proj" / ".sidegraph"
    s = Store(db)
    s.upsert_entity(Entity(canonical_name="x"))
    s.add_fact(Fact(statement="s", source="src", valid_from=datetime.now(UTC), provenance=_prov()))
    s.add_domain(Domain(slug="d", title="D", summary="sum", provenance=_prov()))
    s.upsert_initiative(Initiative(name="n"))
    s.close()
    tables = {
        "entities": "entities",
        "facts": "facts",
        "domains": "domains",
        "initiatives": "initiatives",
    }
    for sub in tables:
        (only,) = list((db / sub).glob("*.json"))
        _put(db, sub, "OTHER.json", json.loads(only.read_text(encoding="utf-8")))
    _drop_index(db)
    s = Store(db)
    assert "OTHER.json" in capsys.readouterr().err
    listed = {e["path"] for e in _skip_list(s)}
    for sub, table in tables.items():
        assert f"{sub}/OTHER.json" in listed
        assert _count(s, table) == 1
    s.close()


def test_upgrade_reloads_a_poisoned_certified_index(tmp_path, capsys, monkeypatch):
    db, real = _seed(tmp_path)
    stem = str(ULID())
    _put(db, "decisions", f"{stem}.json", {**_dump(real), "id": "../../victim"})
    with monkeypatch.context() as m:
        m.setattr(store_module, "_IDENTITY_RULE", "")
        s = Store(db)  # certifies an index under the pre-fix digest rule
        poisoned = Decision.model_validate({**_dump(real), "id": "../../victim"})
        s._index_write_decision(poisoned)
        s._conn.execute("DELETE FROM meta WHERE key = ?", (SKIP_KEY,))
        s._conn.commit()
        s.close()
    capsys.readouterr()
    counter = _ReloadCounter(monkeypatch)
    s = Store(db)
    err = capsys.readouterr().err
    assert counter.n == 1
    assert s.get_decision("../../victim") is None
    assert f"decisions/{stem}.json" in err
    s.close()


def test_compact_ignores_a_hot_file_the_reload_skipped(tmp_path, capsys):
    """An archived X plus a hot ``X.json`` whose JSON id is OTHER: the reload keeps the
    archive copy, so compact must not call the skipped file a different copy of X."""
    db, real = _seed(tmp_path)
    payload = _dump(real)
    seg = db / "archive"
    seg.mkdir(exist_ok=True)
    segment = seg / "2026-01-01-1.jsonl"
    segment.write_text(json.dumps({"record_type": "decision", **payload}) + "\n", encoding="utf-8")
    _put(db, "decisions", f"{real.id}.json", {**payload, "id": "OTHER"})
    _drop_index(db)
    s = Store(db)
    hot = db / "decisions" / f"{real.id}.json"
    hot_bytes, segment_bytes = hot.read_bytes(), segment.read_bytes()
    capsys.readouterr()

    dry = s.compact(dry_run=True)
    done = s.compact()
    s.close()

    assert "exists in BOTH" not in capsys.readouterr().err
    assert (dry.decisions_compacted, dry.cleaned_up_hot_files) == (0, 0)
    assert (done.decisions_compacted, done.cleaned_up_hot_files) == (0, 0)
    assert hot.read_bytes() == hot_bytes
    assert segment.read_bytes() == segment_bytes
