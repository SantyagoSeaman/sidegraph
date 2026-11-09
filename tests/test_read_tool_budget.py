"""Registry-wide wire limits and the remainder of bounded JSON read tools."""

# ruff: noqa: F811  (imported fixtures are named test arguments)

from __future__ import annotations

import json
import shutil

import mcp.types as mt
import pytest
from ulid import ULID

from sidegraph import server, verify
from sidegraph.engine.reader import GraphifyReader
from sidegraph.schema import DecisionKind, Descriptor, Domain, DomainStatus, Provenance
from sidegraph.store import Store
from tests.bounded_listings_support import ctx_env, ctx_template  # noqa: F401
from tests.test_bounded_listings import _bind, _decision, _fact, _graph, _proposed, call

PATHS = [f"src/pkg{f}/mod{f}.py" for f in range(8)]
WORST_CALLS: dict[str, dict] = {
    "find_entity": {"name": "Hub", "budget_chars": 10**9},
    "verify_store": {"budget_chars": 10**9},
    "retrieve_decisions": {
        "files": ["listing.py"],
        "status": "accepted",
        "limit": 100,
        "budget_chars": 10**9,
    },
    "list_facts": {"limit": 100, "budget_chars": 10**9},
    "get_entity_history": {"entity_id": "Listing", "limit": 100, "budget_chars": 10**9},
    "drill_down": {"domain_slug": "listing", "limit": 100, "budget_chars": 10**9},
    "list_proposed": {"budget_chars": 10**9},
    "list_domains": {"limit": 100, "budget_chars": 10**9},
    "list_domain_candidates": {"limit": 0, "budget_chars": 10**9},
    "get_task_context": {"files": PATHS, "structure_budget": 10**9, "memory_budget": 10**9},
    "query_structure": {"files": PATHS, "budget_chars": 10**9},
    "query_decisions": {"files": PATHS, "budget_chars": 10**9},
}


def _large_store(tmp_path):
    store = Store(tmp_path / "store")
    with store._mutation():
        hub = store.get_or_create_entity(Descriptor(name="Hub", file_path="hub.py"))
        listing = store.get_or_create_entity(Descriptor(name="Listing", file_path="listing.py"))
        for i in range(600):
            _bind(store, _decision(store, i, size=1).id, hub.entity_id, tier=2)
        for i in range(400):
            store.get_or_create_entity(
                Descriptor(name="Twin", file_path=f"twins/area{i:03}/shared_name_entities/twin.py")
            )
        domain = server._add_domain_impl(store, None, slug="listing", title="Listing", summary="s")
        server._ratify_impl(store, accept=[domain["domain_id"]])
        abstract = store.find_abstract_entity("domain:listing")
        for i in range(120):
            d = _decision(
                store, 1000 + i, size=200, kind=DecisionKind.GOTCHA if i % 2 else DecisionKind.ADR
            )
            _bind(store, d.id, listing.entity_id, tier=2)
            if i < 80:
                _bind(store, d.id, abstract.entity_id)
        for i in range(200):
            _fact(store, i)
        for i in range(100):
            store.add_domain(
                Domain(
                    slug=f"large-{i:03}",
                    title=f"Large {i}",
                    summary="x" * 900,
                    status=DomainStatus.ACCEPTED,
                    provenance=Provenance(source="manual"),
                )
            )
        _proposed(store, decisions=100, facts=50)
        return store, _graph(tmp_path / "graph.json", 300)


@pytest.fixture(scope="module")
def large_template(tmp_path_factory):
    store, graph = _large_store(tmp_path_factory.mktemp("large-read"))
    root = store.path
    store.close()
    return root, graph


@pytest.fixture
def large_env(large_template, tmp_path, monkeypatch):
    root, graph = large_template
    shutil.copytree(root, tmp_path / "store")
    with Store(tmp_path / "store") as store:
        reader = GraphifyReader(graph)
        monkeypatch.setattr(server, "_store", store)
        monkeypatch.setattr(server, "_load_reader", lambda: reader)
        monkeypatch.setattr(server, "_synced_reader", lambda: (reader, None))
        yield store, reader


