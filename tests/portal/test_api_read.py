"""HTTP contracts for the Portal's read views (D4–D6, D9)."""

import json
import sys

import pytest

from sidegraph.portal import contract
from sidegraph.schema import AnchorBinding, Descriptor, Entity
from sidegraph.server import _decision_listing
from sidegraph.store import Store
from tests.store_record_support import _decision, _domain, _fact


def test_graph_record_domains(portal_ctx, portal_client) -> None:
    ctx = portal_ctx()
    with Store(ctx.store_path) as store:
        decision = store.add_decision(_decision())
        evidence = store.add_fact(_fact(supports=[decision.id]))
        domain = store.add_domain(_domain())
        store.ratify_domains(accept=[domain.domain_id], actor="Owner")
        entity = store.upsert_entity(
            Entity(
                canonical_name="read_client",
                descriptor=Descriptor(name="read_client", file_path="src/client.py"),
                last_seen_node_id="client_read",
            )
        )
        store.add_binding(AnchorBinding(record_id=decision.id, entity_id=entity.entity_id, tier=2))
        store.replace_domain_members(domain.domain_id, [("client_read", "src/client.py", None)])
    with portal_client(ctx) as client:
        response = client.get("/api/graph")
        assert response.status_code == 200
        graph = contract.Graph.model_validate(response.json())
        assert graph.record_domains[decision.id] == [domain.slug]
        assert not graph.membership_certified
        response = client.get("/api/records", params={"domain": domain.slug})
        assert response.status_code == 200
        rows = contract.Records.model_validate(response.json()).rows
        assert {row.id for row in rows} == {decision.id, evidence.id}


def test_search_equals_tool_order(portal_ctx, portal_client) -> None:
    ctx = portal_ctx()
    with Store(ctx.store_path) as store:
        adr = store.add_decision(_decision("retry with bounded delay"))
        gotcha = store.add_decision(_decision("retry with bounded backoff", kind="gotcha"))
        fact = store.add_fact(_fact("bounded retry took 12 seconds", supports=[adr.id]))
        draft = store.add_decision(_decision("bounded retry proposal", status="proposed"))
        unrelated = store.add_decision(_decision("retry without a match"))
        expected_full = json.loads(_decision_listing(store, None, query="retry bound"))
        expected_partial = json.loads(_decision_listing(store, None, query="retry bound absent"))
    assert [row["id"] for row in expected_full["decisions"]] == [gotcha.id, adr.id, draft.id]
    assert [row["id"] for row in expected_full["facts"]] == [fact.id]
    with portal_client(ctx) as client:
        for query, expected in [
            ("retry bound", expected_full),
            ("retry bound absent", expected_partial),
        ]:
            response = client.get("/api/search", params={"q": query})
            assert response.status_code == 200
            hits = contract.SearchResult.model_validate(response.json()).hits
            decisions = [hit.id for hit in hits if hit.type == "decision"]
            facts = [hit.id for hit in hits if hit.type == "fact"]
            assert decisions == [row["id"] for row in expected["decisions"]]
            assert facts == [row["id"] for row in expected["facts"]]
            assert [hit.id for hit in hits] == [gotcha.id, adr.id, fact.id, draft.id]
            assert unrelated.id not in decisions
            assert all(hit.match["words"] == ["retry", "bound"] for hit in hits)


def test_search_includes_regulated_proposals(portal_ctx, portal_client, monkeypatch) -> None:
    monkeypatch.setenv("SIDEGRAPH_UNRATIFIED", "off")
    ctx = portal_ctx()
    with Store(ctx.store_path) as store:
        draft = store.add_decision(_decision("regulated test", status="proposed"))
        fact = store.add_fact(_fact("regulated evidence", status="proposed", supports=[draft.id]))
        tool_result = json.loads(_decision_listing(store, None, query="regulated"))
    assert not tool_result["decisions"]
    assert not tool_result.get("facts")
    with portal_client(ctx) as client:
        response = client.get("/api/search", params={"q": "regulated"})
        assert response.status_code == 200
        assert [hit.id for hit in contract.SearchResult.model_validate(response.json()).hits] == [
            draft.id,
            fact.id,
        ]


def test_id_prefix_8_chars_opens_record(portal_ctx, portal_client) -> None:
    ctx = portal_ctx()
    with Store(ctx.store_path) as store:
        record = store.add_decision(_decision(id="01KAB123000000000000000001"))
    with portal_client(ctx) as client:
        response = client.get("/api/search", params={"q": record.id[:8].lower()})
        assert response.status_code == 200
        assert contract.SearchResult.model_validate(response.json()).open_id == record.id


