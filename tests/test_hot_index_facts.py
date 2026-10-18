"""Automatic facts share the existing file, trust and delivery budgets."""

from __future__ import annotations

import io
import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta

import pytest

from sidegraph.host import hooks
from sidegraph.hot_index import HotIndex
from sidegraph.retrieval import _live_facts
from sidegraph.schema import DecisionKind, Fact, Provenance
from sidegraph.store import Store
from sidegraph.store_layout import VOLATILE_STALE_KEY
from tests.test_hot_index import _Seeder


@pytest.fixture
def fact_project(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    for path in ("a.py", "b.py"):
        (root / "src" / path).write_text("x = 1\n", encoding="utf-8")
    monkeypatch.chdir(root)
    monkeypatch.setenv("SIDEGRAPH_DIR", str(root / ".sidegraph"))
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(root))
    return root


def _fact(
    root,
    *,
    supporting=False,
    paths=("src/a.py",),
    status="accepted",
    age_days=0,
    valid_to=None,
    assert_eligible=True,
    statement=None,
    source=None,
    fixed_id=None,
):
    store = Store(root / ".sidegraph")
    try:
        seed = _Seeder(store)
        supports = (
            [seed.decision("Independent supporter", DecisionKind.ADR).id] if supporting else []
        )
        fact = store.add_fact(
            Fact(
                **({"id": fixed_id} if fixed_id is not None else {}),
                statement=statement
                if statement is not None
                else "Sample n=20 completed in 8 ms. Version 0.14.0 used two workers.",
                source=source
                if source is not None
                else "Synthetic fixture, Python 3.13 on a local test machine.",
                supports=supports,
                status=status,
                valid_from=datetime(2026, 10, 18, tzinfo=UTC) - timedelta(days=age_days),
                valid_to=valid_to,
                provenance=Provenance(source="agent"),
            )
        )
        for path in paths:
            target = root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                target.write_text("x = 1\n", encoding="utf-8")
            seed.anchor(fact, path)
        store.set_meta(VOLATILE_STALE_KEY, "0")
        index = HotIndex.open(store.path)
        assert index is not None
        try:
            entity = index.anchored_entities([paths[0]])[paths[0]][0]
        finally:
            index.close()
        if assert_eligible:
            assert [f.id for f in _live_facts(store.valid_facts_for_entity(entity))] == [fact.id]
        return fact
    finally:
        store.close()


def _canonical(root):
    directory = root / ".sidegraph"
    return {
        **{p.relative_to(directory): p.read_bytes() for p in directory.rglob("*.json")},
        "format": (directory / "format").read_bytes(),
    }


def _call(monkeypatch, capfd, payload):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    hooks.pre_tool_use()
    captured = capfd.readouterr()
    assert captured.err == ""
    return json.loads(captured.out)


def _journal(root, kind):
    with closing(sqlite3.connect(root / ".sidegraph" / "index.db")) as conn:
        return conn.execute(
            "SELECT key, detail FROM retrieval_events WHERE kind=? ORDER BY key, detail", (kind,)
        ).fetchall()


def test_fact_only_raw_candidates_match_typed_eligibility(fact_project):
    fact = _fact(fact_project)
    index = HotIndex.open(fact_project / ".sidegraph")
    assert index is not None
    try:
        records = index.records_for(index.anchored_entities(["src/a.py"])["src/a.py"])
        assert [r.decision["id"] for r in records] == [fact.id]
        assert records[0].record_type == "fact"
    finally:
        index.close()


@pytest.mark.parametrize("supporting", [False, True], ids=["standalone", "supporting"])
def test_read_delivers_complete_fact_and_journals_actual_id(
    fact_project, monkeypatch, capfd, supporting
):
    fact = _fact(fact_project, supporting=supporting)
    before = _canonical(fact_project)
    output = _call(
        monkeypatch,
        capfd,
        {
            "session_id": "facts",
            "tool_name": "Read",
            "tool_input": {"file_path": str(fact_project / "src/a.py")},
        },
    )
    context = output["hookSpecificOutput"]["additionalContext"]
    assert "[fact]" in context
    assert fact.id in context and fact.statement in context and fact.source in context
    assert _journal(fact_project, "inject") == [("src/a.py", fact.id)]
    assert _canonical(fact_project) == before


