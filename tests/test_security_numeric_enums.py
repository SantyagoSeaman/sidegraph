"""Enum coercion must not expand zero-byte scalars into unbudgeted strings."""

from enum import Enum, IntEnum, IntFlag

import pytest

from sidegraph import capture, doc_import, input_limits, server
from sidegraph.store import Store


class IntegerValue(IntEnum):
    VALUE = 12


class FlagValue(IntFlag):
    VALUE = 12


class FloatingValue(float, Enum):
    VALUE = 1.25


class BinaryValue(bytes, Enum):
    VALUE = b"ordinary"


class TextValue(str, Enum):  # noqa: UP042 - exercise the legacy string Enum mixin
    VALUE = "ordinary"


class LargeValue(IntEnum):
    VALUE = 10**4299 + 7


class UnconvertibleValue(IntEnum):
    VALUE = 10**10000

    def __str__(self):
        pytest.fail("numeric enum conversion before rejection")


ENUMS = [IntegerValue, FlagValue, FloatingValue, BinaryValue, UnconvertibleValue]


def decision(**updates):
    raw = dict(title="bounded", kind="lesson", context="context", choice="choice")
    raw.update(updates)
    return raw


@pytest.mark.parametrize("enum_type", ENUMS, ids=[item.__name__ for item in ENUMS])
@pytest.mark.parametrize("position", ["value", "key"])
def test_non_string_enums_reject_without_conversion(enum_type, position):
    value = enum_type.VALUE
    raw = {value: "ordinary"} if position == "key" else {"field": value}
    with pytest.raises(input_limits.InputLimitError) as error:
        input_limits.measure_item(raw)
    assert str(error.value) == "Input exceeds resource limits."


@pytest.mark.parametrize("field", ["title", "tags"])
def test_numeric_enum_expansion_never_enters_redaction_and_sibling_survives(
    tmp_path, monkeypatch, field
):
    raw = decision(**{field: LargeValue.VALUE if field == "title" else [LargeValue.VALUE] * 1000})
    # A thousand decimal strings would exceed the shared budget despite the node count.
    if field == "tags":
        assert input_limits.MAX_REQUEST_BYTES < 4300 * 1000
    original = capture.redact

    def guarded_redact(text):
        if len(text) >= 4300:
            pytest.fail("unbudgeted enum string reached redaction")
        return original(text)

    monkeypatch.setattr(capture, "redact", guarded_redact)
    path = tmp_path / "store"
    with Store(path) as store:
        results = capture.propose([raw, decision(title="sibling")], store, None)
        assert [item.title for item in store.iter_decisions()] == ["sibling"]
        assert list(store.iter_facts()) == []
    with Store(path) as reopened:
        assert [item.title for item in reopened.iter_decisions()] == ["sibling"]
        assert list(reopened.iter_facts()) == []
    assert [item.status for item in results] == ["rejected", "written"]
    assert results[0].reason == "Input exceeds resource limits."


@pytest.mark.parametrize("model_path", ["copy", "construct"])
@pytest.mark.parametrize("field", ["title", "facts"])
def test_stored_model_numeric_enums_are_rechecked(tmp_path, monkeypatch, model_path, field):
    value = LargeValue.VALUE if field == "title" else [dict(statement=LargeValue.VALUE, source="s")]
    raw = decision(**{field: value})
    if model_path == "construct":
        model = capture.DraftDecision.model_construct(**raw)
    else:
        model = capture.DraftDecision(**decision()).model_copy(update={field: value})
    calls = []
    monkeypatch.setattr(capture, "_propose_one", lambda *args, **kwargs: calls.append(args))
    with Store(tmp_path / "store") as store:
        result = capture.propose([model], store, None)
        assert list(store.iter_decisions()) == []
        assert list(store.iter_facts()) == []
    assert calls == []
    assert result[0].status == "rejected"


@pytest.mark.parametrize("enum_type", [IntegerValue, FlagValue, FloatingValue])
def test_numeric_enum_metadata_rejects_request_before_core(tmp_path, monkeypatch, enum_type):
    calls = []
    monkeypatch.setattr(capture, "_propose_one", lambda *args, **kwargs: calls.append(args))
    with Store(tmp_path / "store") as store:
        with pytest.raises(input_limits.InputLimitError):
            capture.propose([decision()], store, None, author=enum_type.VALUE)
        assert list(store.iter_decisions()) == []
    assert calls == []


@pytest.mark.parametrize("enum_type", [IntegerValue, FlagValue, FloatingValue])
def test_direct_numeric_enum_metadata_rejects_before_redaction(tmp_path, monkeypatch, enum_type):
    calls = []
    monkeypatch.setattr(server, "redact", lambda value: (calls.append(value) or value, 0))
    with Store(tmp_path / "store") as store:
        with pytest.raises(input_limits.InputLimitError):
            server._add_decision_impl(store, None, **decision(), author=enum_type.VALUE)
        assert list(store.iter_decisions()) == []
    assert calls == []


@pytest.mark.parametrize("dry_run", [False, True])
def test_import_numeric_enum_tags_reject_before_normalization(tmp_path, monkeypatch, dry_run):
    calls = []
    monkeypatch.setattr(doc_import, "normalize_tags", lambda value: (calls.append(value) or [], 0))
    with Store(tmp_path / "store") as store:
        with pytest.raises(input_limits.InputLimitError):
            doc_import.import_docs(store, None, [], tags=[LargeValue.VALUE], dry_run=dry_run)
        assert list(store.iter_decisions()) == []
    assert calls == []


def test_ordinary_scalars_and_string_enums_keep_their_measured_behavior(tmp_path):
    measured = input_limits.measure_item([None, True, 12, 1.25, TextValue.VALUE])
    assert measured.string_bytes == 8
    assert measured.nodes == 6
    assert input_limits.measure_item(b"ordinary").string_bytes == 8
    assert input_limits.measure_item(bytearray(b"ordinary")).string_bytes == 8
    with Store(tmp_path / "store") as store:
        result = capture.propose(
            [decision(title=TextValue.VALUE, kind=capture.DecisionKind.LESSON, tags=("safe",))],
            store,
            None,
        )
        assert [item.title for item in store.iter_decisions()] == ["ordinary"]
    assert result[0].status == "written"
