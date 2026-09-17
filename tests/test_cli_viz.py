import json
from datetime import UTC, datetime

from sidegraph.cli import viz_main
from sidegraph.schema import (
    AnchorBinding,
    Decision,
    DecisionKind,
    Descriptor,
    Entity,
    Provenance,
)
from sidegraph.store import Store


def _seed(tmp_path):
    db = tmp_path / "store"
    s = Store(db)
    e = s.upsert_entity(
        Entity(canonical_name="alpha", descriptor=Descriptor(name="alpha", file_path="a.py"))
    )
    d = s.add_decision(
        Decision(
            title="an adr",
            kind=DecisionKind.ADR,
            context="c",
            choice="ch",
            valid_from=datetime.now(UTC),
            provenance=Provenance(source="manual"),
        )
    )
    s.add_binding(AnchorBinding(record_id=d.id, entity_id=e.entity_id, tier=2, status="degraded"))
    return db


def test_viz_writes_html_and_json(tmp_path, capsys):
    db = _seed(tmp_path)
    out = tmp_path / "g"
    assert viz_main(["--db", str(db), "--out", str(out)]) == 0
    assert (tmp_path / "g.html").is_file()
    assert (tmp_path / "g.json").is_file()
    doc = json.loads((tmp_path / "g.json").read_text())
    assert set(doc) == {"nodes", "edges", "stats"}
    html = (tmp_path / "g.html").read_text()
    assert "unpkg.com" not in html  # self-contained
    assert "1 degraded" in capsys.readouterr().out


def test_viz_json_flag_stdout_only(tmp_path, capsys):
    db = _seed(tmp_path)
    out = tmp_path / "g"
    assert viz_main(["--db", str(db), "--out", str(out), "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert set(doc) == {"nodes", "edges", "stats"}
    assert not (tmp_path / "g.html").exists()  # --json writes no HTML
    assert not (tmp_path / "g.json").exists()  # --json writes no JSON file either


def test_viz_only_problems_narrows(tmp_path):
    db = _seed(tmp_path)
    s = Store(db)
    # a healthy decision: LIVE binding to a NEW entity -> not dangling, not degraded/orphaned,
    # hence not a problem. --only-problems must drop it (an unbound decision would be dangling,
    # hence a problem, and would NOT exercise the filter).
    healthy_entity = s.upsert_entity(
        Entity(canonical_name="healthy", descriptor=Descriptor(name="healthy", file_path="h.py"))
    )
    healthy = s.add_decision(
        Decision(
            title="a healthy adr",
            kind=DecisionKind.ADR,
            context="c",
            choice="ch",
            valid_from=datetime.now(UTC),
            provenance=Provenance(source="manual"),
        )
    )
    s.add_binding(
        AnchorBinding(
            record_id=healthy.id, entity_id=healthy_entity.entity_id, tier=2, status="live"
        )
    )

    out = tmp_path / "p"
    assert viz_main(["--db", str(db), "--out", str(out), "--only-problems"]) == 0
    doc = json.loads((tmp_path / "p.json").read_text())
    ids = {n["id"] for n in doc["nodes"]}
    # the degraded-anchored decision is a problem, so it survives
    assert any(n["type"] == "decision" for n in doc["nodes"])
    # the healthy decision is not a problem -- --only-problems drops it
    assert healthy.id not in ids


def test_viz_empty_store_ok(tmp_path):
    db = tmp_path / "empty"
    Store(db)  # initialize an empty store (writes the format marker)
    out = tmp_path / "e"
    assert viz_main(["--db", str(db), "--out", str(out)]) == 0
    doc = json.loads((tmp_path / "e.json").read_text())
    assert doc["nodes"] == []


def test_viz_uninitialized_store_exits_2(tmp_path, capsys):
    missing = tmp_path / "nope"
    assert viz_main(["--db", str(missing), "--out", str(tmp_path / "x")]) == 2
    assert "sidegraph-init" in capsys.readouterr().err


def test_viz_unwritable_out_exits_2(tmp_path, capsys):
    db = _seed(tmp_path)
    bad = tmp_path / "no-such-dir" / "g"  # parent dir does not exist
    assert viz_main(["--db", str(db), "--out", str(bad)]) == 2
    assert "cannot write" in capsys.readouterr().err