@pytest.fixture(scope="module")
def damaged_template(tmp_path_factory):
    with Store(tmp_path_factory.mktemp("damaged-read") / "store") as store:
        with store._mutation():
            decisions = [_decision(store, i, size=1) for i in range(600)]
        for d in decisions:
            (store.path / "decisions" / f"{d.id}.json").rename(
                store.path / "decisions" / f"{ULID()}.json"
            )
        violations = verify.verify_snapshot(store.path)
        assert len(violations) == 600
        assert {v.code for v in violations} == {verify.FILENAME_ID_MISMATCH}
        return store.path


def test_B1_registry():
    assert set(WORST_CALLS) == server._READ_TOOLS | server._LAZY_READ_TOOLS


@pytest.mark.parametrize("name", sorted(WORST_CALLS))
@pytest.mark.slow
def test_B2_every_read_tool(name, large_env, damaged_template, ctx_env, monkeypatch):
    store, reader = large_env
    args = WORST_CALLS[name].copy()
    if name in {"get_task_context", "query_decisions", "query_structure"}:
        store, reader, _, _ = ctx_env
    elif name == "verify_store":
        store = Store(damaged_template)
    if name == "get_entity_history":
        args["entity_id"] = store.find_entity("Listing", "listing.py").entity_id
    monkeypatch.setattr(server, "_store", store)
    monkeypatch.setattr(server, "_load_reader", lambda: reader)
    monkeypatch.setattr(server, "_synced_reader", lambda: (reader, None))
    try:
        result = call(name, args)
        assert not result.is_error, result.content
        assert len(result.content) == 1 and isinstance(result.content[0], mt.TextContent)
        text = result.content[0].text
        print(
            f"{name}: text={len(text)}, structured="
            f"{len(json.dumps(result.structured_content)) if result.structured_content else 0}"
        )
        assert len(text) <= server._HARD_CAP
        assert result.structured_content is None
        assert result.data is None
    finally:
        if name == "verify_store":
            store.close()


@pytest.mark.slow
def test_fixture_uncapped_populations(large_env):
    store, reader = large_env
    lengths = {
        "find_entity": len(server._dump(server._find_entity_impl(store, "Hub"))),
        "list_proposed": len(server._list_proposed_impl(store)),
        "list_domains": len(server._dump(server._list_domains_impl(store))),
        "list_domain_candidates": len(
            server._dump(server._list_domain_candidates_impl(store, reader, limit=None))
        ),
    }
    print(lengths)
    assert all(n > 40000 for n in lengths.values())


@pytest.mark.parametrize("name", ["find_entity", "verify_store"])
@pytest.mark.slow
def test_new_tools_default_wire_budget(name, large_env, damaged_template, monkeypatch):
    store = large_env[0] if name == "find_entity" else Store(damaged_template)
    monkeypatch.setattr(server, "_store", store)
    try:
        result = call(name, {"name": "Hub"} if name == "find_entity" else {})
        assert not result.is_error
        assert len(result.content) == 1 and isinstance(result.content[0], mt.TextContent)
        print(
            name,
            len(result.content[0].text),
            len(json.dumps(result.structured_content)) if result.structured_content else 0,
        )
        assert len(result.content[0].text) <= 24000
        assert result.structured_content is None and result.data is None
    finally:
        if name == "verify_store":
            store.close()


@pytest.mark.parametrize("budget", [1, 4000, 24000, 10**9])
@pytest.mark.slow
def test_B3_find_entity_bindings(large_env, budget):
    store, _ = large_env
    full = server._find_entity_impl(store, "Hub")
    text = server._find_entity_listing(store, "Hub", None, budget)
    out = json.loads(text)
    assert len(text) <= max(4000, min(40000, budget))
    taken = len(out["bindings"])
    print(f"find_entity budget={budget}: text={len(text)}")
    assert taken + out["omitted"] == 600
    ids = [b["record_id"] for b in full["bindings"][taken:]]
    assert out["omitted_ids"] == ids[: len(out["omitted_ids"])]
    assert len(out["omitted_ids"]) + out.get("omitted_ids_unlisted", 0) == len(ids)
    assert "get_entity_history(entity_id=" in out["hint"]


