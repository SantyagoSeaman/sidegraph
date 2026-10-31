"""Pure resource-policy regressions: bounded work before capture or serialization."""

import importlib

import pytest
from pydantic import BaseModel, ConfigDict


@pytest.fixture
def limits():
    return importlib.import_module("sidegraph.input_limits")


def test_shipped_limits_match_the_documented_numbers(limits):
    """The boundary tests below shrink the ceilings; this pins the shipped values to the
    limits table in docs/reference/mcp-tools.md."""
    assert limits.MAX_FIELD_BYTES == 256 * 1024
    assert limits.MAX_REQUEST_BYTES == 4 * 1024 * 1024
    assert limits.MAX_DRAFTS == 100
    assert limits.MAX_DEPTH == 32
    assert limits.MAX_ITEM_NODES == 4096
    assert limits.MAX_REQUEST_NODES == 65_536
    assert limits.MAX_IMPORT_BYTES == 8 * 1024 * 1024


@pytest.fixture
def tiny(limits, monkeypatch):
    """The same policy with small ceilings: a 4-item request is exactly full, 8 bytes or
    8 nodes make an item exactly full, so the boundaries need no megabyte inputs."""
    monkeypatch.setattr(limits, "MAX_FIELD_BYTES", 8)
    monkeypatch.setattr(limits, "MAX_REQUEST_BYTES", 32)
    monkeypatch.setattr(limits, "MAX_ITEM_NODES", 8)
    monkeypatch.setattr(limits, "MAX_REQUEST_NODES", 32)
    return limits


@pytest.mark.parametrize(
    "text", ["a" * 262_144, "é" * 131_072, "😀" * 65_536], ids=["ascii", "two", "four"]
)
def test_field_admits_exact_utf8_boundary(limits, text):
    assert limits.measure_item(text).string_bytes == 262_144


@pytest.mark.parametrize(
    "text", ["a" * 262_145, "é" * 131_073, "😀" * 65_537], ids=["ascii", "two", "four"]
)
def test_field_rejects_one_utf8_unit_over_boundary(limits, text):
    with pytest.raises(limits.InputLimitError):
        limits.measure_item(text)


def test_long_ascii_rejected_before_encoding(limits):
    class Unencodable(str):
        def encode(self, *args, **kwargs):
            pytest.fail("oversized input was encoded")

    with pytest.raises(limits.InputLimitError):
        limits.measure_item(Unencodable("x" * 1_048_576))


@pytest.mark.parametrize("value", ["private_secret\ud800", {"private_secret\udfff": None}])
def test_invalid_utf8_has_static_secret_free_error(limits, value):
    with pytest.raises(limits.InputLimitError) as caught:
        limits.measure_item(value)
    assert str(caught.value) == "Input exceeds resource limits."
    assert "private_secret" not in repr(caught.value)
    assert caught.value.__cause__ is None


def test_ignored_mapping_keys_are_bounded(limits):
    with pytest.raises(limits.InputLimitError):
        limits.measure_item({"a" * 262_145: None})


def test_mapping_keys_and_repeated_values_count_each_occurrence(limits):
    result = limits.measure_item({"k": ["é", "é"]})
    assert (result.string_bytes, result.nodes) == (5, 5)


def test_model_fields_and_copied_unknown_fields_are_inspected_without_dump(limits):
    class Draft(BaseModel):
        model_config = ConfigDict(extra="allow")
        title: str

        def model_dump(self, *args, **kwargs):
            pytest.fail("preflight must not serialize")

    good = Draft(title="ok", extra="é")
    assert limits.measure_item(good).string_bytes == 14
    bad = good.model_copy(update={"unknown": "x" * 262_145})
    assert limits.preflight_drafts([bad, good])[0] is not None
    assert limits.preflight_drafts([bad, good])[1] is None


@pytest.mark.parametrize("container", [list, tuple])
def test_exact_per_item_node_boundary(limits, container):
    assert limits.measure_item(container([None] * 4095)).nodes == 4096
    with pytest.raises(limits.InputLimitError):
        limits.measure_item(container([None] * 4096))


def test_wide_mapping_rejected(limits):
    assert limits.measure_item(dict.fromkeys(range(2047))).nodes == 4095
    with pytest.raises(limits.InputLimitError):
        limits.measure_item(dict.fromkeys(range(2048)))


def test_depth_boundary(limits):
    value = None
    for _ in range(32):
        value = [value]
    assert limits.measure_item(value).nodes == 33
    with pytest.raises(limits.InputLimitError):
        limits.measure_item([value])


def test_shared_subtrees_count_per_occurrence_and_are_valid(limits):
    shared = ["ok"]
    assert limits.measure_item([shared, shared]).nodes == 5


def test_invalid_parent_attached_fact_is_one_item_rejection(limits):
    bad = {"title": "ok", "facts": [{"source": "x" * 262_145}]}
    errors = limits.preflight_drafts([bad, {"title": "good"}], decision_indices=[0, 1])
    assert isinstance(errors[0], limits.InputLimitError)
    assert errors[1] is None