def test_id_prefix_7_chars_does_not(portal_ctx, portal_client) -> None:
    ctx = portal_ctx()
    with Store(ctx.store_path) as store:
        record = store.add_decision(_decision(id="01KAB123000000000000000001"))
    with portal_client(ctx) as client:
        response = client.get("/api/search", params={"q": record.id[:7]})
        assert response.status_code == 200
        assert contract.SearchResult.model_validate(response.json()).open_id is None


def test_search_cyrillic_and_long_text(portal_ctx, portal_client) -> None:
    ctx = portal_ctx()
    choice = "🐍 " + "д" * 20_000 + " ОГРАНИЧЕНИЕ"
    with Store(ctx.store_path) as store:
        decision = store.add_decision(_decision("Задержка чтения", choice=choice))
    with portal_client(ctx) as client:
        response = client.get("/api/search", params={"q": "задержка ограничение"})
        assert response.status_code == 200
        (hit,) = contract.SearchResult.model_validate(response.json()).hits
        assert hit.id == decision.id
        assert hit.title == decision.title
        assert hit.snippet == choice


def test_records_filters(portal_ctx, portal_client) -> None:
    ctx = portal_ctx()
    with Store(ctx.store_path) as store:
        decision = store.add_decision(_decision("Retry delay", kind="gotcha", layer="technical"))
        draft = store.add_decision(_decision("Retry delay", status="proposed"))
        rejected = store.add_decision(_decision("Retry delay", status="proposed"))
        store.drop(rejected.id)
        domain = store.add_domain(_domain("retry"))
        store.ratify_domains(accept=[domain.domain_id], actor="Owner")
        entity = store.upsert_entity(Entity(canonical_name="domain:retry", kind="abstract"))
        store.add_binding(AnchorBinding(record_id=decision.id, entity_id=entity.entity_id, tier=1))
    with portal_client(ctx) as client:
        for filters, wanted in [
            (
                {
                    "q": "retry",
                    "status": "accepted",
                    "kind": "gotcha",
                    "domain": "retry",
                    "layer": "technical",
                    "anchors": "unchecked",
                },
                [decision.id],
            ),
            ({"q": "retry", "anchors": "none"}, [draft.id]),
            ({"status": "rejected", "history": "true"}, [rejected.id]),
            ({"layer": "business"}, []),
        ]:
            response = client.get("/api/records", params=filters)
            assert response.status_code == 200
            assert [r.id for r in contract.Records.model_validate(response.json()).rows] == wanted


def test_empty_store_no_graph_every_read_route_200(portal_ctx, portal_client) -> None:
    ctx = portal_ctx()
    with portal_client(ctx) as client:
        # Collection routes and session metadata are meaningful without any records.
        for path, model, field in [
            ("/api/graph", contract.Graph, "nodes"),
            ("/api/search", contract.SearchResult, "hits"),
            ("/api/records", contract.Records, "rows"),
            ("/api/queue", contract.Queue, "decisions"),
        ]:
            response = client.get(path)
            assert response.status_code == 200, path
            assert not getattr(model.model_validate(response.json()), field)
        response = client.get("/api/session")
        assert response.status_code == 200
        assert not contract.Session.model_validate(response.json()).graph_newer_than_sync


def _with_code(
    ctx,
    *,
    file: str | None = "src/client.py",
    status: str = "accepted",
    binding_status: str = "live",
) -> tuple[str, str]:
    with Store(ctx.store_path) as store:
        decision = store.add_decision(_decision(status=status))
        entity = store.upsert_entity(
            Entity(
                canonical_name="read_client",
                descriptor=Descriptor(name="read_client", file_path=file),
                last_seen_node_id="client_read",
            )
        )
        store.add_binding(
            AnchorBinding(
                record_id=decision.id, entity_id=entity.entity_id, tier=2, status=binding_status
            )
        )
        return decision.id, entity.entity_id


