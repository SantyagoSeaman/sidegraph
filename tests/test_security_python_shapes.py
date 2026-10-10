"""Opaque Python coercions must not bypass bounded capture admission."""

from collections import deque
from enum import Enum

import pytest

from sidegraph import capture, doc_import, input_limits, server
from sidegraph.store import Store


class LegacySequence:
    def __getitem__(self, index):
        pytest.fail("legacy sequence consumed before admission")


class PoisonIterable:
    def __iter__(self):
        pytest.fail("opaque iterable consumed before admission")


class OpaqueTitle(Enum):
    TITLE = "x" * 262145


def opaque(kind):
    values = ["x" * 262145]
    return {
        "set": lambda: set(values),
        "frozenset": lambda: frozenset(values),
        "deque": lambda: deque(values),
        "keys": lambda: dict.fromkeys(values).keys(),
        "values": lambda: dict(enumerate(values)).values(),
        "items": lambda: dict(enumerate(values)).items(),
        "iterator": lambda: iter(values),
        "generator": lambda: (value for value in values),
        "range": lambda: range(10),
        "legacy": LegacySequence,
        "poison": PoisonIterable,
        "enum": lambda: OpaqueTitle.TITLE,
        "object": object,
    }[kind]()


KINDS = [
    "set",
    "frozenset",
    "deque",
    "keys",
    "values",
    "items",
    "iterator",
    "generator",
    "range",
    "legacy",
    "poison",
    "enum",
    "object",
]


def decision(**updates):
    raw = dict(title="bounded", kind="lesson", context="context", choice="choice")
    raw.update(updates)
    return raw


@pytest.mark.parametrize("kind", KINDS)
def test_opaque_nested_values_reject_without_consumption_or_reflection(kind):
    value = opaque(kind)
    with pytest.raises(input_limits.InputLimitError) as error:
        input_limits.measure_item({"ignored": value})
    assert str(error.value) == "Input exceeds resource limits."


@pytest.mark.parametrize("kind", KINDS)
def test_capture_opaque_values_never_enter_item_pipeline(tmp_path, monkeypatch, kind):
    calls = []
    monkeypatch.setattr(capture, "_propose_one", lambda *args, **kwargs: calls.append(args))
    raw = decision(**{"title" if kind == "enum" else "tags": opaque(kind)})
    with Store(tmp_path / "store") as store:
        results = capture.propose([raw], store, None)
        assert list(store.iter_decisions()) == []
    assert calls == []
    assert results[0].status == "rejected"
    assert results[0].reason == "Input exceeds resource limits."


@pytest.mark.parametrize("field", ["tags", "anchors", "facts"])
@pytest.mark.parametrize("model_path", ["copy", "construct"])
def test_stored_model_fields_are_rechecked_before_validation(
    tmp_path, monkeypatch, field, model_path
):
    values = decision(**{field: PoisonIterable()})
    if model_path == "construct":
        raw = capture.DraftDecision.model_construct(**values)
    else:
        raw = capture.DraftDecision(**decision()).model_copy(update={field: PoisonIterable()})
    calls = []
    monkeypatch.setattr(capture, "_propose_one", lambda *args, **kwargs: calls.append(args))
    with Store(tmp_path / "store") as store:
        result = capture.propose([raw], store, None)
        assert list(store.iter_decisions()) == []
        assert list(store.iter_facts()) == []
    assert calls == []
    assert result[0].status == "rejected"


def test_generator_facts_parent_rejection_preserves_valid_sibling_after_reopen(tmp_path):
    path = tmp_path / "store"
    yielded = []

    def facts():
        for index in range(150):
            yielded.append(index)
            yield dict(statement=f"fact {index}", source="source")

    with Store(path) as store:
        results = capture.propose([decision(facts=facts()), decision(title="sibling")], store, None)
        assert [item.title for item in store.iter_decisions()] == ["sibling"]
        assert list(store.iter_facts()) == []
    with Store(path) as reopened:
        assert [item.title for item in reopened.iter_decisions()] == ["sibling"]
        assert list(reopened.iter_facts()) == []
    assert yielded == []
    assert [result.status for result in results] == ["rejected", "written"]


