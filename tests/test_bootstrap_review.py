from __future__ import annotations

import json
import re
import stat
import subprocess
from pathlib import Path

import pytest

from sidegraph.bootstrap.model import (
    WARNING_CONSEQUENCES,
    AnchorPlan,
    BootstrapCandidate,
    BootstrapPlan,
    DecisionKind,
    DecisionStatus,
    Descriptor,
    ReviewAction,
    WarningCode,
)
from sidegraph.bootstrap.review import edit_candidate, render_candidate, review_plan


def candidate(number: int = 1) -> BootstrapCandidate:
    return BootstrapCandidate.from_fields(
        file_path=f"docs/adr/{number:03}.md",
        ref=f"docs/adr/{number:03}.md",
        fragment=None,
        source_hash=f"hash-{number}",
        title=f"Decision {number}",
        context="A context.",
        choice="Use SQLite.",
        rejected="Do not add Redis.",
        consequences=None,
        kind=DecisionKind.ADR,
        default_status=DecisionStatus.PROPOSED,
        redacted_anchor_text="Use SQLite.",
    )


def plan_of(*candidates: BootstrapCandidate) -> BootstrapPlan:
    return BootstrapPlan(
        root=".",
        profile="generic-adr",
        fingerprint="plan",
        catalog_fingerprint="catalog",
        candidates=candidates,
    )


@pytest.fixture
def plan() -> BootstrapPlan:
    return plan_of(candidate())


@pytest.fixture
def plan_of_three() -> BootstrapPlan:
    return plan_of(candidate(1), candidate(2), candidate(3))


def test_empty_input_does_not_accept_candidate(plan):
    """An accidental enter must take the explicit skip branch, never accept."""
    answers = iter([""])

    result = review_plan(plan, read_line=lambda _: next(answers), write_line=lambda _: None)

    assert result.items[0].action == ReviewAction.SKIP


def test_batch_requires_explicit_candidate_numbers(plan_of_three):
    """Removing selected IDs from a batch must stop those candidates being accepted."""
    answers = iter(["batch 1,3"])

    result = review_plan(
        plan_of_three, read_line=lambda _: next(answers), write_line=lambda _: None
    )

    assert [item.action for item in result.items] == [
        ReviewAction.ACCEPT,
        ReviewAction.KEEP_PROPOSED,
        ReviewAction.ACCEPT,
    ]


def test_batch_tolerates_spaces_after_commas(plan_of_three):
    """Red against unfixed code: ' 3'.isdigit() is False, so one space turned the run into
    skip-all with exit 0."""
    answers = iter(["batch 1, 3"])
    result = review_plan(
        plan_of_three, read_line=lambda _: next(answers), write_line=lambda _: None
    )
    assert [item.action for item in result.items] == [
        ReviewAction.ACCEPT,
        ReviewAction.KEEP_PROPOSED,
        ReviewAction.ACCEPT,
    ]


def test_batch_only_numbers_before_the_current_candidate_reprompts(plan_of_three):
    """Red against unfixed code: 'batch 1,2' typed at candidate 3 silently no-ops both
    numbers and ends the run with candidate 3 KEEP_PROPOSED; the fixed contract reprompts.
    Observed unfixed: 'At index 2: KEEP_PROPOSED != ACCEPT'."""
    answers = iter(["s", "s", "batch 1,2", "a"])
    result = review_plan(
        plan_of_three, read_line=lambda _: next(answers), write_line=lambda _: None
    )
    assert [item.action for item in result.items] == [
        ReviewAction.SKIP,
        ReviewAction.SKIP,
        ReviewAction.ACCEPT,
    ]


