"""Admission, safe registration and argument-only validation remain separate guards."""

import asyncio
import inspect
import logging
from enum import IntEnum
from types import SimpleNamespace

import fastmcp
import pytest
from fastmcp.exceptions import ToolError
from fastmcp.server.middleware import MiddlewareContext
from fastmcp.tools import FunctionTool
from mcp.types import CallToolRequestParams

from sidegraph import input_limits, server, write_boundary

MARKER = "opaque" + "S3Probe"


def tool(name):
    return asyncio.run(server.mcp.get_tool(name))


def attempt(current, arguments):
    try:
        write_boundary.validate_write_arguments(current, arguments)
    except ToolError as error:
        return error
    return None


@pytest.mark.parametrize(
    "name,field",
    [
        ("propose_decisions", "drafts"),
        ("propose_decisions", "facts"),
        ("propose_domains", "drafts"),
    ],
)
def test_raw_opaque_batch_does_not_start_iteration(name, field):
    activity = []

    class Poison:
        def __iter__(self):
            activity.append("iter")
            raise RuntimeError(MARKER)

    arguments = dict(drafts=[])
    arguments[field] = Poison()
    error = attempt(tool(name), arguments)
    assert activity == []
    assert error is not None
    assert field in str(error)
    assert MARKER not in str(error)


@pytest.mark.parametrize("name", ["propose_decisions", "propose_domains"])
def test_over_100_batch_rejects_before_iteration(name):
    activity = []

    class Wide(list):
        def __iter__(self):
            activity.append("iter")
            raise RuntimeError(MARKER)

    error = attempt(tool(name), dict(drafts=Wide([{}] * 101)))
    assert activity == []
    assert error is not None
    assert str(error) == "Input exceeds resource limits."


@pytest.mark.parametrize("name", ["propose_decisions", "propose_domains", "add_decision"])
def test_numeric_enum_metadata_rejects_before_coercion(name):
    activity = []

    class Numeric(IntEnum):
        VALUE = 10**4299 + 7

        def __str__(self):
            activity.append("str")
            raise RuntimeError(MARKER)

    arguments = dict(drafts=[], author=Numeric.VALUE)
    if name == "add_decision":
        arguments = dict(
            title="ordinary", kind="adr", context="c", choice="c", author=Numeric.VALUE
        )
    error = attempt(tool(name), arguments)
    assert activity == []
    assert error is not None
    assert str(error) == "Input exceeds resource limits."


def test_proposal_prevalidation_does_not_consume_nested_rejected_value():
    activity = []

    class Poison:
        def __iter__(self):
            activity.append("iter")
            raise RuntimeError(MARKER)

    invalid = dict(title="bad", kind="lesson", context=Poison(), choice="c")
    valid = dict(title="sibling", kind="lesson", context="c", choice="c")
    assert attempt(tool("propose_decisions"), dict(drafts=[invalid, valid])) is None
    assert activity == []


def test_request_union_overflow_is_rejected_before_schema(monkeypatch):
    monkeypatch.setattr(input_limits, "MAX_REQUEST_BYTES", 30)
    error = attempt(
        tool("propose_decisions"),
        dict(drafts=[dict(context="x" * 20)], facts=[dict(source="y" * 20)]),
    )
    assert error is not None
    assert str(error) == "Input exceeds resource limits."


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("exception", [ValueError, RuntimeError, ToolError])
def test_sync_and_async_wrappers_mask_arbitrary_failures(asynchronous, exception, caplog):
    calls = []

    def sync_failure(value: int = 4):
        calls.append(value)
        raise exception(MARKER)

    async def async_failure(value: int = 4):
        calls.append(value)
        raise exception(MARKER)

    original = async_failure if asynchronous else sync_failure
    guarded = write_boundary.safe_write(original)
    assert inspect.signature(guarded) == inspect.signature(original)
    temp = fastmcp.FastMCP("guard-test")
    temp.add_tool(FunctionTool.from_function(guarded, name="guarded"))

    async def run():
        async with fastmcp.Client(temp) as client:
            return await client.call_tool("guarded", {"value": "7"}, raise_on_error=False)

    with caplog.at_level(logging.WARNING):
        result = asyncio.run(run())
    assert calls == [7]
    rendered = str(result.content)
    logs = "\n".join(logging.Formatter("%(message)s").format(record) for record in caplog.records)
    assert MARKER not in rendered + logs
    assert result.is_error
    assert "may be on disk" in rendered and "reopened" in rendered


