"""Host transcript readers for ``sidegraph-stats``' cited-back figure (host seam).

Each host writes its session transcript in its own dialect. This module knows those dialects:
where a delivery of records sits, which channel it came through, and what text the agent wrote
itself. It turns a file into neutral :class:`~sidegraph.stats.echo.Delivery` and
:class:`~sidegraph.stats.echo.Authored` events and hands them to the portable core, which owns
the placement and echo rules.

Local and read-only: files are opened for reading, nothing is written, no network. The report
holds counts only. A path from the index is read only when it ends in ``.jsonl`` and ``stat``
says it is a regular file, so a FIFO or a device planted in the index is never opened. Reading is
bounded by bytes, not time: the same journal and the same files give the same report.
# see design/superpowers/specs/2026-11-07-stats-cited-back-and-asked-design.md (D5, D8-D10)
"""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

from sidegraph.stats import echo
from sidegraph.stats.echo import CHANNELS, Authored, Delivery
from sidegraph.stats.model import ChannelCitedBack, CitedBackStats, SessionTranscript

# Bytes of transcript read per run, charged by ``st_size`` before a file is opened. Chosen once
# from the product reader's throughput: two runs over one repository's 30-day window (643
# sessions, 1.85 GiB, all read) took 7.4 s and 6.2 s, which projects to 7.9 s and 6.7 s for
# 2 GiB, inside the 10 s that owner decision N1 allows. No runtime timing fallback: the bound is
# bytes only.
READ_BUDGET_BYTES = 2 * 1024**3
LINE_CAP_BYTES = 16 * 1024**2

# Which delivery channels each dialect's transcripts can show (spec D9).
VISIBLE: dict[str, frozenset[str]] = {
    "claude": frozenset(CHANNELS),
    "codex": frozenset({"mcp"}),
    "grok": frozenset({"mcp"}),
}

_SIDEGRAPH_SERVERS = frozenset({"sidegraph", "plugin_sidegraph_sidegraph"})
# Tools whose input is a command, not something the agent wrote: a grep of the store carries ids.
_SHELL_TOOLS = frozenset({"Bash", "bash", "exec", "exec_command", "shell", "run_terminal_command"})
_BRIEF_MARKER = "[Sidegraph memory:"
_HOOK_CHANNELS = {"SessionStart": "session_start", "PreToolUse": "pre_tool_use"}


@dataclass(frozen=True)
class TranscriptEvents:
    """What one transcript file holds, for the portable core."""

    dialect: str
    deliveries: list[Delivery]
    authored: list[Authored]


# -- small helpers ----------------------------------------------------------------------------


def _obj(value: object) -> dict:
    """``value`` when it is a JSON object, else an empty one."""
    return value if isinstance(value, dict) else {}


def _alias_tool(name: object) -> str | None:
    """The Sidegraph tool a call name stands for: ``mcp__sidegraph__x``,
    ``mcp__plugin_sidegraph_sidegraph__x`` or ``sidegraph__x``; ``None`` for anything else."""
    if not isinstance(name, str):
        return None
    if name.startswith("mcp__") and "__" in name[5:]:
        server, tool = name[5:].rsplit("__", 1)
        return tool if server in _SIDEGRAPH_SERVERS else None
    if name.startswith("sidegraph__"):
        return name.split("__", 1)[1]
    return None


def _is_shell(name: object) -> bool:
    return isinstance(name, str) and name.rsplit(".", 1)[-1] in _SHELL_TOOLS


def _text(value: object) -> str:
    """Plain text of a message content: a string, or the text of a list of blocks."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(t for t in (_text(v) for v in value) if t)
    if isinstance(value, dict):
        if value.get("type") in ("thinking", "reasoning", "tool_use", "tool_result"):
            return ""
        if "text" in value:
            return _text(value["text"])
        if "content" in value:
            return _text(value["content"])
    return ""


def _unwrap(text: str) -> str:
    """A host that wraps a tool's text as ``{"result": "<text>"}`` gets the text back."""
    try:
        value = json.loads(text)
    except ValueError:
        return text
    if isinstance(value, dict) and set(value) == {"result"} and isinstance(value["result"], str):
        return _unwrap(value["result"])
    return text


