import io
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

import pytest

import sidegraph.host.hooks as hooks


def _run(monkeypatch, capsys, payload, db):
    # SIDEGRAPH_DIR (not the deprecated SIDEGRAPH_DB) so `db` is used LITERALLY -- no
    # legacy-dispatch redirection to its parent when it doesn't exist yet (see
    # config._dispatch_sidegraph_db). Back-compat/dispatch behavior itself is covered by
    # tests/test_config.py and the two SIDEGRAPH_DB-specific tests below.
    monkeypatch.setenv("SIDEGRAPH_DIR", str(db))
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    hooks.stop()
    return json.loads(capsys.readouterr().out)


# -- transcript helpers (substance gate, E1) ------------------------------------------------
#
# Real Claude Code transcripts (JSONL) use `type: "user"` for BOTH an actual typed prompt
# (`message.content` a string, or a list with a non-tool_result block) and a tool result
# being handed back to the agent (`message.content` a list of ONLY `tool_result` blocks).
# These helpers build minimal lines of each shape so tests can control exactly how many
# "real" prompts a transcript contains.


def _user_prompt(text="hello"):
    return {"type": "user", "message": {"role": "user", "content": text}}


def _tool_result(text="result"):
    return {
        "type": "user",
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "t1", "content": text}],
        },
    }


def _assistant(text="ok"):
    return {
        "type": "assistant",
        "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
    }


def _write_transcript(path, entries):
    with open(path, "w") as fh:
        for entry in entries:
            fh.write(json.dumps(entry) + "\n")
    return str(path)


def _transcript(tmp_path, real_prompts=2, tool_results=0, name="transcript.jsonl"):
    """A transcript with `real_prompts` real user prompts (each followed by an assistant
    turn) plus `tool_results` tool-result-shaped user entries that must NOT count."""
    entries = []
    for i in range(real_prompts):
        entries.append(_user_prompt(f"prompt {i}"))
        entries.append(_assistant())
    for i in range(tool_results):
        entries.append(_tool_result(f"result {i}"))
    return _write_transcript(tmp_path / name, entries)


def _substantial(tmp_path, name="transcript.jsonl"):
    return _transcript(tmp_path, real_prompts=hooks._MIN_USER_PROMPTS, name=name)


def test_first_stop_blocks_with_nudge_and_marks_ledger(tmp_path, monkeypatch, capsys):
    db = tmp_path / "s.db"
    # Named after the session, as both hosts do — the ledger key comes from this path.
    transcript = _substantial(tmp_path, name="s1.jsonl")
    out = _run(
        monkeypatch,
        capsys,
        {"session_id": "s1", "stop_hook_active": False, "transcript_path": transcript},
        db,
    )
    assert out["decision"] == "block"
    assert "propose_decisions" in out["reason"]
    from sidegraph.store import Store

    assert Store(db).was_captured("s1") is True


def test_nudge_also_invites_domain_drafts(tmp_path, monkeypatch, capsys):
    """Extended for the mind-model layer (M2): the nudge mentions propose_domains too,
    without displacing the propose_decisions instruction it already had."""
    db = tmp_path / "s.db"
    transcript = _substantial(tmp_path)
    out = _run(
        monkeypatch,
        capsys,
        {"session_id": "s1", "stop_hook_active": False, "transcript_path": transcript},
        db,
    )
    assert "propose_domains" in out["reason"]
    assert "propose_decisions" in out["reason"]


def test_nudge_text_is_the_one_liner(tmp_path, monkeypatch, capsys):
    """E3: the wall-of-text template was replaced with a compact, branded one-liner; the
    full What/Why/Where/Learned field guidance now lives solely in propose_decisions'
    docstring (server.py), not duplicated here."""
    db = tmp_path / "s.db"
    transcript = _substantial(tmp_path)
    out = _run(
        monkeypatch,
        capsys,
        {"session_id": "s1", "stop_hook_active": False, "transcript_path": transcript},
        db,
    )
    assert out["reason"] == hooks.CAPTURE_NUDGE
    assert out["reason"].startswith("Sidegraph:")
    assert "\n" not in out["reason"]


def test_nudge_mentions_supersede_decision_within_length_bound(tmp_path, monkeypatch, capsys):
    """Staleness machinery D3: the nudge gains a supersede clause. The pre-existing
    exact-text test (test_nudge_text_is_the_one_liner, above) is a tautology that can't
    guard this -- it compares against the constant itself, so editing CAPTURE_NUDGE keeps
    it green regardless. This substring assert is the genuinely-red guard, plus the
    ``<= 850`` char bound: 800 when the staleness design set it, raised by the 43-character
    re-arm sentence (a re-armed nudge with the drift clause is pinned by
    ``test_stop_rearm.py``). The first nudge itself stays far below it.
    see design/superpowers/specs/2026-10-03-capture-rearm-design.md (D4, T9)"""
    db = tmp_path / "s.db"
    transcript = _substantial(tmp_path)
    out = _run(
        monkeypatch,
        capsys,
        {"session_id": "sd1", "stop_hook_active": False, "transcript_path": transcript},
        db,
    )
    assert "supersede_decision" in out["reason"]
    assert len(out["reason"]) <= 850


def test_block_response_sets_suppress_output(tmp_path, monkeypatch, capsys):
    db = tmp_path / "s.db"
    transcript = _substantial(tmp_path)
    out = _run(
        monkeypatch,
        capsys,
        {"session_id": "s1", "stop_hook_active": False, "transcript_path": transcript},
        db,
    )
    assert out["suppressOutput"] is True


def test_second_stop_same_session_allows(tmp_path, monkeypatch, capsys):
    db = tmp_path / "s.db"
    transcript = _substantial(tmp_path)
    payload = {"session_id": "s1", "stop_hook_active": False, "transcript_path": transcript}
    _run(monkeypatch, capsys, payload, db)
    out = _run(monkeypatch, capsys, payload, db)
    assert out == {}


def test_stop_hook_active_allows(tmp_path, monkeypatch, capsys):
    out = _run(
        monkeypatch, capsys, {"session_id": "s2", "stop_hook_active": True}, tmp_path / "s.db"
    )
    assert out == {}


def test_missing_session_id_allows(tmp_path, monkeypatch, capsys):
    out = _run(monkeypatch, capsys, {"stop_hook_active": False}, tmp_path / "s.db")
    assert out == {}


def test_stop_never_raises(tmp_path, monkeypatch, capsys):
    def boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr("sidegraph.store.Store", boom)
    transcript = _substantial(tmp_path)
    out = _run(
        monkeypatch,
        capsys,
        {"session_id": "s3", "stop_hook_active": False, "transcript_path": transcript},
        tmp_path / "s.db",
    )
    assert out == {}


# -- substance gate (E1) ---------------------------------------------------------------------


def test_gate_suppresses_below_threshold_and_does_not_mark(tmp_path, monkeypatch, capsys):
    db = tmp_path / "s.db"
    transcript = _transcript(tmp_path, real_prompts=hooks._MIN_USER_PROMPTS - 1)
    out = _run(
        monkeypatch,
        capsys,
        {"session_id": "g1", "stop_hook_active": False, "transcript_path": transcript},
        db,
    )
    assert out == {}
    from sidegraph.store import Store

    assert Store(db).was_captured("g1") is False


def test_gate_passes_at_threshold(tmp_path, monkeypatch, capsys):
    db = tmp_path / "s.db"
    transcript = _substantial(tmp_path)
    out = _run(
        monkeypatch,
        capsys,
        {"session_id": "g2", "stop_hook_active": False, "transcript_path": transcript},
        db,
    )
    assert out["decision"] == "block"


def test_tool_results_do_not_count_toward_the_gate(tmp_path, monkeypatch, capsys):
    # One real prompt plus a pile of tool results -- still below threshold, since tool
    # results are not real user prompts.
    db = tmp_path / "s.db"
    transcript = _transcript(tmp_path, real_prompts=1, tool_results=10)
    out = _run(
        monkeypatch,
        capsys,
        {"session_id": "g3", "stop_hook_active": False, "transcript_path": transcript},
        db,
    )
    assert out == {}