def test_agent_brief_delivers_fact_without_changing_other_input(fact_project, monkeypatch, capfd):
    fact = _fact(fact_project)
    before = _canonical(fact_project)
    tool_input = {"prompt": "Inspect src/a.py", "subagent_type": "Explore", "description": "test"}
    output = _call(
        monkeypatch,
        capfd,
        {
            "session_id": "facts",
            "tool_name": "Agent",
            "tool_input": tool_input,
        },
    )
    updated = output["hookSpecificOutput"]["updatedInput"]
    assert updated["description"] == "test" and updated["subagent_type"] == "Explore"
    assert updated["prompt"].startswith(tool_input["prompt"] + "\n\n")
    assert "[fact]" in updated["prompt"]
    assert fact.id in updated["prompt"] and fact.statement in updated["prompt"]
    assert fact.source in updated["prompt"]
    assert _journal(fact_project, "inject_brief") == [("src/a.py", fact.id)]
    assert _canonical(fact_project) == before


def test_claimed_file_cannot_hide_shared_fact_from_new_file(fact_project, monkeypatch, capfd):
    fact = _fact(fact_project, paths=("src/a.py", "src/b.py"))
    index = HotIndex.open(fact_project / ".sidegraph")
    assert index is not None
    try:
        assert index.claim_file(
            hooks._PRETOOL_FILE_KEY_PREFIX + "facts:-:",
            "src/a.py",
            datetime.now(UTC).isoformat(),
            hooks._FILES_PER_AGENT,
        )
    finally:
        index.close()
    before = _canonical(fact_project)
    output = _call(
        monkeypatch,
        capfd,
        {
            "session_id": "facts",
            "cwd": str(fact_project),
            "tool_name": "Bash",
            "tool_input": {"command": "cat src/a.py src/b.py"},
        },
    )
    context = output["hookSpecificOutput"]["additionalContext"]
    assert fact.statement in context and "Recorded for src/b.py" in context
    assert "Recorded for src/a.py" not in context
    assert _journal(fact_project, "inject") == [("src/b.py", fact.id)]
    assert (
        _call(
            monkeypatch,
            capfd,
            {
                "session_id": "facts",
                "tool_name": "Read",
                "tool_input": {"file_path": str(fact_project / "src/b.py")},
            },
        )
        == {}
    )
    assert _journal(fact_project, "inject") == [("src/b.py", fact.id)]
    assert _canonical(fact_project) == before


@pytest.mark.parametrize(
    "status,age_days,closure,env,eligible",
    [
        ("accepted", 40, None, {}, True),
        ("proposed", 1, None, {}, True),
        ("proposed", 40, None, {}, False),
        ("proposed", 1, None, {"SIDEGRAPH_UNRATIFIED": "off"}, False),
        ("proposed", 40, None, {"SIDEGRAPH_PROPOSAL_WINDOW_DAYS": "0"}, True),
        ("accepted", 10, 1, {}, False),
        ("accepted", 10, -1, {}, False),
        ("rejected", 10, None, {}, False),
        ("deprecated", 10, None, {}, False),
        ("superseded", 10, -1, {}, False),
        ("accepted", -1, None, {}, True),
    ],
)
def test_fact_predicate_matches_explicit_typed_oracle(
    fact_project,
    monkeypatch,
    status,
    age_days,
    closure,
    env,
    eligible,
):
    from sidegraph import store_layout

    now = datetime(2026, 10, 18, tzinfo=UTC)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now if tz is not None else now.replace(tzinfo=None)

    monkeypatch.setattr(store_layout, "datetime", Clock)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    fact = _fact(
        fact_project,
        status=status,
        age_days=age_days,
        valid_to=now + timedelta(days=closure) if closure is not None else None,
        assert_eligible=False,
    )
    store = Store(fact_project / ".sidegraph")
    try:
        index = HotIndex.open(store.path)
        assert index is not None
        try:
            entity = index.anchored_entities(["src/a.py"])["src/a.py"][0]
            expected = _live_facts(store.valid_facts_for_entity(entity))
            assert [f.id for f in expected] == ([fact.id] if eligible else [])
            actual = index.records_for([entity])
            assert [r.decision["id"] for r in actual] == [f.id for f in expected]
        finally:
            index.close()
    finally:
        store.close()