@pytest.mark.parametrize("field", ["canonical_name", "name", "file_path", "last_seen_node_id"])
def test_B3_zero_binding_oversized_envelope(tmp_path, monkeypatch, field):
    with Store(tmp_path / "store") as store:
        entity = store.get_or_create_entity(Descriptor(name="Small", file_path="small.py"))
        full = server._find_entity_impl(store, "Small")
        target = full["descriptor"] if field in {"name", "file_path"} else full
        target[field] = '\\"\n' * 20000
        monkeypatch.setattr(server, "_find_entity_impl", lambda *_args: full)
        out_text = server._find_entity_listing(store, "Small", None, 1)
        out = json.loads(out_text)
        assert len(out_text) <= 4000
        assert out["entity_id"] == entity.entity_id
        assert "[clipped:" in out_text
        assert "Bounded: the entity" in out["hint"]
        assert "omitted_ids" not in out


def test_B4_small_find_entity_is_byte_identical(tmp_path, monkeypatch):
    with Store(tmp_path / "small") as store:
        entity = store.get_or_create_entity(Descriptor(name="Small", file_path="small.py"))
        for i in range(3):
            _bind(store, _decision(store, i).id, entity.entity_id)
        monkeypatch.setattr(server, "_store", store)
        result = call("find_entity", {"name": "Small"})
        assert result.content[0].text == server._dump(server._find_entity_impl(store, "Small"))


@pytest.mark.parametrize("budget", [1, 4000, 24000, 10**9])
@pytest.mark.slow
def test_B5_ambiguous_candidates(large_env, budget):
    store, _ = large_env
    text = server._find_entity_listing(store, "Twin", None, budget)
    out = json.loads(text)
    assert len(text) <= max(4000, min(40000, budget))
    assert not out["found"]
    taken = len(out["candidates"])
    assert taken + out["candidates_omitted"] == 400
    ids = [c["entity_id"] for c in server._find_entity_impl(store, "Twin")["candidates"][taken:]]
    assert out["omitted_ids"] == ids[: len(out["omitted_ids"])]
    assert len(out["omitted_ids"]) + out.get("omitted_ids_unlisted", 0) == len(ids)
    assert "pass file_path to choose one" in out["hint"]


def test_B5_oversized_individual_candidate(tmp_path, monkeypatch):
    with Store(tmp_path / "store") as store:
        candidates = [
            {"entity_id": str(ULID()), "canonical_name": "x" * 50000, "file_path": "y" * 50000}
        ]
        monkeypatch.setattr(
            server, "_find_entity_impl", lambda *_args: {"found": False, "candidates": candidates}
        )
        text = server._find_entity_listing(store, "name", None, 4000)
        out = json.loads(text)
        assert len(text) <= 4000
        assert out["candidates"][0]["entity_id"] == candidates[0]["entity_id"]
        assert "[clipped:" in text and "Bounded: the entity" in out["hint"]


def test_B10_small_find_entity_has_no_omitted_ids(tmp_path):
    with Store(tmp_path / "store") as store:
        entity = store.get_or_create_entity(Descriptor(name="Small", file_path="small.py"))
        _bind(store, _decision(store, 0).id, entity.entity_id)
        for path in ["a.py", "b.py"]:
            store.get_or_create_entity(Descriptor(name="Twin", file_path=path))
        found = json.loads(server._find_entity_listing(store, "Small", None, 4000))
        ambiguous = json.loads(server._find_entity_listing(store, "Twin", None, 4000))
        assert found["found"] and len(found["bindings"]) == 1
        assert not ambiguous["found"] and len(ambiguous["candidates"]) == 2
        assert "omitted_ids" not in found and "omitted_ids" not in ambiguous