def test_card_fields_history_evidence_provenance(portal_ctx, portal_client) -> None:
    from sidegraph.schema import Provenance
    from sidegraph.store_layout import DRIFT_CACHE_KEY

    ctx = portal_ctx()
    with Store(ctx.store_path) as store:
        old = store.add_decision(_decision("Previous"))
        current = store.add_decision(
            _decision(
                "Current",
                supersedes=old.id,
                choice="🐍 " + "д" * 20_000,
                rejected="An alternative",
                consequences="An effect",
                provenance=Provenance(source="manual", author="Jo", commit="f" * 40),
                ratified_by="Owner",
            )
        )
        fact = store.add_fact(_fact("Supporting fact", supports=[current.id]))
        pending = store.add_fact(_fact(status="proposed", supports=[current.id]))
        successor = store.add_decision(
            _decision("Pending successor", status="proposed", supersedes=current.id)
        )
        store.set_meta(DRIFT_CACHE_KEY, json.dumps({"by_commit": {"f" * 40: [current.id]}}))
    with portal_client(ctx) as client:
        for record in (old, current, successor):
            response = client.get(f"/api/records/{record.id}")
            assert response.status_code == 200
            card = contract.RecordCard.model_validate(response.json())
            assert [row.id for row in card.history] == [old.id, current.id, successor.id]
            if record == current:
                assert card.record.model_dump() == current.model_dump()
                assert [item.id for item in card.evidence] == [fact.id, pending.id]
                assert card.pending_evidence == [pending.id]
                assert card.pending_successor == successor.id
                assert card.drifted


def test_anchor_words_cold_index_unchecked_on_card(portal_ctx, portal_client) -> None:
    ctx = portal_ctx()
    decision_id, entity_id = _with_code(ctx)
    with portal_client(ctx) as client:
        response = client.get(f"/api/records/{decision_id}")
        assert response.status_code == 200
        (anchor,) = contract.RecordCard.model_validate(response.json()).anchors
        assert anchor.entity_id == entity_id
        assert anchor.internal_status == "live"
        assert anchor.status_word == "unchecked"


def test_banner_graph_newer_than_sync(portal_ctx, portal_client, tmp_path) -> None:
    # The session banner was already implemented by the launch routes.
    from sidegraph.sync import LAST_SYNCED_KEY
    from tests.graph_fixtures import reader_of

    reader = reader_of(tmp_path)
    ctx = portal_ctx(graph_path=reader.path)
    stamp = reader.sync_stamp()
    with portal_client(ctx) as client:
        for last_stamp, newer in [("older", True), (stamp, False), (f"index-only:{stamp}", False)]:
            with Store(ctx.store_path) as store:
                store.set_meta(LAST_SYNCED_KEY, last_stamp)
            response = client.get("/api/session")
            assert response.status_code == 200
            assert contract.Session.model_validate(response.json()).graph_newer_than_sync is newer


def test_shifted_flag_when_file_newer_than_graph(portal_ctx, portal_client, tmp_path) -> None:
    import os

    from tests.graph_fixtures import node, reader_of
    from tests.graph_repo_support import git

    git(tmp_path, "init")
    (tmp_path / "src").mkdir()
    source = tmp_path / "src/client.py"
    source.write_text("line one\nline two\nline three\n", encoding="utf-8")
    reader = reader_of(
        tmp_path,
        node("next", "next_client", "src/client.py", location="L3"),
        node("client_read", "read_client", "src/client.py", location="L2"),
        node("note", "docstring", "src/client.py", location="L2", file_type="docstring"),
    )
    os.utime(reader.path, (1_700_000_000, 1_700_000_000))
    ctx = portal_ctx(graph_path=reader.path)
    decision_id, entity_id = _with_code(ctx)
    with portal_client(ctx) as client:
        response = client.get(f"/api/records/{decision_id}")
        assert response.status_code == 200
        (anchor,) = contract.RecordCard.model_validate(response.json()).anchors
        assert (anchor.file, anchor.start, anchor.end) == ("src/client.py", 2, 2)
        assert anchor.editor_path == str(source)
        for timestamp, shifted in [(1_700_000_001, True), (1_700_000_000, False)]:
            os.utime(source, (timestamp, timestamp))
            response = client.get(f"/api/records/{decision_id}/anchors/{entity_id}/snippet")
            assert response.status_code == 200
            snippet = contract.Snippet.model_validate(response.json())
            assert snippet.lines == ["line two"]
            assert snippet.shifted is shifted


def test_record_card_404_has_reason(portal_ctx, portal_client) -> None:
    ctx = portal_ctx()
    with portal_client(ctx) as client:
        response = client.get("/api/records/01KAA000000000000000000000")
    assert response.status_code == 404
    error = contract.ErrorBody.model_validate(response.json()).error
    assert error.code == "not_found"
    assert error.details["reason"] == "not_on_this_branch"