@pytest.mark.parametrize("command", ["batch", "batch all", "batch 1, 9", "batch 1,1"])
def test_invalid_batch_reprompts_without_accepting(plan_of_three, command):
    """Accepting an omitted, duplicate, or out-of-range ID would violate explicit consent; the
    fixed contract reprompts instead of terminating the run as skip-all. The trailing 'a' is
    the actual red target: unfixed code's skip-all-and-return branch never reads it, so
    candidate 1 stays SKIP instead of taking the ACCEPT this answer explicitly requests —
    ['s', 's', 's'] alone cannot distinguish reprompt from skip-all, since both produce
    all-SKIP."""
    answers = iter([command, "a", "s", "s"])
    result = review_plan(
        plan_of_three, read_line=lambda _: next(answers), write_line=lambda _: None
    )
    assert [item.action for item in result.items] == [
        ReviewAction.ACCEPT,
        ReviewAction.SKIP,
        ReviewAction.SKIP,
    ]


def test_batch_number_with_non_ascii_digit_reprompts_instead_of_crashing(plan_of_three):
    """Red against unfixed code: '²'.isdigit() is True but int('²') raises
    ValueError, so this input escaped _batch_numbers as an uncaught exception and crashed the
    whole review instead of reprompting — cli.py only catches EOFError around review_plan, so
    the user would get a traceback with no RESUME line. Superscript digits are trivially
    pasteable."""
    answers = iter(["batch ²,3", "s", "s", "s"])
    result = review_plan(
        plan_of_three, read_line=lambda _: next(answers), write_line=lambda _: None
    )
    assert [item.action for item in result.items] == [
        ReviewAction.SKIP,
        ReviewAction.SKIP,
        ReviewAction.SKIP,
    ]


def test_batch_number_with_arabic_indic_digit_is_rejected(plan_of_three):
    """Deliberate scope decision, not an accidental side effect: batch selection accepts plain
    ASCII digits only. Before this fix, non-ASCII decimal digits such as Arabic-Indic
    '١'/'٣' parsed via int() to their numeric value and were silently accepted;
    closing the isdigit()/int() mismatch that let superscripts crash (see the sibling test)
    also closes this door, since the smallest fix is an isascii() gate rather than a
    superscript-specific carve-out. Candidate numbers are indices this same CLI prints in
    ASCII, so accepting an alternate digit script would add a silent-conversion surface with
    no user benefit — treat it like any other malformed batch command and reprompt."""
    answers = iter(["batch ١,٣", "s", "s", "s"])
    result = review_plan(
        plan_of_three, read_line=lambda _: next(answers), write_line=lambda _: None
    )
    assert [item.action for item in result.items] == [
        ReviewAction.SKIP,
        ReviewAction.SKIP,
        ReviewAction.SKIP,
    ]


def test_batch_reprompt_example_is_a_valid_command_at_the_last_candidate(plan_of_three):
    """Red against unfixed code: the reprompt example was 'batch {number},{len(candidates)}'
    unconditionally, so at the last candidate it printed 'batch 3,3' — which the distinctness
    check itself refuses. A user following the printed advice would loop forever."""
    messages: list[str] = []
    answers = iter(["s", "s", "batch", "s"])
    result = review_plan(
        plan_of_three, read_line=lambda _: next(answers), write_line=messages.append
    )
    reprompt_messages = [message for message in messages if "batch needs distinct" in message]
    assert reprompt_messages
    assert all("3,3" not in message for message in reprompt_messages)
    assert any("batch 3" in message for message in reprompt_messages)
    assert [item.action for item in result.items] == [
        ReviewAction.SKIP,
        ReviewAction.SKIP,
        ReviewAction.SKIP,
    ]


def test_individual_actions_preserve_plan_order(plan_of_three):
    """Swapping the loop order would attach user actions to the wrong candidate."""
    answers = iter(["p", "a", "s"])

    result = review_plan(
        plan_of_three, read_line=lambda _: next(answers), write_line=lambda _: None
    )

    assert [item.candidate.key for item in result.items] == [
        candidate(1).key,
        candidate(2).key,
        candidate(3).key,
    ]
    assert [item.action for item in result.items] == [
        ReviewAction.KEEP_PROPOSED,
        ReviewAction.ACCEPT,
        ReviewAction.SKIP,
    ]