def _strings(value: object) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def _dump(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _lines(path: Path, limit: int | None = None) -> Iterator[tuple[int, dict]]:
    """Decoded JSON objects with their physical line index; a line longer than
    ``LINE_CAP_BYTES`` and a line that is not JSON are skipped, their index still counted.
    With ``limit``, nothing past the first ``limit`` physical lines is read."""
    with path.open("rb") as handle:
        index = -1
        while True:
            if limit is not None and index + 1 >= limit:
                return
            chunk = handle.readline(LINE_CAP_BYTES + 1)
            if not chunk:
                return
            index += 1
            if len(chunk) > LINE_CAP_BYTES and not chunk.endswith(b"\n"):
                while True:
                    rest = handle.readline(LINE_CAP_BYTES)
                    if not rest or rest.endswith(b"\n"):
                        break
                continue
            try:
                doc = json.loads(chunk)
            except ValueError:
                continue
            if isinstance(doc, dict):
                yield index, doc


# -- dialects ---------------------------------------------------------------------------------


def dialect_of(path: Path, host: str) -> str | None:
    """The row's ``host`` when this version reads it, else sniffed from the first 20 lines."""
    if host in VISIBLE:
        return host
    for _index, doc in _lines(path, limit=20):
        if doc.get("type") == "session_meta":
            return "codex"
        update = (
            _obj(doc.get("params")).get("update") if isinstance(doc.get("params"), dict) else None
        )
        if isinstance(update, dict) and "sessionUpdate" in update:
            return "grok"
        if doc.get("type") in ("user", "assistant", "attachment"):
            return "claude"
    return None


def transcript_files(path: Path, dialect: str) -> list[Path]:
    """The recorded file, plus a Claude session's subagent transcripts beside it."""
    files = [path]
    if dialect == "claude":
        files += sorted((path.parent / path.stem / "subagents").glob("*.jsonl"))
    return files


def _read_claude(path: Path) -> TranscriptEvents:
    deliveries: list[Delivery] = []
    authored: list[Authored] = []
    pending: dict[str, tuple[str, str]] = {}
    subagent = "subagents" in path.parts
    first_user = True
    for index, doc in _lines(path):
        kind = doc.get("type")
        message = _obj(doc.get("message"))
        content = message.get("content")
        if kind == "attachment":
            attachment = _obj(doc.get("attachment"))
            channel = _HOOK_CHANNELS.get(str(attachment.get("hookEvent")))
            if attachment.get("type") == "hook_additional_context" and channel:
                deliveries.append(Delivery(channel, index, _text(attachment.get("content"))))
        elif kind == "assistant" and isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                btype = block.get("type")
                if btype == "text" and block.get("text"):
                    authored.append(Authored(index, str(block["text"])))
                elif btype == "thinking" and block.get("thinking"):
                    authored.append(Authored(index, str(block["thinking"])))
                elif btype == "tool_use":
                    name = block.get("name")
                    tool = _alias_tool(name)
                    if tool is not None:
                        pending[str(block.get("id"))] = (tool, _dump(block.get("input")))
                    elif not _is_shell(name):
                        authored.append(Authored(index, _dump(block.get("input"))))
        elif kind == "user":
            if subagent and first_user:
                first_user = False
                text = _text(content)
                at = text.find(_BRIEF_MARKER)
                if at >= 0:
                    deliveries.append(Delivery("subagent_brief", index, text[at:]))
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        call = pending.pop(str(block.get("tool_use_id")), None)
                        if call is not None:
                            text = _unwrap(_text(block.get("content")))
                            deliveries.append(
                                Delivery(
                                    "mcp",
                                    index,
                                    text,
                                    call[0],
                                    str(block.get("tool_use_id")),
                                    call[1],
                                )
                            )
    return TranscriptEvents("claude", deliveries, authored)


def _read_codex(path: Path) -> TranscriptEvents:
    deliveries: list[Delivery] = []
    authored: list[Authored] = []
    for index, doc in _lines(path):
        payload = _obj(doc.get("payload"))
        kind = doc.get("type")
        if kind == "event_msg" and payload.get("type") == "item_completed":
            item = _obj(payload.get("item"))
            if item.get("type") == "McpToolCall" and item.get("server") in _SIDEGRAPH_SERVERS:
                result = _obj(item.get("result"))
                text = _unwrap(_text(result.get("content")) if isinstance(result, dict) else "")
                deliveries.append(
                    Delivery(
                        "mcp",
                        index,
                        text,
                        str(item.get("tool")),
                        str(item.get("id")),
                        _dump(item.get("arguments")),
                    )
                )
        elif kind == "response_item":
            ptype = payload.get("type")
            if ptype == "message" and payload.get("role") == "assistant":
                text = _text(payload.get("content"))
                if text:
                    authored.append(Authored(index, text))
            elif ptype in ("function_call", "custom_tool_call"):
                name = payload.get("name")
                if _alias_tool(name) is None and not _is_shell(name):
                    body = payload.get("arguments", payload.get("input"))
                    authored.append(Authored(index, body if isinstance(body, str) else _dump(body)))
    return TranscriptEvents("codex", deliveries, authored)


def _read_grok(path: Path) -> TranscriptEvents:
    deliveries: list[Delivery] = []
    authored: list[Authored] = []
    pending: dict[str, tuple[str, str]] = {}
    run_kind: str | None = None
    run_position = 0
    run_parts: list[str] = []

    def flush() -> None:
        nonlocal run_kind, run_parts
        if run_parts:
            authored.append(Authored(run_position, "".join(run_parts)))
        run_kind, run_parts = None, []

    for index, doc in _lines(path):
        params = _obj(doc.get("params"))
        update = _obj(params.get("update"))
        kind = update.get("sessionUpdate")
        if kind in ("agent_message_chunk", "agent_thought_chunk"):
            if run_kind != kind:
                flush()
                run_kind, run_position = kind, index
            run_parts.append(_text(update.get("content")))
            continue
        if kind in ("tool_call", "tool_call_update", "user_message_chunk", "turn_completed"):
            flush()
        if kind == "tool_call":
            raw = _obj(update.get("rawInput"))
            title = update.get("title")
            name = raw.get("tool_name") if title == "use_tool" else title
            tool = _alias_tool(name)
            if tool is not None:
                pending[str(update.get("toolCallId"))] = (tool, _dump(raw.get("tool_input", raw)))
            elif not _is_shell(title) and not _is_shell(name):
                authored.append(Authored(index, _dump(raw)))
        elif kind == "tool_call_update" and update.get("status") == "completed":
            call = pending.pop(str(update.get("toolCallId")), None)
            if call is not None:
                output = update.get("rawOutput")
                if isinstance(output, str):
                    text = output
                elif isinstance(output, dict):
                    text = "\n".join(_strings(output.get("output")))
                else:
                    text = ""
                deliveries.append(
                    Delivery(
                        "mcp", index, _unwrap(text), call[0], str(update.get("toolCallId")), call[1]
                    )
                )
    flush()
    return TranscriptEvents("grok", deliveries, authored)


_READERS = {"claude": _read_claude, "codex": _read_codex, "grok": _read_grok}


def read_events(path: Path, dialect: str) -> TranscriptEvents:
    """Deliveries and agent-authored text of one transcript file."""
    return _READERS[dialect](path)


# -- the aggregator ---------------------------------------------------------------------------


def _admit(path: Path) -> int | None:
    """The file's size when it may be read: a regular ``.jsonl`` file; else ``None``."""
    if path.suffix != ".jsonl":
        return None
    try:
        info = os.stat(path)
    except OSError:
        return None
    return info.st_size if stat.S_ISREG(info.st_mode) else None


class _Tally:
    def __init__(self) -> None:
        self.visible = dict.fromkeys(CHANNELS, 0)
        self.placements = dict.fromkeys(CHANNELS, 0)
        self.cited = dict.fromkeys(CHANNELS, 0)
        self.asked_by_id = 0
        self.by_tool: dict[str, list[int]] = {}

    def add(self, events: TranscriptEvents, known_ids: frozenset[str], needle_of: dict[str, str]):
        placed: list[tuple[Delivery, list[str]]] = []
        for delivery in events.deliveries:
            ids = echo.placements(delivery, known_ids)
            if delivery.channel == "mcp":
                asked = set(echo.asked_by_id(delivery, ids, needle_of))
                self.asked_by_id += len(asked)
                ids = [i for i in ids if i not in asked]
            placed.append((delivery, ids))
        wanted = {needle_of[i] for _d, ids in placed for i in ids}
        last = echo.last_positions(events.authored, wanted) if wanted else {}
        for delivery, ids in placed:
            hits = sum(1 for i in ids if last.get(needle_of[i], -1) > delivery.position)
            self.placements[delivery.channel] += len(ids)
            self.cited[delivery.channel] += hits
            if delivery.channel == "mcp" and delivery.tool:
                slot = self.by_tool.setdefault(delivery.tool, [0, 0])
                slot[0] += len(ids)
                slot[1] += hits


def cited_back(
    sessions: Sequence[SessionTranscript],
    known_ids: frozenset[str],
    *,
    population: Sequence[str] = (),
    budget: int = READ_BUDGET_BYTES,
) -> CitedBackStats:
    """Count, per channel, delivered records the agent quoted back later in the same session.

    A session is read whole or not at all and ends in exactly one state: ``read``,
    ``no_transcript``, ``unreadable``, ``no_reader`` or ``over_budget``. Missing is unknown,
    never zero: only read sessions contribute, and a channel no read session can see is ``None``.
    """
    grouped: dict[str, list[SessionTranscript]] = {}
    for item in sessions:
        grouped.setdefault(item.session_id, []).append(item)
    states = dict.fromkeys(
        ("population", "read", "no_transcript", "unreadable", "no_reader", "over_budget"), 0
    )
    states["population"] = len(set(population) | set(grouped))
    states["no_transcript"] = len(set(population) - set(grouped))
    needle_of = echo.needles(known_ids)
    tally = _Tally()
    remaining = budget
    read_bytes = 0
    order = sorted(grouped, key=lambda sid: (max(r.at for r in grouped[sid]), sid), reverse=True)
    # Newest first by the latest `at`, ties by session id (the sort above reverses both).
    for session_id in order:
        rows = grouped[session_id]
        recorded = list(dict.fromkeys(Path(r.path) for r in rows))
        host_of = {Path(r.path): r.host for r in rows}
        files: dict[Path, str | None] = {}
        failed = False
        recorded_bytes = 0
        sniffed = 0  # the recorded files the dialect sniff opens: charged before it does, and
        # again by the read, because the sniff and the reader each open the file
        for path in recorded:
            size = _admit(path)
            if size is None:
                failed = True
                break
            recorded_bytes += size
            if host_of[path] not in VISIBLE:
                sniffed += size
        if not failed and recorded_bytes > remaining:
            # Decided from st_size alone: a file that cannot fit is not opened, not even to sniff.
            states["over_budget"] += 1
            continue
        dialects: dict[Path, str | None] = {}
        if not failed:
            remaining -= sniffed
            read_bytes += sniffed
            for path in recorded:
                try:
                    dialects[path] = dialect_of(path, host_of[path])
                except Exception:  # noqa: BLE001 - a line too deep to decode is this session's problem
                    failed = True
                    break
        if failed:
            states["unreadable"] += 1
            continue
        for path in recorded:
            dialect = dialects[path]
            for member in transcript_files(path, dialect) if dialect else [path]:
                files.setdefault(member, dialect)
        sizes = {member: _admit(member) for member in files}
        if any(size is None for size in sizes.values()):
            states["unreadable"] += 1
            continue
        if any(dialect is None for dialect in dialects.values()):
            states["no_reader"] += 1
            continue
        total = sum(size for size in sizes.values() if size is not None)
        if total > remaining:
            states["over_budget"] += 1
            continue
        remaining -= total
        read_bytes += total
        try:
            batch = [_READERS[d](member) for member, d in files.items() if d is not None]
        except Exception:  # noqa: BLE001 - any one bad transcript is that session's problem
            states["unreadable"] += 1
            continue
        states["read"] += 1
        seen_channels: set[str] = set()
        for events in batch:
            tally.add(events, known_ids, needle_of)
            seen_channels |= VISIBLE[events.dialect]
        for channel in seen_channels:
            tally.visible[channel] += 1
    channels: dict[str, ChannelCitedBack | None] = {}
    for channel in CHANNELS:
        if not tally.visible[channel]:
            channels[channel] = None
            continue
        channels[channel] = ChannelCitedBack(
            sessions_visible=tally.visible[channel],
            placements=tally.placements[channel],
            cited=tally.cited[channel],
            asked_by_id=tally.asked_by_id if channel == "mcp" else 0,
            by_tool={t: (v[0], v[1]) for t, v in sorted(tally.by_tool.items())}
            if channel == "mcp"
            else {},
        )
    return CitedBackStats(
        sessions=states, channels=channels, read_bytes=read_bytes, budget_bytes=budget
    )