def test_oversize_only_files_do_not_spend_the_call_quota(fact_project, monkeypatch, capfd):
    oversized = _fact(
        fact_project,
        paths=("src/a.py", "src/b.py", "src/c.py"),
        statement="Measured result " + "long detail " * 80 + "only for version 0.14.0, n=20.",
    )
    short = _fact(fact_project, paths=("src/d.py",))
    before = _canonical(fact_project)
    assert (
        _call(
            monkeypatch,
            capfd,
            {
                "session_id": "facts",
                "tool_name": "Read",
                "tool_input": {"file_path": str(fact_project / "src/a.py")},
            },
        )
        == {}
    )
    output = _call(
        monkeypatch,
        capfd,
        {
            "session_id": "facts",
            "cwd": str(fact_project),
            "tool_name": "Bash",
            "tool_input": {"command": "cat src/a.py src/b.py src/c.py src/d.py"},
        },
    )
    context = output["hookSpecificOutput"]["additionalContext"]
    assert short.statement in context and short.source in context
    assert "Recorded for src/d.py" in context and oversized.id not in context
    assert _journal(fact_project, "inject") == [("src/d.py", short.id)]
    with closing(sqlite3.connect(fact_project / ".sidegraph" / "index.db")) as conn:
        assert conn.execute("SELECT key FROM meta WHERE key LIKE 'pretool_file:%'").fetchall() == [
            (hooks._PRETOOL_FILE_KEY_PREFIX + "facts:-:src/d.py",)
        ]
    assert _canonical(fact_project) == before


def test_same_tier_facts_have_recency_then_id_order(fact_project):
    older = _fact(fact_project, age_days=2, fixed_id=f"{3:026d}")
    later_id = _fact(fact_project, age_days=1, fixed_id=f"{2:026d}", assert_eligible=False)
    first_id = _fact(fact_project, age_days=1, fixed_id=f"{1:026d}", assert_eligible=False)
    index = HotIndex.open(fact_project / ".sidegraph")
    assert index is not None
    try:
        records = index.records_for(index.anchored_entities(["src/a.py"])["src/a.py"])
        assert [r.decision["id"] for r in records] == [first_id.id, later_id.id, older.id]
    finally:
        index.close()


def _add_decision(root, title, kind, status="accepted"):
    store = Store(root / ".sidegraph")
    try:
        seed = _Seeder(store)
        decision = seed.decision(title, kind, status=status)
        seed.anchor(decision, "src/a.py")
        store.set_meta(VOLATILE_STALE_KEY, "0")
        return decision
    finally:
        store.close()


def test_two_accepted_mistakes_keep_the_bonus_slot_for_a_fact(fact_project, monkeypatch, capfd):
    fact = _fact(fact_project)
    _add_decision(fact_project, "First mistake", DecisionKind.GOTCHA)
    _add_decision(fact_project, "Second mistake", DecisionKind.LESSON)
    output = _call(
        monkeypatch,
        capfd,
        {
            "session_id": "facts",
            "tool_name": "Read",
            "tool_input": {"file_path": str(fact_project / "src/a.py")},
        },
    )
    context = output["hookSpecificOutput"]["additionalContext"]
    assert "(3 of 3, mistakes first)" in context and fact.statement in context
    assert context.index("[gotcha]") < context.index("[fact]")
    assert context.index("[lesson]") < context.index("[fact]")


