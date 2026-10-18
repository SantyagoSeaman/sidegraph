"""Reserved projection markers shared by document consumers and the ADR writer."""

from __future__ import annotations

import re

MARKER_PREFIX = "<!-- sidegraph-adr:"
END_MARKER = "<!-- /sidegraph-adr -->"
_RESERVED = re.compile(
    r"^(?:<!-- sidegraph-adr:|<!-- /sidegraph-adr)", re.MULTILINE | re.IGNORECASE
)
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")


def generated_projection(text: str) -> bool:
    """Recognize reserved claims outside fenced, indented and inline examples.

    A claim with an unknown version or malformed fields is still a projection, never
    authority to create a new record. A leading UTF-8 BOM does not change that identity.
    """
    fence: tuple[str, int] | None = None
    for line in text.removeprefix("\ufeff").splitlines():
        match = _FENCE.match(line)
        if match:
            run = match.group(1)
            if fence is None:
                fence = (run[0], len(run))
            elif run[0] == fence[0] and len(run) >= fence[1] and not line[match.end() :].strip():
                fence = None
            continue
        if fence is None and line.casefold().startswith(MARKER_PREFIX):
            return True
    return False


def reserved_lines(text: str) -> bool:
    """Whether literal template data could introduce a nested managed region."""
    return _RESERVED.search(text) is not None


def escape_markers(text: str) -> str:
    """Escape authored opening AND closing lines regardless of Markdown fence state.

    The file-region parser is positional. In code fences the entity is intentionally
    literal; the canonical source pointer still supplies the exact original bytes.
    """
    return _RESERVED.sub(lambda match: "&lt;" + match.group(0)[1:], text)