def test_complete_redacted_candidate_is_rendered_before_each_action(plan):
    """Omitting a field or rendering after input defeats informed, private review."""
    secret = "abcdefghijklmnopqrstuvwxyz123456"
    rich = plan.candidates[0].model_copy(
        update={
            "context": f"Context token={secret}",
            "consequences": "Requires a migration.",
            "anchors": (
                AnchorPlan(
                    descriptor=Descriptor(name="RetryClient", file_path="src/retry.py"),
                    status="resolved",
                    tier=2,
                ),
            ),
        }
    )
    messages: list[str] = []

    def answer(_prompt: str) -> str:
        rendered = "\n".join(messages)
        for expected in (
            "title: Decision 1",
            "source: docs/adr/001.md",
            "context: Context [REDACTED]",
            "choice: Use SQLite.",
            "rejected: Do not add Redis.",
            "consequences: Requires a migration.",
            "anchor: RetryClient (resolved, tier 2)",
        ):
            assert expected in rendered
        assert secret not in rendered
        return "s"

    result = review_plan(plan_of(rich), read_line=answer, write_line=messages.append)

    assert result.items[0].action == ReviewAction.SKIP


def test_editor_file_is_0600_and_removed_after_failure(plan):
    """Dropping restrictive permissions or cleanup exposes edited sensitive content."""
    observed: dict[str, object] = {}

    def failing_editor(argv, check=False):
        path = Path(argv[-1])
        observed["path"] = path
        observed["mode"] = stat.S_IMODE(path.stat().st_mode)
        raise RuntimeError("editor failed")

    with pytest.raises(RuntimeError, match="editor failed"):
        edit_candidate(plan.candidates[0], editor="fake-editor", run_editor=failing_editor)

    assert observed["mode"] == 0o600
    assert not observed["path"].exists()


def test_editor_file_is_removed_after_keyboard_interrupt(plan):
    """Interrupting an editor must not strand its temporary candidate file."""
    observed: dict[str, Path] = {}

    def interrupted_editor(argv, check=False):
        observed["path"] = Path(argv[-1])
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        edit_candidate(plan.candidates[0], editor="fake-editor", run_editor=interrupted_editor)

    assert not observed["path"].exists()


@pytest.mark.parametrize("replacement", ["{", '{"choice": 42}'])
def test_invalid_editor_json_removes_temp_file(plan, replacement):
    """Accepting malformed editor output would bypass the editable schema."""
    observed: dict[str, Path] = {}

    def edit_json(argv, check=False):
        path = Path(argv[-1])
        observed["path"] = path
        path.write_text(replacement, encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0)

    with pytest.raises(ValueError):
        edit_candidate(plan.candidates[0], editor="fake-editor", run_editor=edit_json)

    assert not observed["path"].exists()


def test_invalid_editor_field_is_redacted_before_validation_error_and_cleanup(plan):
    """Validating raw editor JSON would echo a secret embedded in an invalid field."""
    secret = "abcdefghijklmnopqrstuvwxyz123456"
    observed: dict[str, Path] = {}

    def edit_json(argv, check=False):
        path = Path(argv[-1])
        observed["path"] = path
        edited = json.loads(path.read_text(encoding="utf-8"))
        edited["kind"] = f"token={secret}"
        path.write_text(json.dumps(edited), encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0)

    with pytest.raises(ValueError) as error:
        edit_candidate(plan.candidates[0], editor="fake-editor", run_editor=edit_json)

    assert secret not in str(error.value)
    assert not observed["path"].exists()