def _meta_injection(text="Stop hook feedback: Sidegraph: consider capturing..."):
    """A hook-injected turn as Claude Code persists it: `type: "user"` plus a top-level
    `isMeta: true` (measured on real session files 2026-07-30 — Stop-hook feedback,
    <local-command-caveat> wrappers, and skill-injection turns all carry it; genuinely
    typed prompts never do)."""
    entry = _user_prompt(text)
    entry["isMeta"] = True
    return entry


def _compact_summary():
    """A post-compaction continuation summary: `isCompactSummary` (+`isVisibleInTranscriptOnly`),
    same measurement source as `_meta_injection`."""
    entry = _user_prompt("This session is being continued from a previous conversation...")
    entry["isCompactSummary"] = True
    entry["isVisibleInTranscriptOnly"] = True
    return entry


def _assistant_tool_use(name="Bash"):
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": "t1", "name": name, "input": {}}],
        },
    }


def _with_entrypoint(entries, entrypoint):
    """Stamp the host's top-level `entrypoint` field on every entry (measured shape:
    Claude Code stamps it on user/assistant entries; the gate reads the first one seen)."""
    return [{**e, "entrypoint": entrypoint} for e in entries]


def test_one_shot_session_with_tool_work_arms_the_gate(tmp_path, monkeypatch, capsys):
    """Spec D1 (E10 H-F0): an sdk-cli one-shot session — 1 real prompt, >= _MIN_TOOL_USES
    assistant tool_use blocks — must nudge and mark the ledger. Red against unfixed code:
    the prompt-only gate suppresses it."""
    db = tmp_path / "s.db"
    entries = _with_entrypoint(
        [_user_prompt()] + [_assistant_tool_use() for _ in range(hooks._MIN_TOOL_USES)],
        "sdk-cli",
    )
    transcript = _write_transcript(tmp_path / "os1.jsonl", entries)
    out = _run(
        monkeypatch,
        capsys,
        {"session_id": "os1", "stop_hook_active": False, "transcript_path": transcript},
        db,
    )
    assert out["decision"] == "block"
    from sidegraph.store import Store

    assert Store(db).was_captured("os1") is True


def test_one_shot_below_tool_threshold_stays_suppressed(tmp_path, monkeypatch, capsys):
    # Threshold-exactness guard (declared exception in the spec ledger: red against
    # nothing — it passes before and after; it exists so the fix cannot over-reach).
    db = tmp_path / "s.db"
    entries = _with_entrypoint(
        [_user_prompt()] + [_assistant_tool_use() for _ in range(hooks._MIN_TOOL_USES - 1)],
        "sdk-cli",
    )
    transcript = _write_transcript(tmp_path / "transcript.jsonl", entries)
    out = _run(
        monkeypatch,
        capsys,
        {"session_id": "os2", "stop_hook_active": False, "transcript_path": transcript},
        db,
    )
    assert out == {}
    from sidegraph.store import Store

    assert Store(db).was_captured("os2") is False


def test_interactive_session_tool_use_does_not_arm_turn_one(tmp_path, monkeypatch, capsys):
    """Spec review F2 (BLOCKING): an UNSCOPED tool branch armed at the end of turn 1 in
    93% of multi-prompt interactive sessions, burning the once-per-session ledger before
    the session's richer content existed. The branch is scoped to sdk-cli; a cli
    (interactive) session with heavy tool use at turn 1 must stay on the prompt gate.
    Red against the rev-1 unscoped design (discarded-design red, per the spec ledger)."""
    db = tmp_path / "s.db"
    entries = _with_entrypoint(
        [_user_prompt()] + [_assistant_tool_use() for _ in range(20)],
        "cli",
    )
    transcript = _write_transcript(tmp_path / "transcript.jsonl", entries)
    out = _run(
        monkeypatch,
        capsys,
        {"session_id": "os3", "stop_hook_active": False, "transcript_path": transcript},
        db,
    )
    assert out == {}
    from sidegraph.store import Store

    assert Store(db).was_captured("os3") is False  # still eligible for a later nudge


def test_missing_entrypoint_degrades_to_the_prompt_gate(tmp_path, monkeypatch, capsys):
    # A host that stamps no `entrypoint` anywhere degrades to the old gate — never to
    # arming (spec D1). 1 prompt + many tools, no entrypoint field → suppressed.
    db = tmp_path / "s.db"
    entries = [_user_prompt()] + [_assistant_tool_use() for _ in range(20)]
    transcript = _write_transcript(tmp_path / "transcript.jsonl", entries)
    out = _run(
        monkeypatch,
        capsys,
        {"session_id": "os4", "stop_hook_active": False, "transcript_path": transcript},
        db,
    )
    assert out == {}


def test_entrypoint_reads_the_first_entry_not_the_last(tmp_path, monkeypatch, capsys):
    """Code review F6: `claude -p --resume` of an interactive session appends sdk-cli
    entries to a transcript whose FIRST entries are cli — first-seen keeps that session on
    the prompt gate (the conservative choice D1 argues for); last-seen would arm the tool
    branch inside an interactive session, the exact F2 blast-radius shape. Red against a
    last-seen reading."""
    db = tmp_path / "s.db"
    entries = _with_entrypoint([_user_prompt(), _assistant()], "cli") + _with_entrypoint(
        [_assistant_tool_use() for _ in range(20)], "sdk-cli"
    )
    transcript = _write_transcript(tmp_path / "transcript.jsonl", entries)
    out = _run(
        monkeypatch,
        capsys,
        {"session_id": "ep1", "stop_hook_active": False, "transcript_path": transcript},
        db,
    )
    assert out == {}
    from sidegraph.store import Store

    assert Store(db).was_captured("ep1") is False


def test_tool_results_do_not_count_in_an_sdk_cli_session(tmp_path, monkeypatch, capsys):
    """Code review F7: the pre-existing tool-result test carries no entrypoint, so it only
    exercises the prompt branch — a counter that also counted `tool_result` blocks (or
    scanned user entries) would arm sdk-cli sessions with no test noticing. 1 prompt +
    10 tool results, sdk-cli → still suppressed. Red against a counter that reads
    tool_result blocks or non-assistant entries."""
    db = tmp_path / "s.db"
    entries = _with_entrypoint(
        [_user_prompt()] + [_tool_result(f"r{i}") for i in range(10)], "sdk-cli"
    )
    transcript = _write_transcript(tmp_path / "transcript.jsonl", entries)
    out = _run(
        monkeypatch,
        capsys,
        {"session_id": "ep2", "stop_hook_active": False, "transcript_path": transcript},
        db,
    )
    assert out == {}
    from sidegraph.store import Store

    assert Store(db).was_captured("ep2") is False


def test_malformed_lines_do_not_crash_the_combined_counter(tmp_path, monkeypatch, capsys):
    """Spec T5 (declared: no red exists against the generalized single-pass counter —
    the malformed-line skip is inherited by construction; this red applies only to the
    discarded sibling-counter variant). Kept as the cheap crash guard."""
    db = tmp_path / "s.db"
    entries = _with_entrypoint(
        [_user_prompt()] + [_assistant_tool_use() for _ in range(hooks._MIN_TOOL_USES)],
        "sdk-cli",
    )
    transcript = tmp_path / "transcript.jsonl"
    with open(transcript, "w") as fh:
        fh.write("{not json\n\n")
        for entry in entries:
            fh.write(json.dumps(entry) + "\n")
        fh.write("also not json\n")
    out = _run(
        monkeypatch,
        capsys,
        {"session_id": "os5", "stop_hook_active": False, "transcript_path": str(transcript)},
        db,
    )
    assert out["decision"] == "block"


