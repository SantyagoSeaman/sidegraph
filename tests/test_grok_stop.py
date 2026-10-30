"""The Stop capture nudge on Grok Build (T1-T8).

Grok's transcript is its own ``updates.jsonl`` (``{timestamp, method, params}`` lines), Stop also
fires on shutdown and after a subagent's or background task's completion, and a Stop block in a
headless ``grok -p`` run is appended to the run's output (measured on 1.0.70).

see design/superpowers/specs/2026-10-30-grok-stop-capture-nudge-design.md
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

import sidegraph.host.hooks as hooks
from sidegraph.store import Store

_SID = "01a18a3a-7390-7091-b44a-a7d936497024"


def _update(kind: str, **fields) -> dict:
    method = (
        "_x.ai/session/update"
        if kind in ("hook_annotation", "turn_completed", "image_dropped")
        else "session/update"
    )
    return {
        "timestamp": 1793320449,
        "method": method,
        "params": {"sessionId": _SID, "update": {"sessionUpdate": kind, **fields}},
    }


def _prompt(text: str) -> dict:
    return _update("user_message_chunk", content={"type": "text", "text": text})


def _tool() -> list[dict]:
    return [
        _update("tool_call", toolCallId="c1", title="read_file", rawInput={"target_file": "a.txt"}),
        _update("tool_call_update", toolCallId="c1", status="completed"),
    ]


def _reply(text: str = "DONE") -> dict:
    return _update("agent_message_chunk", content={"type": "text", "text": text})


def _session(tmp_path: Path, entries: list[dict], kind: str | None = None) -> str:
    """A Grok session directory: ``updates.jsonl`` and, with ``kind``, its ``summary.json``."""
    session_dir = tmp_path / "home" / "sessions" / "%2Frepo" / _SID
    session_dir.mkdir(parents=True)
    (session_dir / "updates.jsonl").write_text(
        "".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8"
    )
    if kind is not None:
        (session_dir / "summary.json").write_text(
            json.dumps({"info": {"id": _SID}, "session_kind": kind}), encoding="utf-8"
        )
    return str(session_dir / "updates.jsonl")


def _two_prompts() -> list[dict]:
    return [_prompt("Read a.txt"), *_tool(), _reply(), _prompt("Now grep for beta"), _reply()]


def _payload(transcript: str, **fields) -> dict:
    return {
        "hookEventName": "stop",
        "sessionId": _SID,
        "session_id": _SID,
        "transcriptPath": transcript,
        "transcript_path": transcript,
        "promptId": "bb6823bd-65ef-4861-a392-4d59ef0f4c27",
        "reason": "end_turn",
        "stopHookActive": False,
        "stop_hook_active": False,
        **fields,
    }


def _stop(monkeypatch, capsys, tmp_path: Path, payload: dict, host: str | None = "grok") -> dict:
    sd = tmp_path / ".sidegraph"
    if not sd.exists():
        Store(sd).close()
    monkeypatch.setenv("SIDEGRAPH_DIR", str(sd))
    if host is None:
        monkeypatch.delenv("SIDEGRAPH_HOST", raising=False)
    else:
        monkeypatch.setenv("SIDEGRAPH_HOST", host)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    hooks.stop()
    return json.loads(capsys.readouterr().out)


def _captured(tmp_path: Path) -> bool:
    s = Store(tmp_path / ".sidegraph")
    try:
        return s.was_captured(_SID)
    finally:
        s.close()


def test_t1_grok_prompts_count_and_grok_notices_do_not(tmp_path):
    """Red against unfixed code: no Grok line counted, so the nudge never arms."""
    notice = _prompt('<system-reminder>\nBackground task "6ca2" completed (exit code: 0).')
    stats = hooks._transcript_stats(_session(tmp_path, [*_two_prompts(), notice, _reply()]))
    assert stats.real_prompts == 2


def test_t2_a_prompt_streamed_in_chunks_counts_once(tmp_path):
    """Red against counting each chunk."""
    entries = [_prompt("part one "), _prompt("part two "), _prompt("part three"), _reply()]
    assert hooks._transcript_stats(_session(tmp_path, entries)).real_prompts == 1


@pytest.mark.parametrize(
    "lead",
    [
        _update("user_message_chunk", content={"type": "image", "mimeType": "image/png"}),
        _prompt("<system-reminder>\nBackground task done."),
    ],
)
def test_t2b_a_chunk_that_is_not_a_person_s_does_not_hide_the_prompt_after_it(tmp_path, lead):
    """Red against opening the run on any chunk: an image block, or a Grok notice, directly
    before the text left the prompt uncounted (Grok review, ``grok agent stdio`` image prompt)."""
    entries = [lead, _prompt("describe this"), _reply(), lead, _prompt("and this"), _reply()]
    assert hooks._transcript_stats(_session(tmp_path, entries)).real_prompts == 2


def _chunk(content: dict, index: int | None) -> dict:
    line = _update("user_message_chunk", content=content)
    if index is not None:
        line["params"]["update"]["_meta"] = {"modelId": "grok-4.7", "promptIndex": index}
    return line


def test_t2c_one_prompt_split_by_a_dropped_image_counts_once(tmp_path):
    """Red against ending the run at ``image_dropped``: Grok writes it between the text blocks
    of one prompt (``grok agent stdio``, all ``promptIndex`` 0), which counted 2 (Grok review
    round 2). A different ``promptIndex`` inside a run is a second prompt."""
    image = {"type": "image", "mimeType": "image/png", "data": "x"}
    dropped = _update("image_dropped", notes=["Image 1 was dropped before send"])
    one = [
        _chunk(image, 0),
        _chunk({"type": "text", "text": "Part A"}, 0),
        dropped,
        _chunk({"type": "text", "text": "Part B"}, 0),
        _reply("OK"),
    ]
    assert hooks._transcript_stats(_session(tmp_path, one)).real_prompts == 1
    two = [_chunk({"type": "text", "text": "a"}, 0), _chunk({"type": "text", "text": "b"}, 1)]
    assert hooks._transcript_stats(_session(tmp_path / "2", two)).real_prompts == 2


def test_t3_tool_calls_count_and_their_updates_do_not(tmp_path):
    """Red against counting ``tool_call_update`` lines."""
    entries = [_prompt("go"), *_tool(), *_tool(), _reply()]
    assert hooks._transcript_stats(_session(tmp_path, entries)).tool_uses == 2


@pytest.mark.parametrize(("kind", "substantial"), [("headless", False), (None, True)])
def test_t4_a_headless_grok_session_is_never_substantial(tmp_path, kind, substantial):
    """Red against no headless rule: a Stop block is appended to a ``grok -p`` run's output."""
    stats = hooks._transcript_stats(_session(tmp_path, _two_prompts(), kind=kind))
    assert stats.real_prompts == 2
    assert hooks._is_substantial(stats) is substantial