def test_invalid_editor_nested_key_is_redacted_before_validation_error_and_cleanup(plan):
    """Leaving nested object keys raw would leak a secret in Pydantic's invalid input."""
    secret = "token=shh"
    observed: dict[str, Path] = {}

    def edit_json(argv, check=False):
        path = Path(argv[-1])
        observed["path"] = path
        edited = json.loads(path.read_text(encoding="utf-8"))
        edited["choice"] = {secret: "unexpected object"}
        path.write_text(json.dumps(edited), encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0)

    with pytest.raises(ValueError) as error:
        edit_candidate(plan.candidates[0], editor="fake-editor", run_editor=edit_json)

    assert secret not in str(error.value)
    assert not observed["path"].exists()


def test_edit_reintroducing_secret_is_redacted_before_diff_and_result(plan):
    """Moving redaction after diffing would disclose the editor-inserted secret."""
    secret = "abcdefghijklmnopqrstuvwxyz123456"

    def edit_json(argv, check=False):
        path = Path(argv[-1])
        edited = json.loads(path.read_text(encoding="utf-8"))
        edited["choice"] = f"Use token={secret}"
        path.write_text(json.dumps(edited), encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0)

    edited = edit_candidate(plan.candidates[0], editor="fake-editor --wait", run_editor=edit_json)

    assert secret not in edited.diff
    assert edited.candidate.choice == "Use [REDACTED]"


def test_editor_command_is_split_without_shell_interpretation(plan):
    """Passing a shell string would let editor configuration execute unintended syntax."""
    observed: dict[str, list[str]] = {}

    def edit_json(argv, check=False):
        observed["argv"] = argv
        return subprocess.CompletedProcess(argv, 0)

    edit_candidate(
        plan.candidates[0], editor='fake-editor --label "two words"', run_editor=edit_json
    )

    assert observed["argv"][:-1] == ["fake-editor", "--label", "two words"]


def test_visual_takes_precedence_over_editor_and_no_editor_skips(plan):
    """Choosing EDITOR first or invoking an absent editor breaks predictable review."""
    messages: list[str] = []
    observed: dict[str, list[str]] = {}

    def edit_json(argv, check=False):
        observed["argv"] = argv
        return subprocess.CompletedProcess(argv, 0)

    answers = iter(["e", "proposed"])
    result = review_plan(
        plan,
        read_line=lambda _: next(answers),
        write_line=messages.append,
        environ={"VISUAL": "visual-editor", "EDITOR": "other-editor"},
        run_editor=edit_json,
    )
    assert observed["argv"][0] == "visual-editor"
    assert result.items[0].action == ReviewAction.KEEP_PROPOSED

    messages.clear()
    result = review_plan(
        plan,
        read_line=lambda _: "e",
        write_line=messages.append,
        environ={},
        run_editor=edit_json,
    )
    assert result.items[0].action == ReviewAction.KEEP_PROPOSED
    assert any("Set VISUAL or EDITOR" in message for message in messages)


def test_late_batch_preserves_edited_proposed_candidate(plan_of_three):
    """Rebuilding from the plan during a late batch would discard the edited review state."""
    answers = iter(["e", "proposed", "batch 2,3"])

    def edit_json(argv, check=False):
        path = Path(argv[-1])
        edited = json.loads(path.read_text(encoding="utf-8"))
        edited["choice"] = "Use PostgreSQL."
        path.write_text(json.dumps(edited), encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0)

    result = review_plan(
        plan_of_three,
        read_line=lambda _: next(answers),
        write_line=lambda _: None,
        environ={"EDITOR": "fake-editor"},
        run_editor=edit_json,
    )

    assert result.items[0].candidate.choice == "Use PostgreSQL."
    assert result.items[0].action == ReviewAction.KEEP_PROPOSED
    assert [item.action for item in result.items[1:]] == [
        ReviewAction.ACCEPT,
        ReviewAction.ACCEPT,
    ]