def test_hook_injected_meta_turns_do_not_count_toward_the_gate(tmp_path, monkeypatch, capsys):
    """4c-findings §2: one genuine prompt + one hook injection used to read as exactly
    `_MIN_USER_PROMPTS`, arming the nudge off a single real request."""
    db = tmp_path / "s.db"
    entries = [_user_prompt(), _assistant()]
    entries += [_meta_injection() for _ in range(hooks._MIN_USER_PROMPTS)]
    transcript = _write_transcript(tmp_path / "transcript.jsonl", entries)
    out = _run(
        monkeypatch,
        capsys,
        {"session_id": "m1", "stop_hook_active": False, "transcript_path": transcript},
        db,
    )
    assert out == {}
    from sidegraph.store import Store

    assert Store(db).was_captured("m1") is False


def test_compact_summary_and_slash_command_turns_do_not_count(tmp_path, monkeypatch, capsys):
    """Slash-command wrapper turns (`<command-name>`/`<local-command-stdout>`) persist with
    NO distinguishing flag (measured 2026-07-30), so they are excluded by their host-emitted
    content prefix; compaction summaries carry `isCompactSummary`. Neither is a typed prompt."""
    db = tmp_path / "s.db"
    entries = [
        _user_prompt(),
        _assistant(),
        _compact_summary(),
        _user_prompt(
            "<command-name>/compact</command-name>\n<command-message>compact</command-message>"
        ),
        _user_prompt("<local-command-stdout>Compacted</local-command-stdout>"),
    ]
    transcript = _write_transcript(tmp_path / "transcript.jsonl", entries)
    out = _run(
        monkeypatch,
        capsys,
        {"session_id": "m2", "stop_hook_active": False, "transcript_path": transcript},
        db,
    )
    assert out == {}


# -- host-injected user turns (the prompt count must not include them) ------------------------
#
# Claude Code persists messages it injects itself as `type: "user"` lines with no `isMeta` flag.
# Each shape below is a REAL line redacted to its opening tag and the minimal structure, with no
# content (census of the 400 most recent transcripts, Claude Code 2.1.28x). The gate reads the
# content prefix only; the `origin` field a newer host adds is not consulted.


def _task_notification():
    """A background agent or command finished: the host injects the notification as a user turn."""
    entry = _user_prompt(
        "<task-notification>\n<task-id></task-id>\n<status></status>\n"
        "<summary></summary>\n</task-notification>"
    )
    entry["origin"] = {"kind": "task-notification"}
    return entry


_OTHER_HOST_INJECTED = {
    # An agent-team message another agent sent; the attribute makes the prefix open-ended.
    "teammate_message": '<teammate-message teammate_id="">\n</teammate-message>',
    # The output of a `!` shell command the person ran; its input line is typed, so it counts.
    "bash_stdout": "<bash-stdout></bash-stdout><bash-stderr></bash-stderr>",
    # The same agent-team message as the lead's own session receives it: a plain-text line
    # first, the tag on the second line.
    "lead_session_teammate_message": (
        'Another Claude session sent a message:\n<teammate-message teammate_id="">\n'
        "</teammate-message>"
    ),
}


def _stop_on(tmp_path, monkeypatch, capsys, session, entries):
    """Run the Stop hook over a transcript of `entries`; return its output and the db."""
    db = tmp_path / "s.db"
    transcript = _write_transcript(tmp_path / f"{session}.jsonl", entries)
    out = _run(
        monkeypatch,
        capsys,
        {"session_id": session, "stop_hook_active": False, "transcript_path": transcript},
        db,
    )
    return out, db


def test_task_notification_messages_do_not_count_as_prompts(tmp_path, monkeypatch, capsys):
    """T1: one typed prompt plus two injected task-notifications is one prompt, so the gate
    stays shut. Red against `_HOST_EMITTED_PREFIXES` without `<task-notification>`: the three
    lines read as three prompts and the nudge fires, then every later stop is silenced by the
    once-per-session ledger. The control shows a second typed prompt still arms it."""
    from sidegraph.store import Store

    injected = [_task_notification(), _assistant(), _task_notification(), _assistant()]
    out, db = _stop_on(
        tmp_path, monkeypatch, capsys, "tn1", [_user_prompt(), _assistant(), *injected]
    )
    assert out == {}
    assert Store(db).was_captured("tn1") is False

    out, _ = _stop_on(
        tmp_path,
        monkeypatch,
        capsys,
        "tn2",
        [_user_prompt(), _assistant(), *injected, _user_prompt("second request")],
    )
    assert out["decision"] == "block"


@pytest.mark.parametrize("kind", sorted(_OTHER_HOST_INJECTED))
def test_other_host_injected_messages_do_not_count_as_prompts(tmp_path, monkeypatch, capsys, kind):
    """T2: every further opening tag the census found host-injected, one redacted line each.
    Red against `_HOST_EMITTED_PREFIXES` without that prefix. The control shows the injection
    does not hide a second typed prompt."""
    from sidegraph.store import Store

    injected = _user_prompt(_OTHER_HOST_INJECTED[kind])
    out, db = _stop_on(
        tmp_path,
        monkeypatch,
        capsys,
        f"hi-{kind}",
        [_user_prompt(), _assistant(), injected, _assistant(), injected],
    )
    assert out == {}, kind
    assert Store(db).was_captured(f"hi-{kind}") is False

    out, _ = _stop_on(
        tmp_path,
        monkeypatch,
        capsys,
        f"hic-{kind}",
        [_user_prompt(), _assistant(), injected, _user_prompt("second request")],
    )
    assert out["decision"] == "block", kind


@pytest.mark.parametrize(
    "provenance",
    [{"origin": {"kind": "task-notification"}}, {"promptSource": "system"}],
    ids=["origin-task-notification", "prompt-source-system"],
)
def test_a_line_the_host_marks_as_not_typed_does_not_count(
    tmp_path, monkeypatch, capsys, provenance
):
    """T4: newer Claude Code marks some injected lines with provenance fields, including task
    notifications written as plain prose that no prefix can catch ("2 background agents were
    stopped"). A line marked `origin.kind == "task-notification"` or `promptSource == "system"`
    is not a person's prompt. The fields are only ever a "not a person" signal: their absence
    proves nothing, since teammate and slash-command lines never carry them. Red against a
    gate that reads only the content prefix."""
    from sidegraph.store import Store

    injected = {**_user_prompt("2 background agents were stopped"), **provenance}
    session = "pv-" + next(iter(provenance))
    out, db = _stop_on(
        tmp_path, monkeypatch, capsys, session, [_user_prompt(), _assistant(), injected]
    )
    assert out == {}
    assert Store(db).was_captured(session) is False


def test_typed_slash_command_still_counts_as_a_prompt(tmp_path, monkeypatch, capsys):
    """T3, the regression guard: a slash command the person typed starts with
    `<command-message>` in current Claude Code and is a prompt (origin `human`), unlike the
    `<command-name>`-first wrapper of older versions. It counts by design; dropping it with
    the host-injected tags would silence the gate for sessions driven by commands."""
    typed = _user_prompt(
        "<command-message></command-message>\n<command-name></command-name>\n"
        "<command-args></command-args>"
    )
    typed["origin"] = {"kind": "human"}
    out, _ = _stop_on(tmp_path, monkeypatch, capsys, "sc1", [_user_prompt(), _assistant(), typed])
    assert out["decision"] == "block"


def test_gated_session_nudges_on_a_later_substantial_stop(tmp_path, monkeypatch, capsys):
    """A session gated at turn 1 (not yet substantial) must not be permanently suppressed:
    mark_captured must not have been written, so a later, substantial stop still nudges."""
    db = tmp_path / "s.db"
    thin = _transcript(tmp_path, real_prompts=1, name="thin.jsonl")
    out1 = _run(
        monkeypatch,
        capsys,
        {"session_id": "g4", "stop_hook_active": False, "transcript_path": thin},
        db,
    )
    assert out1 == {}

    thick = _transcript(tmp_path, real_prompts=3, name="thick.jsonl")
    out2 = _run(
        monkeypatch,
        capsys,
        {"session_id": "g4", "stop_hook_active": False, "transcript_path": thick},
        db,
    )
    assert out2["decision"] == "block"