@pytest.mark.parametrize("annotations", [None, server._LOCAL_WRITE, server._READ_ONLY])
def test_unknown_registration_with_valid_arguments_fails_before_body(annotations, caplog):
    calls = []

    def unknown_write(value: str):
        calls.append(value)
        return value

    temp = fastmcp.FastMCP("unguarded-test")
    temp.add_middleware(server._ArgumentNames())
    temp.add_tool(FunctionTool.from_function(unknown_write, annotations=annotations))

    async def run():
        async with fastmcp.Client(temp) as client:
            return await client.call_tool("unknown_write", {"value": MARKER}, raise_on_error=False)

    with caplog.at_level(logging.WARNING):
        result = asyncio.run(run())
    assert calls == []
    assert result.is_error
    assert write_boundary.CONFIGURATION_ERROR in str(result.content)
    assert MARKER not in str(result.content)
    assert MARKER not in "\n".join(
        logging.Formatter("%(message)s").format(record) for record in caplog.records
    )


def test_known_read_name_rebound_to_another_function_is_not_a_read_exemption():
    calls = []

    def impostor():
        calls.append(True)
        return MARKER

    forged = FunctionTool.from_function(impostor, name="list_facts", annotations=server._READ_ONLY)
    error = None
    try:
        server._classify_tool(forged)
    except ToolError as caught:
        error = caught
    assert calls == []
    assert error is not None
    assert str(error) == write_boundary.CONFIGURATION_ERROR


def test_copied_wrapper_marker_is_not_trusted():
    def original(value: str):
        return value

    safe = write_boundary.safe_write(original)
    original._sidegraph_safe_write = safe._sidegraph_safe_write
    assert write_boundary.is_safe_write(safe)
    assert not write_boundary.is_safe_write(original)


def test_registered_tool_partition_is_total_and_identity_bound():
    current = asyncio.run(server.mcp.list_tools())
    classified = {item.name: server._classify_tool(item) for item in current}
    assert len(classified) == 24
    assert {name for name, write in classified.items() if write} == server._WRITE_TOOLS
    assert {
        name for name, write in classified.items() if not write
    } == server._READ_TOOLS | server._LAZY_READ_TOOLS


def test_unsupported_core_schema_fails_closed_without_body(monkeypatch):
    calls = []

    @write_boundary.safe_write
    def guarded(value: str):
        calls.append(value)
        return value

    current = FunctionTool.from_function(guarded)

    class Unsupported:
        core_schema = {"type": "str"}

    monkeypatch.setattr(write_boundary, "get_cached_typeadapter", lambda fn: Unsupported())
    error = attempt(current, {"value": MARKER})
    assert calls == []
    assert error is not None
    assert str(error) == write_boundary.CONFIGURATION_ERROR


def test_argument_validator_cache_is_bounded_and_never_executes_bodies():
    calls = []
    write_boundary._arguments_validator.cache_clear()
    for _ in range(40):

        @write_boundary.safe_write
        def guarded(value: int = 4):
            calls.append(value)
            return value

        current = FunctionTool.from_function(guarded)
        assert attempt(current, {"value": "7"}) is None
    assert calls == []
    assert write_boundary._arguments_validator.cache_info().currsize == 32


@pytest.mark.parametrize("lookup", ["absent", "missing", "raising"])
def test_unresolved_middleware_context_fails_closed_before_continuation(lookup):
    calls = []

    class Lookup:
        async def get_tool(self, name):
            if lookup == "raising":
                raise RuntimeError(MARKER)
            return None

    async def continuation(context):
        calls.append(context.message.name)
        return MARKER

    context = MiddlewareContext(
        message=CallToolRequestParams(name="add_decision", arguments={"title": MARKER}),
        fastmcp_context=None if lookup == "absent" else SimpleNamespace(fastmcp=Lookup()),
    )
    with pytest.raises(ToolError) as caught:
        asyncio.run(server._ArgumentNames().on_call_tool(context, continuation))
    assert calls == []
    assert str(caught.value) == write_boundary.CONFIGURATION_ERROR
    assert MARKER not in str(caught.value)


@pytest.mark.parametrize("name", ["propose_decisions", "propose_domains"])
def test_raw_admission_method_failure_has_a_static_cause(name):
    calls = []

    class BelowLimit(list):
        def __iter__(self):
            calls.append("iter")
            raise RuntimeError(MARKER)

    error = attempt(tool(name), {"drafts": BelowLimit([{}])})
    assert calls == ["iter"]
    assert error is not None
    assert str(error) == write_boundary.CONFIGURATION_ERROR
    assert MARKER not in str(error)