def test_edit_confirmation_accepts_the_edited_candidate(plan):
    """Dropping an explicit acceptance after edit would lose the re-planned candidate."""
    answers = iter(["e", "accept"])

    def edit_json(argv, check=False):
        path = Path(argv[-1])
        edited = json.loads(path.read_text(encoding="utf-8"))
        edited["choice"] = "Use PostgreSQL."
        path.write_text(json.dumps(edited), encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0)

    result = review_plan(
        plan,
        read_line=lambda _: next(answers),
        write_line=lambda _: None,
        environ={"EDITOR": "fake-editor"},
        run_editor=edit_json,
    )

    assert result.items[0].action == ReviewAction.ACCEPT
    assert result.items[0].candidate.choice == "Use PostgreSQL."
    assert result.items[0].edited is True


def test_edit_confirmation_cancel_keeps_original_candidate(plan):
    """Assigning the edited candidate on cancel makes an explicitly skipped edit visible."""
    answers = iter(["e", "cancel"])

    def edit_json(argv, check=False):
        path = Path(argv[-1])
        edited = json.loads(path.read_text(encoding="utf-8"))
        edited["choice"] = "Use PostgreSQL."
        path.write_text(json.dumps(edited), encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0)

    result = review_plan(
        plan,
        read_line=lambda _: next(answers),
        write_line=lambda _: None,
        environ={"EDITOR": "fake-editor"},
        run_editor=edit_json,
    )

    assert result.items[0].action == ReviewAction.SKIP
    assert result.items[0].candidate.key == plan.candidates[0].key
    assert result.items[0].candidate.choice == "Use SQLite."


def test_invalid_edit_confirmation_reprompts_until_explicit_choice(plan):
    """Treating an invalid confirmation as cancel silently discards an edit."""
    answers = iter(["e", "", "proposed"])

    def edit_json(argv, check=False):
        path = Path(argv[-1])
        edited = json.loads(path.read_text(encoding="utf-8"))
        edited["choice"] = "Use PostgreSQL."
        path.write_text(json.dumps(edited), encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0)

    result = review_plan(
        plan,
        read_line=lambda _: next(answers),
        write_line=lambda _: None,
        environ={"EDITOR": "fake-editor"},
        run_editor=edit_json,
    )

    assert result.items[0].action == ReviewAction.KEEP_PROPOSED
    assert result.items[0].candidate.choice == "Use PostgreSQL."


def test_review_records_ephemeral_action_elapsed_time_without_canonical_metadata(
    plan_of_three,
):
    """Dropping action timing prevents aggregate activation metrics from being computed."""
    answers = iter(["a", "p", "s"])
    ticks = iter([10.0, 12.0, 20.0, 25.0, 30.0, 31.5])

    result = review_plan(
        plan_of_three,
        read_line=lambda _: next(answers),
        write_line=lambda _: None,
        monotonic=lambda: next(ticks),
    )

    assert [item.action_elapsed_seconds for item in result.items] == [2.0, 5.0, 1.5]
    assert [item.edited for item in result.items] == [False, False, False]
    assert result.elapsed_seconds == 8.5


def test_editor_returning_nonzero_exit_cancels_the_edit(plan_of_three):
    """The rc!=0 translation in edit_candidate is load-bearing: a run_editor that RETURNS
    CompletedProcess(rc=1) instead of raising must cancel the edit. Kills Task 7's
    mutation 4 (restoring check=True drops the translation)."""
    messages: list[str] = []
    answers = iter(["e", "p", "s", "s"])

    def exits_nonzero(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 1)

    result = review_plan(
        plan_of_three,
        read_line=lambda _: next(answers),
        write_line=messages.append,
        environ={"EDITOR": "stub"},
        run_editor=exits_nonzero,
    )

    assert result.items[0].action == ReviewAction.KEEP_PROPOSED
    assert result.items[0].edited is False
    assert any("edit cancelled by the editor (exit 1)" in message for message in messages)