def test_substantial_from_start_nudges_once_and_never_again(tmp_path, monkeypatch, capsys):
    db = tmp_path / "s.db"
    transcript = _substantial(tmp_path)
    payload = {"session_id": "g5", "stop_hook_active": False, "transcript_path": transcript}
    out1 = _run(monkeypatch, capsys, payload, db)
    assert out1["decision"] == "block"
    out2 = _run(monkeypatch, capsys, payload, db)
    assert out2 == {}


def test_missing_transcript_path_allows_without_marking(tmp_path, monkeypatch, capsys):
    db = tmp_path / "s.db"
    out = _run(monkeypatch, capsys, {"session_id": "g6", "stop_hook_active": False}, db)
    assert out == {}
    from sidegraph.store import Store

    assert Store(db).was_captured("g6") is False


def test_nonexistent_transcript_file_allows(tmp_path, monkeypatch, capsys):
    db = tmp_path / "s.db"
    out = _run(
        monkeypatch,
        capsys,
        {
            "session_id": "g7",
            "stop_hook_active": False,
            "transcript_path": str(tmp_path / "does-not-exist.jsonl"),
        },
        db,
    )
    assert out == {}


def test_garbled_transcript_file_allows(tmp_path, monkeypatch, capsys):
    db = tmp_path / "s.db"
    garbled = tmp_path / "garbled.jsonl"
    garbled.write_text("not json\n{also not json\n")
    out = _run(
        monkeypatch,
        capsys,
        {"session_id": "g8", "stop_hook_active": False, "transcript_path": str(garbled)},
        db,
    )
    assert out == {}


def test_non_string_transcript_path_allows_without_crashing(tmp_path):
    """A non-string ``transcript_path`` (e.g. the int ``1``, as a malformed/adversarial hook
    payload might send) must be treated as absent, not handed to ``open()``. Python's
    ``open()`` accepts an int as a raw file descriptor -- ``1`` is stdout -- so passing it
    through would open, then (on the ``with`` block's exit) CLOSE the hook's own stdout
    before ``stop`` ever gets to print its result, turning a should-be-benign case into an
    unrecoverable crash instead of the usual degrade-to-``{}``.

    Runs the hook in a real subprocess with real (pipe-backed) stdio -- unlike every other
    test here, which calls ``hooks.stop()`` in-process under pytest's own stdout capture.
    That capture relocates ``sys.stdout`` off of literal fd 1 (verified: its ``fileno()`` is
    some other number entirely under ``capsys``/``capfd``), so closing fd 1 in-process is
    inert and this bug cannot be reproduced there -- only a real subprocess, where fd 1 IS
    the hook's actual stdout (exactly how Claude Code invokes it), exhibits the crash.
    """
    db = tmp_path / "s.db"
    payload = json.dumps({"session_id": "g11", "stop_hook_active": False, "transcript_path": 1})
    env = {**os.environ, "SIDEGRAPH_DIR": str(db)}
    result = subprocess.run(
        [sys.executable, "-c", "from sidegraph.host.hooks import stop; stop()"],
        input=payload,
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {}


# -- off switch (E2) --------------------------------------------------------------------------


def test_capture_nudge_off_switch_suppresses_without_marking(tmp_path, monkeypatch, capsys):
    db = tmp_path / "s.db"
    transcript = _substantial(tmp_path)
    monkeypatch.setenv("SIDEGRAPH_CAPTURE_NUDGE", "off")
    out = _run(
        monkeypatch,
        capsys,
        {"session_id": "g9", "stop_hook_active": False, "transcript_path": transcript},
        db,
    )
    assert out == {}
    from sidegraph.store import Store

    assert Store(db).was_captured("g9") is False


def test_capture_nudge_off_switch_requires_exact_match(tmp_path, monkeypatch, capsys):
    db = tmp_path / "s.db"
    transcript = _substantial(tmp_path)
    monkeypatch.setenv("SIDEGRAPH_CAPTURE_NUDGE", "true")
    out = _run(
        monkeypatch,
        capsys,
        {"session_id": "g10", "stop_hook_active": False, "transcript_path": transcript},
        db,
    )
    assert out["decision"] == "block"


# -- CLAUDE_PROJECT_DIR anchoring (plugin hooks may run with an arbitrary cwd) --------------


def test_stop_hook_opens_store_under_claude_project_dir_for_relative_path(
    tmp_path, monkeypatch, capsys
):
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project_dir))
    monkeypatch.setenv("SIDEGRAPH_DIR", "s.db")
    transcript = _substantial(tmp_path)
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO(
            json.dumps(
                {"session_id": "s4", "stop_hook_active": False, "transcript_path": transcript}
            )
        ),
    )
    hooks.stop()
    out = json.loads(capsys.readouterr().out)
    assert out["decision"] == "block"
    assert (project_dir / "s.db").exists()


def test_stop_hook_absolute_db_unaffected_by_claude_project_dir(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path / "unrelated-root"))
    db = tmp_path / "abs.db"
    transcript = _substantial(tmp_path)
    out = _run(
        monkeypatch,
        capsys,
        {"session_id": "s5", "stop_hook_active": False, "transcript_path": transcript},
        db,
    )
    assert out["decision"] == "block"
    assert db.exists()
    assert not (tmp_path / "unrelated-root").exists()


def test_stop_hook_relative_db_resolves_against_cwd_without_claude_project_dir(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SIDEGRAPH_DIR", "rel.db")
    transcript = _substantial(tmp_path)
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO(
            json.dumps(
                {"session_id": "s6", "stop_hook_active": False, "transcript_path": transcript}
            )
        ),
    )
    hooks.stop()
    out = json.loads(capsys.readouterr().out)
    assert out["decision"] == "block"
    assert (tmp_path / "rel.db").exists()


# -- SIDEGRAPH_DB back-compat end-to-end (deprecated, but still honored -- design §5) ------


def test_stop_hook_honors_legacy_sidegraph_db_with_deprecation_notice(
    tmp_path, monkeypatch, capsys
):
    """The hooks actually route through ``config.resolve_store_path`` too -- not just the
    CLI/server -- so a project still configured with the legacy ``SIDEGRAPH_DB=<dir>/
    decisions.db`` snippet keeps working (dispatch rescues the nonexistent-file-in-a-dir
    case to the parent dir) and prints the one-line deprecation notice."""
    import sidegraph.config as config

    monkeypatch.setattr(config, "_deprecation_warned", False)
    monkeypatch.delenv("SIDEGRAPH_DIR", raising=False)
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SIDEGRAPH_DB", ".sidegraph/decisions.db")
    transcript = _substantial(tmp_path)
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO(
            json.dumps(
                {"session_id": "s7", "stop_hook_active": False, "transcript_path": transcript}
            )
        ),
    )
    hooks.stop()
    captured = capsys.readouterr()
    out = json.loads(captured.out)
    assert out["decision"] == "block"
    # rescued to the PARENT (.sidegraph), never a directory literally named decisions.db
    assert (tmp_path / ".sidegraph" / "format").exists()
    assert not (tmp_path / ".sidegraph" / "decisions.db").exists()

    assert "SIDEGRAPH_DB is deprecated" in captured.err
    assert "SIDEGRAPH_DIR" in captured.err


# -- Codex rollouts (the capture gate reads the host's own transcript shape) -----------------
#
# A Codex rollout is not a Claude Code transcript: no `type: "user"` lines, no `entrypoint`, no
# `assistant` lines. The helpers below build the shape a real rollout has. Until they existed
# the only "Codex" fixture was a Claude-shaped transcript with a rollout-looking name, which
# is why the gate counted zero prompts on every real Codex session unnoticed.

_CODEX_UMBRELLA = "01a0b3e2-39d2-7180-86a5-408a6f9ce058"
_CODEX_TS = "2026-09-18T09:31:13.124Z"
_CODEX_TUI = {"thread_source": "user", "originator": "codex-tui"}


