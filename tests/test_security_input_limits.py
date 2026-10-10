"""Pure resource-policy regressions: bounded work before capture or serialization."""

import importlib
from collections.abc import Mapping

import pytest
from pydantic import BaseModel, ConfigDict


@pytest.fixture
def limits():
    return importlib.import_module("sidegraph.input_limits")


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


def test_model_construct_cannot_bypass_nested_field_limit(limits):
    class Draft(BaseModel):
        metadata: dict

    draft = Draft.model_construct(metadata={"author": "x" * 262_145})
    with pytest.raises(limits.InputLimitError):
        limits.measure_item(draft)


@pytest.mark.parametrize("container", [list, tuple])
def test_exact_per_item_node_boundary(limits, container):
    assert limits.measure_item(container([None] * 4095)).nodes == 4096
    with pytest.raises(limits.InputLimitError):
        limits.measure_item(container([None] * 4096))


def test_wide_mapping_rejected_before_iteration(limits):
    class WideMapping(Mapping):
        def __len__(self):
            return 4096

        def __iter__(self):
            pytest.fail("wide mapping iterated")

        def __getitem__(self, key):
            pytest.fail("wide mapping accessed")

    with pytest.raises(limits.InputLimitError):
        limits.measure_item(WideMapping())


def test_wide_list_rejected_before_iteration(limits):
    class WideList(list):
        def __iter__(self):
            pytest.fail("wide list iterated")

    with pytest.raises(limits.InputLimitError):
        limits.measure_item(WideList([None] * 4096))


def test_depth_boundary(limits):
    value = None
    for _ in range(32):
        value = [value]
    assert limits.measure_item(value).nodes == 33
    with pytest.raises(limits.InputLimitError):
        limits.measure_item([value])


@pytest.mark.parametrize("kind", ["list", "mapping", "model"])
def test_cycles_reject_but_shared_subtrees_are_valid(limits, kind):
    if kind == "list":
        value = []
        value.append(value)
    elif kind == "mapping":
        value = {}
        value["self"] = value
    else:

        class Draft(BaseModel):
            child: object = None

        value = Draft()
        value.child = value
    with pytest.raises(limits.InputLimitError):
        limits.measure_item(value)
    shared = ["ok"]
    assert limits.measure_item([shared, shared]).nodes == 5


def test_invalid_parent_attached_fact_is_one_item_rejection(limits):
    bad = {"title": "ok", "facts": [{"source": "x" * 262_145}]}
    errors = limits.preflight_drafts([bad, {"title": "good"}], decision_indices=[0, 1])
    assert isinstance(errors[0], limits.InputLimitError)
    assert errors[1] is None


def test_rejected_items_do_not_spend_admitted_byte_budget(limits):
    bad = ["x" * 262_144] * 16 + ["x" * 262_145]
    good = ["x" * 262_144] * 16
    errors = limits.preflight_drafts([bad] * 20 + [good])
    assert all(error is not None for error in errors[:-1])
    assert errors[-1] is None


def test_rejected_items_do_not_spend_admitted_node_budget(limits):
    bad = [None] * 4094 + ["x" * 262_145]
    assert limits.preflight_drafts([bad] * 99 + ["ok"])[-1] is None


def test_request_byte_boundary_and_combined_batches(limits):
    good = ["x" * 262_144] * 16
    assert limits.preflight_drafts(good) == (None,) * 16
    with pytest.raises(limits.InputLimitError):
        limits.preflight_drafts([*good, "x"])


def test_request_node_boundary(limits):
    good = [[None] * 4095] * 16
    assert limits.preflight_drafts(good) == (None,) * 16
    with pytest.raises(limits.InputLimitError):
        limits.preflight_drafts([*good, None])


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


def test_wide_draft_batch_rejects_before_iteration(limits):
    class WideBatch(list):
        def __iter__(self):
            pytest.fail("wide batch iterated")

    with pytest.raises(limits.InputLimitError):
        limits.preflight_drafts(WideBatch([None] * 101))


def test_wide_attached_fact_batch_rejects_before_iteration(limits):
    class WideFacts(list):
        def __iter__(self):
            pytest.fail("wide attached facts iterated")

    with pytest.raises(limits.InputLimitError):
        limits.preflight_drafts([{"facts": WideFacts([None] * 101)}], decision_indices=[0])


def test_malformed_attached_facts_remains_structural_item_error(limits):
    errors = limits.preflight_drafts(
        [{"facts": "x" * 262_145}, {"title": "ok"}], decision_indices=[0, 1]
    )
    assert errors[0] is not None
    assert errors[1] is None


def test_direct_arguments_share_aggregate_and_individual_limits(limits):
    exact = ["x" * 262_144] * 16
    measurement = limits.preflight_direct(*exact)
    assert (measurement.string_bytes, measurement.nodes) == (4_194_304, 16)
    with pytest.raises(limits.InputLimitError):
        limits.preflight_direct(*exact, "x")
    with pytest.raises(limits.InputLimitError):
        limits.preflight_direct([None] * 4096)


def test_direct_arguments_share_node_budget(limits):
    exact = [[None] * 4095] * 16
    assert limits.preflight_direct(*exact).nodes == 65_536
    with pytest.raises(limits.InputLimitError):
        limits.preflight_direct(*exact, None)


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


def test_draft_metadata_shares_admitted_byte_budget(limits):
    exact = ["x" * 262_144] * 16
    assert limits.preflight_drafts(exact, metadata=[""]) == (None,) * 16
    with pytest.raises(limits.InputLimitError):
        limits.preflight_drafts(exact, metadata=["x"])


def test_draft_metadata_shares_node_budget(limits):
    exact = [[None] * 4095] * 16
    with pytest.raises(limits.InputLimitError):
        limits.preflight_drafts(exact, metadata=[None])


def test_draft_metadata_rejection_is_request_wide(limits):
    with pytest.raises(limits.InputLimitError):
        limits.preflight_drafts(["good"], metadata=["x" * 262_145])
    with pytest.raises(limits.InputLimitError):
        limits.preflight_drafts(["good"], metadata=[[None] * 4096])


def test_wide_metadata_collection_rejects_before_iteration(limits):
    class WideMetadata(list):
        def __iter__(self):
            pytest.fail("wide metadata collection iterated")

    with pytest.raises(limits.InputLimitError):
        limits.preflight_drafts(["good"], metadata=WideMetadata([None] * 65_537))
