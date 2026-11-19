"""Examples pin the Portal wire contract for the backend and the development mock."""

import json
import re
from collections.abc import Iterator
from importlib.resources import files
from pathlib import Path

import pytest
from pydantic import TypeAdapter, ValidationError
from starlette.testclient import TestClient

EXAMPLES = Path(__file__).with_name("contract")
ROOT = Path(__file__).resolve().parents[2]
ORIGIN = "http://127.0.0.1:8765"
ROUTE_PATHS = {
    "index": ("GET", "/", "static", None),
    "static": ("GET", "/static/{path}", "static", None),
    "live-ticket": ("POST", "/api/live-ticket", "api_session", "LiveTicket"),
    "live": ("GET", "/live", "live", None),
    "session": ("GET", "/api/session", "api_session", "Session"),
    "settings": ("POST", "/api/settings", "api_session", "Settings"),
    "graph": ("GET", "/api/graph", "api_read", "Graph"),
    "search": ("GET", "/api/search", "api_read", "SearchResult"),
    "records": ("GET", "/api/records", "api_read", "Records"),
    "record": ("GET", "/api/records/{id}", "api_read", "RecordCard"),
    "snippet": ("GET", "/api/records/{id}/anchors/{entity_id}/snippet", "api_read", "Snippet"),
    "queue": ("GET", "/api/queue", "api_read", "Queue"),
    "preview": ("POST", "/api/queue/preview", "api_write", "PreviewResult"),
    "apply": ("POST", "/api/queue/apply", "api_write", "ApplyResult"),
    "edit": ("POST", "/api/records/{id}/edit", "api_write", "OperationResult"),
    "edit-and-accept": (
        "POST",
        "/api/records/{id}/edit-and-accept",
        "api_write",
        "OperationResult",
    ),
    "save-as-new": ("POST", "/api/records/{id}/save-as-new", "api_write", "OperationResult"),
    "catalog": ("GET", "/api/agent-view/catalog", "api_agent", "Catalog"),
    "tool": ("POST", "/api/agent-view/tool", "api_agent", "Emulation"),
    "hook": ("POST", "/api/agent-view/hook", "api_agent", "Emulation"),
}


def test_route_table_and_examples_cover_the_contract() -> None:
    from sidegraph.portal import contract

    routes = {(route.method, route.path): route for route in contract.ROUTES}
    assert len(routes) == len(contract.ROUTES) == len(ROUTE_PATHS)
    assert set(routes) == {entry[:2] for entry in ROUTE_PATHS.values()}
    for key, (method, path, owner, response) in ROUTE_PATHS.items():
        route = routes[method, path]
        expected_auth = (
            "ticket" if key == "live" else "token" if path.startswith("/api/") else "none"
        )
        assert route.auth == expected_auth
        assert route.module == owner
        assert route.response is (getattr(contract, response) if response else None)
        assert (EXAMPLES / f"{key}.ok.json").is_file()
        if method == "GET" and expected_auth == "token":
            empty = json.loads((EXAMPLES / f"{key}.empty_store.json").read_text(encoding="utf-8"))
            assert empty.get("error", {}).get("code") == (
                "not_found" if key in ("record", "snippet") else None
            )


def test_examples_validate_against_their_route() -> None:
    from sidegraph.portal import contract

    routes = {(route.method, route.path): route for route in contract.ROUTES}
    asset_paths = {"index": "/", "static": contract.VIS_NETWORK_PATH}
    for example in sorted(EXAMPLES.glob("*.json")):
        key, scenario = example.stem.split(".")
        model = routes[ROUTE_PATHS[key][:2]].response
        body = json.loads(example.read_text(encoding="utf-8"))
        if isinstance(body, dict) and "error" in body:
            assert scenario != "ok", example.name
            contract.ErrorBody.model_validate(body, extra="forbid")
            assert body["error"]["code"] in contract.ERROR_CODES
        elif model is not None:
            model.model_validate(body, extra="forbid")
        elif key == "live":
            TypeAdapter(contract.LiveEvent).validate_python(body, extra="forbid")
        else:
            assert scenario == "ok"
            assert body == {"path": asset_paths[key]}


def test_later_list_element_is_checked() -> None:
    from sidegraph.portal.contract import OperationResult

    with pytest.raises(ValidationError, match=r"parts\.1\.outcome"):
        OperationResult.model_validate(
            {
                "op_key": "00000000-0000-4000-8000-000000000001",
                "state": "started",
                "parts": [
                    {"part": "version", "outcome": "done"},
                    {"part": "bindings", "outcome": "unrecognised"},
                ],
                "offer_save_as_new": False,
            }
        )


