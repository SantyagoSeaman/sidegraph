"""Capture admission completes before validation, redaction and store writes."""

import pytest

from sidegraph import capture, input_limits, server
from sidegraph.store import Store


def decision(**updates):
    item = dict(title="A bounded decision", kind="lesson", context="context", choice="choice")
    item.update(updates)
    return item


def test_oversized_nested_fact_rejects_whole_parent_and_preserves_sibling(tmp_path):
    with Store(tmp_path / "store") as store:
        items = [
            decision(facts=[dict(statement="x" * (256 * 1024 + 1), source="source")]),
            decision(title="Valid sibling"),
        ]
        results = capture.propose(items, store, None)
        stored = list(store.iter_decisions())
        assert [item.title for item in stored] == ["Valid sibling"]
        assert list(store.iter_facts()) == []
        assert [result.status for result in results] == ["rejected", "written"]
        assert "resource limits" in results[0].reason


def test_raw_rejection_never_enters_validation_or_redaction(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(capture, "_propose_one", lambda *args, **kwargs: calls.append(args))
    with Store(tmp_path / "store") as store:
        results = capture.propose([decision(context="x" * (256 * 1024 + 1))], store, None)
    assert calls == []
    assert results[0].status == "rejected"


def test_request_byte_overflow_prevents_all_writes(tmp_path, monkeypatch):
    monkeypatch.setattr(input_limits, "MAX_REQUEST_BYTES", 1500, raising=False)
    with Store(tmp_path / "store") as store:
        with pytest.raises(ValueError, match="resource limits"):
            capture.propose([decision(context="x" * 900), decision(context="y" * 900)], store, None)
        assert list(store.iter_decisions()) == []


def test_combined_decisions_facts_use_one_budget_before_write(tmp_path, monkeypatch):
    monkeypatch.setattr(input_limits, "MAX_REQUEST_BYTES", 1500, raising=False)
    with Store(tmp_path / "store") as store:
        with pytest.raises(ValueError, match="resource limits"):
            server._propose_decisions_impl(
                store,
                None,
                [decision(context="x" * 900)],
                facts=[dict(statement="y" * 900, source="source")],
            )
        assert list(store.iter_decisions()) == []
        assert list(store.iter_facts()) == []


DIRECT = [
    (server._add_decision_impl, dict(title="t", kind="adr", context="c", choice="c")),
    (
        server._supersede_decision_impl,
        dict(old_decision_id="old", title="t", kind="adr", context="c", choice="c"),
    ),
    (server._add_fact_impl, dict(statement="s", source="s")),
    (server._supersede_fact_impl, dict(old_fact_id="old", statement="s", source="s")),
    (server._add_domain_impl, dict(slug="s", title="t", summary="s")),
    (
        server._supersede_domain_impl,
        dict(old_slug_or_id="old", new_slug="s", new_title="t", new_summary="s"),
    ),
]


@pytest.mark.parametrize(
    "function,kwargs",
    DIRECT,
    ids=[
        "add-decision",
        "supersede-decision",
        "add-fact",
        "supersede-fact",
        "add-domain",
        "supersede-domain",
    ],
)
def test_direct_metadata_cap_before_lookup_redact_or_mutation(
    tmp_path, monkeypatch, function, kwargs
):
    calls = []
    monkeypatch.setattr(server, "redact", lambda value: (calls.append(value) or value, 0))
    with Store(tmp_path / "store") as store:
        with pytest.raises(ValueError, match="resource limits"):
            function(store, None, **kwargs, author="x" * (256 * 1024 + 1))
        assert list(store.iter_decisions()) == []
        assert list(store.iter_facts()) == []
        assert list(store.iter_domains()) == []
    assert calls == []


def test_combined_request_width_rejected_before_flattening(tmp_path):
    class WideList(list):
        def __len__(self):
            return 101

        def __iter__(self):
            raise AssertionError("oversized collection was iterated")

    with Store(tmp_path / "store") as store:
        with pytest.raises(ValueError, match="resource limits"):
            server._propose_decisions_impl(store, None, WideList())
        assert list(store.iter_decisions()) == []


def test_metadata_shares_batch_aggregate_before_write(tmp_path, monkeypatch):
    monkeypatch.setattr(input_limits, "MAX_REQUEST_BYTES", 1500)
    with Store(tmp_path / "store") as store:
        with pytest.raises(ValueError, match="resource limits"):
            capture.propose([decision(context="x" * 900)], store, None, author="y" * 800)
        assert list(store.iter_decisions()) == []


def test_copied_validated_model_is_rechecked(tmp_path):
    copied = capture.DraftDecision(**decision()).model_copy(
        update={"context": "x" * (256 * 1024 + 1)}
    )
    with Store(tmp_path / "store") as store:
        result = capture.propose([copied], store, None)
        assert list(store.iter_decisions()) == []
        assert result[0].status == "rejected"
        assert "resource limits" in result[0].reason