@pytest.mark.parametrize("budget", [1, 4000, 24000, 10**9])
@pytest.mark.slow
def test_B6_verify_store(damaged_template, budget):
    with Store(damaged_template) as store:
        text = server._verify_listing(store, budget)
        out = json.loads(text)
        assert len(text) <= max(4000, min(40000, budget))
        assert out["clean"] is False
        print(f"verify_store budget={budget}: text={len(text)}")
        assert out["total"] == 600
        assert out["counts"] == {verify.FILENAME_ID_MISMATCH: 600}
        assert len(out["violations"]) + out["omitted"] == 600
        assert "sidegraph-verify --json" in out["hint"]


def test_B6_clean_store(tmp_path):
    with Store(tmp_path / "clean") as store:
        assert json.loads(server._verify_listing(store, 4000)) == {
            "clean": True,
            "total": 0,
            "counts": {},
            "violations": [],
        }


@pytest.mark.parametrize("field", ["path", "detail"])
def test_B6_oversized_violation(tmp_path, monkeypatch, field):
    with Store(tmp_path / "clean") as store:
        violation = {"code": "invalid-record", "path": "a.json", "detail": "bad"}
        violation[field] = '\\"\n' * 20000
        monkeypatch.setattr(
            server, "_verify_store_impl", lambda _store: {"clean": False, "violations": [violation]}
        )
        text = server._verify_listing(store, 4000)
        out = json.loads(text)
        assert len(text) <= 4000
        assert out["clean"] is False and out["total"] == 1
        assert sum(out["counts"].values()) == 1
        assert len(out["violations"]) == 1
        assert out["violations"][0][field].endswith("…")


def test_B6_counts_order(tmp_path, monkeypatch):
    with Store(tmp_path / "clean") as store:
        rows = [{"code": code, "path": "a", "detail": "b"} for code in ["b", "a", "c", "c"]]
        monkeypatch.setattr(
            server, "_verify_store_impl", lambda _store: {"clean": False, "violations": rows}
        )
        out = json.loads(server._verify_listing(store, 4000))
        assert list(out["counts"]) == ["c", "a", "b"]
        assert out["violations"] == rows
        assert "hint" not in out and "omitted" not in out


@pytest.mark.parametrize("budget", [1, 4000, 24000, 40000, 10**9])
@pytest.mark.slow
def test_B7_decision_remainder(large_env, budget):
    store, reader = large_env
    entity = store.find_entity("Listing", "listing.py")
    records = [store.get_decision(b.record_id) for b in store.bindings_for_entity(entity.entity_id)]
    records.sort(key=lambda d: (d.valid_from, d.id), reverse=True)
    records.sort(key=lambda d: 0 if d.kind == DecisionKind.GOTCHA else 2)
    text = server._decision_listing(
        store, reader, files=["listing.py"], status="accepted", limit=100, budget_chars=budget
    )
    out = json.loads(text)
    assert out["matched"] == 120
    assert len(text) <= max(4000, min(40000, budget))
    taken = out["returned"]
    assert [d["id"] for d in out["decisions"]] == [d.id for d in records[:taken]]
    ids = [d.id for d in records[taken:]]
    assert out["omitted_ids"] == ids[: len(out["omitted_ids"])]
    assert out["omitted"] == len(ids)
    assert len(out["omitted_ids"]) + out.get("omitted_ids_unlisted", 0) == len(ids)
    assert "retrieve_decisions(ids=[...])" in out["hint"]


@pytest.mark.parametrize("budget", [1, 4000, 24000, 40000, 10**9])
@pytest.mark.slow
def test_B8_fact_remainder(large_env, budget):
    store, reader = large_env
    facts = sorted(
        (f for f in store.iter_facts() if f.status.value == "accepted"),
        key=lambda f: (f.valid_from, f.id),
        reverse=True,
    )
    text = server._fact_listing(store, reader, status="accepted", limit=100, budget_chars=budget)
    out = json.loads(text)
    assert out["matched"] == 200
    assert len(text) <= max(4000, min(40000, budget))
    taken = out["returned"]
    assert [f["id"] for f in out["facts"]] == [f.id for f in facts[:taken]]
    ids = [f.id for f in facts[taken:]]
    assert out["omitted_ids"] == ids[: len(out["omitted_ids"])]
    assert len(out["omitted_ids"]) + out.get("omitted_ids_unlisted", 0) == len(ids)
    assert "list_facts(ids=[...])" in out["hint"]


