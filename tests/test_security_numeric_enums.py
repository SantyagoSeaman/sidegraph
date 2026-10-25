"""Ordinary scalars and string enums keep their measured behaviour in capture admission."""

from enum import Enum

from sidegraph import capture, input_limits
from sidegraph.store import Store


class TextValue(str, Enum):  # noqa: UP042 - exercise the legacy string Enum mixin
    VALUE = "ordinary"


def decision(**updates):
    raw = dict(title="bounded", kind="lesson", context="context", choice="choice")
    raw.update(updates)
    return raw


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
