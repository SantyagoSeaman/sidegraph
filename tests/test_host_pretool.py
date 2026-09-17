"""``pre_tool_use`` — the PreToolUse Read/Grep redirect nudge (M6, FR8.3).

See ``docs/reference/hooks.md#sidegraph-pre-tool-use``: on Read/Grep of something that
looks like a source file, when the store has decision memory to offer, emit a non-blocking
``additionalContext`` nudge toward ``get_task_context``/``drill_down`` — never a hard
block — at most once per session.
"""

import io
import json
from datetime import UTC, datetime

import sidegraph.host.hooks as hooks
from sidegraph.schema import Decision, DecisionKind, DecisionStatus, Domain, Provenance
from sidegraph.store import Store


def _run(monkeypatch, capsys, payload, db, extra_env=None):
    monkeypatch.setenv("SIDEGRAPH_DB", str(db))
    for key, value in (extra_env or {}).items():
        monkeypatch.setenv(key, value)
    stdin = payload if isinstance(payload, str) else json.dumps(payload)
    monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    hooks.pre_tool_use()
    return json.loads(capsys.readouterr().out)


def _seed_one_decision(db) -> None:
    store = Store(db)
    store.add_decision(
        Decision(
            title="Use SQLite for the store",
            kind=DecisionKind.ADR,
            status=DecisionStatus.ACCEPTED,
            context="c",
            choice="ch",
            valid_from=datetime.now(UTC),
            provenance=Provenance(source="manual"),
        )
    )


def _seed_one_domain(db) -> None:
    store = Store(db)
    d = store.add_domain(
        Domain(
            slug="payments",
            title="Payments",
            summary="Handles settlement.",
            provenance=Provenance(source="manual"),
        )
    )
    store.ratify_domains(accept=[d.domain_id])


READ_PAYLOAD = {
    "session_id": "p1",
    "tool_name": "Read",
    "tool_input": {"file_path": "src/sidegraph/server.py"},
}


def test_pretool_nudge_fires_with_decision_memory(tmp_path, monkeypatch, capsys):
    db = tmp_path / "s.db"
    _seed_one_decision(db)
    out = _run(monkeypatch, capsys, READ_PAYLOAD, db)
    hso = out["hookSpecificOutput"]
    assert hso["hookEventName"] == "PreToolUse"
    assert "permissionDecision" not in hso
    assert "get_task_context" in hso["additionalContext"]
    assert "drill_down" in hso["additionalContext"]
    assert "1 decisions" in hso["additionalContext"]


def test_pretool_nudge_fires_with_domain_memory(tmp_path, monkeypatch, capsys):
    db = tmp_path / "s.db"
    _seed_one_domain(db)
    out = _run(monkeypatch, capsys, READ_PAYLOAD, db)
    assert "1 domains" in out["hookSpecificOutput"]["additionalContext"]


def test_pretool_nudge_fires_once_per_session(tmp_path, monkeypatch, capsys):
    db = tmp_path / "s.db"
    _seed_one_decision(db)
    first = _run(monkeypatch, capsys, READ_PAYLOAD, db)
    assert "hookSpecificOutput" in first
    second = _run(monkeypatch, capsys, READ_PAYLOAD, db)
    assert second == {}


def test_pretool_nudge_new_session_fires_again(tmp_path, monkeypatch, capsys):
    db = tmp_path / "s.db"
    _seed_one_decision(db)
    _run(monkeypatch, capsys, READ_PAYLOAD, db)
    other = dict(READ_PAYLOAD, session_id="p2")
    out = _run(monkeypatch, capsys, other, db)
    assert "hookSpecificOutput" in out


def test_pretool_nudge_respects_off_switch(tmp_path, monkeypatch, capsys):
    db = tmp_path / "s.db"
    _seed_one_decision(db)
    out = _run(monkeypatch, capsys, READ_PAYLOAD, db, extra_env={"SIDEGRAPH_GREP_NUDGE": "off"})
    assert out == {}