def test_queue_nested_evidence_has_no_actions(portal_ctx, portal_client) -> None:
    ctx = portal_ctx()
    with Store(ctx.store_path) as store:
        first = store.add_decision(_decision("First", status="proposed"))
        second = store.add_decision(_decision("Second", status="proposed"))
        settled = store.add_decision(_decision("Settled"))
        evidence = store.add_fact(_fact(status="proposed", supports=[first.id, second.id]))
        standalone = store.add_fact(_fact(status="proposed", supports=[settled.id]))
        domain = store.add_domain(_domain())
    with portal_client(ctx) as client:
        response = client.get("/api/queue")
        assert response.status_code == 200
        body = response.json()
        queue = contract.Queue.model_validate(body)
        assert {item.record.id for item in queue.decisions} == {first.id, second.id}
        assert [item.record.id for item in queue.facts] == [standalone.id]
        assert [item.record.domain_id for item in queue.domains] == [domain.domain_id]
        for item in body["decisions"]:
            assert [fact["id"] for fact in item["evidence"]] == [evidence.id]
            assert set(item["evidence"][0]) <= set(type(evidence).model_fields)


def test_queue_signal_unchecked_on_cold_index(portal_ctx, portal_client) -> None:
    ctx = portal_ctx()
    decision_id, entity_id = _with_code(ctx, status="proposed")
    with portal_client(ctx) as client:
        response = client.get("/api/queue")
        assert response.status_code == 200
        (item,) = contract.Queue.model_validate(response.json()).decisions
        assert item.record.id == decision_id
        (anchor,) = item.signals["anchors"]
        assert anchor.entity_id == entity_id
        assert anchor.status_word == "unchecked"


def test_queue_hides_stale_advice(portal_ctx, portal_client) -> None:
    from tests.store_format2_support import put_file

    ctx = portal_ctx()
    with Store(ctx.store_path) as store:
        draft = store.add_decision(_decision("Retry connection", status="proposed"))
        similar = store.add_decision(_decision("Retry connection later"))
        assert store.write_advice([(draft.id, "accept", "Evidence checked.")], None) == {
            draft.id: "recorded"
        }
    with portal_client(ctx) as client:
        response = client.get("/api/queue")
        assert response.status_code == 200
        (item,) = contract.Queue.model_validate(response.json()).decisions
        assert item.advice == {"verdict": "accept", "reason": "Evidence checked."}
        assert [row.id for row in item.signals["similar"]] == [similar.id]

        changed = draft.model_dump(mode="json")
        changed["choice"] = "New choice."
        put_file(ctx.store_path, "decision", changed)
        response = client.get("/api/queue")
        assert response.status_code == 200
        (item,) = contract.Queue.model_validate(response.json()).decisions
        assert item.record.choice == "New choice."
        assert item.advice is None


def test_search_honors_limit(portal_ctx, portal_client) -> None:
    ctx = portal_ctx()
    with Store(ctx.store_path) as store:
        store.add_decision(_decision("Retry sooner"))
        first = store.add_decision(_decision("Retry longer", kind="gotcha"))
    with portal_client(ctx) as client:
        response = client.get("/api/search", params={"q": "retry", "limit": 1})
        assert response.status_code == 200
        assert [hit.id for hit in contract.SearchResult.model_validate(response.json()).hits] == [
            first.id
        ]
        for invalid in ("no-limit", "0", "-1"):
            response = client.get("/api/search", params={"q": "retry", "limit": invalid})
            assert response.status_code == 422
            error = contract.ErrorBody.model_validate(response.json()).error
            assert (error.code, error.details["field"]) == ("invalid", "limit")


@pytest.mark.parametrize(
    ("status", "word"), [("live", "found"), ("degraded", "ambiguous"), ("orphaned", "missing")]
)
def test_anchor_words_on_warm_card(portal_ctx, portal_client, status, word) -> None:
    from sidegraph.store_layout import VOLATILE_STALE_KEY

    ctx = portal_ctx()
    decision_id, _ = _with_code(ctx, binding_status=status)
    with Store(ctx.store_path) as store:
        store.delete_meta(VOLATILE_STALE_KEY)
    with portal_client(ctx) as client:
        response = client.get(f"/api/records/{decision_id}")
        assert response.status_code == 200
        (anchor,) = contract.RecordCard.model_validate(response.json()).anchors
        assert (anchor.internal_status, anchor.status_word) == (status, word)


