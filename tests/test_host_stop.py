import io
import json
import os
import subprocess
import sys

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
    ``<= 800`` char bound the same design decision commits to."""
    db = tmp_path / "s.db"
    transcript = _substantial(tmp_path)
    out = _run(
        monkeypatch,
        capsys,
        {"session_id": "sd1", "stop_hook_active": False, "transcript_path": transcript},
        db,
    )
    assert "supersede_decision" in out["reason"]
    assert len(out["reason"]) <= 800


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


def test_env_path_relative_anchors_to_claude_project_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    monkeypatch.setenv("SIDEGRAPH_DB", ".sidegraph/decisions.db")
    resolved = hooks._env_path("SIDEGRAPH_DB", "sidegraph.db")
    assert resolved == str(tmp_path / ".sidegraph/decisions.db")


def test_env_path_absolute_value_wins_over_claude_project_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path / "root"))
    abs_db = str(tmp_path / "elsewhere" / "s.db")
    monkeypatch.setenv("SIDEGRAPH_DB", abs_db)
    assert hooks._env_path("SIDEGRAPH_DB", "sidegraph.db") == abs_db


def test_env_path_without_claude_project_dir_matches_prior_behavior(monkeypatch):
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    monkeypatch.delenv("SIDEGRAPH_DB", raising=False)
    assert hooks._env_path("SIDEGRAPH_DB", "sidegraph.db") == "sidegraph.db"


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


def test_each_codex_thread_earns_its_own_capture_nudge(tmp_path, monkeypatch, capsys):
    """The capture ledger is per session, and Codex reports one umbrella `session_id` for
    every thread under a workspace (hooks._session_identity). Red against reading that field
    directly: the first thread to finish spent the nudge for all of them — a day of Codex
    threads got one capture prompt between them."""
    db = tmp_path / "s.db"
    umbrella = "01a0b3e2-39d2-7180-86a5-408a6f9ce058"
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
                    "session_id": umbrella,
                    "stop_hook_active": False,
                    "transcript_path": _substantial(tmp_path, name=f"{thread}.jsonl"),
                },
                db,
            )
        )

    assert [out.get("decision") for out in outs] == ["block", "block"], outs