class _ImagePrompt(NamedTuple):
    """A prompt with a pasted image: Codex writes four content blocks for it, and sometimes
    mirrors it as a `UserMessage` event (`mirrored=True`)."""

    text: str
    mirrored: bool = False


def _codex_rollout(
    tmp_path,
    name,
    *,
    metas=(_CODEX_TUI,),
    prompts=(),
    injected=(),
    replies=(),
    tool_calls=0,
):
    """Write a Codex rollout (`~/.codex/sessions/…/rollout-*.jsonl`) and return its path.

    `metas` are `session_meta` payload overrides, one line each (a forked subagent carries its
    own meta first and its parent's second); a key that is absent stays absent, so `{}` is an
    older Codex with no `thread_source`. `prompts` are a person's turns, each a `str` or an
    `_ImagePrompt`; `injected` are user-role messages Codex wrote itself, each a `str` or a
    tuple of `str` blocks; `replies` are answers to an agent's question. Every prompt is a
    `response_item` followed by its `event_msg` `item_completed` mirror (`UserMessage`; a
    `<hook_prompt` injection is mirrored as `HookPrompt`): the gate must count each turn once.
    Codex mirrors the other user messages only sometimes (33 of 116 question replies and 24 of
    40 image prompts sit beside a `UserMessage`; no injected project instructions do), so the
    fixture writes no mirror for injections and replies, and one for an image prompt only when
    its `_ImagePrompt` is `mirrored`.

    Real lines of every kind, redacted (paths, ids and account fields replaced; long fields
    elided with …; wrapped for width here, one line each in a rollout). Measured on
    rollouts of codex-cli 0.135 to 0.159.3, 2026-09 and 2026-10.

    session_meta, a person's thread (a subagent's has "source":{"subagent":{"thread_spawn":
    {"parent_thread_id":"…","depth":1,"agent_path":"/root/worker","agent_nickname":"…",
    "agent_role":"worker"}}} and "thread_source":"subagent"; Codex's automatic reviewer has
    "source":{"subagent":{"other":"guardian"}} and "thread_source":"guardian_review"):

        {"timestamp":"2026-09-18T09:31:07.106Z","ordinal":0,"type":"session_meta","payload":{
        "session_id":"01a0b3da-…","id":"01a0b3da-…","timestamp":"2026-09-18T09:30:54.133Z",
        "cwd":"/work/project","originator":"codex-tui","cli_version":"0.154.0","source":"cli",
        "thread_source":"user","model_provider":"openai","base_instructions":{"text":"…"},
        "history_mode":"paginated"}}

    a person's prompt, then its mirror:

        {"timestamp":"2026-09-18T09:31:13.124Z","ordinal":9,"type":"response_item","payload":{
        "type":"message","id":"msg_01a0b3db-…","role":"user","content":[{"type":"input_text",
        "text":"check whether sidegraph works for us"}],
        "internal_chat_message_metadata_passthrough":{"turn_id":"01a0b3da-…",
        "create_time":1789723873.124257,"content_item_kinds":["user.text"]}}}
        {"timestamp":"2026-09-18T09:31:13.124Z","ordinal":10,"type":"event_msg","payload":{
        "type":"item_completed","thread_id":"01a0b3da-…","turn_id":"01a0b3da-…","item":{
        "type":"UserMessage","id":"01a0b3db-…","client_id":"f02a900c-…","content":[
        {"type":"text","text":"check whether sidegraph works for us","text_elements":[]}]},
        "started_at_ms":1789723873124,"completed_at_ms":1789723873124}}

    the injected project instructions (two blocks, no mirror):

        {"timestamp":"2026-09-18T09:31:08.509Z","ordinal":5,"type":"response_item","payload":{
        "type":"message","id":"msg_01a0b3da-…","role":"user","content":[{"type":"input_text",
        "text":"# AGENTS.md instructions\\n\\n<INSTRUCTIONS>\\n…"},{"type":"input_text",
        "text":"<environment_context>\\n  <cwd>/work/project</cwd>…"}],
        "internal_chat_message_metadata_passthrough":{"turn_id":"01a0b3da-…",
        "create_time":1789723868.450406,"content_item_kinds":["agents_md.instructions",
        "environments.environment_context"]}}}

    a Stop hook's block reason, written back as a user message, and its mirror:

        {"timestamp":"2026-10-01T19:22:02.510Z","ordinal":31,"type":"response_item","payload":{
        "type":"message","id":"msg_01a0f8ea-…","role":"user","content":[{"type":"input_text",
        "text":"<hook_prompt hook_run_id=\\"stop:2:/work/project/.codex/hooks.json\\">…
        </hook_prompt>"}],"internal_chat_message_metadata_passthrough":{"turn_id":"01a0f8ea-…",
        "create_time":1790882522.510235,"content_item_kinds":["unknown"]},"metadata":{
        "client_authored":false,"user_input_order":6}}}
        {"timestamp":"2026-10-01T19:22:02.511Z","ordinal":32,"type":"event_msg","payload":{
        "type":"item_completed","thread_id":"01a0f8ea-…","turn_id":"01a0f8ea-…","item":{
        "type":"HookPrompt","id":"msg_01a0f8ea-…","fragments":[{"text":"REASON",
        "hookRunId":"stop:2:/work/project/.codex/hooks.json"}]},
        "started_at_ms":1790882522511,"completed_at_ms":1790882522511}}

    an answer to an agent's question, a person's reply but mid-turn:

        {"timestamp":"2026-09-28T18:06:53.938Z","ordinal":1,"type":"response_item","payload":{
        "type":"message","id":"msg_01a0e919-…","role":"user","content":[{"type":"input_text",
        "text":"<send_user_message_question_reply>\\n[{\\"answer\\":\\"…\\",\\"question\\":\\"…\\"}]"}],
        "internal_chat_message_metadata_passthrough":{"turn_id":"01a0e8db-…",
        "create_time":1790617167.519117,"content_item_kinds":["user.text"]}}}

    a prompt with a pasted image (the prompt text is the last block):

        {"timestamp":"2026-09-24T08:46:31.178Z","ordinal":30,"type":"response_item","payload":{
        "type":"message","id":"msg_01a0d293-…","role":"user","content":[{"type":"input_text",
        "text":"<image name=[Image #1] path=\\"/tmp/codex-clipboard-XXXXXX.png\\">"},
        {"type":"input_image","image_url":"data:image/png;base64,…","detail":"high"},
        {"type":"input_text","text":"</image>"},
        {"type":"input_text","text":"[Image #1] an example in the screenshot"}],
        "internal_chat_message_metadata_passthrough":{"turn_id":"01a0d26a-…",
        "create_time":1790239276.173001,"content_item_kinds":["user.text","user.image",
        "user.text","user.text"]}}}

    its mirror, when it has one (the text carries the placeholder after the prompt):

        {"timestamp":"2026-09-21T20:39:22.018Z","ordinal":2245,"type":"event_msg","payload":{
        "type":"item_completed","thread_id":"01a0badb-…","turn_id":"01a0c59e-…","item":{
        "type":"UserMessage","id":"01a0c5b1-…","client_id":"7ded235d-…","content":[
        {"type":"local_image","path":"/tmp/pasted-image-XXXX.png"},{"type":"text",
        "text":"an example in the screenshot\\n\\n[Image #1]","text_elements":[
        {"byte_range":{"start":30,"end":40},"placeholder":"[Image #1]"}]}]},
        "started_at_ms":1790023162018,"completed_at_ms":1790023162018}}

    and a tool call:

        {"timestamp":"2026-09-17T12:55:32.479Z","ordinal":64,"type":"response_item","payload":{
        "type":"function_call","id":"fc_007de1db…","name":"request_user_input_async",
        "arguments":"{\\"questions\\":[…]}","call_id":"call_R2GFaM7k…"}}
    """
    entries = []

    def emit(kind, payload):
        entries.append(
            {"timestamp": _CODEX_TS, "ordinal": len(entries), "type": kind, "payload": payload}
        )

    def user_message(blocks):
        emit(
            "response_item",
            {
                "type": "message",
                "id": f"msg_01a0b3db-{len(entries):04d}",
                "role": "user",
                "content": blocks,
            },
        )

    def mirror(item):
        emit(
            "event_msg",
            {
                "type": "item_completed",
                "thread_id": "01a0b3da-c522-7af2-8a6d-2e1aed53b269",
                "turn_id": "01a0b3da-f503-7b40-814f-8b7cbda5ef8e",
                "item": item,
                "started_at_ms": 1789723873124,
                "completed_at_ms": 1789723873124,
            },
        )

    def text_block(text):
        return {"type": "input_text", "text": text}

    for i, meta in enumerate(metas):
        thread = f"01a0b3da-c522-7af2-8a6d-2e1aed53b2{i:02d}"
        emit(
            "session_meta",
            {
                "session_id": thread,
                "id": thread,
                "timestamp": "2026-09-18T09:30:54.133Z",
                "cwd": "/work/project",
                "originator": "codex-tui",
                "cli_version": "0.159.3",
                "source": "cli",
                "model_provider": "openai",
                "base_instructions": {"text": "You are Codex, an agent based on GPT-5."},
                **meta,
            },
        )
    for item in injected:
        blocks = (item,) if isinstance(item, str) else tuple(item)
        user_message([text_block(b) for b in blocks])
        if blocks[0].startswith("<hook_prompt"):
            mirror(
                {
                    "type": "HookPrompt",
                    "id": f"msg_01a0b3db-{len(entries):04d}",
                    "fragments": [{"text": "REASON", "hookRunId": "stop:2:/work/project"}],
                }
            )
    for reply in replies:
        user_message(
            [
                text_block(
                    f'<send_user_message_question_reply>\n[{{"answer":"{reply}","question":"?"}}]'
                )
            ]
        )
    for prompt in prompts:
        if isinstance(prompt, _ImagePrompt):
            user_message(
                [
                    text_block('<image name=[Image #1] path="/tmp/codex-clipboard-XXXXXX.png">'),
                    {
                        "type": "input_image",
                        "image_url": "data:image/png;base64,iVBOR",
                        "detail": "high",
                    },
                    text_block("</image>"),
                    text_block(f"[Image #1] {prompt.text}"),
                ]
            )
            if prompt.mirrored:
                mirror(
                    {
                        "type": "UserMessage",
                        "id": f"01a0b3db-{len(entries):04d}",
                        "client_id": "7ded235d-08b4-4311-9e49-f0638c52fdc8",
                        "content": [
                            {"type": "local_image", "path": "/tmp/pasted-image-XXXX.png"},
                            {
                                "type": "text",
                                "text": f"{prompt.text}\n\n[Image #1]",
                                "text_elements": [
                                    {
                                        "byte_range": {"start": 30, "end": 40},
                                        "placeholder": "[Image #1]",
                                    }
                                ],
                            },
                        ],
                    }
                )
            continue
        user_message([text_block(prompt)])
        mirror(
            {
                "type": "UserMessage",
                "id": f"01a0b3db-{len(entries):04d}",
                "client_id": "f02a900c-5681-45fe-b3a0-e4e4d71f623f",
                "content": [{"type": "text", "text": prompt, "text_elements": []}],
            }
        )
    for i in range(tool_calls):
        emit(
            "response_item",
            {
                "type": "function_call",
                "id": f"fc_{i:04d}",
                "name": "exec_command",
                "arguments": '{"cmd":"ls"}',
                "call_id": f"call_{i:04d}",
            },
        )
    return _write_transcript(tmp_path / name, entries)