def test_snippet_unavailable_has_reason(portal_ctx, portal_client, tmp_path) -> None:
    from tests.graph_repo_support import git

    git(tmp_path, "init")
    ctx = portal_ctx()
    decision_id, entity_id = _with_code(ctx)
    with portal_client(ctx) as client:
        response = client.get(f"/api/records/{decision_id}/anchors/{entity_id}/snippet")
        assert response.status_code == 404
        error = contract.ErrorBody.model_validate(response.json()).error
        assert error.details["reason"] == "snippet_unavailable"


def test_search_shows_matching_text_even_when_found_in_history(portal_ctx, portal_client) -> None:
    ctx = portal_ctx()
    with Store(ctx.store_path) as store:
        contextual = store.add_decision(_decision("Context case", context="A splendid result."))
        old = store.add_decision(_decision("Past version", choice="Use the mnemonic delay."))
        former = store.upsert_entity(Entity(canonical_name="former_probe"))
        store.add_binding(AnchorBinding(record_id=old.id, entity_id=former.entity_id, tier=2))
        current = store.add_decision(_decision("Current version", supersedes=old.id))
    with portal_client(ctx) as client:
        for q, record, text in [
            ("splendid", contextual, contextual.context),
            ("mnemonic", current, old.choice),
        ]:
            response = client.get("/api/search", params={"q": q})
            assert response.status_code == 200
            (hit,) = contract.SearchResult.model_validate(response.json()).hits
            assert (hit.id, hit.snippet) == (record.id, text)
        response = client.get("/api/search", params={"q": "former_probe"})
        assert response.status_code == 200
        (hit,) = contract.SearchResult.model_validate(response.json()).hits
        assert (hit.id, hit.match["via_anchor"]) == (current.id, "former_probe")


@pytest.mark.parametrize("pathless", [False, True])
@pytest.mark.parametrize(
    "graph_location",
    [
        "default",
        "absolute",
        "custom-relative",
        "custom-absolute",
        pytest.param(
            "alias",
            marks=pytest.mark.skipif(sys.platform == "win32", reason="POSIX directory symlink"),
        ),
    ],
)
def test_nested_project_snippet_uses_paths_in_that_project(
    portal_ctx, portal_client, tmp_path, monkeypatch, pathless, graph_location
) -> None:
    from tests.graph_fixtures import node, reader_of
    from tests.graph_repo_support import git

    git(tmp_path, "init")
    (tmp_path / "src").mkdir()
    (tmp_path / "src/client.py").write_text("unrelated source\n")
    project = tmp_path / "nested"
    (project / "src").mkdir(parents=True)
    (project / "src/client.py").write_text("right source\n")
    graph_directory = "build/graphs" if graph_location.startswith("custom") else "graphify-out"
    (project / graph_directory).mkdir(parents=True)
    monkeypatch.delenv("SIDEGRAPH_GRAPH", raising=False)
    reader = reader_of(
        project / graph_directory,
        node("client_read", "read_client", "src/client.py"),
    )
    graph_path = reader.path
    if graph_location in ("absolute", "custom-absolute"):
        monkeypatch.setenv("SIDEGRAPH_GRAPH", str(graph_path))
    if graph_location == "custom-relative":
        monkeypatch.setenv("SIDEGRAPH_GRAPH", f"{graph_directory}/graph.json")
    if graph_location == "alias":
        alias = tmp_path / "graph-alias"
        alias.symlink_to(graph_path.parent, target_is_directory=True)
        graph_path = alias / graph_path.name
    ctx = portal_ctx(store_path=project / ".sidegraph", graph_path=graph_path)
    decision_id, entity_id = _with_code(ctx, file=None if pathless else "src/client.py")
    with portal_client(ctx) as client:
        response = client.get(f"/api/records/{decision_id}")
        assert response.status_code == 200
        (anchor,) = contract.RecordCard.model_validate(response.json()).anchors
        assert (anchor.file, anchor.start) == ("nested/src/client.py", 1)
        assert anchor.editor_path == str(project / "src/client.py")
        response = client.get(f"/api/records/{decision_id}/anchors/{entity_id}/snippet")
        assert response.status_code == 200
        snippet = contract.Snippet.model_validate(response.json())
        assert (snippet.file, snippet.lines) == (anchor.file, ["right source"])


