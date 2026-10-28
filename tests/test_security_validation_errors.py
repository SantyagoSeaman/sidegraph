"""Useful schema diagnostics must contain only bounded static vocabulary."""

import importlib

import pytest
from pydantic_core import PydanticCustomError, ValidationError

MARKER = "opaque" + "S3Probe"


def formatter():
    return importlib.import_module("sidegraph.validation_errors").format_validation_error


def error(loc=(), code="value_error", count=1):
    return ValidationError.from_exception_data(
        MARKER,
        [
            dict(
                type=PydanticCustomError(code, MARKER, {"secret": MARKER}),
                loc=loc,
                input=MARKER,
            )
            for _ in range(count)
        ],
    )


def test_custom_error_input_message_context_type_location_and_model_are_not_rendered():
    text = formatter()(error((MARKER, "facts", 2, "source"), MARKER))
    assert MARKER not in text
    assert "invalid_value" in text
    assert "facts" in text and "source" in text and "2" in text
    assert "https:" not in text
    assert text.startswith("invalid draft:")


@pytest.mark.parametrize(
    "code",
    [
        "missing",
        "string_type",
        "list_type",
        "dict_type",
        "enum",
        "string_pattern_mismatch",
        "string_too_short",
        "string_too_long",
        "too_short",
        "too_long",
    ],
)
def test_known_fields_and_codes_provide_help(code):
    text = formatter()(error(("anchors", 3, "relation"), code))
    assert "anchors" in text and "relation" in text and "3" in text
    assert code in text
    assert MARKER not in text


@pytest.mark.parametrize("index", [-1, 65536, 10**30])
def test_out_of_range_indices_are_fixed_placeholders(index):
    text = formatter()(error(("facts", index, "source")))
    assert str(index) not in text
    assert "source" in text
    assert MARKER not in text


def test_root_and_empty_error_sets_are_safe():
    format_error = formatter()
    assert MARKER not in format_error(error())
    empty = ValidationError.from_exception_data(MARKER, [])
    assert format_error(empty).startswith("invalid draft:")


def test_displayed_error_count_and_path_depth_are_bounded():
    text = formatter()(error(("source",) * 30, "string_type", count=50))
    assert text.count("string_type") <= 10
    assert text.count("source") <= 80
    assert "truncated" in text
    assert len(text) <= 1024
    assert MARKER not in text
    # A long schema path hits the character cap deterministically.
    failure = error(("old_decision_id",) * 8, "missing_argument", count=50)
    long_path = formatter()(failure)
    assert len(long_path) <= 1024
    assert long_path == formatter()(failure)
    assert "truncated" in long_path


def test_invalid_relation_names_the_static_schema_choices_without_the_input():
    from typing import get_args

    from sidegraph import server, validation_errors
    from sidegraph.schema import Relation

    with pytest.raises(ValueError) as caught:
        server._validate_anchors([{"name": "ordinary", "relation": MARKER}])
    message = validation_errors.safe_write_error(caught.value)
    assert all(value in message for value in get_args(Relation))
    assert "relation" in message and "enum" in message
    assert MARKER not in message