def test_pretool_nudge_fires_for_grep(tmp_path, monkeypatch, capsys):
    db = tmp_path / "s.db"
    _seed_one_decision(db)
    payload = {"session_id": "p3", "tool_name": "Grep", "tool_input": {"pattern": "foo"}}
    out = _run(monkeypatch, capsys, payload, db)
    assert "hookSpecificOutput" in out


def test_pretool_nudge_silent_for_other_tools(tmp_path, monkeypatch, capsys):
    db = tmp_path / "s.db"
    _seed_one_decision(db)
    payload = {"session_id": "p4", "tool_name": "Bash", "tool_input": {"command": "ls"}}
    out = _run(monkeypatch, capsys, payload, db)
    assert out == {}


def test_pretool_nudge_silent_for_edit_and_write(tmp_path, monkeypatch, capsys):
    # Pins the _PRETOOL_NUDGE_TOOLS exclusion for tools that ARE inside the hook matcher
    # (Edit/Write ride it for touch recording) — review 2026-08-08 measured that widening
    # the frozenset to include them left the whole suite green; this test is the pin.
    # _looks_like_source_target passes these payloads (catch-all on any string value), so
    # the frozenset is the only thing standing between an Edit and a nudge.
    db = tmp_path / "s.db"
    _seed_one_decision(db)
    for i, (tool, tin) in enumerate(
        [("Edit", {"file_path": "src/x.py"}), ("Write", {"file_path": "src/y.py"})]
    ):
        payload = {"session_id": f"pew{i}", "tool_name": tool, "tool_input": tin}
        out = _run(monkeypatch, capsys, payload, db)
        assert out == {}, tool


def test_pretool_nudge_silent_when_no_string_target(tmp_path, monkeypatch, capsys):
    db = tmp_path / "s.db"
    _seed_one_decision(db)
    payload = {"session_id": "p5", "tool_name": "Read", "tool_input": {}}
    out = _run(monkeypatch, capsys, payload, db)
    assert out == {}


def test_pretool_nudge_silent_when_store_empty(tmp_path, monkeypatch, capsys):
    db = tmp_path / "s.db"
    Store(db)  # create an empty store, no decisions or domains
    out = _run(monkeypatch, capsys, READ_PAYLOAD, db)
    assert out == {}


def test_pretool_use_never_raises_on_garbage_stdin(tmp_path, monkeypatch, capsys):
    db = tmp_path / "s.db"
    _seed_one_decision(db)
    out = _run(monkeypatch, capsys, "not valid json{", db)
    assert out == {}


def test_pretool_use_never_raises_on_store_failure(tmp_path, monkeypatch, capsys):
    def boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr("sidegraph.store.Store", boom)
    out = _run(monkeypatch, capsys, READ_PAYLOAD, tmp_path / "s.db")
    assert out == {}


def test_pretool_nudge_missing_session_id_allows_silently(tmp_path, monkeypatch, capsys):
    db = tmp_path / "s.db"
    _seed_one_decision(db)
    payload = {"tool_name": "Read", "tool_input": {"file_path": "x.py"}}
    out = _run(monkeypatch, capsys, payload, db)
    assert out == {}


# -- path-specific nudge (whitepaper Phase-1 finding) ----------------------------------------
#
# Measured 2026-07-31 across 168 arm-A sessions: this nudge FIRES (its `pretool_nudge:`
# ledger keys are in the store — the transcript never shows it, because Claude Code does
# not echo PreToolUse additionalContext) and the agent reads the file anyway. 40-45% of
# sessions on code corpora paid for memory and never called retrieval, and those sessions
# scored BELOW the no-memory control. The nudge announced that memory exists without
# saying what memory holds about the path being opened.


