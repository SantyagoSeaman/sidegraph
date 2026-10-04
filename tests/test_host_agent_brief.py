"""``pre_tool_use`` on ``Agent`` appends the records for the files a brief names (T1-T6).

The parent agent writes the brief a subagent starts from, so the records for the files that brief
names can ride in it: a PreToolUse hook on ``Agent`` (and ``Task``, the older name) returns the
tool input with the block appended to ``prompt``, as ``updatedInput``. The tests drive the hook
in-process through ``pre_tool_use`` over a synthetic project: a repo root with a ``.sidegraph``
store, files on disk and records anchored to them. The block's grouped layout, the caps (two
records per file, six lines, 3,000 characters, whole files only) and the guards (the opening
marker, the kill switch, a prompt that is not a string) are pinned byte for byte.

see design/superpowers/specs/2026-10-03-records-in-subagent-briefs-design.md (D1-D4, T1-T6)
"""

from __future__ import annotations

import io
import json
import re

import pytest

import sidegraph.host.hooks as hooks
from sidegraph.schema import DecisionKind, DecisionStatus
from tests.test_host_pretool_records import GUARD, Project, _payload, _run

OPEN = "--- Recorded decisions for the files in this task (added by Sidegraph) ---"
CLOSE = "--- end of Sidegraph records ---"

LONG_TITLE = (
    "A title that runs on well past the ninety character clip so that the clip applies here"
)
LONG_CHOICE = (
    "A choice sentence that runs on well past the hundred and twenty character clip so that the "
    "clip applies to it as well and the line reaches the length of a real record"
)


@pytest.fixture
def project(tmp_path):
    p = Project(tmp_path / "repo")
    yield p
    p.close()


def _agent(
    monkeypatch, capsys, project: Project, prompt, env=None, tool="Agent", cwd=None, **extra
) -> tuple[dict, dict]:
    """The hook's output for an ``Agent`` call with ``prompt``, and the ``tool_input`` sent.
    ``cwd`` is the payload's working directory (the project root when not given)."""
    tool_input = {"description": "d", "subagent_type": "general-purpose", "prompt": prompt, **extra}
    payload = _payload(tool, **tool_input)
    if cwd is not None:
        payload["cwd"] = str(cwd)
    out = _run(monkeypatch, capsys, project, payload, env)
    return out, tool_input


def _block(out: dict, prompt: str) -> str:
    """The text the hook appended to ``prompt``."""
    new = out["hookSpecificOutput"]["updatedInput"]["prompt"]
    assert new.startswith(prompt + "\n\n")
    return new[len(prompt) + 2 :]


def _record_lines(block: str) -> list[str]:
    return [line for line in block.splitlines() if line.startswith("- [")]


def _headers(block: str) -> list[str]:
    return [line for line in block.splitlines() if line.endswith(":") and not line.startswith("-")]


# -- T1: the block, byte for byte --------------------------------------------------------------


def test_t1_an_agent_call_naming_an_anchored_file_gets_the_block_appended(
    project, monkeypatch, capsys
):
    """The prompt is the original, then a blank line, then the block; every other field of the
    tool input comes back as it went in, and no ``permissionDecision`` is set (mutation M1).
    Red while the hook has no Agent branch."""
    payments = project.record(
        "src/payments.py",
        "Never retry a charge",
        choice="The provider double-charges on a retry. Wrap nothing around charge().",
        age_days=1,
    )
    ledger = project.record(
        "src/ledger.py",
        "Amounts are integer cents",
        kind=DecisionKind.CONSTRAINT,
        choice="Store amount_cents as an int",
    )
    prompt = "Fix the refund path in src/payments.py and src/ledger.py."
    out, tool_input = _agent(
        monkeypatch, capsys, project, prompt, run_in_background=True, model="haiku"
    )
    hso = out["hookSpecificOutput"]
    assert set(hso) == {"hookEventName", "updatedInput"}
    assert hso["hookEventName"] == "PreToolUse"
    assert "permissionDecision" not in hso
    block = "\n".join(
        [
            OPEN,
            GUARD,
            "src/payments.py:",
            "- [gotcha] Never retry a charge — The provider double-charges on a retry. "
            f"(id {payments.id})",
            "src/ledger.py:",
            "- [constraint] Amounts are integer cents — Store amount_cents as an int "
            f"(id {ledger.id})",
            'More: get_task_context(files=["src/payments.py", "src/ledger.py"]).',
            CLOSE,
        ]
    )
    assert hso["updatedInput"] == {**tool_input, "prompt": prompt + "\n\n" + block}