def test_rejected_items_do_not_spend_admitted_byte_budget(tiny):
    bad = ["x" * 8] * 4 + ["x" * 9]
    good = ["x" * 8] * 4
    errors = tiny.preflight_drafts([bad] * 20 + [good])
    assert all(error is not None for error in errors[:-1])
    assert errors[-1] is None


def test_rejected_items_do_not_spend_admitted_node_budget(tiny):
    bad = [None] * 6 + ["x" * 9]
    assert tiny.preflight_drafts([bad] * 9 + ["ok"])[-1] is None


def test_request_byte_boundary_and_combined_batches(limits):
    good = ["x" * 262_144] * 16
    assert limits.preflight_drafts(good) == (None,) * 16
    with pytest.raises(limits.InputLimitError):
        limits.preflight_drafts([*good, "x"])


def test_request_node_boundary(tiny):
    good = [[None] * 7] * 4
    assert tiny.preflight_drafts(good) == (None,) * 4
    with pytest.raises(tiny.InputLimitError):
        tiny.preflight_drafts([*good, None])


def test_draft_count_includes_attached_facts_even_for_rejected_parents(limits):
    parent = {"title": "x" * 262_145, "facts": [None] * 99}
    assert limits.preflight_drafts([parent], decision_indices=[0])[0] is not None
    with pytest.raises(limits.InputLimitError):
        limits.preflight_drafts([parent, None], decision_indices=[0])


def test_only_designated_decisions_count_attached_facts(limits):
    standalone = {"facts": [None] * 100}
    assert limits.preflight_drafts([standalone]) == (None,)
    with pytest.raises(limits.InputLimitError):
        limits.preflight_drafts([standalone], decision_indices=[0])


def test_model_attached_facts_count_without_dump(limits):
    class Draft(BaseModel):
        facts: list[object]

        def model_dump(self, *args, **kwargs):
            pytest.fail("draft count serialized model")

    with pytest.raises(limits.InputLimitError):
        limits.preflight_drafts([Draft(facts=[None] * 100)], decision_indices=[0])


def test_wide_draft_batch_rejects(limits):
    with pytest.raises(limits.InputLimitError):
        limits.preflight_drafts([None] * 101)


def test_malformed_attached_facts_remains_structural_item_error(limits):
    errors = limits.preflight_drafts(
        [{"facts": "x" * 262_145}, {"title": "ok"}], decision_indices=[0, 1]
    )
    assert errors[0] is not None
    assert errors[1] is None


def test_direct_arguments_share_aggregate_and_individual_limits(tiny):
    exact = ["x" * 8] * 4
    measurement = tiny.preflight_direct(*exact)
    assert (measurement.string_bytes, measurement.nodes) == (32, 4)
    with pytest.raises(tiny.InputLimitError):
        tiny.preflight_direct(*exact, "x")
    with pytest.raises(tiny.InputLimitError):
        tiny.preflight_direct([None] * 8)


def test_direct_arguments_share_node_budget(tiny):
    exact = [[None] * 7] * 4
    assert tiny.preflight_direct(*exact).nodes == 32
    with pytest.raises(tiny.InputLimitError):
        tiny.preflight_direct(*exact, None)


def test_document_text_uses_import_byte_limit_instead_of_capture_field_limit(limits):
    limits.check_document_text("x" * 8_388_608)
    with pytest.raises(limits.InputLimitError):
        limits.check_document_text("x" * 8_388_609)
    limits.check_document_text("😀" * 2, limit=8)
    with pytest.raises(limits.InputLimitError):
        limits.check_document_text("😀" * 3, limit=8)
    with pytest.raises(limits.InputLimitError):
        limits.check_document_text("private_secret\ud800")


def test_oversized_document_does_not_encode(limits):
    class Unencodable(str):
        def encode(self, *args, **kwargs):
            pytest.fail("oversized document was encoded")

    with pytest.raises(limits.InputLimitError):
        limits.check_document_text(Unencodable("x" * 9), limit=8)


def test_draft_metadata_shares_admitted_byte_budget(tiny):
    exact = ["x" * 8] * 4
    assert tiny.preflight_drafts(exact, metadata=[""]) == (None,) * 4
    with pytest.raises(tiny.InputLimitError):
        tiny.preflight_drafts(exact, metadata=["x"])


def test_draft_metadata_shares_node_budget(tiny):
    exact = [[None] * 7] * 4
    with pytest.raises(tiny.InputLimitError):
        tiny.preflight_drafts(exact, metadata=[None])


def test_draft_metadata_rejection_is_request_wide(limits):
    with pytest.raises(limits.InputLimitError):
        limits.preflight_drafts(["good"], metadata=["x" * 262_145])
    with pytest.raises(limits.InputLimitError):
        limits.preflight_drafts(["good"], metadata=[[None] * 4096])


def test_wide_metadata_collection_rejects(limits):
    with pytest.raises(limits.InputLimitError):
        limits.preflight_drafts(["good"], metadata=[None] * 65_537)
