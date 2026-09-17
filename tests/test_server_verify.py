"""``verify_store`` MCP tool — the snapshot-only read-only counterpart to
``sidegraph-verify`` (design/superpowers/specs/2026-07-11-ci-integrity-design.md ruling 2).
Mirrors ``tests/test_verify_snapshot.py``'s corrupt-one-thing-per-test style, but through
the tool's testable core (``_verify_store_impl``) so it also pins the dict shape
``{"clean", "violations": [{"code","path","detail"}]}`` and the "pass the already-open
Store, don't re-resolve/re-open" contract (see the tool's docstring)."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from sidegraph.schema import Decision, DecisionKind, Provenance
from sidegraph.server import _verify_store_impl
from sidegraph.store import Store
from sidegraph.verify import BAD_VALIDITY_WINDOW


def _decision(**overrides) -> Decision:
    base = dict(
        title="Use file-per-record JSON",
        kind=DecisionKind.ADR,
        context="A single committed SQLite file can't be merged by git.",
        choice="One JSON file per record, plus a derived, gitignored local index.",
        valid_from=datetime(2026, 1, 10, tzinfo=UTC),
        provenance=Provenance(source="manual"),
    )
    base.update(overrides)
    return Decision(**base)


def test_verify_store_clean_store_reports_clean(tmp_path):
    store = Store(tmp_path / "srv.db")
    store.add_decision(_decision())
    out = _verify_store_impl(store)
    assert out == {"clean": True, "violations": []}


def test_verify_store_reports_snapshot_violations(tmp_path):
    store = Store(tmp_path / "srv.db")
    d = store.add_decision(_decision())
    path = store.path / "decisions" / f"{d.id}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["valid_to"] = "2020-01-01T00:00:00Z"  # before valid_from -- BAD_VALIDITY_WINDOW
    path.write_text(json.dumps(data), encoding="utf-8")

    out = _verify_store_impl(store)
    assert out["clean"] is False
    codes = {v["code"] for v in out["violations"]}
    assert BAD_VALIDITY_WINDOW in codes
    hit = next(v for v in out["violations"] if v["code"] == BAD_VALIDITY_WINDOW)
    assert set(hit) == {"code", "path", "detail"}
    assert hit["path"] == str(path)


def test_verify_store_empty_store_is_clean(tmp_path):
    store = Store(tmp_path / "srv.db")
    out = _verify_store_impl(store)
    assert out == {"clean": True, "violations": []}