def test_card_history_orders_competing_versions_by_date(portal_ctx, portal_client) -> None:
    from datetime import UTC, datetime

    ctx = portal_ctx()
    with Store(ctx.store_path) as store:
        old = store.add_decision(_decision("Old", valid_from=datetime(2020, 1, 1, tzinfo=UTC)))
        early = store.add_decision(
            _decision(
                "Rejected draft",
                id="01KAA000000000000000000002",
                status="proposed",
                valid_from=datetime(2021, 1, 1, tzinfo=UTC),
                supersedes=old.id,
            )
        )
        store.drop(early.id)
        late = store.add_decision(
            _decision(
                "Accepted successor",
                id="01KAA000000000000000000001",
                valid_from=datetime(2022, 1, 1, tzinfo=UTC),
                supersedes=old.id,
            )
        )
    with portal_client(ctx) as client:
        response = client.get(f"/api/records/{old.id}")
        assert response.status_code == 200
        card = contract.RecordCard.model_validate(response.json())
        assert [row.id for row in card.history] == [old.id, early.id, late.id]


def test_snippet_uses_graph_snapshot_that_supplied_span(
    portal_ctx, portal_client, tmp_path, monkeypatch
) -> None:
    import os

    from sidegraph.portal.context import PortalContext
    from tests.graph_fixtures import node, reader_of, write_graph
    from tests.graph_repo_support import git

    git(tmp_path, "init")
    (tmp_path / "src").mkdir()
    source = tmp_path / "src/client.py"
    source.write_text("old line\nnew line\n")
    reader = reader_of(tmp_path, node("client_read", "read_client", "src/client.py", location="L1"))
    os.utime(reader.path, (1_700_000_000, 1_700_000_000))
    os.utime(source, (1_700_000_001, 1_700_000_001))
    ctx = portal_ctx(graph_path=reader.path)
    decision_id, entity_id = _with_code(ctx)
    original = PortalContext.reader

    def rebuild_after_read(context):
        result = original(context)
        if reader.path.stat().st_mtime == 1_700_000_000:
            replacement = write_graph(
                tmp_path,
                "replacement.json",
                {"nodes": [node("client_read", "read_client", "src/client.py", location="L2")]},
            )
            os.utime(replacement, (1_700_000_002, 1_700_000_002))
            replacement.replace(reader.path)
        return result

    monkeypatch.setattr(PortalContext, "reader", rebuild_after_read)
    with portal_client(ctx) as client:
        response = client.get(f"/api/records/{decision_id}/anchors/{entity_id}/snippet")
        assert response.status_code == 200
        snippet = contract.Snippet.model_validate(response.json())
        assert (snippet.start, snippet.lines, snippet.shifted) == (2, ["new line"], False)


def test_search_excerpt_covers_matches_in_separate_fields(portal_ctx, portal_client) -> None:
    ctx = portal_ctx()
    with Store(ctx.store_path) as store:
        record = store.add_decision(
            _decision("Colour", context="Start with cobalt.", choice="Then use saffron.")
        )
    with portal_client(ctx) as client:
        response = client.get("/api/search", params={"q": "cobalt saffron"})
        assert response.status_code == 200
        (hit,) = contract.SearchResult.model_validate(response.json()).hits
        assert hit.id == record.id
        assert all(word in hit.snippet for word in hit.match["words"])


@pytest.mark.parametrize(
    ("field", "original", "updated"),
    [
        ("choice", "Use one worker", "Use 200 workers"),
        ("valid_from", "2026-06-10T23:30:00Z", "2026-06-11T01:30:00+02:00"),
    ],
)
def test_queue_advice_matches_displayed_draft_in_one_snapshot(
    portal_ctx, portal_client, monkeypatch, field, original, updated
) -> None:
    from tests.store_format2_support import put_file

    ctx = portal_ctx()
    with Store(ctx.store_path) as store:
        old = store.add_decision(_decision("Worker count", status="proposed", **{field: original}))
        assert store.write_advice([(old.id, "accept", "One worker is fine.")], None) == {
            old.id: "recorded"
        }
    canonical = old.model_dump(mode="json")
    put_file(ctx.store_path, "decision", {**canonical, field: updated})
    iter_proposed = Store.iter_proposed

    def switch_after_read(store):
        yield from iter_proposed(store)
        put_file(ctx.store_path, "decision", canonical)

    monkeypatch.setattr(Store, "iter_proposed", switch_after_read)
    with portal_client(ctx) as client:
        response = client.get("/api/queue")
        assert response.status_code == 200
        (item,) = contract.Queue.model_validate(response.json()).decisions
        assert item.record.model_dump(mode="json")[field] == updated
        assert item.advice is None