def test_examples_exclude_internal_issue_ids() -> None:
    issue_id = re.compile(r"\b(?:SG|SAN)-\d+\b")
    examples = sorted(EXAMPLES.glob("*.json"))
    assert examples
    assert issue_id.search("-".join(("SG", "1")))
    for example in examples:
        assert not issue_id.search(example.read_text(encoding="utf-8")), example.name


@pytest.fixture
def mock() -> Iterator[TestClient]:
    if (
        not (ROOT / "tools" / "portal_mock.py").is_file()
        and not (ROOT / "CLAUDE.public.md").is_file()
    ):
        pytest.skip("The development mock is absent from the release snapshot")
    from tools.portal_mock import create_app

    with TestClient(
        create_app(), base_url=ORIGIN, headers={"X-Sidegraph-Token": "mock-token", "Origin": ORIGIN}
    ) as client:
        yield client


def test_mock_selects_a_sticky_scenario_and_logs_operation_keys(mock: TestClient) -> None:
    from sidegraph.portal.contract import ApplyResult, OperationResult, Queue

    assert mock.post("/__mock/scenario", json={"name": "busy"}).status_code == 200
    for _ in range(2):
        applied = mock.post("/api/queue/apply", json={"accept": [], "reject": []})
        assert "busy" in {
            item.outcome for item in ApplyResult.model_validate(applied.json()).outcomes.values()
        }
    assert mock.get("/api/graph").status_code == 503
    queue = Queue.model_validate(mock.get("/api/queue").json())
    assert mock.post("/__mock/scenario", json={"name": "failed_part"}).status_code == 200
    item = queue.decisions[0]
    path = f"/api/records/{item.record.id}/edit-and-accept"
    request = {
        "op_key": "00000000-0000-4000-8000-000000000001",
        "fields": {},
        "keep_facts": [item.evidence[0].id],
    }
    operation = OperationResult.model_validate(mock.post(path, json=request).json())
    assert operation.op_key == request["op_key"]
    assert not operation.offer_save_as_new
    assert mock.get("/__mock/requests").json()[-1] == {
        "method": "POST",
        "path": path,
        "op_key": request["op_key"],
    }


def test_mock_applies_theme_headers_and_authentication(mock: TestClient) -> None:
    from sidegraph.portal.contract import VIS_NETWORK_PATH, ErrorBody

    dark = mock.get("/")
    assert 'class="th-dark"' in dark.text
    assert dark.headers["x-frame-options"] == "DENY"
    assert dark.headers["referrer-policy"] == "no-referrer"
    assert dark.headers["content-security-policy"] == (
        "default-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; frame-ancestors 'none'"
    )
    assert mock.post("/api/settings", json={"theme": "light"}).json()["theme"] == "light"
    assert 'class="th-light"' in mock.get("/").text
    assert (
        mock.get(VIS_NETWORK_PATH).content
        == files("sidegraph.viz").joinpath("assets", "vis-network.min.js").read_bytes()
    )
    bad_token = mock.get("/api/session", headers={"X-Sidegraph-Token": "wrong"})
    assert bad_token.status_code == 401
    assert ErrorBody.model_validate(bad_token.json()).error.code == "unauthorized"
    assert bad_token.headers["x-frame-options"] == "DENY"
    assert mock.get("/", headers={"Host": "localhost:8765"}).status_code == 421
    assert mock.post("/api/settings", headers={"Origin": "http://example.test"}).status_code == 403


def test_mock_tickets_are_single_use_and_closing_is_streamed(mock: TestClient) -> None:
    ticket = mock.post("/api/live-ticket").json()["ticket"]
    assert mock.get("/live", params={"ticket": "unknown"}).status_code == 401
    assert mock.post("/__mock/close").status_code == 204
    head = mock.head("/live", params={"ticket": ticket})
    assert head.status_code == 405
    response = mock.get("/live", params={"ticket": ticket})
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "event: closing" in response.text
    assert mock.get("/live", params={"ticket": ticket}).status_code == 401
    assert mock.post("/api/live-ticket").json()["ticket"] != ticket


