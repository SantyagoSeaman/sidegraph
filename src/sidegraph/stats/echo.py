"""The cited-back figure's pure rules (portable core, stdlib only).

Two questions, both about Sidegraph's own output: which record ids did one delivery place in
front of the agent (:func:`placements`), and did the agent quote one of them later
(:func:`cited`). Where a delivery sits in a host's transcript, and which of the agent's words
count as its own, is the host seam's business (``host/transcripts.py``); nothing here knows a
host.

The figure is a lower bound on how often a delivered record was used: an id quoted back is an
observable event, a record acted on without being named is invisible.
# see design/superpowers/specs/2026-11-07-stats-cited-back-and-asked-design.md (D6, D7)
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

ID_PREFIX = 10
CHANNELS = ("session_start", "pre_tool_use", "subagent_brief", "mcp")

_ULID = r"[0-9A-HJKMNP-TV-Z]{26}"
_ID_END = re.compile(rf"\(id:? ({_ULID})\)[ \t]*$")
# A product record starts a block: `- [kind] ...`, `- fact ...` or the superseded `~ tried, ...`.
_START = re.compile(r"^\s*(?:- \[|- fact\b|~ tried, reverted)")
# A legend (`[drifted] = ...`) or the hook's `More: ...` pointer ends the record before it. A
# blank line does not (a record's prose can hold one); a heading does only in the places the
# renderer puts one: see :func:`_text_ids`.
_BOUNDARY = re.compile(r"^\s*(?:\[[^\]]+\] = |More: )")
_HEADING = re.compile(r"^\s*#")
# What the renderer prints after a record's id line without belonging to the record: a blank
# line, a supporting fact's indented `  evidence:` line (prose that starts with the word at the
# line start is the record's own), the `- (+N more ...)` overflow line and the
# standing supersede hint.
_TRAILER = re.compile(
    r"^(?:\s*$|  evidence: |\s*(?:- \(\+\d+ more |If this session's work invalidates ))"
)
# The tags that follow a record's kind: `- [adr] [unratified] [sent earlier] title`.
_TAGS = re.compile(r"^\s*(?:- |~ tried[^:]*: )(?:\[[^\]]*\]|fact)((?:\s*\[[^\]]*\])*)")
_SENT_EARLIER = "[sent earlier]"
_LISTS = ("decisions", "facts", "records")
# The hooks print one line per record (whitespace collapsed) between per-file headers, an
# overflow line and closing markers; none of those is a record's trailer. A record there is its
# own start line.
_ONE_LINE_CHANNELS = frozenset({"pre_tool_use", "subagent_brief"})
_RUN = re.compile(r"[0-9A-HJKMNP-TV-Z]{10,}")


@dataclass(frozen=True)
class Delivery:
    """One delivery of records to the agent, as a host transcript recorded it."""

    channel: str  # one of CHANNELS
    position: int  # line index in the file
    text: str  # the delivered text as the host stored it
    tool: str | None = None  # MCP tool name (alias stripped) for channel "mcp"
    call_id: str | None = None
    call_input: str = ""  # the MCP call's input, JSON-encoded


@dataclass(frozen=True)
class Authored:
    """Text the agent wrote itself, at a line position of the same file."""

    position: int
    text: str


def _pointer(head: str) -> bool:
    tags = _TAGS.match(head)
    return bool(tags and _SENT_EARLIER in tags.group(1))


def _body(lines: list[str]) -> list[str]:
    """``lines`` without the trailer lines at its end."""
    end = len(lines)
    while end and _TRAILER.match(lines[end - 1]):
        end -= 1
    return lines[:end]


def _rendered(record: str) -> list[str]:
    """The id a rendered record ends with, or nothing for a pointer to an earlier copy.

    Only the record's own terminal suffix counts (spec D6): an id quoted at the end of an earlier
    line of its prose is somebody else's.
    """
    first = record.lstrip().split("\n", 1)[0]
    if _pointer(first):
        return []
    body = _body(record.split("\n"))
    tail = _ID_END.search(body[-1]) if body else None
    return [tail.group(1)] if tail else []


def _text_ids(text: str, *, one_line: bool = False) -> list[str]:
    ids: list[str] = []
    if one_line:
        for line in text.splitlines():
            if _START.match(line):
                ids.extend(_rendered(line))
        return ids
    block: list[str] = []

    def close() -> None:
        if block:
            ids.extend(_rendered("\n".join(block)))
            block.clear()

    def open_record() -> bool:
        """A record without its id yet: a heading inside it is its own prose."""
        body = _body(block)
        return bool(block) and not (body and _ID_END.search(body[-1]))

    previous = ""
    for line in text.splitlines():
        # The renderer sets a blank line before each section heading of its own; a heading
        # straight after a line of prose is that prose's.
        heading = _HEADING.match(line) and not previous.strip() and not open_record()
        if _START.match(line):
            close()
            block.append(line)
        elif _BOUNDARY.match(line) or heading:
            close()
        elif block:
            block.append(line)
        previous = line
    close()
    return ids


def _json_ids(answer: dict) -> list[str]:
    if answer.get("overview") is True:
        return []
    ids: list[str] = []
    for key in _LISTS:
        items = answer.get(key)
        if not isinstance(items, list):
            continue
        for item in items:
            if isinstance(item, dict):
                if not item.get("sent_earlier") and isinstance(item.get("id"), str):
                    ids.append(item["id"])
            elif isinstance(item, str):
                ids.extend(_rendered(item))
    return ids


def placements(delivery: Delivery, known_ids: frozenset[str]) -> list[str]:
    """The record ids ``delivery`` placed in full, in order, once each.

    Only Sidegraph's own markers count (spec D6), and only ids the store knows: an entity id, a
    session id or a ULID inside another record's prose is not a placement.
    """
    text = delivery.text
    found: list[str] | None = None
    if text.lstrip().startswith("{"):
        try:
            answer = json.loads(text)
        except ValueError:
            answer = None
        if isinstance(answer, dict):
            found = _json_ids(answer)
    if found is None:
        found = _text_ids(text, one_line=delivery.channel in _ONE_LINE_CHANNELS)
    return [i for i in dict.fromkeys(found) if i in known_ids]


def needles(known_ids: frozenset[str]) -> dict[str, str]:
    """Each id's search string: its first ten characters, or all 26 when another id shares them."""
    by_prefix: dict[str, int] = {}
    for rid in known_ids:
        by_prefix[rid[:ID_PREFIX]] = by_prefix.get(rid[:ID_PREFIX], 0) + 1
    return {rid: rid if by_prefix[rid[:ID_PREFIX]] > 1 else rid[:ID_PREFIX] for rid in known_ids}


