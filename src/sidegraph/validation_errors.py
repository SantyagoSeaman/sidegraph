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
        "decision_id",
        "checked_commit",
        "files",
        "rationale",
        "force",
        "weight",
        "entity_id",
        "tier",
        "include_superseded",
        "sections",
        "file",
        "headings",
        "also_watch",
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
        "string_too_long",
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
    "unavailable_anchor_scope": (
        "invalid draft: files: unavailable_anchor_scope; a required anchor entity could not "
        "be read. Inspect sidegraph-doctor and sidegraph-verify before checking this scope."
    ),
    "git_scope_unavailable": (
        "invalid draft: checked_commit: git_scope_unavailable; Git scope lookup failed "
        "before check publication. Inspect repository access and retry after it is available."
    ),
    "missing_required_files": (
        "invalid draft: files: missing_required_files; "
        "include every current checkable anchor file. "
        "Inspect Store.bindings_for_record(decision_id) and each Store.get_entity(entity_id) "
        "descriptor on the configured store; retry only after inspecting the complete scope."
    ),
    "no_checkable_files": (
        "invalid draft: files: no_checkable_files; no current tracked regular anchor file"
    ),
    "untracked_files": (
        "invalid draft: files: untracked_files; "
        "inspected files must exist as tracked regular files at HEAD"
    ),
    "section_file_not_in_files": (
        "invalid draft: sections: section_file_not_in_files; each sections file "
        "must also be listed in files"
    ),
    "section_file_not_markdown": (
        "invalid draft: sections: section_file_not_markdown; sections apply to .md "
        "and .markdown files only; make a whole-file check for other files"
    ),
    "section_file_too_large": (
        "invalid draft: sections: section_file_too_large; a declared file is over 2"
        " MiB or 20,000 lines; make a whole-file check for it"
    ),
    "section_scope_too_large": (
        "invalid draft: sections: section_scope_too_large; the declared files "
        "together are over 8 MiB or 20,000 lines; declare fewer files or make a "
        "whole-file check"
    ),
    "section_text_unavailable": (
        "invalid draft: sections: section_text_unavailable; a declared file could "
        "not be read as UTF-8 text within the time budget; retry, or make a whole-"
        "file check"
    ),
    "section_file_unsupported": (
        "invalid draft: sections: section_file_unsupported; the markdown structure "
        "of a declared file cannot be bounded (a heading inside a list or quote, or"
        " a footnote reference in a declared span); make a whole-file check"
    ),
    "unknown_section": (
        "invalid draft: sections: unknown_section; no heading chain ends with the "
        "given headings; copy the heading text from the file"
    ),
    "ambiguous_section": (
        "invalid draft: sections: ambiguous_section; the given headings match more "
        "than one section; add the parent heading"
    ),
    "duplicate_section": (
        "invalid draft: sections: duplicate_section; a section is listed twice, or "
        "in both headings and also_watch"
    ),
    "duplicate_section_file": (
        "invalid draft: sections: duplicate_section_file; list each file once in sections"
    ),
    "too_many_sections": (
        "invalid draft: sections: too_many_sections; declare at most 32 sections "
        "per file and 64 per check"
    ),
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