def test_missing_editor_binary_returns_to_the_prompt(plan_of_three):
    """Red against unfixed code: FileNotFoundError is an OSError, so main() turned a typo in
    $EDITOR into exit 1 with the whole review discarded."""
    answers = iter(["e", "p", "s", "s"])

    def missing(argv, **kwargs):
        raise FileNotFoundError(2, "No such file or directory", argv[0])

    result = review_plan(
        plan_of_three,
        read_line=lambda _: next(answers),
        write_line=lambda _: None,
        environ={"EDITOR": "definitely-not-an-editor"},
        run_editor=missing,
    )

    assert result.items[0].action == ReviewAction.KEEP_PROPOSED


def test_invalid_edited_json_returns_to_the_prompt(plan_of_three):
    """Red against unfixed code: ValueError('edited JSON is invalid') reached main() and exit
    1; on a 20-candidate review a typo at 19 lost everything."""
    answers = iter(["e", "a", "s", "s"])

    def write_garbage(argv, **kwargs):
        Path(argv[-1]).write_text("{not json", encoding="utf-8")
        return subprocess.CompletedProcess(argv, 0)

    result = review_plan(
        plan_of_three,
        read_line=lambda _: next(answers),
        write_line=lambda _: None,
        environ={"EDITOR": "stub"},
        run_editor=write_garbage,
    )

    assert result.items[0].action == ReviewAction.ACCEPT
    assert result.items[0].edited is False


def test_reedit_after_invalid_json_reopens_the_users_own_buffer(plan):
    """Red against unfixed code AND against the first draft of this fix: returning to the
    prompt with a freshly-rendered candidate loses everything the user typed."""
    seen: list[str] = []
    answers = iter(["e", "e", "accept", "s"])

    def type_then_fix(argv, **kwargs):
        path = Path(argv[-1])
        seen.append(path.read_text(encoding="utf-8"))
        if len(seen) == 1:
            path.write_text('{"title": "Renamed", not json', encoding="utf-8")
        else:
            path.write_text(
                json.dumps(
                    {
                        "title": "Renamed",
                        "context": "A context.",
                        "choice": "Use SQLite.",
                        "rejected": "Do not add Redis.",
                        "consequences": None,
                        "kind": "adr",
                    }
                ),
                encoding="utf-8",
            )
        return subprocess.CompletedProcess(argv, 0)

    result = review_plan(
        plan,
        read_line=lambda _: next(answers),
        write_line=lambda _: None,
        environ={"EDITOR": "stub"},
        run_editor=type_then_fix,
    )

    assert seen[1] == '{"title": "Renamed", not json'  # the retry reopened the failed buffer
    assert result.items[0].candidate.title == "Renamed"
    assert result.items[0].edited is True


def test_transient_os_error_during_edit_clears_seed_for_the_next_attempt(plan):
    """Red against unfixed code: the OSError arm neither preserves nor clears `seed`, so this
    sequence silently discards the user's work — edit 1 is rejected as invalid (seed becomes
    the bad buffer), edit 2's replan hits a transient OSError, and edit 3 would reopen the old
    garbage instead of a fresh render of the candidate."""
    seen: list[str] = []
    calls = {"count": 0}
    answers = iter(["e", "e", "e", "accept", "s"])

    def flaky_editor(argv, **kwargs):
        path = Path(argv[-1])
        seen.append(path.read_text(encoding="utf-8"))
        calls["count"] += 1
        if calls["count"] == 1:
            path.write_text('{"title": "Renamed", not json', encoding="utf-8")
            return subprocess.CompletedProcess(argv, 0)
        if calls["count"] == 2:
            raise OSError("transient failure")
        path.write_text(
            json.dumps(
                {
                    "title": "Renamed",
                    "context": "A context.",
                    "choice": "Use SQLite.",
                    "rejected": "Do not add Redis.",
                    "consequences": None,
                    "kind": "adr",
                }
            ),
            encoding="utf-8",
        )
        return subprocess.CompletedProcess(argv, 0)

    result = review_plan(
        plan,
        read_line=lambda _: next(answers),
        write_line=lambda _: None,
        environ={"EDITOR": "stub"},
        run_editor=flaky_editor,
    )

    assert seen[1] == '{"title": "Renamed", not json'  # attempt 2 reopened the rejected buffer
    assert seen[2] == seen[0]  # attempt 3 is a fresh render again, not the stale rejected buffer
    assert result.items[0].candidate.title == "Renamed"
    assert result.items[0].edited is True