def test_facts_do_not_displace_accepted_decisions(fact_project, monkeypatch, capfd):
    _fact(fact_project)
    _add_decision(fact_project, "Mistake", DecisionKind.GOTCHA)
    _add_decision(fact_project, "Choice", DecisionKind.ADR)
    output = _call(
        monkeypatch,
        capfd,
        {
            "session_id": "facts",
            "tool_name": "Read",
            "tool_input": {"file_path": str(fact_project / "src/a.py")},
        },
    )
    context = output["hookSpecificOutput"]["additionalContext"]
    assert "[gotcha]" in context and "[adr]" in context
    assert "[fact]" not in context


def test_accepted_facts_precede_a_surfacing_proposed_mistake(fact_project, monkeypatch, capfd):
    from sidegraph import store_layout

    first = _fact(fact_project, fixed_id=f"{1:026d}")
    second = _fact(fact_project, fixed_id=f"{2:026d}", assert_eligible=False)
    proposed = _add_decision(fact_project, "Unratified mistake", DecisionKind.GOTCHA, "proposed")
    assert store_layout.proposal_surfaces(proposed.valid_from)
    output = _call(
        monkeypatch,
        capfd,
        {
            "session_id": "facts",
            "tool_name": "Read",
            "tool_input": {"file_path": str(fact_project / "src/a.py")},
        },
    )
    context = output["hookSpecificOutput"]["additionalContext"]
    assert first.id in context and second.id in context
    assert proposed.id not in context


@pytest.mark.parametrize("callback_raises", [False, True])
def test_fact_query_failure_keeps_decisions_and_is_observable(fact_project, callback_raises):
    _fact(fact_project)
    decision = _add_decision(fact_project, "Still available", DecisionKind.ADR)
    before = _canonical(fact_project)
    with closing(sqlite3.connect(fact_project / ".sidegraph" / "index.db")) as conn, conn:
        conn.execute("DROP TABLE facts")
    errors = []

    def observe(error):
        errors.append(type(error))
        if callback_raises:
            raise RuntimeError("Synthetic callback failure")

    index = HotIndex.open(fact_project / ".sidegraph")
    assert index is not None
    try:
        records = index.records_for(
            index.anchored_entities(["src/a.py"])["src/a.py"],
            on_error=observe,
        )
        assert [r.decision["id"] for r in records] == [decision.id]
        assert errors and all(t is sqlite3.OperationalError for t in errors)
    finally:
        index.close()
    assert _canonical(fact_project) == before


def test_decision_query_failure_does_not_erase_independent_fact(fact_project):
    fact = _fact(fact_project)
    before = _canonical(fact_project)
    with closing(sqlite3.connect(fact_project / ".sidegraph" / "index.db")) as conn, conn:
        conn.execute("DROP TABLE decisions")
    errors = []
    index = HotIndex.open(fact_project / ".sidegraph")
    assert index is not None
    try:
        records = index.records_for(
            index.anchored_entities(["src/a.py"])["src/a.py"],
            on_error=lambda error: errors.append(type(error)),
        )
        assert [r.decision["id"] for r in records] == [fact.id]
        assert errors and all(t is sqlite3.OperationalError for t in errors)
    finally:
        index.close()
    assert _canonical(fact_project) == before


@pytest.mark.parametrize(
    "damage",
    [
        "json",
        "source_type",
        "empty_source",
        "statement_type",
        "naive_time",
        "id_mismatch",
    ],
)
def test_malformed_fact_row_does_not_hide_other_records(fact_project, damage):
    good = _fact(fact_project, age_days=1, statement="Valid n=20 result on version 0.14.0.")
    bad = _fact(
        fact_project,
        age_days=0,
        assert_eligible=False,
        statement="Damaged row must never displace the valid result.",
    )
    decision = _add_decision(fact_project, "Still available", DecisionKind.ADR)
    store = Store(fact_project / ".sidegraph")
    try:
        entity_id = store.bindings_for_record(good.id)[0].entity_id
        entity = store.get_entity(entity_id)
        seed = _Seeder(store)
        seed.anchor(bad, "src/a.py", entity=entity)
        seed.anchor(decision, "src/a.py", entity=entity)
        store.set_meta(VOLATILE_STALE_KEY, "0")
    finally:
        store.close()
    before = _canonical(fact_project)
    raw = bad.model_dump(mode="json")
    if damage == "source_type":
        raw["source"] = []
    elif damage == "empty_source":
        raw["source"] = " \n "
    elif damage == "statement_type":
        raw["statement"] = 42
    elif damage == "naive_time":
        raw["valid_from"] = "2026-10-18T00:00:00"
    elif damage == "id_mismatch":
        raw["id"] = f"{9:026d}"
    data = "{" if damage == "json" else json.dumps(raw)
    with closing(sqlite3.connect(fact_project / ".sidegraph" / "index.db")) as conn, conn:
        conn.execute("UPDATE facts SET data=? WHERE id=?", (data, bad.id))
    errors = []
    index = HotIndex.open(fact_project / ".sidegraph")
    assert index is not None
    try:
        records = index.records_for(
            [entity_id],
            on_error=lambda error: errors.append(type(error)),
        )
        assert [r.decision["id"] for r in records] == [decision.id, good.id]
        assert errors == []
    finally:
        index.close()
    assert _canonical(fact_project) == before