def test_t1_the_block_ranks_mistakes_first_and_shows_two_records_per_file(
    project, monkeypatch, capsys
):
    """Three records on one file: the two mistakes are shown (newest first), the ADR is not."""
    rel = "src/store.py"
    adr = project.record(rel, "File-per-record store", kind=DecisionKind.ADR, age_days=0)
    older = project.record(rel, "An older gotcha", age_days=3)
    newer = project.record(rel, "A newer lesson", kind=DecisionKind.LESSON, age_days=1)
    out, _ = _agent(monkeypatch, capsys, project, f"Look at {rel}.")
    lines = _record_lines(_block(out, f"Look at {rel}."))
    assert [re.search(r"\(id (\w+)\)$", line).group(1) for line in lines] == [newer.id, older.id]
    assert adr.id not in "\n".join(lines)


def test_t1_a_proposal_is_marked_unratified(project, monkeypatch, capsys):
    project.record("src/a.py", "A proposal", status=DecisionStatus.PROPOSED)
    out, _ = _agent(monkeypatch, capsys, project, "Fix src/a.py.")
    assert "[unratified]" in _block(out, "Fix src/a.py.")


def test_t1_the_hook_claims_none_of_the_main_agents_keys(project, monkeypatch, capsys):
    """The parent cannot see the block, so claiming its keys would only suppress its own first
    delivery of the file (D1)."""
    project.anchored("src/a.py")
    _agent(monkeypatch, capsys, project, "Fix src/a.py.")
    assert project.meta("pretool_file:%") == {}
    out = _run(
        monkeypatch, capsys, project, _payload("Read", file_path=str(project.root / "src/a.py"))
    )
    assert "Recorded for src/a.py" in out["hookSpecificOutput"]["additionalContext"]


# -- T2: the hop -------------------------------------------------------------------------------


def test_t2_a_file_a_named_plan_names_is_grouped_under_the_plan(project, monkeypatch, capsys):
    """Brief-named files first, then hop files, each labelled with the document that named it
    (mutation M2 drops the hop)."""
    payments = project.record("src/payments.py", "Never retry a charge")
    ledger = project.record("src/ledger.py", "Amounts are integer cents")
    (project.root / "docs").mkdir()
    (project.root / "docs/plan.md").write_text("Step 1: change `src/ledger.py`.\n")
    prompt = "Implement docs/plan.md step 1, and look at src/payments.py."
    out, _ = _agent(monkeypatch, capsys, project, prompt)
    block = _block(out, prompt)
    assert _headers(block) == ["src/payments.py:", "src/ledger.py (named in docs/plan.md):"]
    assert [re.search(r"\(id (\w+)\)$", ln).group(1) for ln in _record_lines(block)] == [
        payments.id,
        ledger.id,
    ]
    assert 'More: get_task_context(files=["src/payments.py", "src/ledger.py"]).' in block


def test_t2_a_plan_alone_is_enough(project, monkeypatch, capsys):
    project.record("src/ledger.py", "Amounts are integer cents")
    (project.root / "docs").mkdir()
    (project.root / "docs/plan.md").write_text("Step 1: change `src/ledger.py`.\n")
    out, _ = _agent(monkeypatch, capsys, project, "Implement docs/plan.md step 1.")
    assert "src/ledger.py (named in docs/plan.md):" in _block(out, "Implement docs/plan.md step 1.")


# -- T2b: the suffix fallback, through the hook -------------------------------------------------


def test_t2b_a_brief_relative_to_a_subdirectory_reaches_the_one_anchored_file(
    project, monkeypatch, capsys
):
    record = project.record("pkg/sub/x.py", "Gotcha of x")
    out, _ = _agent(monkeypatch, capsys, project, "Fix sub/x.py.")
    block = _block(out, "Fix sub/x.py.")
    assert _headers(block) == ["pkg/sub/x.py:"]
    assert record.id in block


def test_t2b_two_anchored_files_with_the_suffix_name_neither(project, monkeypatch, capsys):
    project.record("pkg/sub/x.py", "Gotcha of pkg x")
    project.record("other/sub/x.py", "Gotcha of other x")
    out, _ = _agent(monkeypatch, capsys, project, "Fix sub/x.py.")
    assert out == {}


def test_t2b_a_climbing_token_never_takes_the_fallback(project, monkeypatch, capsys):
    project.record("pkg/sibling/sub/x.py", "Gotcha of x")
    out, _ = _agent(monkeypatch, capsys, project, "Fix ../sibling/sub/x.py.")
    assert out == {}