def test_edit_failure_message_clips_to_200_characters_including_suffix(plan_of_three):
    """Red against unfixed code: _edit_failure clipped only the detail to 200 characters and
    then appended '; candidate unchanged', so the full message could reach 221 characters."""
    messages: list[str] = []
    answers = iter(["e", "s", "s", "s"])

    def flaky_editor(argv, **kwargs):
        raise OSError(5, "x" * 250)

    result = review_plan(
        plan_of_three,
        read_line=lambda _: next(answers),
        write_line=messages.append,
        environ={"EDITOR": "stub"},
        run_editor=flaky_editor,
    )

    failure_messages = [message for message in messages if "editor step failed" in message]
    assert failure_messages
    assert all(len(message) <= 200 for message in failure_messages)
    assert failure_messages[0].endswith("; candidate unchanged")
    assert result.items[0].action == ReviewAction.SKIP


def test_every_warning_code_has_a_consequence_sentence():
    """Red against unfixed code: no consequence text exists, so a code cannot tell a reviewer
    what accepting will do."""
    assert set(WARNING_CONSEQUENCES) == set(WarningCode)
    assert all(text and text[0].islower() for text in WARNING_CONSEQUENCES.values())


def test_rendered_candidate_explains_duplicate_canonical_memory():
    """Red against unfixed code: the only signal was the bare code, so a rerun that supersedes
    a human's earlier edit with raw document content read as a routine duplicate."""
    warned = candidate(1).model_copy(update={"warnings": (WarningCode.DUPLICATE_CANONICAL,)})
    rendered = render_candidate(warned)
    assert "a record already exists for this source" in rendered
    assert "edits made in an earlier review are not carried over" in rendered


_DOC_WARNING_ROW_RE = re.compile(r"^\| `([a-z0-9-]+)` \| (.+) \|$")


def _parsed_doc_warning_table() -> dict[str, str]:
    """``{code: sentence}`` from the warning-code table in docs/getting-started/bootstrap.md —
    scoped to the contiguous rows right after the table's own header (identified by its literal
    "| Warning code |" lead cell), so an unrelated table elsewhere in the doc whose first column
    also happens to be backtick-quoted (e.g. the profiles table) is never mistaken for this
    one."""
    guide = Path(__file__).resolve().parent.parent / "docs" / "getting-started" / "bootstrap.md"
    lines = guide.read_text(encoding="utf-8").splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("| Warning code |"))
    rows: dict[str, str] = {}
    for line in lines[start + 2 :]:  # skip the header row and its `| --- | --- |` separator
        match = _DOC_WARNING_ROW_RE.match(line.strip())
        if match is None:
            break
        rows[match.group(1)] = match.group(2).strip()
    return rows


def test_docs_bootstrap_guide_mirrors_every_consequence_sentence():
    """Red against a drifted mirror: merely checking that each bare code STRING appears
    somewhere in the guide (the code name itself, not its sentence) lets the guide's prose
    regress to a disproven draft, or to outright nonsense, while WARNING_CONSEQUENCES stays
    correct and the suite stays green — the docs table is a mirror of WARNING_CONSEQUENCES
    (spec §3.8), and a mirror test that never compares the mirror doesn't implement one. This
    asserts full equality, both directions: neither a missing row nor an extra/stale one
    passes."""
    doc_table = _parsed_doc_warning_table()
    expected = {code.value: WARNING_CONSEQUENCES[code] for code in WarningCode}
    assert doc_table == expected