def _seed_anchored_decision(
    db, file_path: str, title: str, kind=DecisionKind.GOTCHA, status=None, valid_from=None
) -> None:
    from sidegraph.schema import AnchorBinding, Descriptor, Entity

    store = Store(db)
    d = store.add_decision(
        Decision(
            title=title,
            kind=kind,
            status=status or DecisionStatus.ACCEPTED,
            context="c",
            choice="ch",
            valid_from=valid_from or datetime.now(UTC),
            provenance=Provenance(source="manual"),
        )
    )
    e = store.upsert_entity(
        Entity(
            canonical_name=title.split()[0],
            descriptor=Descriptor(name=title.split()[0], file_path=file_path),
        )
    )
    store.add_binding(AnchorBinding(record_id=d.id, entity_id=e.entity_id, tier=2, status="live"))


def test_nudge_names_what_memory_holds_about_this_path(tmp_path, monkeypatch, capsys):
    """The fix: quote the record titles anchored to the file being read, instead of
    counting domains. A count is what the agent demonstrably ignored."""
    db = tmp_path / "s.db"
    _seed_anchored_decision(db, "src/sidegraph/server.py", "Never write into graph.json")
    out = _run(monkeypatch, capsys, READ_PAYLOAD, db)
    text = out["hookSpecificOutput"]["additionalContext"]
    assert "Never write into graph.json" in text
    assert "src/sidegraph/server.py" in text
    assert "get_task_context" in text


def test_nudge_puts_mistakes_first_and_caps_the_list(tmp_path, monkeypatch, capsys):
    """Mistakes-first is the product's one hard ranking guarantee; the nudge must not
    invert it, and must stay short enough to read. With three records on one path — two
    of them mistake kinds — the cap of two must spend itself on the mistakes and leave
    the ADR out."""
    db = tmp_path / "s.db"
    path = "src/sidegraph/server.py"
    _seed_anchored_decision(db, path, "Adr one choice", kind=DecisionKind.ADR)
    _seed_anchored_decision(db, path, "Gotcha bites here", kind=DecisionKind.GOTCHA)
    _seed_anchored_decision(db, path, "Lesson learned twice", kind=DecisionKind.LESSON)
    out = _run(monkeypatch, capsys, READ_PAYLOAD, db)
    text = out["hookSpecificOutput"]["additionalContext"]
    assert "Gotcha bites here" in text and "Lesson learned twice" in text
    assert "Adr one choice" not in text  # capped at two, mistakes win the slots
    assert len(text) <= 400


def test_pretool_cap_is_spent_on_accepted_before_proposed(tmp_path, monkeypatch):
    db = tmp_path / "s.db"
    path = "src/sidegraph/server.py"
    _seed_anchored_decision(
        db, path, "Accepted gotcha", kind=DecisionKind.GOTCHA, status=DecisionStatus.ACCEPTED
    )
    _seed_anchored_decision(
        db, path, "Accepted ADR", kind=DecisionKind.ADR, status=DecisionStatus.ACCEPTED
    )
    _seed_anchored_decision(
        db,
        path,
        "Newest proposed gotcha",
        kind=DecisionKind.GOTCHA,
        status=DecisionStatus.PROPOSED,
    )
    monkeypatch.setattr(hooks, "_PRETOOL_TITLE_CAP", 2)

    assert hooks._titles_for_path(Store(db), path) == ["Accepted gotcha", "Accepted ADR"]


def test_uncovered_path_keeps_the_generic_form(tmp_path, monkeypatch, capsys):
    """Deliberate scope line: with nothing recorded about THIS path the nudge keeps its
    pre-fix wording rather than going silent — silence would change documented behaviour,
    not the wording, and is a separate measurable follow-up."""
    db = tmp_path / "s.db"
    _seed_anchored_decision(db, "src/sidegraph/retrieval.py", "Budget is char-based")
    out = _run(monkeypatch, capsys, READ_PAYLOAD, db)  # reads server.py — not covered
    assert "decision memory" in out["hookSpecificOutput"]["additionalContext"]