@pytest.mark.parametrize("binding_status", ["live", "degraded", "orphaned", "ambiguous"])
@pytest.mark.parametrize("stale", [False, True])
def test_facts_require_certified_live_or_degraded_bindings(fact_project, binding_status, stale):
    fact = _fact(fact_project)
    before = _canonical(fact_project)
    with closing(sqlite3.connect(fact_project / ".sidegraph" / "index.db")) as conn, conn:
        rows = conn.execute("SELECT rowid, data FROM anchor_bindings").fetchall()
        for rowid, data in rows:
            binding = json.loads(data)
            binding["status"] = binding_status
            conn.execute(
                "UPDATE anchor_bindings SET data=? WHERE rowid=?", (json.dumps(binding), rowid)
            )
        conn.execute("UPDATE meta SET value=? WHERE key=?", (str(int(stale)), VOLATILE_STALE_KEY))
    index = HotIndex.open(fact_project / ".sidegraph")
    assert index is not None
    try:
        records = index.records_for(index.anchored_entities(["src/a.py"])["src/a.py"])
        expected = [fact.id] if not stale and binding_status in ("live", "degraded") else []
        assert [r.decision["id"] for r in records] == expected
    finally:
        index.close()
    assert _canonical(fact_project) == before


@pytest.mark.parametrize("suffix", ["", "+requires"])
def test_both_supported_index_stamps_deliver_facts(fact_project, suffix):
    from sidegraph.schema import SCHEMA_VERSION

    fact = _fact(fact_project)
    with closing(sqlite3.connect(fact_project / ".sidegraph" / "index.db")) as conn, conn:
        conn.execute(
            "UPDATE meta SET value=? WHERE key='schema_version'", (SCHEMA_VERSION + suffix,)
        )
    index = HotIndex.open(fact_project / ".sidegraph")
    assert index is not None
    try:
        assert [
            r.decision["id"]
            for r in index.records_for(index.anchored_entities(["src/a.py"])["src/a.py"])
        ] == [fact.id]
    finally:
        index.close()


def test_proposed_fact_keeps_trust_and_full_qualifiers(fact_project, monkeypatch, capfd):
    from sidegraph import store_layout

    monkeypatch.setenv("SIDEGRAPH_PROPOSAL_WINDOW_DAYS", "0")
    fact = _fact(fact_project, status="proposed")
    assert store_layout.proposal_surfaces(fact.valid_from)
    output = _call(
        monkeypatch,
        capfd,
        {
            "session_id": "facts",
            "tool_name": "Read",
            "tool_input": {"file_path": str(fact_project / "src/a.py")},
        },
    )
    context = output["hookSpecificOutput"]["additionalContext"]
    assert (
        f"- [fact] [unratified] {fact.statement} — Source: {fact.source} (id {fact.id})" in context
    )
    assert _journal(fact_project, "inject") == [("src/a.py", fact.id)]