def test_queue_reuses_similar_record_summaries(portal_ctx, portal_client, monkeypatch) -> None:
    from collections import Counter

    ctx = portal_ctx()
    size = 5
    with Store(ctx.store_path) as store:
        for index in range(size):
            store.add_decision(_decision("Retry connection", status="proposed", choice=str(index)))
    get_bindings = Store.bindings_for_record
    lookups = Counter()

    def count_bindings(store, record_id):
        lookups[record_id] += 1
        return get_bindings(store, record_id)

    monkeypatch.setattr(Store, "bindings_for_record", count_bindings)
    with portal_client(ctx) as client:
        response = client.get("/api/queue")
        assert response.status_code == 200
        items = contract.Queue.model_validate(response.json()).decisions
        assert len(items) == size
        for item in items:
            assert len(item.signals["similar"]) == size - 1
            # Once for its own anchors and once for the shared similar summary.
            assert lookups[item.record.id] == 2


def test_search_excerpt_covers_words_on_separate_anchors(portal_ctx, portal_client) -> None:
    ctx = portal_ctx()
    with Store(ctx.store_path) as store:
        record = store.add_decision(_decision("Colour", choice="Ink"))
        for name in ("cobalt", "saffron"):
            anchor = store.upsert_entity(Entity(canonical_name=name))
            store.add_binding(
                AnchorBinding(record_id=record.id, entity_id=anchor.entity_id, tier=2)
            )
    with portal_client(ctx) as client:
        response = client.get("/api/search", params={"q": "cobalt saffron"})
        assert response.status_code == 200
        (hit,) = contract.SearchResult.model_validate(response.json()).hits
        assert hit.id == record.id
        assert hit.match["via_anchor"] in ("cobalt", "saffron")
        assert all(
            word in f"{hit.snippet} {hit.match['via_anchor']}" for word in hit.match["words"]
        )


def test_queue_reads_decisions_and_evidence_from_same_snapshot(
    portal_ctx, portal_client, monkeypatch
) -> None:
    from sidegraph import store as store_module

    # WAL permits the other connection to commit while this reader holds an older snapshot.
    monkeypatch.setattr(store_module, "INDEX_JOURNAL_PRAGMA", "PRAGMA journal_mode=WAL")
    ctx = portal_ctx()
    draft = _decision("Arriving with evidence", status="proposed")
    fact = _fact(status="proposed", supports=[draft.id])
    iter_proposed = Store.iter_proposed

    with Store(ctx.store_path) as writer, portal_client(ctx) as client:
        wrote = False

        def commit_after_read(store):
            nonlocal wrote
            yield from iter_proposed(store)
            if not wrote:
                wrote = True
                with writer._mutation():
                    writer.add_decision(draft)
                    writer.add_fact(fact)

        monkeypatch.setattr(Store, "iter_proposed", commit_after_read)
        response = client.get("/api/queue")
        assert response.status_code == 200
        before = contract.Queue.model_validate(response.json())
        assert before.decisions == before.facts == []
        response = client.get("/api/queue")
        assert response.status_code == 200
        after = contract.Queue.model_validate(response.json())
        (item,) = after.decisions
        assert item.record.id == draft.id
        assert [evidence.id for evidence in item.evidence] == [fact.id]
        assert after.facts == []


def test_graph_route_passes_the_portal_node_cap(
    portal_ctx, portal_client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The portal's cap is its own (3,000); the 800 of the static export stays the default."""
    from sidegraph.portal import api_read
    from sidegraph.viz import model

    seen: list[int] = []
    real = api_read.build_graph

    def spy(store, **kwargs):
        seen.append(kwargs.get("max_nodes", model.build_graph.__kwdefaults__["max_nodes"]))
        return real(store, **kwargs)

    monkeypatch.setattr(api_read, "build_graph", spy)
    ctx = portal_ctx()
    with portal_client(ctx) as client:
        assert client.get("/api/graph").status_code == 200
    assert seen == [3000]
    assert model.build_graph.__kwdefaults__["max_nodes"] == 800