def test_required_scenarios_cover_success_and_error_responses() -> None:
    required = {
        "session": {"cold_index": None, "graph_newer_than_sync": None, "reviewing": None},
        "record": {"cold_index": None, "not_on_this_branch": "not_found"},
        "snippet": {"shifted_lines": None, "no_repository_root": "refused"},
        "queue": {"reviewing": None},
        "preview": {"shared_evidence": None, "empty_store": None},
        "apply": {"busy": None, "refused": None},
        "edit": {
            "pending_evidence": "pending_evidence",
            "not_current": "not_current",
            "busy": "busy",
        },
        "edit-and-accept": {"failed_part": None, "not_current": "not_current"},
        "tool": {"adopted_moves": None, "no_sync": None, "empty_store": None},
        "hook": {"not_emitted": None, "empty_store": None},
    }
    for route, scenarios in required.items():
        for scenario, code in scenarios.items():
            body = json.loads((EXAMPLES / f"{route}.{scenario}.json").read_text(encoding="utf-8"))
            assert body.get("error", {}).get("code") == code, (route, scenario)


@pytest.mark.parametrize("field", ["id", "status"])
def test_card_and_evidence_require_identity_and_status(field: str) -> None:
    from sidegraph.portal.contract import RecordCard

    for key in ("record", "evidence"):
        body = json.loads((EXAMPLES / "record.ok.json").read_text(encoding="utf-8"))
        record = body["record"] if key == "record" else body["evidence"][0]
        del record[field]
        with pytest.raises(ValidationError, match=field):
            RecordCard.model_validate(body)


def test_queue_requires_proposed_records_and_evidence() -> None:
    from sidegraph.portal.contract import Queue

    for group in ("decisions", "facts", "domains", "evidence"):
        body = json.loads((EXAMPLES / "queue.ok.json").read_text(encoding="utf-8"))
        item = (
            body["decisions"][0]["evidence"][0] if group == "evidence" else body[group][0]["record"]
        )
        item["status"] = "accepted"
        with pytest.raises(ValidationError, match="proposed"):
            Queue.model_validate(body)


def test_apply_refusal_requires_a_reason() -> None:
    from sidegraph.portal.contract import ApplyResult

    body = {"outcomes": {"item": {"outcome": "refused"}}}
    with pytest.raises(ValidationError, match="reason"):
        ApplyResult.model_validate(body)
    body["outcomes"]["item"]["error"] = {"code": "refused", "message": "Refused.", "details": {}}
    with pytest.raises(ValidationError, match="reason"):
        ApplyResult.model_validate(body)


def test_published_operation_cannot_offer_save_as_new() -> None:
    from sidegraph.portal.contract import OperationResult

    body = json.loads((EXAMPLES / "edit-and-accept.failed_part.json").read_text(encoding="utf-8"))
    body["offer_save_as_new"] = True
    with pytest.raises(ValidationError, match="offer_save_as_new"):
        OperationResult.model_validate(body)


def test_mock_card_matches_the_requested_queue_id(mock: TestClient) -> None:
    queue = mock.get("/api/queue").json()
    for group in queue.values():
        for item in group:
            record = item["record"]
            record_id = record.get("id", record.get("domain_id"))
            view = mock.get(f"/api/records/{record_id}").json()
            card = view["record"]
            assert card.get("id", card.get("domain_id")) == record_id
            assert view["anchors"] == item["signals"]["anchors"]
    for id_ in ("unknown", "ok", "missing"):
        assert mock.get(f"/api/records/{id_}").status_code == 404


@pytest.mark.parametrize("request_body", [{}, {"op_key": "bad", "fields": {}, "keep_facts": []}])
def test_mock_requires_client_operation_key(mock: TestClient, request_body: dict) -> None:
    response = mock.post("/api/records/example/edit-and-accept", json=request_body)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid"


def test_mock_method_error_uses_the_error_envelope(mock: TestClient) -> None:
    from sidegraph.portal.contract import ErrorBody

    response = mock.post("/api/session")
    assert response.status_code == 405
    assert ErrorBody.model_validate(response.json()).error.code == "invalid"


def test_mock_review_deadline_does_not_restart_on_scenario_selection(mock: TestClient) -> None:
    for _ in range(2):
        assert mock.post("/__mock/scenario", json={"name": "reviewing"}).status_code == 200
        body = mock.get("/api/session").json()
        if _ == 0:
            first = body["reviewing_until"]
    assert body["reviewing_until"] == first