@pytest.mark.parametrize("mode", ["limit", "budget", "zero"])
def test_B9_domain_remainder(tmp_path, mode):
    with Store(tmp_path / "domains") as store:
        for i in range(3):
            store.add_domain(
                Domain(
                    slug=f"d-{i}",
                    title="Domain",
                    summary="x" * (5000 if mode == "zero" else 2000 if mode == "budget" else 10),
                    provenance=Provenance(source="manual"),
                )
            )
        text = server._domain_listing(store, limit=1 if mode == "limit" else 100, budget_chars=1)
        out = json.loads(text)
        assert len(text) <= 4000
        taken = out["returned"]
        assert taken == {"limit": 1, "budget": 1, "zero": 0}[mode]
        ids = [d.domain_id for d in sorted(store.iter_domains(), key=lambda d: d.slug)][taken:]
        assert out["omitted_ids"] == ids
        assert "omitted_ids_unlisted" not in out
        assert "list_domains(ids=[...])" in out["hint"]
        assert ("Store.get_domain" in out["hint"]) == (mode == "zero")


@pytest.mark.parametrize("key", ["decisions", "facts"])
def test_B7_B8_digit_boundaries(key):
    pool = [{"id": f"{i:026}", "body": ""} for i in range(102)]
    # Three-digit remainders and envelopes near 4000: reserving an unlisted count of
    # zero, or filling ids without measuring the final JSON, can grow past the budget.
    for padding in range(3480, 3780):
        pool[0]["body"] = "x" * padding
        text = server._bounded_records(
            pool,
            key=key,
            lead={},
            middle={},
            limit=1,
            budget=4000,
            echo="",
            unresolved=[],
            files_given=False,
            has_query=False,
            history_hint=False,
        )
        out = json.loads(text)
        assert len(text) <= 4000
        taken = out["returned"]
        assert out["omitted_ids"] == [r["id"] for r in pool[taken:]][: len(out["omitted_ids"])]
        assert len(out["omitted_ids"]) + out.get("omitted_ids_unlisted", 0) == 102 - taken


def test_B9_digit_boundaries(tmp_path, monkeypatch):
    with Store(tmp_path / "domains") as store:
        rows = [
            {"id": f"{i:026}", "slug": f"s{i}", "title": "Title", "summary": ""} for i in range(102)
        ]
        monkeypatch.setattr(server, "_list_domains_impl", lambda *_args, **_kw: rows)
        for padding in range(3400, 3720):
            rows[0]["summary"] = "x" * padding
            text = server._domain_listing(store, limit=1, budget_chars=4000)
            out = json.loads(text)
            assert len(text) <= 4000
            taken = out["returned"]
            ids = [r["id"] for r in rows[taken:]]
            assert out["omitted_ids"] == ids[: len(out["omitted_ids"])]
            assert len(out["omitted_ids"]) + out.get("omitted_ids_unlisted", 0) == len(ids)


def test_B10_uncut_listings_have_no_omitted_ids(tmp_path):
    with Store(tmp_path / "small") as store:
        d = _decision(store, 0)
        f = _fact(store, 0)
        store.add_domain(
            Domain(slug="small", title="Small", summary="s", provenance=Provenance(source="manual"))
        )
        for text in [
            server._decision_listing(store, None, ids=[d.id]),
            server._fact_listing(store, None, ids=[f.id]),
            server._domain_listing(store),
        ]:
            out = json.loads(text)
            assert out["returned"] == 1
            assert "omitted_ids" not in out and "omitted_ids_unlisted" not in out