@pytest.mark.parametrize(
    "fields",
    [
        {"reason": "shutdown", "promptId": None},
        {"promptId": "subagent-completed-01a18755-5d7a-7b32-ad1c-66e795ec2e2d"},
        {"promptId": "task-completed-6ca22204-d2dd-41c5-8e2b-0441b3689eae"},
        {"promptId": "workflow-completed-abc"},
        {"promptId": "plan-resume-abc"},
        {"reason": "channel_closed"},
    ],
)
def test_t5_shutdown_and_completion_turns_never_nudge(tmp_path, monkeypatch, capsys, fields):
    """Red against no event rule: each of these Stops would take the session's one nudge."""
    payload = _payload(_session(tmp_path, _two_prompts()), **fields)
    assert _stop(monkeypatch, capsys, tmp_path, payload) == {}
    assert not _captured(tmp_path)


def test_t6_a_grok_turn_end_nudges_once(tmp_path, monkeypatch, capsys):
    """Red against unfixed code: the transcript counted no prompts, so no nudge."""
    payload = _payload(_session(tmp_path, _two_prompts()))
    first = _stop(monkeypatch, capsys, tmp_path, payload)
    assert first.get("decision") == "block"
    assert _stop(monkeypatch, capsys, tmp_path, payload) == {}
    assert _stop(monkeypatch, capsys, tmp_path, {**payload, "stop_hook_active": True}) == {}