def test_grep_pattern_without_a_path_still_uses_the_generic_form(tmp_path, monkeypatch, capsys):
    """A Grep with a pattern but no path cannot be path-matched; it keeps the pre-fix
    behaviour rather than going silent, since there is no path to be specific about."""
    db = tmp_path / "s.db"
    _seed_anchored_decision(db, "src/sidegraph/server.py", "Never write into graph.json")
    out = _run(
        monkeypatch,
        capsys,
        {"session_id": "g1", "tool_name": "Grep", "tool_input": {"pattern": "def sync"}},
        db,
    )
    assert "decision memory" in out["hookSpecificOutput"]["additionalContext"]


def test_generic_nudge_does_not_consume_the_path_specific_one(tmp_path, monkeypatch, capsys):
    """Review finding 3, measured: 94% of first Read/Grep calls on this repo land on an
    unanchored spec or plan. Sharing one ledger meant that first read burned the session's
    only nudge on the counting form, so the path-specific form reached 6% of sessions —
    it would have measured as "no effect" while the mechanism was fine."""
    db = tmp_path / "s.db"
    _seed_anchored_decision(db, "src/sidegraph/retrieval.py", "Budget is char-based")
    first = _run(monkeypatch, capsys, READ_PAYLOAD, db)  # unanchored path -> generic
    assert "decision memory" in first["hookSpecificOutput"]["additionalContext"]
    covered = {
        "session_id": "p1",
        "tool_name": "Read",
        "tool_input": {"file_path": "src/sidegraph/retrieval.py"},
    }
    second = _run(monkeypatch, capsys, covered, db)  # SAME session, anchored path
    assert "Budget is char-based" in second["hookSpecificOutput"]["additionalContext"]
    # each form is still one-shot on its own key
    assert _run(monkeypatch, capsys, covered, db) == {}


def test_titles_are_newest_first_within_the_mistakes_bucket(tmp_path, monkeypatch, capsys):
    """Review finding 2: scan order is ULID mint order, so without an explicit sort the
    cap spent itself on the two OLDEST records of a busy path (measured: two of eighteen
    on store.py)."""
    from datetime import timedelta

    db = tmp_path / "s.db"
    path = "src/sidegraph/server.py"
    old = datetime.now(UTC) - timedelta(days=30)
    for title, when in (("Oldest gotcha", old), ("Middle gotcha", old + timedelta(days=10))):
        _seed_anchored_decision(db, path, title, valid_from=when)
    _seed_anchored_decision(db, path, "Newest gotcha")
    text = _run(monkeypatch, capsys, READ_PAYLOAD, db)["hookSpecificOutput"]["additionalContext"]
    assert "Newest gotcha" in text and "Middle gotcha" in text
    assert "Oldest gotcha" not in text


def test_proposed_record_is_tagged_unratified(tmp_path, monkeypatch, capsys):
    """Review finding 5: retrieval always tags a PROPOSED record; the nudge must not
    present an unreviewed draft as settled memory."""
    db = tmp_path / "s.db"
    _seed_anchored_decision(
        db, "src/sidegraph/server.py", "Draft ruling", status=DecisionStatus.PROPOSED
    )
    text = _run(monkeypatch, capsys, READ_PAYLOAD, db)["hookSpecificOutput"]["additionalContext"]
    assert "Draft ruling [unratified]" in text


def test_title_is_clipped_and_sanitised(tmp_path, monkeypatch, capsys):
    """Review findings 6 and 7: one ADR-scale title must not swallow the nudge, and a
    newline or a quote inside a title must not break the emitted line."""
    db = tmp_path / "s.db"
    _seed_anchored_decision(
        db, "src/sidegraph/server.py", 'Never write\ninto "graph.json" ' + "x" * 200
    )
    text = _run(monkeypatch, capsys, READ_PAYLOAD, db)["hookSpecificOutput"]["additionalContext"]
    assert "\n" not in text
    assert text.count('"') == 2  # exactly the pair this nudge adds
    assert "…" in text