def test_B4_complete_binding_answer_near_budget_is_byte_identical(tmp_path):
    with Store(tmp_path / "near-budget") as store:
        entity = store.get_or_create_entity(Descriptor(name="Near", file_path="near.py"))
        with store._mutation():
            for i in range(40):
                _bind(store, _decision(store, i, size=1).id, entity.entity_id)
        expected = server._dump(server._find_entity_impl(store, "Near"))
        assert 3800 < len(expected) <= 4000
        assert server._find_entity_listing(store, "Near", None, 4000) == expected


def test_B6_complete_violation_answer_near_budget_is_not_clipped(tmp_path, monkeypatch):
    with Store(tmp_path / "near-budget") as store:
        rows = [
            {"code": "parse-error", "path": "a", "detail": "x" * 3800},
            {"code": "parse-error", "path": "b", "detail": "short"},
        ]
        expected = {"clean": False, "total": 2, "counts": {"parse-error": 2}, "violations": rows}
        expected_text = server._dump(expected)
        assert 3800 < len(expected_text) <= 4000
        monkeypatch.setattr(
            server, "_verify_store_impl", lambda _store: {"clean": False, "violations": rows}
        )
        assert server._verify_listing(store, 4000) == expected_text


@pytest.mark.parametrize("key", ["decisions", "facts"])
@pytest.mark.parametrize("budget", [4000, 40000])
def test_B7_B8_oversized_record_does_not_suggest_the_same_call(key, budget):
    text = server._bounded_records(
        [{"id": "0" * 26, "body": "x" * 45000}],
        key=key,
        lead={},
        middle={},
        limit=100,
        budget=budget,
        echo="ids=[...]",
        unresolved=[],
        files_given=False,
        has_query=False,
        history_hint=False,
    )
    out = json.loads(text)
    assert len(text) <= budget
    assert out["returned"] == 0 and out["omitted_ids"] == ["0" * 26]
    assert "(ids=[...])" not in out["hint"]


def test_B5_complete_candidates_near_budget_are_byte_identical(tmp_path):
    with Store(tmp_path / "near-budget") as store:
        with store._mutation():
            for i in range(45):
                store.get_or_create_entity(Descriptor(name="Twin", file_path=f"{i}.py"))
        expected = server._dump(server._find_entity_impl(store, "Twin"))
        assert 3800 < len(expected) <= 4000
        assert server._find_entity_listing(store, "Twin", None, 4000) == expected


def test_B6_complete_answer_after_clipping_near_budget_is_not_cut(tmp_path, monkeypatch):
    with Store(tmp_path / "near-budget") as store:
        rows = [
            {"code": "parse-error", "path": "a", "detail": "x" * 1856},
            {"code": "parse-error", "path": "b", "detail": "y" * 1856},
            {"code": "parse-error", "path": "c", "detail": "z" * 50000},
        ]
        expected = {
            "clean": False,
            "total": 3,
            "counts": {"parse-error": 3},
            "violations": [*rows[:2], {**rows[2], "detail": "z" * 79 + "…"}],
        }
        expected_text = server._dump(expected)
        assert len(expected_text) == 3997
        monkeypatch.setattr(
            server, "_verify_store_impl", lambda _store: {"clean": False, "violations": rows}
        )
        assert server._verify_listing(store, 4000) == expected_text


@pytest.mark.parametrize("key", ["decisions", "facts"])
def test_B10_complete_record_pool_near_budget_is_not_cut(key):
    pool = [{"id": "0" * 26, "body": "x" * 3700}, {"id": "1" * 26, "body": ""}]
    expected = server._dump({"matched": 2, "returned": 2, "omitted": 0, key: pool, "hint": None})
    assert 3800 < len(expected) <= 4000
    text = server._bounded_records(
        pool,
        key=key,
        lead={},
        middle={},
        limit=100,
        budget=4000,
        echo="",
        unresolved=[],
        files_given=False,
        has_query=False,
        history_hint=False,
    )
    assert text == expected