def test_t6b_a_headless_session_is_not_asked_again_on_the_re_arm_path(
    tmp_path, monkeypatch, capsys
):
    """Red against a headless rule in the substance gate only: a session already nudged skips
    the transcript, and a due re-arm would print the request into a ``grok -p`` run's output."""
    payload = _payload(_session(tmp_path, _two_prompts(), kind="headless"))
    monkeypatch.setattr(
        hooks, "_read_capture_ledger", lambda conn, sid: ("2026-10-01T00:00:00+00:00", None)
    )
    monkeypatch.setattr(hooks, "_rearm_commits", lambda *args: 11)
    assert _stop(monkeypatch, capsys, tmp_path, payload) == {}


def test_t7_the_host_predicates_do_not_cross(tmp_path):
    """Red against nothing: a guard that the Grok branch and the Claude and Codex predicates
    each count only their own lines."""
    grok = _prompt("a person's prompt")
    assert not hooks._is_real_user_prompt(grok)
    assert not hooks._is_codex_real_user_prompt(grok)
    claude = {"type": "user", "message": {"role": "user", "content": "hello"}}
    codex = {
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "hello"}],
        },
    }
    assert not hooks._is_grok_line(claude)
    assert not hooks._is_grok_line(codex)
    path = tmp_path / "t.jsonl"
    path.write_text(json.dumps(claude) + "\n" + json.dumps(codex) + "\n", encoding="utf-8")
    stats = hooks._transcript_stats(str(path))
    assert (stats.real_prompts, stats.session_kind) == (2, None)


def test_t8_the_event_rules_apply_on_grok_only(tmp_path, monkeypatch, capsys):
    """Red against an ungated rule: a Claude Stop carrying ``reason`` still nudges."""
    claude = [
        {"type": "user", "message": {"role": "user", "content": "one"}},
        {"type": "user", "message": {"role": "user", "content": "two"}},
    ]
    path = tmp_path / "claude.jsonl"
    path.write_text("".join(json.dumps(e) + "\n" for e in claude), encoding="utf-8")
    payload = {"session_id": "claude-1", "transcript_path": str(path), "reason": "shutdown"}
    assert _stop(monkeypatch, capsys, tmp_path, payload, host="claude").get("decision") == "block"


@pytest.mark.parametrize("bad", [[], {}, 1])
def test_t9_a_line_with_a_non_string_method_does_not_stop_the_count(tmp_path, bad):
    """Red against testing set membership on an unchecked ``method``: an unhashable value
    raised ``TypeError``, which dropped the nudge for Claude and Codex transcripts too
    (pre-release panel)."""
    claude = {"type": "user", "message": {"role": "user", "content": "one"}}
    codex = {
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "two"}],
        },
    }
    path = tmp_path / "t.jsonl"
    lines = [claude, {"method": bad, "params": {"update": {}}}, codex]
    path.write_text("".join(json.dumps(e) + "\n" for e in lines), encoding="utf-8")
    assert hooks._transcript_stats(str(path)).real_prompts == 2


@pytest.mark.parametrize("field", ["origin", "promptSource"])
def test_t9b_an_unhashable_claude_provenance_field_does_not_stop_the_count(tmp_path, field):
    """Red against membership tests on unchecked Claude provenance values (``origin.kind``,
    ``promptSource``): an unhashable one raised ``TypeError`` and dropped the count
    (pre-release panel, round 2)."""
    bad = {"type": "user", "message": {"role": "user", "content": "one"}}
    bad[field] = {"kind": []} if field == "origin" else []
    good = {"type": "user", "message": {"role": "user", "content": "two"}}
    path = tmp_path / "t.jsonl"
    path.write_text(json.dumps(bad) + "\n" + json.dumps(good) + "\n", encoding="utf-8")
    assert hooks._transcript_stats(str(path)).real_prompts == 2
