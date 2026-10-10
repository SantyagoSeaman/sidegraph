"""Binary values that Pydantic decodes into strings spend the same budgets."""

import pytest

from sidegraph import capture, input_limits
from sidegraph.store import Store


@pytest.mark.parametrize("binary", [bytes, bytearray])
@pytest.mark.parametrize("size", [262143, 262144, 262145])
def test_binary_string_field_same_byte_limit(binary, size):
    value = binary(b"x" * size)
    if size > input_limits.MAX_FIELD_BYTES:
        with pytest.raises(input_limits.InputLimitError):
            input_limits.measure_item(value)
    else:
        assert input_limits.measure_item(value).string_bytes == size


@pytest.mark.parametrize("binary", [bytes, bytearray])
def test_invalid_utf8_binary_is_static_rejection(binary):
    with pytest.raises(input_limits.InputLimitError) as error:
        input_limits.measure_item(binary(b"privateMarker\xff"))
    assert "privateMarker" not in str(error.value)


@pytest.mark.parametrize("binary", [bytes, bytearray])
def test_binary_values_count_toward_aggregate(binary, monkeypatch):
    monkeypatch.setattr(input_limits, "MAX_REQUEST_BYTES", 30)
    with pytest.raises(input_limits.InputLimitError):
        input_limits.preflight_drafts([binary(b"x" * 20), binary(b"y" * 20)])
    with pytest.raises(input_limits.InputLimitError):
        input_limits.preflight_direct(binary(b"x" * 20), binary(b"y" * 20))
    with pytest.raises(input_limits.InputLimitError):
        input_limits.preflight_drafts([binary(b"x" * 20)], metadata=[binary(b"y" * 20)])


@pytest.mark.parametrize("binary", [bytes, bytearray])
def test_binary_capture_parent_rejects_before_validation(tmp_path, monkeypatch, binary):
    calls = []
    monkeypatch.setattr(capture, "_propose_one", lambda *args, **kwargs: calls.append(args))
    raw = dict(title=binary(b"x" * 262145), kind="lesson", context="c", choice="c")
    with Store(tmp_path / "store") as store:
        result = capture.propose([raw], store, None)
        assert list(store.iter_decisions()) == []
        assert result[0].status == "rejected"
    assert calls == []


@pytest.mark.parametrize("binary", [bytes, bytearray])
def test_nested_binary_attached_fact_bounds_parent(binary):
    parent = dict(facts=[dict(statement=binary(b"x" * 262145))])
    assert input_limits.preflight_drafts([parent], decision_indices=[0])[0] is not None
