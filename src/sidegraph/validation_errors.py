"""Bounded write diagnostics using schema vocabulary, never caller values or causes."""

from pydantic import ValidationError

MAX_ERRORS = 10
MAX_PATH_DEPTH = 8
MAX_CHARACTERS = 1024
_MAX_INDEX = 65535
_TRUNCATED = " [truncated]"
_FIELDS = frozenset(
    [
        "title",
        "kind",
        "context",
        "choice",
        "rejected",
        "consequences",
        "anchors",
        "initiative",
        "supersedes",
        "tags",
        "facts",
        "statement",
        "source",
        "supports",
        "slug",
        "summary",
        "parent_slug",
        "path_prefixes",
        "seed_anchors",
        "name",
        "file_path",
        "relation",
        "provenance",
        "ref",
        "author",
        "session_id",
        "graph_version",
        "commit",
        "id",
        "status",
        "valid_from",
        "valid_to",
        "scope",
        "layer",
        "domain_id",
        "parent_id",
        "communities",
        "stamping_live_since",
        "ratified_by",
        "ratified_at",
        "old_decision_id",
        "old_fact_id",
        "old_slug_or_id",
        "new_slug",
        "new_title",
        "new_summary",
        "drafts",
        "accept",
        "drop",
        "record_id",
        "force",
        "weight",
        "entity_id",
        "tier",
        "include_superseded",
    ]
)
_CODES = frozenset(
    [
        "missing",
        "missing_argument",
        "string_type",
        "string_unicode",
        "string_pattern_mismatch",
        "string_too_short",
        "too_short",
        "too_long",
        "list_type",
        "tuple_type",
        "dict_type",
        "model_type",
        "model_attributes_type",
        "enum",
        "literal_error",
        "value_error",
        "int_type",
        "int_parsing",
        "bool_type",
        "bool_parsing",
        "float_type",
        "float_parsing",
        "none_required",
        "extra_forbidden",
        "unexpected_keyword_argument",
        "unexpected_positional_argument",
        "invalid_value",
    ]
)

_MESSAGES = {
    "no_named_anchor": "anchors: none of the given anchors has a name",
    "no_live_support": (
        "anchorless fact has no live supporting decision — add an anchor, or "
        "re-point supports at the successor of a superseded/rejected/deprecated one"
    ),
}
_CODES = _CODES | frozenset(_MESSAGES)


def _field(value: object) -> str:
    return value if type(value) is str and value in _FIELDS else "field"


def _code(value: object) -> str:
    return value if type(value) is str and value in _CODES else "invalid_value"


def _location(parts: tuple[object, ...]) -> str:
    result: list[str] = []
    for part in parts[:MAX_PATH_DEPTH]:
        if type(part) is int and 0 <= part <= _MAX_INDEX:
            result.append(f"[{part}]")
        else:
            result.append(_field(part))
    return ".".join(result) or "field"


def format_validation_error(exc: ValidationError) -> str:
    """Format only known field/code tokens and bounded indices from structured errors."""
    errors = exc.errors(include_input=False, include_context=False, include_url=False)
    lines = [f"{_location(error['loc'])}: {_code(error['type'])}" for error in errors[:MAX_ERRORS]]
    text = "invalid draft: " + ("; ".join(lines) or "invalid_value")
    if len(errors) > MAX_ERRORS or any(
        len(error["loc"]) > MAX_PATH_DEPTH for error in errors[:MAX_ERRORS]
    ):
        text += _TRUNCATED
    if len(text) > MAX_CHARACTERS:
        text = text[: MAX_CHARACTERS - len(_TRUNCATED)] + _TRUNCATED
    return text


class SafeWriteError(ValueError):
    """A controlled field/code rejection; no free-form error message is accepted."""

    def __init__(self, field: str, code: str = "invalid_value") -> None:
        self.field = _field(field)
        self.code = _code(code)
        super().__init__(safe_write_error(self))


def safe_write_error(exc: SafeWriteError) -> str:
    """Read stored bounded tokens rather than invoking an exception's string override."""
    field = _field(exc.__dict__.get("field"))
    code = _code(exc.__dict__.get("code"))
    if code in _MESSAGES:
        return _MESSAGES[code]
    if field == "relation" and code == "enum":
        return (
            "invalid anchor: relation: enum; expected creates, modifies, affects, "
            "deprecates, considered"
        )
    prefix = "invalid anchor" if field in {"anchors", "relation"} else "invalid draft"
    return f"{prefix}: {field}: {code}"