def _codex_stop(tmp_path, monkeypatch, capsys, name, **rollout):
    """Run the Stop hook over a Codex rollout in a store of its own; return what it printed."""
    return _run(
        monkeypatch,
        capsys,
        {
            "session_id": _CODEX_UMBRELLA,
            "stop_hook_active": False,
            "transcript_path": _codex_rollout(tmp_path, name, **rollout),
        },
        tmp_path / f"{name}.db",
    )


def test_each_codex_thread_earns_its_own_capture_nudge(tmp_path, monkeypatch, capsys):
    """The capture ledger is per session, and Codex reports one umbrella `session_id` for
    every thread under a workspace (hooks._session_identity). Red against reading that field
    directly: the first thread to finish spent the nudge for all of them — a day of Codex
    threads got one capture prompt between them. Built from real rollouts (`_codex_rollout`),
    so it also fails while the gate cannot read a rollout at all."""
    db = tmp_path / "s.db"
    outs = []
    for thread in (
        "rollout-2026-09-19T12-00-31-01a0b953-2d7c",
        "rollout-2026-09-19T12-16-06-01a0b961-7463",
    ):
        outs.append(
            _run(
                monkeypatch,
                capsys,
                {
                    "session_id": _CODEX_UMBRELLA,
                    "stop_hook_active": False,
                    "transcript_path": _codex_rollout(
                        tmp_path, f"{thread}.jsonl", prompts=("first request", "second request")
                    ),
                },
                db,
            )
        )

    assert [out.get("decision") for out in outs] == ["block", "block"], outs


def test_codex_tui_thread_with_two_prompts_arms_the_gate(tmp_path, monkeypatch, capsys):
    """Spec T1: the bug. Every Codex session counted 0 prompts and never armed."""
    out = _codex_stop(
        tmp_path, monkeypatch, capsys, "t1.jsonl", prompts=("first request", "second request")
    )
    assert out.get("decision") == "block", out
    assert "propose_decisions" in out["reason"]


# One message per injected marker the gate must not count as a prompt, each spelled out here
# (not read back from hooks.py: a marker dropped from the tuple must make a case below fail).
_CODEX_INJECTED_MESSAGES = {
    "agents": (
        "# AGENTS.md instructions for /work/project\n\n<INSTRUCTIONS>\nuse uv\n</INSTRUCTIONS>",
        "<environment_context>\n  <cwd>/work/project</cwd>\n</environment_context>",
    ),
    "environment_context": (
        "<environment_context>\n  <cwd>/work/project</cwd>\n</environment_context>"
    ),
    "recommended_plugins": "<recommended_plugins>\nHere is a list of plugins that are available.",
    "skill": "<skill>\n<name>writing-plans</name>\n<path>/work/skills/SKILL.md</path>\n</skill>",
    "turn_aborted": "<turn_aborted>\nThe user interrupted the previous turn on purpose.",
    "user_instructions": "<user_instructions>\nAlways answer briefly.\n</user_instructions>",
    "codex_internal_context": (
        '<codex_internal_context source="daemon_recovery">\nThe server restarted.\n'
        "</codex_internal_context>"
    ),
    "leading_whitespace": "\n  <environment_context>\n  <cwd>/work/project</cwd>",
    "claude_command_name": "<command-name>/clear</command-name>\n<command-message>clear",
    "claude_local_stdout": "<local-command-stdout>ok</local-command-stdout>",
    "claude_local_caveat": "<local-command-caveat>Caveat: the messages below",
}


@pytest.mark.parametrize("kind", sorted(_CODEX_INJECTED_MESSAGES))
def test_codex_injected_user_messages_do_not_count(tmp_path, monkeypatch, capsys, kind):
    """Spec T2: Codex writes its own user-role messages into the rollout (project
    instructions, environment, plugin and skill blocks, an aborted-turn note). Red against
    counting every user-role message (M1): the `# AGENTS.md` message alone is 563 of the 6,200
    user messages measured, so nearly every thread would arm on its first stop. The control
    shows the same injection does not hide a second real prompt."""
    injected = _CODEX_INJECTED_MESSAGES[kind]
    out = _codex_stop(
        tmp_path,
        monkeypatch,
        capsys,
        f"t2-{kind}.jsonl",
        prompts=("only request",),
        injected=(injected,),
    )
    assert out == {}, (kind, out)
    out = _codex_stop(
        tmp_path,
        monkeypatch,
        capsys,
        f"t2c-{kind}.jsonl",
        prompts=("first request", "second request"),
        injected=(injected,),
    )
    assert out.get("decision") == "block", (kind, out)