@pytest.mark.parametrize("length", [512, 513])
def test_whole_fact_line_bound_includes_source_and_id(fact_project, monkeypatch, capfd, length):
    record_id = f"{1:026d}"
    wrapper = f"- [fact]  — Source: S (id {record_id})"
    statement = "n=20; v0.14; " + "x" * (length - len(wrapper) - len("n=20; v0.14; "))
    fact = _fact(fact_project, statement=statement, source="S", fixed_id=record_id)
    output = _call(
        monkeypatch,
        capfd,
        {
            "session_id": "facts",
            "tool_name": "Read",
            "tool_input": {"file_path": str(fact_project / "src/a.py")},
        },
    )
    expected = f"- [fact] {statement} — Source: S (id {record_id})"
    assert len(expected) == length
    if length == 512:
        assert expected in output["hookSpecificOutput"]["additionalContext"].splitlines()
        assert _journal(fact_project, "inject") == [("src/a.py", fact.id)]
    else:
        assert output == {} and _journal(fact_project, "inject") == []


def test_oversized_fact_does_not_hide_later_short_fact(fact_project, monkeypatch, capfd):
    large = _fact(fact_project, statement="x" * 600)
    short = _fact(fact_project, age_days=1, assert_eligible=False)
    output = _call(
        monkeypatch,
        capfd,
        {
            "session_id": "facts",
            "tool_name": "Read",
            "tool_input": {"file_path": str(fact_project / "src/a.py")},
        },
    )
    context = output["hookSpecificOutput"]["additionalContext"]
    assert short.statement in context and large.id not in context
    assert "(1 of 1, mistakes first)" in context and "More:" not in context
    assert _journal(fact_project, "inject") == [("src/a.py", short.id)]


def test_support_link_does_not_inherit_the_decision_anchor(fact_project):
    fact = _fact(fact_project, supporting=True, paths=("src/b.py",))
    store = Store(fact_project / ".sidegraph")
    try:
        seed = _Seeder(store)
        seed.anchor(store.get_decision(fact.supports[0]), "src/a.py")
        store.set_meta(VOLATILE_STALE_KEY, "0")
    finally:
        store.close()
    index = HotIndex.open(fact_project / ".sidegraph")
    assert index is not None
    try:
        records = index.records_for(index.anchored_entities(["src/a.py"])["src/a.py"])
        assert [r.decision["id"] for r in records] == fact.supports
        assert all(r.record_type == "decision" for r in records)
    finally:
        index.close()


def test_mixed_agent_character_cap_drops_whole_group_and_journals_only_final_plan(
    fact_project,
    monkeypatch,
    capfd,
):
    from sidegraph.hot_index import Record
    from sidegraph.schema import Decision

    by_file = {}
    for path in ("src/a.py", "src/b.py", "src/c.py"):
        by_file[path] = [
            _fact(
                fact_project,
                paths=(path,),
                statement="n=20; "
                + "x" * (512 - len(f"- [fact] n=20;  — Source: S (id {'0' * 26})")),
                source="S",
                assert_eligible=False,
            )
            for _ in range(2)
        ]
    store = Store(fact_project / ".sidegraph")
    try:
        decision = store.add_decision(
            Decision(
                title="A long decision title " * 8,
                kind=DecisionKind.ADR,
                status="accepted",
                context="Synthetic mixed-cap fixture",
                valid_from=datetime(2026, 10, 18, tzinfo=UTC),
                choice="A long choice without a sentence stop " * 8,
                provenance=Provenance(source="manual"),
            )
        )
        _Seeder(store).anchor(decision, "src/c.py")
        store.set_meta(VOLATILE_STALE_KEY, "0")
    finally:
        store.close()
    later = _fact(fact_project, paths=("src/d.py",), assert_eligible=False)
    before = _canonical(fact_project)
    prompt = "Inspect src/a.py src/b.py src/c.py src/d.py"
    output = _call(
        monkeypatch,
        capfd,
        {
            "session_id": "facts",
            "tool_name": "Agent",
            "tool_input": {"prompt": prompt},
        },
    )
    block = output["hookSpecificOutput"]["updatedInput"]["prompt"][len(prompt) + 2 :]
    assert len(block) <= 3000
    assert block.startswith(
        "--- Recorded decisions for the files in this task (added by Sidegraph) ---"
    )
    assert "src/a.py:" in block and "src/b.py:" in block
    assert "src/c.py:" not in block and "src/d.py:" not in block
    assert later.id not in block and decision.id not in block
    # With the complete next group, the block would exceed the cap; no line was truncated.
    plan = [
        (None, path, [Record(f.model_dump(mode="json"), False, "fact") for f in facts])
        for path, facts in list(by_file.items())[:2]
    ]
    next_group = [
        Record(decision.model_dump(mode="json"), False),
        Record(by_file["src/c.py"][0].model_dump(mode="json"), False, "fact"),
    ]
    assert len(hooks._render_brief([*plan, (None, "src/c.py", next_group)])) > 3000
    expected = sorted(
        (path, fact.id) for path in ("src/a.py", "src/b.py") for fact in by_file[path]
    )
    assert _journal(fact_project, "inject_brief") == expected
    for facts in list(by_file.values())[:2]:
        for fact in facts:
            assert fact.statement in block and fact.source in block
    assert _canonical(fact_project) == before


