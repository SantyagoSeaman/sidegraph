"""Real MCP dispatch must reject malformed writes before framework exception logging."""

import asyncio
import json
import logging

import fastmcp
import pytest

from sidegraph import server
from sidegraph.engine.reader import GraphifyReader
from sidegraph.schema import Descriptor, Entity
from sidegraph.store import Store

MARKER = "opaque" + "S3Probe"


def decision_args(**updates):
    args = dict(title="ordinary", kind="adr", context="context", choice="choice")
    args.update(updates)
    return args


def call(name, arguments):
    async def run():
        async with fastmcp.Client(server.mcp) as client:
            return await client.call_tool(name, arguments, raise_on_error=False)

    return asyncio.run(run())


@pytest.fixture
def store(tmp_path, monkeypatch):
    with Store(tmp_path / "store") as current:
        monkeypatch.setattr(server, "_store", current)
        monkeypatch.setattr(server, "_load_reader", lambda: None)
        yield current


def canonical(store):
    return {str(p.relative_to(store.path)): p.read_bytes() for p in store.path.rglob("*.json")}


def rendered(result):
    return json.dumps([str(result.content), result.structured_content], ensure_ascii=False)


CASES = [
    ("propose_decisions", dict(drafts=MARKER), "drafts"),
    ("propose_decisions", dict(drafts=[MARKER]), "drafts: dict_type"),
    ("propose_decisions", dict(drafts=[], facts=MARKER), "facts"),
    ("propose_decisions", dict(drafts=[], author={MARKER: MARKER}), "author"),
    ("propose_domains", dict(drafts=MARKER), "drafts"),
    ("add_decision", decision_args(title={MARKER: MARKER}), "title"),
    ("add_decision", decision_args(anchors=[dict(name=MARKER, relation=MARKER)]), "relation"),
    ("add_decision", decision_args(**{MARKER: True}), "parameters"),
    ("add_domain", dict(slug=MARKER.upper(), title="ordinary", summary="ordinary"), "slug"),
]


@pytest.mark.parametrize("name,arguments,field", CASES)
def test_write_response_and_warning_error_logs_do_not_reflect_input(
    store, caplog, name, arguments, field
):
    before = canonical(store)
    with caplog.at_level(logging.WARNING):
        result = call(name, arguments)
    assert canonical(store) == before
    with Store(store.path) as reopened:
        assert list(reopened.iter_decisions()) == []
        assert list(reopened.iter_domains()) == []
    payload = rendered(result)
    logs = "\n".join(
        logging.Formatter("%(levelname)s %(message)s").format(record) for record in caplog.records
    )
    assert MARKER.casefold() not in payload.casefold()
    assert MARKER.casefold() not in logs.casefold()
    assert result.is_error
    assert field in payload


def test_body_failure_does_not_reach_framework_traceback(store, monkeypatch, caplog):
    def fail(*args, **kwargs):
        raise RuntimeError(MARKER)

    monkeypatch.setattr(server, "_add_decision_impl", fail)
    before = canonical(store)
    with caplog.at_level(logging.WARNING):
        result = call("add_decision", decision_args())
    assert canonical(store) == before
    assert MARKER not in rendered(result)
    assert MARKER not in "\n".join(
        logging.Formatter("%(levelname)s %(message)s").format(record) for record in caplog.records
    )
    assert all(not record.exc_info or record.exc_info[0] is None for record in caplog.records)
    assert result.is_error
    assert "may" in rendered(result) and "reopen" in rendered(result)


def test_prevalidation_does_not_invoke_write_body_and_preserves_default_execution(
    store, monkeypatch
):
    calls = []
    original = server._add_decision_impl

    def spy(*args, **kwargs):
        calls.append(kwargs)
        return original(*args, **kwargs)

    monkeypatch.setattr(server, "_add_decision_impl", spy)
    rejected = call("add_decision", dict(kind="adr", context="c", choice="c"))
    assert calls == []
    assert rejected.is_error
    accepted = call("add_decision", decision_args())
    assert len(calls) == 1
    assert not accepted.is_error
    with Store(store.path) as reopened:
        assert len(list(reopened.iter_decisions())) == 1
    assert calls[0]["author"] is None