# -- T3: nothing to say ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "prompt",
    [
        "Refactor the payments module.",
        "See /etc/hosts and ../outside.py",
        "Edit src/plain.py",
        "See https://example.com/src/x.py and v1.2.3, e.g. the ledger.",
        "",
    ],
    ids=["no-path", "outside-the-root", "unanchored-file", "url-version-abbreviation", "empty"],
)
def test_t3_a_brief_that_resolves_to_nothing_prints_an_empty_object(
    project, monkeypatch, capsys, prompt
):
    project.file("src/plain.py")
    project.record("src/x.py", "Gotcha of src/x.py")
    out, _ = _agent(monkeypatch, capsys, project, prompt)
    assert out == {}


def test_t3_a_store_with_no_index_prints_an_empty_object_and_creates_nothing(
    tmp_path, monkeypatch, capsys
):
    root = tmp_path / "bare"
    (root / "src").mkdir(parents=True)
    (root / "src/a.py").write_text("x = 1\n")
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(root))
    monkeypatch.setenv("SIDEGRAPH_DIR", str(root / ".sidegraph"))
    payload = _payload("Agent", prompt="Fix src/a.py.")
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    hooks.pre_tool_use()
    assert json.loads(capsys.readouterr().out) == {}
    assert not (root / ".sidegraph").exists()


# -- T4: the guards ----------------------------------------------------------------------------


def test_t4_a_prompt_that_already_holds_the_opening_marker_is_left_alone(
    project, monkeypatch, capsys
):
    """A brief forwarded from another agent already carries a block (mutation M3)."""
    project.record("src/x.py", "Gotcha of src/x.py")
    prompt = f"Fix src/x.py.\n\n{OPEN}\nsrc/x.py:\n- [gotcha] old\n{CLOSE}"
    out, _ = _agent(monkeypatch, capsys, project, prompt)
    assert out == {}


def test_t4_the_kill_switch_prints_an_empty_object(project, monkeypatch, capsys):
    """``SIDEGRAPH_AGENT_BRIEF=off`` on a prompt that would otherwise get a block (mutation M6);
    no other value has any effect."""
    project.record("src/x.py", "Gotcha of src/x.py")
    off, _ = _agent(monkeypatch, capsys, project, "Fix src/x.py.", {"SIDEGRAPH_AGENT_BRIEF": "off"})
    assert off == {}
    on, _ = _agent(monkeypatch, capsys, project, "Fix src/x.py.", {"SIDEGRAPH_AGENT_BRIEF": "on"})
    assert "hookSpecificOutput" in on


def test_t4_the_other_switches_do_not_reach_the_agent_branch(project, monkeypatch, capsys):
    """``SIDEGRAPH_GREP_NUDGE`` is the Read-path switch; the brief has its own."""
    project.record("src/x.py", "Gotcha of src/x.py")
    out, _ = _agent(monkeypatch, capsys, project, "Fix src/x.py.", {"SIDEGRAPH_GREP_NUDGE": "off"})
    assert "hookSpecificOutput" in out


@pytest.mark.parametrize("prompt", [["src/x.py"], {"text": "src/x.py"}, 7, None])
def test_t4_a_prompt_that_is_not_a_string_prints_an_empty_object(
    project, monkeypatch, capsys, prompt
):
    project.record("src/x.py", "Gotcha of src/x.py")
    out, _ = _agent(monkeypatch, capsys, project, prompt)
    assert out == {}


def test_t4_a_call_with_no_prompt_prints_an_empty_object(project, monkeypatch, capsys):
    project.record("src/x.py", "Gotcha of src/x.py")
    out = _run(monkeypatch, capsys, project, _payload("Agent", description="d"))
    assert out == {}


def test_t4_a_tool_input_that_is_not_an_object_prints_an_empty_object(project, monkeypatch, capsys):
    project.record("src/x.py", "Gotcha of src/x.py")
    out = _run(
        monkeypatch,
        capsys,
        project,
        {"session_id": "s", "tool_name": "Agent", "tool_input": "Fix src/x.py."},
    )
    assert out == {}


# -- T5: the caps ------------------------------------------------------------------------------


def _clip_records(project: Project, rel: str, count: int = 2) -> None:
    for k in range(count):
        project.record(rel, f"{LONG_TITLE} {k}", choice=LONG_CHOICE + ".")


def _check_caps(block: str, shown: list[str], brief_files: list[str]) -> None:
    assert len(_record_lines(block)) <= 6
    assert len(block) <= 3000
    assert _headers(block) == [f"{rel}:" for rel in shown]
    assert f"More: get_task_context(files={json.dumps(shown)})." in block
    for rel in brief_files:
        if rel not in shown:
            assert rel not in block, f"a file the caps dropped is named in the block: {rel}"