def test_codex_hook_prompt_feedback_is_not_a_prompt(tmp_path, monkeypatch, capsys):
    """Spec T2b: Codex writes a Stop hook's block reason back as a user `<hook_prompt …>`
    message (its equivalent of Claude's `isMeta`), and the tag carries an attribute, so the
    marker is a prefix without the closing `>`. One review rollout held 283 of them, fed back
    from Sidegraph's own failing Stop hook. Red against a marker list without it (M1b)."""
    feedback = (
        '<hook_prompt hook_run_id="stop:2:/work/project/.codex/hooks.json">'
        "Sidegraph: if this session produced a durable decision…</hook_prompt>"
    )
    out = _codex_stop(
        tmp_path,
        monkeypatch,
        capsys,
        "t2b.jsonl",
        prompts=("only request",),
        injected=(feedback, feedback, feedback),
    )
    assert out == {}, out
    out = _codex_stop(
        tmp_path,
        monkeypatch,
        capsys,
        "t2bc.jsonl",
        prompts=("first request", "second request"),
        injected=(feedback, feedback, feedback),
    )
    assert out.get("decision") == "block", out


def test_codex_question_reply_is_not_a_prompt(tmp_path, monkeypatch, capsys):
    """Spec T3: an answer to the agent's own question arrives mid-turn, as the Claude Code
    `AskUserQuestion` answer arrives as a `tool_result`, which the gate never counts. 33 of 33
    replies in user threads were mid-turn. Red against counting replies (M1c)."""
    out = _codex_stop(
        tmp_path, monkeypatch, capsys, "t3.jsonl", prompts=("only request",), replies=("yes",)
    )
    assert out == {}, out
    out = _codex_stop(
        tmp_path,
        monkeypatch,
        capsys,
        "t3c.jsonl",
        prompts=("first request", "second request"),
        replies=("yes",),
    )
    assert out.get("decision") == "block", out


def test_codex_image_prompt_counts_once_by_its_first_block(tmp_path, monkeypatch, capsys):
    """Spec T3b: a pasted image makes the message `<image …>`, an image, `</image>`, and only
    then the prompt text. It is a person's prompt (the first `input_text` block is not an
    injected marker) and counts once, not once per block. Red against treating `<image` as an
    injected marker (M6), or against counting blocks."""
    out = _codex_stop(
        tmp_path,
        monkeypatch,
        capsys,
        "t3b.jsonl",
        prompts=(_ImagePrompt("an example in the screenshot"), "second request"),
    )
    assert out.get("decision") == "block", out
    out = _codex_stop(
        tmp_path,
        monkeypatch,
        capsys,
        "t3b1.jsonl",
        prompts=(_ImagePrompt("an example in the screenshot"),),
    )
    assert out == {}, out


def test_codex_message_without_an_input_text_block_does_not_count(tmp_path, monkeypatch, capsys):
    """A user message that is only an image holds no `input_text` block to classify."""
    path = _codex_rollout(tmp_path, "t3n.jsonl", prompts=("only request",))
    with open(path, "a") as fh:
        fh.write(
            json.dumps(
                {
                    "timestamp": _CODEX_TS,
                    "ordinal": 99,
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "user",
                        "content": [
                            {"type": "input_image", "image_url": "data:image/png;base64,x"}
                        ],
                    },
                }
            )
            + "\n"
        )
    out = _run(
        monkeypatch,
        capsys,
        {"session_id": _CODEX_UMBRELLA, "stop_hook_active": False, "transcript_path": path},
        tmp_path / "t3n.db",
    )
    assert out == {}, out


def test_codex_subagent_thread_never_arms(tmp_path, monkeypatch, capsys):
    """Spec T4: Codex fires `SubagentStop`, not `Stop`, for a subagent, so this is defence in
    depth for other versions and forks. Red against a gate without the thread check (M2). The
    control is the same rollout as a person's thread."""
    out = _codex_stop(
        tmp_path,
        monkeypatch,
        capsys,
        "t4.jsonl",
        metas=({"thread_source": "subagent", "originator": "codex-tui"},),
        prompts=("a", "b", "c", "d", "e"),
    )
    assert out == {}, out
    out = _codex_stop(tmp_path, monkeypatch, capsys, "t4c.jsonl", prompts=("a", "b", "c", "d", "e"))
    assert out.get("decision") == "block", out


def test_codex_guardian_review_thread_never_arms(tmp_path, monkeypatch, capsys):
    """Spec T5: Codex's automatic reviewer (`guardian_review`) is 412 of the 768 rollouts
    measured, and no person works in it. Red against a gate without the thread check (M2)."""
    out = _codex_stop(
        tmp_path,
        monkeypatch,
        capsys,
        "t5.jsonl",
        metas=({"thread_source": "guardian_review", "originator": "codex-tui"},),
        prompts=("a", "b", "c", "d", "e", "f"),
    )
    assert out == {}, out
    out = _codex_stop(
        tmp_path, monkeypatch, capsys, "t5c.jsonl", prompts=("a", "b", "c", "d", "e", "f")
    )
    assert out.get("decision") == "block", out


def test_codex_subagent_source_without_a_thread_source_never_arms(tmp_path, monkeypatch, capsys):
    """Spec T5b: a meta whose `source` is `{"subagent": …}` marks a subagent even when the
    version writes no `thread_source`. Red against a gate that ignores `source` (M2b)."""
    out = _codex_stop(
        tmp_path,
        monkeypatch,
        capsys,
        "t5b.jsonl",
        metas=({"source": {"subagent": {"other": "x"}}},),
        prompts=("a", "b", "c"),
    )
    assert out == {}, out
    out = _codex_stop(
        tmp_path,
        monkeypatch,
        capsys,
        "t5bc.jsonl",
        metas=({"source": "cli"},),
        prompts=("a", "b", "c"),
    )
    assert out.get("decision") == "block", out


# A headless run is told apart by either field, and each case isolates one: the real `codex
# exec` run carries both; the Codex TypeScript SDK runs `codex exec` under its own originator
# (CODEX_INTERNAL_ORIGINATOR_OVERRIDE), so only `source` marks it; `originator` alone covers a
# build that writes no `source` string. All 149 `codex_exec` rollouts measured carry
# `source: "exec"`, and no person's thread does (`cli`, `vscode`).
@pytest.mark.parametrize(
    ("originator", "source"),
    [("codex_exec", "exec"), ("codex_exec", "cli"), ("codex_sdk_ts", "exec")],
    ids=["exec-both", "originator-only", "source-only-sdk"],
)
def test_codex_exec_run_never_arms_even_with_tool_work(
    tmp_path, monkeypatch, capsys, originator, source
):
    """Spec T6: a headless `codex exec` run is a script or a review panel, and a Stop block
    makes it continue and replaces its `-o` verdict (or an SDK caller's result) with the
    continuation. 33 of 39 `codex_exec` user runs in a field-report corpus would have armed
    under a one-shot rule. Neither prompts nor tool calls arm it. Red against arming
    `codex_exec` (M3) and against a gate that reads only `originator` (M7: the SDK's resumed
    thread with two prompts armed). The control is an interactive thread with the same
    prompts, and one that carries the same originator but a person's `source`."""
    out = _codex_stop(
        tmp_path,
        monkeypatch,
        capsys,
        f"t6-{originator}-{source}.jsonl",
        metas=({"thread_source": "user", "originator": originator, "source": source},),
        prompts=("a", "b", "c"),
        tool_calls=20,
    )
    assert out == {}, out
    out = _codex_stop(
        tmp_path, monkeypatch, capsys, "t6c.jsonl", prompts=("a", "b", "c"), tool_calls=20
    )
    assert out.get("decision") == "block", out


