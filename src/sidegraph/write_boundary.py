"""Write guards for the locked FastMCP call/arguments core-schema contract.

Only argument validation is run here; validating a full call schema executes its body.
Original inputs continue through normal FastMCP execution after this check. The adapter
is deliberately isolated: dependency upgrades must retain the tested schema shape.
"""

import inspect
from collections.abc import Callable, Mapping
from functools import lru_cache, wraps
from typing import Any
from weakref import WeakKeyDictionary

from fastmcp.exceptions import ToolError
from fastmcp.server.dependencies import without_injected_parameters
from fastmcp.tools import FunctionTool, Tool
from fastmcp.utilities.types import get_cached_typeadapter
from pydantic import ValidationError
from pydantic_core import SchemaValidator

from .input_limits import MAX_DRAFTS, InputLimitError, preflight_direct, preflight_drafts
from .validation_errors import SafeWriteError, format_validation_error, safe_write_error

CONFIGURATION_ERROR = "Write tool configuration is unsupported; update its safe registration."
WRITE_FAILURE = (
    "Write failed: operation failed; a record may be on disk but not indexed until the store "
    "is reopened. Do not repeat the write in this session; reopen and inspect the record "
    "in the next session before retrying."
)
_SAFE_WRAPPERS: WeakKeyDictionary[Callable[..., Any], object] = WeakKeyDictionary()


def _safe_tool_error(exc: Exception) -> ToolError:
    if isinstance(exc, ValidationError):
        return ToolError(format_validation_error(exc))
    if isinstance(exc, SafeWriteError):
        return ToolError(safe_write_error(exc))
    if isinstance(exc, InputLimitError):
        return ToolError("Input exceeds resource limits.")
    return ToolError(WRITE_FAILURE)


def safe_write(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Guard sync or async local writes before FastMCP can log their raw exceptions."""
    if inspect.iscoroutinefunction(fn):

        @wraps(fn)
        async def guarded(*args: Any, **kwargs: Any) -> Any:
            try:
                return await fn(*args, **kwargs)
            except Exception as exc:
                raise _safe_tool_error(exc) from None
    else:

        @wraps(fn)
        def guarded(*args: Any, **kwargs: Any) -> Any:
            try:
                return fn(*args, **kwargs)
            except Exception as exc:
                raise _safe_tool_error(exc) from None

    token = object()
    _SAFE_WRAPPERS[guarded] = token
    guarded._sidegraph_safe_write = token  # type: ignore[attr-defined]
    return guarded


def is_safe_write(fn: Callable[..., Any]) -> bool:
    """A copied marker cannot make an unregistered callable a trusted wrapper."""
    try:
        token = _SAFE_WRAPPERS.get(fn)
        return token is not None and getattr(fn, "_sidegraph_safe_write", None) is token
    except TypeError:
        return False


@lru_cache(maxsize=32)
def _arguments_validator(fn: Callable[..., Any], run_in_thread: bool) -> SchemaValidator:
    wrapper = without_injected_parameters(fn, run_in_thread=run_in_thread)
    schema = get_cached_typeadapter(wrapper).core_schema
    if not isinstance(schema, dict) or schema.get("type") != "call":
        raise ToolError(CONFIGURATION_ERROR)
    arguments_schema = schema.get("arguments_schema")
    if not isinstance(arguments_schema, dict) or arguments_schema.get("type") != "arguments":
        raise ToolError(CONFIGURATION_ERROR)
    return SchemaValidator(arguments_schema)


def _raw_admission(name: str, arguments: dict[str, Any]) -> None:
    """Apply finite raw-input policy before any signature coercion or iterator access."""
    if name not in {"propose_decisions", "propose_domains"}:
        preflight_direct(*arguments.values())
        return
    drafts = arguments.get("drafts", ())
    if not isinstance(drafts, (list, tuple)):
        raise SafeWriteError("drafts", "list_type")
    facts = arguments.get("facts") if name == "propose_decisions" else None
    if facts is not None and not isinstance(facts, (list, tuple)):
        raise SafeWriteError("facts", "list_type")
    if len(drafts) + len(facts or ()) > MAX_DRAFTS:
        raise InputLimitError()
    union = [*drafts, *(facts or ())]
    metadata: tuple[object, ...] = (arguments.get("session_id"), arguments.get("author"))
    if name == "propose_decisions":
        metadata = (*metadata, None)
    # Item policy failures are intentionally left for core batching. The outer signature
    # has dict[Any, Any] entries: it does not coerce/consume their nested field values.
    preflight_drafts(
        union,
        decision_indices=range(len(drafts)) if name == "propose_decisions" else (),
        metadata=metadata,
    )
    for index, raw in enumerate(union):
        if not isinstance(raw, Mapping):
            field = "drafts" if index < len(drafts) else "facts"
            raise SafeWriteError(field, "dict_type")


def validate_write_arguments(tool: Tool, arguments: dict[str, Any]) -> None:
    """Validate arguments only, after admission, without running a write function."""
    if not isinstance(tool, FunctionTool) or not is_safe_write(tool.fn):
        raise ToolError(CONFIGURATION_ERROR)
    try:
        _raw_admission(tool.name, arguments)
    except (InputLimitError, SafeWriteError) as exc:
        raise _safe_tool_error(exc) from None
    except Exception:
        raise ToolError(CONFIGURATION_ERROR) from None
    try:
        validator = _arguments_validator(tool.fn, tool.run_in_thread)
    except Exception:
        raise ToolError(CONFIGURATION_ERROR) from None
    try:
        validator.validate_python(arguments)
    except ValidationError as exc:
        raise ToolError(format_validation_error(exc)) from None
    except Exception:
        raise ToolError(CONFIGURATION_ERROR) from None