def last_positions(authored: Iterable[Authored], wanted: set[str]) -> dict[str, int]:
    """For each wanted needle, the last position of ``authored`` text that holds it.

    One linear pass: a needle is made of id characters only, so it can only sit inside a run of
    them. Equal to searching every later text for every needle, without doing that.
    """
    short = {n for n in wanted if len(n) == ID_PREFIX}
    long = [n for n in wanted if len(n) != ID_PREFIX]
    last: dict[str, int] = {}
    for item in authored:
        for run in _RUN.findall(item.text):
            for start in range(len(run) - ID_PREFIX + 1):
                window = run[start : start + ID_PREFIX]
                if window in short and last.get(window, -1) < item.position:
                    last[window] = item.position
            for needle in long:
                if needle in run and last.get(needle, -1) < item.position:
                    last[needle] = item.position
    return last


def cited(
    delivery: Delivery,
    ids: Sequence[str],
    authored: Sequence[Authored],
    needle_of: Mapping[str, str],
) -> list[str]:
    """The ids among ``ids`` that the agent quoted at a later position of the same file."""
    wanted = {needle_of[i] for i in ids if i in needle_of}
    last = last_positions(authored, wanted)
    return [i for i in ids if i in needle_of and last.get(needle_of[i], -1) > delivery.position]


def asked_by_id(delivery: Delivery, ids: Sequence[str], needle_of: Mapping[str, str]) -> list[str]:
    """The ids among ``ids`` the call's own input already named, as ``retrieve_decisions(ids=)``."""
    return [i for i in ids if i in needle_of and needle_of[i] in delivery.call_input]