@pytest.mark.parametrize("name", ["ratify", "ratify_decisions"])
def test_returned_ratification_failure_does_not_echo_exception(store, monkeypatch, name):
    existing = server._add_decision_impl(store, None, **decision_args())
    before = canonical(store)
    original_status = store.get_decision(existing["id"]).status

    def fail(*args, **kwargs):
        raise ValueError(MARKER)

    monkeypatch.setattr(store, "ratify", fail)
    result = call(name, dict(accept=[existing["id"]]))
    assert canonical(store) == before
    with Store(store.path) as reopened:
        assert reopened.get_decision(existing["id"]).status == original_status
    assert MARKER not in rendered(result)
    assert "error" in rendered(result)


def test_returned_domain_ratification_failure_does_not_echo_outcome(store, monkeypatch):
    existing = server._add_domain_impl(
        store, None, slug="ordinary", title="ordinary", summary="ordinary"
    )
    identifier = existing["domain_id"]
    before = canonical(store)
    monkeypatch.setattr(store, "ratify_domains", lambda **kwargs: {identifier: "error: " + MARKER})
    result = call("ratify", dict(accept=[identifier]))
    assert canonical(store) == before
    with Store(store.path) as reopened:
        assert reopened.get_domain(identifier).status.value == "proposed"
    assert MARKER not in rendered(result)
    assert "error" in rendered(result)


def test_unknown_record_anchor_rejection_does_not_echo_value(store):
    before = canonical(store)
    result = call("add_anchors", dict(record_id=MARKER, anchors=[dict(name="sample.py")]))
    assert canonical(store) == before
    assert MARKER not in rendered(result)
    assert "unknown record" in rendered(result)


def test_safe_fixed_reachability_help_survives_write_wrapper(store):
    before = canonical(store)
    result = call("add_fact", dict(statement="ordinary", source="probe"))
    assert canonical(store) == before
    assert result.is_error
    assert "anchorless fact has no live supporting decision" in rendered(result)
    assert "add an anchor" in rendered(result)


def test_mcp_supersede_fact_preserves_reopened_history(store):
    supporting = call("add_decision", decision_args()).data
    original = call(
        "add_fact",
        dict(statement="old observation", source="first probe", supports=[supporting["id"]]),
    ).data
    result = call(
        "supersede_fact",
        dict(old_fact_id=original["id"], statement="new observation", source="second probe"),
    )
    assert not result.is_error
    successor_id = result.data["id"]
    assert result.data["supersedes"] == original["id"]
    with Store(store.path) as reopened:
        predecessor = reopened.get_fact(original["id"])
        successor = reopened.get_fact(successor_id)
        assert predecessor.status.value == "superseded"
        assert predecessor.valid_to is not None
        assert predecessor.statement == "old observation"
        assert successor.supersedes == predecessor.id
        assert successor.statement == "new observation"
        assert successor.supports == [supporting["id"]]
        assert reopened.get_decision(supporting["id"]).status.value == "accepted"


def test_mcp_sync_anchors_reports_without_changing_stable_canonical_files(
    store, tmp_path, monkeypatch
):
    graph = tmp_path / "graph.json"
    graph.write_text(
        json.dumps(
            {
                "built_at_commit": "vA",
                "nodes": [
                    {
                        "id": "s1",
                        "label": "f_stable()",
                        "norm_label": "f_stable()",
                        "file_type": "code",
                        "source_file": "a.py",
                        "community": 1,
                    }
                ],
                "links": [],
            }
        )
        + "\n"
    )
    reader = GraphifyReader(graph)
    entity = store.upsert_entity(
        Entity(
            canonical_name="f_stable",
            descriptor=Descriptor(name="f_stable", file_path="a.py"),
            last_seen_node_id="s1",
            last_seen_graph_version="v0",
        )
    )
    monkeypatch.setattr(server, "_load_reader", lambda: reader)
    before = canonical(store)
    graph_before = graph.read_bytes()
    result = call("sync_anchors", dict(force=True))
    assert canonical(store) == before
    assert graph.read_bytes() == graph_before
    with Store(store.path) as reopened:
        assert reopened.get_entity(entity.entity_id).descriptor.file_path == "a.py"
    assert not result.is_error
    assert result.data["synced"] is True
    assert result.data["to_version"] == reader.graph_version()
    assert "unchanged" in result.data["counts"]
