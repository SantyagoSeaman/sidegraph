"""Symbol boundaries from Graphify locations."""

import sys
from pathlib import Path

import pytest

from tests.graph_fixtures import node, reader_of


def test_symbol_span_uses_next_code_start(tmp_path: Path) -> None:
    path = "pkg/work.py"
    reader = reader_of(
        tmp_path,
        node("last", "last()", path, location="L41"),
        node("work", "work()", path, location="L10"),
        node("docstring", "Work safely.", path, file_type="rationale", location="L11"),
        node("same", "same()", path, location="L10"),
        node("other", "other()", "pkg/other.py", location="L12"),
        node("next", "step()", path, location="L25"),
    )

    assert reader.symbol_span("work") == (path, 10, 24)
    assert reader.symbol_span("same") == (path, 10, 24)
    assert reader.symbol_span("next") == (path, 25, 40)
    assert reader.symbol_span("last") == (path, 41, None)


def test_symbol_span_of_heading_uses_next_heading(tmp_path: Path) -> None:
    path = "docs/work.md"
    reader = reader_of(
        tmp_path,
        node("file", "work.md", path, file_type="document"),
        node("later", "Alternatives", path, file_type="document", location="L19"),
        node("heading", "Choice", path, file_type="document", location="L5"),
        node("concept", "Exclusive access", path, file_type="concept", location="L8"),
        node("code", "example()", path, location="L10"),
    )

    assert reader.symbol_span("heading") == (path, 5, 18)
    assert reader.symbol_span("later") == (path, 19, None)


@pytest.mark.parametrize("location", ["", None, "invalid", "L0"])
def test_symbol_span_requires_a_positive_line(tmp_path: Path, location: str | None) -> None:
    reader = reader_of(tmp_path, node("work", "work()", "work.py", location=location))

    assert reader.symbol_span("work") is None


def test_symbol_span_skips_a_location_that_cannot_convert_to_int(tmp_path: Path) -> None:
    limit = sys.get_int_max_str_digits()
    if not limit:
        pytest.skip("the interpreter has no integer conversion limit")
    path = "pkg/work.py"
    reader = reader_of(
        tmp_path,
        node("start", "start()", path, location="L2"),
        node("malformed", "malformed()", path, location="L" + "9" * (limit + 1)),
        node("end", "end()", path, location="L10"),
    )

    assert reader.symbol_span("start") == (path, 2, 9)
    assert reader.symbol_span("malformed") is None
