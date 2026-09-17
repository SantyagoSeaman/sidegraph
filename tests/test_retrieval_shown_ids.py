"""TaskContext collects the ids it showed (spec D3).

Collected where the Decision/Fact objects are in hand — never by parsing the rendered
text, which holds formatted strings and would make id extraction a regex guess."""

from __future__ import annotations

from sidegraph.retrieval import TaskContext


def test_shown_ids_defaults_empty():
    assert TaskContext().shown_ids == []


def test_render_is_unaffected_by_the_new_field():
    """Additive by design: every existing consumer of render() must be untouched."""
    ctx = TaskContext(mistakes=["- [gotcha] x"])
    ctx.shown_ids.append("r1")
    assert "r1" not in ctx.render()
