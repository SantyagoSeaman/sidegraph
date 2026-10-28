"""md1: markdown sections from markdown-it-py's block tokens.

Pure functions of the file text; no store, no Git. ``markdown_it`` is imported lazily so
callers that never parse markdown do not pay for it.
# see design/superpowers/specs/2026-10-27-declared-section-drift-checks-design.md (section 5.2)
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, NamedTuple

if TYPE_CHECKING:
    from .schema import DeclaredSectionFile

MAX_LINES = 20_000  # checked before the parse: this is what bounds parse time
MAX_HEADINGS = 5_000
MAX_TOKENS = 200_000
_BUDGET_EVERY = 256
_ZERO = b"\0" * 32
_SPACES = re.compile(r"[ \t]+")


class Heading(NamedTuple):
    hash: str
    chain: tuple[str, ...]
    first_line: int  # 0-based
    end_line: int  # exclusive


class ParsedDoc(NamedTuple):
    lines: list[str]
    headings: list[Heading]
    refs_hash: str
    total_lines: int


class Unsupported(Exception):
    """md1 cannot bound this file; ``gate`` names the spec gate that fired."""

    def __init__(self, gate: str) -> None:
        super().__init__(gate)
        self.gate = gate


class SectionResolveError(Exception):
    """A heading suffix matched no section (``unknown_section``) or several."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _split(text: str) -> list[str]:
    return text.replace("\r\n", "\n").replace("\r", "\n").split("\n")


def count_lines(text: str) -> int:
    """Line count used by every cap: a trailing newline does not add a line."""
    lines = _split(text)
    return len(lines) - 1 if lines[-1] == "" and len(lines) > 1 else len(lines)


def _normalize_heading(text: str) -> str:
    return _SPACES.sub(" ", text.replace("\n", " ")).strip()


def _sha(data: bytes) -> bytes:
    return hashlib.sha256(data).digest()


def parse_md1(text: str, remaining: Callable[[], float | None] | None = None) -> ParsedDoc:
    """Resolve the top-level headings of ``text``; raise :class:`Unsupported` at a gate.

    ``remaining`` is the caller's deadline (``None`` result = no deadline); it is polled
    every 256 headings, and a value <= 0 raises ``Unsupported("budget")``.
    """
    lines = _split(text)
    total = len(lines) - 1 if lines[-1] == "" and len(lines) > 1 else len(lines)
    if total > MAX_LINES:
        raise Unsupported("lines")
    try:
        from markdown_it import MarkdownIt

        md = MarkdownIt("commonmark").disable("inline")
        env: dict = {}
        tokens = md.parse("\n".join(lines), env)
        max_nesting = int(md.options["maxNesting"])
    except Exception as exc:
        raise Unsupported("parse") from exc
    if any(token.level >= max_nesting - 1 for token in tokens):
        raise Unsupported("nesting")
    if len(tokens) > MAX_TOKENS:
        raise Unsupported("tokens")
    opens = [i for i, token in enumerate(tokens) if token.type == "heading_open"]
    if len(opens) > MAX_HEADINGS:
        raise Unsupported("headings")
    if any(tokens[i].level != 0 for i in opens):
        raise Unsupported("container")

    refs = sorted(
        [label, ref["href"], ref["title"]] for label, ref in env.get("references", {}).items()
    )
    refs_hash = hashlib.sha256(
        json.dumps(refs, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()

    headings: list[Heading] = []
    # open headings: (level, text, hash bytes, index into headings)
    stack: list[tuple[int, str, bytes, int]] = []
    for n, i in enumerate(opens):
        if remaining is not None and n % _BUDGET_EVERY == 0:
            left = remaining()
            if left is not None and left <= 0:
                raise Unsupported("budget")
        token = tokens[i]
        level = int(token.tag[1])
        first = token.map[0]  # type: ignore[index]
        while stack and stack[-1][0] >= level:
            idx = stack.pop()[3]
            headings[idx] = headings[idx]._replace(end_line=first)
        title = _normalize_heading(tokens[i + 1].content)
        parent = stack[-1][2] if stack else _ZERO
        digest = _sha(b"md1\0" + parent + _sha(title.encode("utf-8")))
        chain = tuple(entry[1] for entry in stack) + (title,)
        headings.append(Heading(digest.hex(), chain, first, len(lines)))
        stack.append((level, title, digest, len(headings) - 1))
    return ParsedDoc(lines, headings, refs_hash, total)


def section_text_hash(doc: ParsedDoc, heading: Heading) -> str:
    body = "\n".join(doc.lines[heading.first_line : heading.end_line])
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def resolve_suffix(doc: ParsedDoc, chain: Sequence[str]) -> Heading:
    """The one heading whose chain ends with ``chain`` (segments normalised like titles)."""
    wanted = tuple(_normalize_heading(segment) for segment in chain)
    found = [h for h in doc.headings if h.chain[-len(wanted) :] == wanted]
    if not found:
        raise SectionResolveError("unknown_section")
    if len(found) > 1:
        raise SectionResolveError("ambiguous_section")
    return found[0]


def footnote_ref(doc: ParsedDoc, heading: Heading) -> bool:
    """Whether the section's span holds something shaped like ``[^id]`` (``\\[\\^[^\\]\\s]+\\]``).

    One forward pass: a failed opener also fails every opener inside the run it scanned,
    so the scan resumes where that run stopped.
    """
    text = "\n".join(doc.lines[heading.first_line : heading.end_line])
    size = len(text)
    i = text.find("[^")
    while i >= 0:
        j = i + 2
        while j < size and text[j] != "]" and not text[j].isspace():
            j += 1
        if j < size and text[j] == "]" and j > i + 2:
            return True
        i = text.find("[^", max(j, i + 1))
    return False


class SectionIndex:
    """Heading lookup and section hashes of one parsed document, computed once and shared by
    every comparison against it."""

    def __init__(self, doc: ParsedDoc) -> None:
        self.doc = doc
        self.by_hash: dict[str, list[Heading]] = {}
        for heading in doc.headings:
            self.by_hash.setdefault(heading.hash, []).append(heading)
        self._text: dict[str, str] = {}

    def text_hash(self, heading: Heading) -> str:
        if heading.hash not in self._text:
            self._text[heading.hash] = section_text_hash(self.doc, heading)
        return self._text[heading.hash]


def compare_entry(
    entry: DeclaredSectionFile, doc: ParsedDoc, index: SectionIndex | None = None
) -> list[str]:
    """Why ``entry`` no longer matches ``doc``, in spec order; ``[]`` means unchanged.

    Causes: ``declared_not_found``, ``declared_changed``, ``refs_changed``,
    ``watch_changed``. A stored section is found only when exactly one heading has its
    hash. A role this module does not know is never judged unchanged. Pass a shared
    ``index`` when many entries are compared against one document.
    """
    index = index or SectionIndex(doc)
    not_found = declared_changed = watch_changed = False
    for section in entry.sections:
        matches = index.by_hash.get(section.heading_hash, [])
        found = len(matches) == 1
        differs = found and index.text_hash(matches[0]) != section.text_hash
        if section.role == "declared":
            not_found |= not found
            declared_changed |= differs
        elif section.role == "watch":
            watch_changed |= not found or differs
        else:
            declared_changed = True
    causes = []
    if not_found:
        causes.append("declared_not_found")
    if declared_changed:
        causes.append("declared_changed")
    if entry.refs_hash != doc.refs_hash:
        causes.append("refs_changed")
    if watch_changed:
        causes.append("watch_changed")
    return causes