def test_mock_cold_index_reports_unchecked_across_views(mock: TestClient) -> None:
    assert mock.post("/__mock/scenario", json={"name": "cold_index"}).status_code == 200
    rows = mock.get("/api/records").json()["rows"]
    card = mock.get(f"/api/records/{rows[0]['id']}").json()
    queue = mock.get("/api/queue").json()
    assert rows[0]["anchor_statuses"] == ["unchecked"]
    assert card["anchors"][0]["status_word"] == "unchecked"
    assert card["history"][-1]["anchor_statuses"] == ["unchecked"]
    assert queue["decisions"][0]["signals"]["similar"][0]["anchor_statuses"] == ["unchecked"]
    predecessor = mock.get(f"/api/records/{card['history'][0]['id']}").json()
    assert predecessor["history"][0]["anchor_statuses"] == ["unchecked"]


def test_started_operation_cannot_offer_save_as_new() -> None:
    from sidegraph.portal.contract import OperationResult

    body = {
        "op_key": "00000000-0000-4000-8000-000000000001",
        "state": "started",
        "parts": [
            {"part": "drop_draft", "outcome": "done"},
            {"part": "version", "outcome": "failed"},
        ],
        "offer_save_as_new": True,
    }
    with pytest.raises(ValidationError, match="offer_save_as_new"):
        OperationResult.model_validate(body)


def test_mock_edit_results_open_the_expected_record_kind(mock: TestClient) -> None:
    queue = mock.get("/api/queue").json()
    proposed = queue["decisions"][0]["record"]
    kept = queue["decisions"][0]["evidence"][0]["id"]
    other = queue["decisions"][1]["record"]
    standalone = queue["facts"][0]["record"]
    rows = mock.get("/api/records").json()["rows"]
    for record_id, action, options, is_fact in [
        (rows[0]["id"], "edit", {"carry_facts": []}, False),
        (rows[1]["id"], "edit", {"carry_facts": []}, True),
        (proposed["id"], "edit-and-accept", {"keep_facts": [kept]}, False),
        (standalone["id"], "edit-and-accept", {"keep_facts": []}, True),
        (proposed["id"], "save-as-new", {}, False),
        (other["id"], "edit-and-accept", {"keep_facts": []}, False),
        (other["id"], "save-as-new", {}, False),
        (standalone["id"], "save-as-new", {}, True),
    ]:
        request = {"op_key": "00000000-0000-4000-8000-000000000001", "fields": {}, **options}
        operation = mock.post(f"/api/records/{record_id}/{action}", json=request).json()
        card = mock.get(f"/api/records/{operation['new_record_id']}").json()
        record = card["record"]
        assert ("statement" in record) == is_fact
        if is_fact:
            assert record["supports"] or card["anchors"]
        if record_id == other["id"]:
            assert record["title"] == other["title"]
        for part in operation["parts"]:
            if part["part"] == "drop_draft":
                assert part["id"] == record_id
        if action == "edit":
            assert record["supersedes"] == record_id
            assert card["history"][-2]["status"] == "superseded"
        else:
            assert record.get("supersedes") is None
        for part in operation["parts"]:
            if part["part"].startswith("fact:"):
                evidence = mock.get(f"/api/records/{part['id']}").json()["record"]
                assert evidence["supports"] == [record["id"]]
                assert part["id"] in [fact["id"] for fact in card["evidence"]]


def test_mock_snippet_matches_the_binding_and_keeps_full_shifted_span(mock: TestClient) -> None:
    rows = mock.get("/api/records").json()["rows"]
    record_path = f"/api/records/{rows[0]['id']}"
    anchor = mock.get(record_path).json()["anchors"][0]
    unavailable = mock.get(f"{record_path}/anchors/missing/snippet")
    assert unavailable.status_code == 404
    assert unavailable.json()["error"]["details"]["reason"] == "snippet_unavailable"
    snippet_path = f"{record_path}/anchors/{anchor['entity_id']}/snippet"
    assert mock.post("/__mock/scenario", json={"name": "shifted_lines"}).status_code == 200
    shifted = mock.get(snippet_path, params={"full": "true"}).json()
    assert shifted["shifted"]
    assert len(shifted["lines"]) == 200
    assert mock.post("/__mock/scenario", json={"name": "no_repository_root"}).status_code == 200
    refused = mock.get(snippet_path)
    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == "refused"
    assert refused.json()["error"]["details"]["reason"] == "no_repository_root"


def test_mock_router_matches_contract(mock: TestClient) -> None:
    from sidegraph.portal.contract import ROUTES

    methods = {
        (method, route.path.replace("{path:path}", "{path}"))
        for route in mock.app.routes
        if not route.path.startswith("/__mock/")
        for method in route.methods
    }
    assert methods == {(route.method, route.path) for route in ROUTES}