@pytest.mark.parametrize(
    "function", [capture.propose, capture.propose_facts, capture.propose_domains]
)
def test_generator_batch_rejects_before_length_or_consumption(tmp_path, function):
    with Store(tmp_path / "store") as store:
        with pytest.raises(input_limits.InputLimitError):
            function(PoisonIterable(), store, None)
        assert list(store.iter_decisions()) == []
        assert list(store.iter_facts()) == []
        assert list(store.iter_domains()) == []


@pytest.mark.parametrize("target", ["drafts", "metadata"])
def test_pure_batch_shape_rejects_before_length_or_consumption(target):
    with pytest.raises(input_limits.InputLimitError):
        if target == "drafts":
            input_limits.preflight_drafts(PoisonIterable())
        else:
            input_limits.preflight_drafts([], metadata=PoisonIterable())


@pytest.mark.parametrize("target", ["drafts", "facts"])
def test_combined_batch_shape_rejects_before_length_or_flattening(tmp_path, target):
    kwargs = {target: PoisonIterable()}
    kwargs.setdefault("drafts", [])
    with Store(tmp_path / "store") as store:
        with pytest.raises(input_limits.InputLimitError):
            server._propose_decisions_impl(store, None, **kwargs)
        assert list(store.iter_decisions()) == []


@pytest.mark.parametrize("nodes", [11, 12])
def test_joint_node_budget_matches_core_before_any_call(tmp_path, monkeypatch, nodes):
    monkeypatch.setattr(input_limits, "MAX_REQUEST_NODES", nodes)
    calls = []
    original = capture.preflight_drafts

    def tracked(*args, **kwargs):
        calls.append(kwargs.get("metadata"))
        return original(*args, **kwargs)

    monkeypatch.setattr(capture, "preflight_drafts", tracked)
    monkeypatch.setattr(server, "preflight_drafts", tracked)
    with Store(tmp_path / "store") as store:
        if nodes == 11:
            with pytest.raises(input_limits.InputLimitError):
                server._propose_decisions_impl(store, None, [decision()])
            assert list(store.iter_decisions()) == []
            assert len(calls) == 1
        else:
            result = server._propose_decisions_impl(store, None, [decision()])
            assert len(list(store.iter_decisions())) == 1
            assert result[0]["status"] == "written"


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


@pytest.mark.parametrize("function,kwargs", DIRECT, ids=[item[0].__name__ for item in DIRECT])
def test_direct_opaque_metadata_rejects_before_redact_or_lookup(
    tmp_path, monkeypatch, function, kwargs
):
    calls = []
    monkeypatch.setattr(server, "redact", lambda value: (calls.append(value) or value, 0))
    with Store(tmp_path / "store") as store:
        with pytest.raises(input_limits.InputLimitError):
            function(store, None, **kwargs, author=PoisonIterable())
        assert list(store.iter_decisions()) == []
        assert list(store.iter_facts()) == []
        assert list(store.iter_domains()) == []
    assert calls == []


@pytest.mark.parametrize("dry_run", [False, True])
def test_import_opaque_tags_reject_before_normalization(tmp_path, monkeypatch, dry_run):
    calls = []
    monkeypatch.setattr(doc_import, "normalize_tags", lambda value: (calls.append(value) or [], 0))
    with Store(tmp_path / "store") as store:
        with pytest.raises(input_limits.InputLimitError):
            doc_import.import_docs(store, None, [], tags=deque(["x" * 262145]), dry_run=dry_run)
        assert list(store.iter_decisions()) == []
    assert calls == []


@pytest.mark.parametrize("batch", [list, tuple])
def test_supported_sequence_and_strenum_control(tmp_path, batch):
    raw = decision(kind=capture.DecisionKind.LESSON, tags=("safe",))
    raw["facts"] = (dict(statement="evidence", source="source"),)
    with Store(tmp_path / "store") as store:
        results = capture.propose(batch([raw]), store, None)
        assert len(list(store.iter_decisions())) == 1
        assert len(list(store.iter_facts())) == 1
    assert results[0].status == "written"