@pytest.mark.parametrize(
    "damage",
    [
        "json",
        "missing_kind",
        "id_list",
        "missing_title",
        "choice_list",
        "naive_time",
    ],
)
def test_damaged_decision_cannot_suppress_valid_decision_or_fact(fact_project, damage):
    fact = _fact(fact_project)
    good = _add_decision(fact_project, "Valid decision survives", DecisionKind.ADR)
    bad = _add_decision(fact_project, "Damaged decision", DecisionKind.ADR)
    store = Store(fact_project / ".sidegraph")
    try:
        entity_id = store.bindings_for_record(fact.id)[0].entity_id
        entity = store.get_entity(entity_id)
        seed = _Seeder(store)
        seed.anchor(good, "src/a.py", entity=entity)
        seed.anchor(bad, "src/a.py", entity=entity)
        store.set_meta(VOLATILE_STALE_KEY, "0")
    finally:
        store.close()
    before = _canonical(fact_project)
    raw = bad.model_dump(mode="json")
    if damage == "missing_kind":
        del raw["kind"]
    elif damage == "id_list":
        raw["id"] = []
    elif damage == "missing_title":
        del raw["title"]
    elif damage == "choice_list":
        raw["choice"] = []
    elif damage == "naive_time":
        raw["valid_from"] = "2026-10-18T00:00:00"
    with closing(sqlite3.connect(fact_project / ".sidegraph" / "index.db")) as conn, conn:
        conn.execute(
            "UPDATE decisions SET data=? WHERE id=?",
            (
                "{" if damage == "json" else json.dumps(raw),
                bad.id,
            ),
        )
    errors = []
    index = HotIndex.open(fact_project / ".sidegraph")
    assert index is not None
    try:
        records = index.records_for([entity_id], on_error=errors.append)
        assert [r.decision["id"] for r in records] == [good.id, fact.id]
        assert errors == []
    finally:
        index.close()
    assert _canonical(fact_project) == before


@pytest.mark.parametrize("callback_raises", [False, True])
def test_unexpected_lookup_error_preserves_other_table_and_reports(
    fact_project,
    monkeypatch,
    callback_raises,
):
    _fact(fact_project)
    decision = _add_decision(fact_project, "Still available", DecisionKind.ADR)
    errors = []

    def unexpected(record_ids):
        raise RuntimeError("Synthetic internal lookup failure")

    def observe(error):
        errors.append(type(error))
        if callback_raises:
            raise ValueError("Synthetic diagnostic failure")

    index = HotIndex.open(fact_project / ".sidegraph")
    assert index is not None
    try:
        monkeypatch.setattr(index, "_facts_for_bindings", unexpected)
        records = index.records_for(
            index.anchored_entities(["src/a.py"])["src/a.py"],
            on_error=observe,
        )
        assert [r.decision["id"] for r in records] == [decision.id]
        assert errors and all(error is RuntimeError for error in errors)
    finally:
        index.close()