def test_mock_failed_operation_links_reflect_what_was_written(mock: TestClient) -> None:
    queue = mock.get("/api/queue").json()
    decision = queue["decisions"][0]["record"]["id"]
    standalone = queue["facts"][0]["record"]["id"]
    evidence = queue["decisions"][0]["evidence"][0]["id"]
    assert mock.post("/__mock/scenario", json={"name": "failed_part"}).status_code == 200
    body = {"op_key": "00000000-0000-4000-8000-000000000001", "fields": {}}
    decision_partial = mock.post(
        f"/api/records/{decision}/edit-and-accept", json={**body, "keep_facts": [evidence]}
    ).json()
    fact_part = next(part for part in decision_partial["parts"] if part["part"].startswith("fact:"))
    assert fact_part["outcome"] == "failed"
    assert mock.get(f"/api/records/{fact_part['id']}").status_code == 404
    assert mock.get(f"/api/records/{decision_partial['new_record_id']}").json()["evidence"] == []

    fact_partial = mock.post(
        f"/api/records/{standalone}/edit-and-accept", json={**body, "keep_facts": []}
    ).json()
    assert fact_partial["parts"][0]["id"] == standalone
    assert "new_record_id" not in fact_partial

    accepted = mock.get("/api/records").json()["rows"][0]["id"]
    failed_close = mock.post(
        f"/api/records/{accepted}/edit", json={**body, "carry_facts": []}
    ).json()
    history = mock.get(f"/api/records/{failed_close['new_record_id']}").json()["history"]
    assert history[-2]["status"] == "accepted"


def test_mock_snippet_uses_the_same_effective_card_as_navigation(mock: TestClient) -> None:
    rows = mock.get("/api/records").json()["rows"]
    first = rows[0]["id"]
    queue = mock.get("/api/queue").json()
    other = queue["decisions"][0]["record"]["id"]
    for name in ("missing", "ambiguous"):
        assert mock.post("/__mock/scenario", json={"name": name}).status_code == 200
        for id_, expected in [(first, 404), (other, 200)]:
            detail = mock.get(f"/api/records/{id_}").json()
            entity = detail["anchors"][0]["entity_id"]
            assert mock.get(f"/api/records/{id_}/anchors/{entity}/snippet").status_code == expected


def test_planned_operation_offers_recovery_only_in_the_conflict_response() -> None:
    from sidegraph.portal.contract import OperationResult

    with pytest.raises(ValidationError, match="offer_save_as_new"):
        OperationResult.model_validate(
            {
                "op_key": "00000000-0000-4000-8000-000000000001",
                "state": "planned",
                "parts": [],
                "offer_save_as_new": True,
            }
        )


def test_mock_edits_require_the_expected_source_status(mock: TestClient) -> None:
    queue = mock.get("/api/queue").json()
    draft = queue["decisions"][0]["record"]["id"]
    accepted = mock.get("/api/records").json()["rows"][0]["id"]
    body = {"op_key": "00000000-0000-4000-8000-000000000001", "fields": {}}
    assert (
        mock.post("/api/records/unknown/edit", json={**body, "carry_facts": []}).status_code == 404
    )
    assert (
        mock.post(f"/api/records/{draft}/edit", json={**body, "carry_facts": []}).status_code == 409
    )
    wrong = mock.post(f"/api/records/{accepted}/edit-and-accept", json={**body, "keep_facts": []})
    assert wrong.status_code == 409


def test_mock_edit_evidence_choices_match_linked_cards(mock: TestClient) -> None:
    queue = mock.get("/api/queue").json()
    draft = queue["decisions"][0]["record"]["id"]
    keep = queue["decisions"][0]["evidence"][0]["id"]
    accepted = mock.get("/api/records").json()["rows"][0]["id"]
    carry = mock.get(f"/api/records/{accepted}").json()["evidence"][0]["id"]
    body = {"op_key": "00000000-0000-4000-8000-000000000001", "fields": {}}
    for path, field, selected_id in (
        (f"/api/records/{draft}/edit-and-accept", "keep_facts", keep),
        (f"/api/records/{accepted}/edit", "carry_facts", carry),
    ):
        for selected in ([], [selected_id]):
            operation = mock.post(path, json={**body, field: selected}).json()
            fact_parts = [
                part["id"] for part in operation["parts"] if part["part"].startswith("fact:")
            ]
            card = mock.get(f"/api/records/{operation['new_record_id']}").json()
            assert len(fact_parts) == len(selected)
            assert [fact["id"] for fact in card["evidence"]] == fact_parts