def test_t5_six_lines_at_most_and_whole_files_only(project, monkeypatch, capsys):
    """Eight files with two clip-length records each: the line cap leaves three files of two
    lines, and the fourth has no line left (mutation M4a drops the cap, and the character cap
    alone would let nine lines through)."""
    rels = [f"src/f{i}.py" for i in range(8)]
    for rel in rels:
        _clip_records(project, rel)
    prompt = "Touch " + ", ".join(rels) + "."
    out, _ = _agent(monkeypatch, capsys, project, prompt)
    block = _block(out, prompt)
    assert len(_record_lines(block)) == 6
    _check_caps(block, rels[:3], rels)
    assert all(len(line) <= 300 for line in _record_lines(block))


def test_t5_the_character_cap_drops_whole_files_from_the_end(project, monkeypatch, capsys):
    """Deep paths make a file's header and its ``More:`` entry long, so 3,000 characters hold
    two files, not the three that six lines allow (mutation M4b drops the cap)."""
    deep = "/".join(chr(97 + i) * 45 for i in range(5))
    rels = [f"src/{deep}/f{i}.py" for i in range(6)]
    for rel in rels:
        _clip_records(project, rel)
    prompt = "Touch " + ", ".join(rels) + "."
    out, _ = _agent(monkeypatch, capsys, project, prompt)
    block = _block(out, prompt)
    shown = [h[:-1] for h in _headers(block)]
    assert shown == rels[:2]
    assert len(_record_lines(block)) == 4
    _check_caps(block, shown, rels)


def test_t5_a_file_that_does_not_fit_whole_gets_its_first_record_on_the_last_line(
    project, monkeypatch, capsys
):
    """One record, then three files of two: five lines are used by the first three files, and the
    fourth file's two lines would make seven, so its first record fills the sixth line. ``More:``
    lists the four files shown."""
    counts = {"src/f0.py": 1, "src/f1.py": 2, "src/f2.py": 2, "src/f3.py": 2}
    for rel, count in counts.items():
        _clip_records(project, rel, count)
    prompt = "Touch " + ", ".join(counts) + "."
    out, _ = _agent(monkeypatch, capsys, project, prompt)
    block = _block(out, prompt)
    assert len(_record_lines(block)) == 6
    _check_caps(block, list(counts), list(counts))
    # the line that fills the last slot is the file's first record, as a brief naming it alone shows
    alone, _ = _agent(monkeypatch, capsys, project, "Touch src/f3.py.")
    assert _record_lines(block)[-1] == _record_lines(_block(alone, "Touch src/f3.py."))[0]
    assert block.index("src/f3.py:") > block.index("src/f2.py:")


def test_t5_a_half_file_ends_the_list_and_a_fifth_file_is_not_shown(project, monkeypatch, capsys):
    """After the half file all six lines are used: the next file is not shown, even with one
    record."""
    counts = {"src/f0.py": 2, "src/f1.py": 2, "src/f2.py": 1, "src/f3.py": 2, "src/f4.py": 1}
    for rel, count in counts.items():
        _clip_records(project, rel, count)
    prompt = "Touch " + ", ".join(counts) + "."
    out, _ = _agent(monkeypatch, capsys, project, prompt)
    block = _block(out, prompt)
    shown = ["src/f0.py", "src/f1.py", "src/f2.py", "src/f3.py"]
    assert len(_record_lines(block)) == 6
    _check_caps(block, shown, list(counts))
    assert "src/f4.py" not in block


def test_t5_a_half_file_that_would_break_the_character_cap_is_dropped(project, monkeypatch, capsys):
    """The first record of the next file fills the last line only when the block still fits in
    3,000 characters: with deep paths it does not, and the list ends at the whole files."""
    deep = "/".join(chr(97 + i) * 45 for i in range(5))
    counts = {f"src/{deep}/f{i}.py": c for i, c in enumerate([1, 2, 2, 2])}
    for rel, count in counts.items():
        _clip_records(project, rel, count)
    rels = list(counts)
    prompt = "Touch " + ", ".join(rels) + "."
    out, _ = _agent(monkeypatch, capsys, project, prompt)
    block = _block(out, prompt)
    shown = [h[:-1] for h in _headers(block)]
    assert len(block) <= 3000
    _check_caps(block, shown, rels)
    assert shown != rels  # the character cap bound before the sixth line
    assert len(_record_lines(block)) < 6


def test_t5_a_file_with_one_record_shows_one_line(project, monkeypatch, capsys):
    project.record("src/a.py", "Only record")
    out, _ = _agent(monkeypatch, capsys, project, "Fix src/a.py.")
    assert len(_record_lines(_block(out, "Fix src/a.py."))) == 1