def test_codex_sdk_originator_with_a_persons_source_is_a_persons_thread(
    tmp_path, monkeypatch, capsys
):
    """Spec T6b: the headless rule is not "any non-TUI originator": an SDK-originated thread
    whose `source` is a person's (`vscode`) still arms. Red against treating `codex_sdk_ts`
    as headless."""
    out = _codex_stop(
        tmp_path,
        monkeypatch,
        capsys,
        "t6b.jsonl",
        metas=({"thread_source": "user", "originator": "codex_sdk_ts", "source": "vscode"},),
        prompts=("a", "b", "c"),
    )
    assert out.get("decision") == "block", out


def test_codex_tool_calls_alone_do_not_arm_an_interactive_thread(tmp_path, monkeypatch, capsys):
    """Spec D2: Codex gets no tool-use branch; `sdk-cli` stays Claude-only."""
    out = _codex_stop(
        tmp_path, monkeypatch, capsys, "t6t.jsonl", prompts=("only request",), tool_calls=40
    )
    assert out == {}, out


def test_codex_mirrored_event_messages_are_not_counted_twice(tmp_path, monkeypatch, capsys):
    """Spec T7: Codex mirrors a prompt as an `event_msg` `item_completed` line. One prompt
    with its mirror is one prompt, a plain one or one with a pasted image: red against
    counting both (M5)."""
    path = _codex_rollout(tmp_path, "t7.jsonl", prompts=("only request",))
    text = Path(path).read_text()
    assert '"type": "event_msg"' in text and '"type": "UserMessage"' in text  # the fixture mirrors
    out = _run(
        monkeypatch,
        capsys,
        {"session_id": _CODEX_UMBRELLA, "stop_hook_active": False, "transcript_path": path},
        tmp_path / "t7.db",
    )
    assert out == {}, out
    out = _codex_stop(
        tmp_path, monkeypatch, capsys, "t7c.jsonl", prompts=("first request", "second request")
    )
    assert out.get("decision") == "block", out
    # An image prompt with its mirror is one prompt as well, not two.
    image = _ImagePrompt("an example in the screenshot", mirrored=True)
    path = _codex_rollout(tmp_path, "t7i.jsonl", prompts=(image,))
    assert '"type": "local_image"' in Path(path).read_text()  # the fixture mirrors it
    out = _run(
        monkeypatch,
        capsys,
        {"session_id": _CODEX_UMBRELLA, "stop_hook_active": False, "transcript_path": path},
        tmp_path / "t7i.db",
    )
    assert out == {}, out
    out = _codex_stop(
        tmp_path, monkeypatch, capsys, "t7ic.jsonl", prompts=(image, "second request")
    )
    assert out.get("decision") == "block", out


def test_codex_without_a_thread_source_counts_as_a_persons_thread(tmp_path, monkeypatch, capsys):
    """Spec T9: an older Codex writes no `thread_source`. Red against a gate that requires
    the field to arm anything."""
    out = _codex_stop(
        tmp_path,
        monkeypatch,
        capsys,
        "t9.jsonl",
        metas=({"originator": "codex-tui"},),
        prompts=("first request", "second request"),
    )
    assert out.get("decision") == "block", out


def test_codex_fork_reads_the_first_session_meta(tmp_path, monkeypatch, capsys):
    """Spec T10: a forked subagent carries its own meta first and its parent's second, so
    the first one wins. Red against letting the last meta win (M4). The control is the
    reverse order: a person's thread that carries a subagent's meta second."""
    subagent = {"thread_source": "subagent", "originator": "codex-tui"}
    user = {"thread_source": "user", "originator": "codex-tui"}
    out = _codex_stop(
        tmp_path, monkeypatch, capsys, "t10.jsonl", metas=(subagent, user), prompts=("a", "b", "c")
    )
    assert out == {}, out
    out = _codex_stop(
        tmp_path, monkeypatch, capsys, "t10c.jsonl", metas=(user, subagent), prompts=("a", "b", "c")
    )
    assert out.get("decision") == "block", out


def test_codex_odd_session_meta_never_raises(tmp_path, monkeypatch, capsys):
    """Hooks never raise: a meta with fields of the wrong type reads as a person's thread, and
    a `session_meta` line with no object payload is skipped."""
    out = _codex_stop(
        tmp_path,
        monkeypatch,
        capsys,
        "todd.jsonl",
        metas=({"thread_source": 5, "originator": ["x"], "source": 7},),
        prompts=("first request", "second request"),
    )
    assert out.get("decision") == "block", out

    path = _write_transcript(
        tmp_path / "tnopayload.jsonl",
        [{"type": "session_meta", "payload": ["not", "an", "object"]}],
    )
    out = _run(
        monkeypatch,
        capsys,
        {"session_id": _CODEX_UMBRELLA, "stop_hook_active": False, "transcript_path": path},
        tmp_path / "tnopayload.db",
    )
    assert out == {}, out


# -- the ledger peek before the transcript parse ---------------------------------------------


def test_stop_skips_transcript_parse_for_a_captured_session(tmp_path, monkeypatch, capsys):
    from sidegraph.store import Store

    db = tmp_path / "s.db"
    transcript = _substantial(tmp_path, name="s1.jsonl")
    Store(db).mark_captured("s1")
    seen = []

    def _recorder(path):
        seen.append(path)
        raise RuntimeError("must not be parsed")

    monkeypatch.setattr(hooks, "_transcript_stats", _recorder)
    out = _run(
        monkeypatch,
        capsys,
        {"session_id": "s1", "stop_hook_active": False, "transcript_path": transcript},
        db,
    )
    assert seen == []
    assert out == {}


def test_stop_peek_none_falls_back(tmp_path, monkeypatch, capsys):
    from sidegraph.store import Store

    db = tmp_path / "s.db"
    Store(db)  # without it the real open_index_ro returns None anyway
    transcript = _substantial(tmp_path, name="s1.jsonl")
    monkeypatch.setattr("sidegraph.gitio.open_index_ro", lambda _d: None)
    out = _run(
        monkeypatch,
        capsys,
        {"session_id": "s1", "stop_hook_active": False, "transcript_path": transcript},
        db,
    )
    assert out["decision"] == "block"


def test_zero_byte_index_still_nudges(tmp_path, monkeypatch, capsys):
    db = tmp_path / "s.db"
    db.mkdir()
    (db / "index.db").write_bytes(b"")
    transcript = _substantial(tmp_path, name="s1.jsonl")
    out = _run(
        monkeypatch,
        capsys,
        {"session_id": "s1", "stop_hook_active": False, "transcript_path": transcript},
        db,
    )
    assert out["decision"] == "block"


def test_stop_peek_uses_the_ledger_key_not_the_umbrella_id(tmp_path, monkeypatch, capsys):
    """Codex reports one umbrella `session_id`; the ledger is keyed by the rollout stem
    (`hooks._session_identity`). The peek must query that key, or a captured thread is
    re-parsed on every later Stop. Red against querying `payload["session_id"]`."""
    from sidegraph.store import Store

    db = tmp_path / "s.db"
    transcript = _substantial(tmp_path, name="rollout-a.jsonl")
    Store(db).mark_captured("rollout-a")
    seen = []

    def _recorder(path):
        seen.append(path)
        raise RuntimeError("must not be parsed")

    monkeypatch.setattr(hooks, "_transcript_stats", _recorder)
    out = _run(
        monkeypatch,
        capsys,
        {"session_id": "umbrella", "stop_hook_active": False, "transcript_path": transcript},
        db,
    )
    assert seen == []
    assert out == {}


def test_stop_peek_does_not_create_a_store(tmp_path, monkeypatch, capsys):
    """The peek resolves the store path without the "creating new store" notice and
    creates nothing. Red when `warn_on_create=False` is dropped."""
    for var in ("SIDEGRAPH_DIR", "SIDEGRAPH_DB", "SIDEGRAPH_STORE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO(json.dumps({"session_id": "s1", "stop_hook_active": False})),
    )
    hooks.stop()
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {}
    assert captured.err == ""
    assert not (tmp_path / ".sidegraph").exists()