# -- T6: Task is Agent, TaskCreate is not ------------------------------------------------------


def test_t6_task_behaves_like_agent(project, monkeypatch, capsys):
    project.record("src/x.py", "Gotcha of src/x.py")
    agent, _ = _agent(monkeypatch, capsys, project, "Fix src/x.py.", tool="Agent")
    task, _ = _agent(monkeypatch, capsys, project, "Fix src/x.py.", tool="Task")
    assert "hookSpecificOutput" in agent
    assert task == agent


@pytest.mark.parametrize("tool", ["TaskCreate", "TaskList", "TaskGet", "TaskUpdate", "agent"])
def test_t6_other_tools_never_reach_the_agent_branch(project, monkeypatch, capsys, tool):
    """The hook compares the tool name exactly: a name that merely contains ``Task`` is not
    an agent spawn, and neither is a different case."""
    project.record("src/x.py", "Gotcha of src/x.py")
    out, _ = _agent(monkeypatch, capsys, project, "Fix src/x.py.", tool=tool)
    assert out == {}


# -- the payload cwd: a launch from a subdirectory ---------------------------------------------


def test_cwd_a_brief_from_a_subdirectory_launch_follows_a_plan_under_that_subdirectory(
    project, monkeypatch, capsys
):
    """The parent works in ``sub``: its ``docs/plan.md`` is ``sub/docs/plan.md`` and the plan's
    ``src/b.py`` is ``sub/src/b.py``, which has the record."""
    record = project.record("sub/src/b.py", "Gotcha of sub b")
    (project.root / "sub/docs").mkdir(parents=True)
    (project.root / "sub/docs/plan.md").write_text("Change `src/b.py`.\n")
    prompt = "Implement docs/plan.md."
    out, _ = _agent(monkeypatch, capsys, project, prompt, cwd=project.root / "sub")
    block = _block(out, prompt)
    assert _headers(block) == ["sub/src/b.py (named in sub/docs/plan.md):"]
    assert record.id in block


def test_cwd_an_unanchored_file_at_the_root_does_not_shadow_the_anchored_one_in_the_subdirectory(
    project, monkeypatch, capsys
):
    """``src/a.py`` from ``sub`` is ``sub/src/a.py``, as a Bash read from there resolves it, even
    when the root holds an unanchored ``src/a.py`` of its own."""
    record = project.record("sub/src/a.py", "Gotcha of sub a")
    project.file("src/a.py")
    out, _ = _agent(monkeypatch, capsys, project, "Fix src/a.py.", cwd=project.root / "sub")
    block = _block(out, "Fix src/a.py.")
    assert _headers(block) == ["sub/src/a.py:"]
    assert record.id in block


@pytest.mark.parametrize("cwd", [None, "", 7, "relative/dir"], ids=["none", "empty", "int", "rel"])
def test_cwd_a_payload_cwd_that_is_not_an_absolute_path_is_ignored(
    project, monkeypatch, capsys, cwd
):
    project.record("src/a.py", "Gotcha of a")
    tool_input = {"description": "d", "prompt": "Fix src/a.py."}
    payload = _payload("Agent", **tool_input)
    payload["cwd"] = cwd
    out = _run(monkeypatch, capsys, project, payload)
    assert _headers(_block(out, "Fix src/a.py.")) == ["src/a.py:"]


# -- project-instruction files are shown, never followed ---------------------------------------


def test_hop_a_brief_naming_claude_md_does_not_follow_it(project, monkeypatch, capsys):
    """``CLAUDE.md`` names ``src/x.py``, which has a record; the brief does not name it, so the
    hop through ``CLAUDE.md`` is not taken and nothing is added."""
    project.record("src/x.py", "Gotcha of x")
    (project.root / "CLAUDE.md").write_text("Layout: `src/x.py`.\n")
    out, _ = _agent(monkeypatch, capsys, project, "Read CLAUDE.md first, then fix the bug.")
    assert out == {}


def test_hop_a_record_anchored_to_claude_md_itself_is_still_shown(project, monkeypatch, capsys):
    record = project.record("CLAUDE.md", "How to run the tests")
    project.record("src/x.py", "Gotcha of x")
    (project.root / "CLAUDE.md").write_text("Layout: `src/x.py`.\n")
    out, _ = _agent(monkeypatch, capsys, project, "Read CLAUDE.md first.")
    block = _block(out, "Read CLAUDE.md first.")
    assert _headers(block) == ["CLAUDE.md:"]
    assert record.id in block
