"""The integrity registry: one list of checks that says what a human should fix.

Unit tests for ``sidegraph.integrity`` (the portable core): ``run``'s contract (never raises,
"not run" is not "clean"), and each detector on its own inputs. The hook, doctor and stats
surfaces that consume the registry have their own test modules. The seeding helpers at the top
are imported by those modules.
see design/superpowers/specs/2026-10-02-integrity-self-check-design.md (D1, D4, T10, T18)
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError
from ulid import ULID

from sidegraph import integrity
from sidegraph.gitio import open_index_ro
from sidegraph.integrity import CHECKS, Check, Inputs, Problem, run
from sidegraph.schema import (
    AnchorBinding,
    Decision,
    DecisionKind,
    DecisionStatus,
    Descriptor,
    Entity,
    Fact,
    Provenance,
)
from sidegraph.store import SKIPPED_CANONICAL_KEY, VOLATILE_STALE_KEY, Store

NOW = datetime.now(UTC)


def add_record(
    store: Store,
    *,
    fact: bool = False,
    age_days: float = 0,
    leaves: tuple[str, ...] = ("orphaned",),
    status: DecisionStatus = DecisionStatus.ACCEPTED,
    now: datetime = NOW,
) -> str:
    """A decision (or fact) whose id was minted ``age_days`` ago, with one Tier-2 binding per
    entry of ``leaves`` (the entry is the binding's status). Returns the record id."""
    when = now - timedelta(days=age_days)
    rid = str(ULID.from_datetime(when))
    prov = Provenance(source="manual")
    if fact:
        store.add_fact(
            Fact(
                id=rid,
                statement=f"fact {rid}",
                source="s",
                status=status,
                valid_from=when,
                provenance=prov,
            )
        )
    else:
        store.add_decision(
            Decision(
                id=rid,
                title=f"decision {rid}",
                kind=DecisionKind.GOTCHA,
                status=status,
                context="c",
                choice="ch",
                valid_from=when,
                provenance=prov,
            )
        )
    for i, leaf_status in enumerate(leaves):
        entity = store.upsert_entity(
            Entity(
                canonical_name=f"{rid}-{i}",
                descriptor=Descriptor(name=f"{rid}-{i}", file_path=f"src/{rid}_{i}.py"),
            )
        )
        store.add_binding(
            AnchorBinding(record_id=rid, entity_id=entity.entity_id, tier=2, status=leaf_status)
        )
    return rid


def settled(store: Store) -> Store:
    """Mark the index as holding computed binding statuses, as a finished sync does."""
    store.set_meta(VOLATILE_STALE_KEY, "0")
    return store


def inputs_for(db: Store, **over) -> Inputs:
    """``Inputs`` over a store the way SessionStart builds them: its own read-only index
    connection (the caller closes it through ``inputs.index.close()``)."""
    kwargs = dict(store_dir=Path(db.path), now=NOW, store=db, index=open_index_ro(db.path))
    kwargs.update(over)
    return Inputs(**kwargs)


def check(check_id: str) -> Check:
    return next(c for c in CHECKS if c.id == check_id)


def detect(check_id: str, inputs: Inputs):
    return check(check_id).detect(inputs)


@pytest.fixture
def store(tmp_path) -> Store:
    return settled(Store(tmp_path / "s"))


# -- run: the contract ----------------------------------------------------------------------


def _problem(check_id: str, **over) -> Problem:
    base = dict(
        check=check_id, severity="advisory", summary="s", fix="f", line="Sidegraph: l", notice=None
    )
    base.update(over)
    return Problem(**base)


def _stub(check_id: str, surfaces, detect_fn) -> Check:
    return Check(id=check_id, surfaces=frozenset(surfaces), detect=detect_fn)


def test_run_keeps_registry_order_and_reports_what_ran_clean(tmp_path):
    checks = (
        _stub("a", {"session"}, lambda i: _problem("a")),
        _stub("b", {"session"}, lambda i: None),
        _stub("c", {"session"}, lambda i: _problem("c")),
    )
    result = run(Inputs(store_dir=tmp_path, now=NOW), "session", checks)
    assert [p.check for p in result.problems] == ["a", "c"]
    assert result.clean == frozenset({"b"})


def test_run_calls_only_the_checks_listed_for_the_surface(tmp_path):
    called: list[str] = []

    def spy(name):
        def detect_fn(i):
            called.append(name)

        return detect_fn

    checks = (
        _stub("s", {"session"}, spy("s")),
        _stub("d", {"doctor"}, spy("d")),
        _stub("sd", {"session", "doctor", "stats"}, spy("sd")),
    )
    run(Inputs(store_dir=tmp_path, now=NOW), "doctor", checks)
    assert called == ["d", "sd"]


def test_t10_a_detector_that_raises_costs_nothing_else(tmp_path):
    """Red against nothing (the registry is new); mutation M9 drops the per-detector try."""

    def boom(i):
        raise RuntimeError("boom")

    checks = (
        _stub("first", {"session"}, lambda i: _problem("first")),
        _stub("boom", {"session"}, boom),
        _stub("last", {"session"}, lambda i: _problem("last")),
    )
    result = run(Inputs(store_dir=tmp_path, now=NOW), "session", checks)
    assert [p.check for p in result.problems] == ["first", "last"]
    assert "boom" not in result.clean


def test_t18_not_run_is_neither_a_problem_nor_clean(tmp_path):
    """Mutation M12 treats ``_NotRun`` as clean, which would delete a notice key on evidence
    that was never read."""

    def not_run(i):
        raise integrity._NotRun

    checks = (_stub("blind", {"session"}, not_run), _stub("ok", {"session"}, lambda i: None))
    result = run(Inputs(store_dir=tmp_path, now=NOW), "session", checks)
    assert result.problems == []
    assert result.clean == frozenset({"ok"})


def test_registry_order_is_the_sessionstart_line_order():
    assert [c.id for c in CHECKS] == [
        "store-unreadable",
        "pending-ratification",
        "code-drift",
        "graph-borrowed",
        "graph-stale",
        "graph-missing",
        "orphaned-records",
        "store-files-skipped",
        "refresh-hook-missing",
        "store-uncommitted",
        "branch-only-records",
    ]


# -- 0 store-unreadable ---------------------------------------------------------------------

VERIFY_SENTENCE = (
    "Run `sidegraph-verify` from the repository root to find the file; a merge conflict left "
    "in .sidegraph/ is the usual cause."
)


def _validation_error() -> ValidationError:
    try:
        Decision.model_validate({})
    except ValidationError as e:
        return e
    raise AssertionError("unreachable")


@pytest.mark.parametrize(
    ("error", "fix_sentence"),
    [
        (json.JSONDecodeError("Expecting value", "x", 0), VERIFY_SENTENCE),
        (_validation_error(), VERIFY_SENTENCE),
        (
            sqlite3.DatabaseError("file is not a database"),
            "The index is derived and safe to delete: remove {dir}/index.db and start a new "
            "session.",
        ),
        (
            sqlite3.OperationalError("attempt to write a readonly database"),
            "The index is derived and safe to delete: remove {dir}/index.db and start a new "
            "session.",
        ),
        (
            ValueError(
                "store format marker 'x'; migration tooling is deferred — use a fresh store"
            ),
            "This store was written by a different Sidegraph version: upgrade Sidegraph (or use "
            "the version that wrote it).",
        ),
        (
            ValueError("something else"),
            "Run `sidegraph-verify` from the repository root for details.",
        ),
        (KeyError("boom"), "Run `sidegraph-verify` from the repository root for details."),
    ],
)
def test_store_unreadable_names_the_cause_and_its_own_fix(tmp_path, error, fix_sentence):
    """Mutation M1b gives every exception the same fix sentence."""
    store_dir = tmp_path / ".sidegraph"
    store_dir.mkdir()
    (store_dir / "index.db").write_bytes(b"")  # an index exists, so removing it is advice
    result = run(
        Inputs(store_dir=store_dir, now=NOW, open_error=error),
        "session",
        (integrity.STORE_UNREADABLE,),
    )
    (problem,) = result.problems
    assert problem.severity == "broken"
    assert problem.check == "store-unreadable"
    assert problem.notice is not None
    assert problem.notice.startswith(f"Sidegraph cannot open its store at {store_dir}, so memory")
    first_line = " ".join((str(error).splitlines() or [""])[0].split())[:200]
    assert f"({type(error).__name__}: {first_line})" in problem.notice
    assert problem.notice.endswith(fix_sentence.format(dir=store_dir))
    assert problem.line == f"{problem.notice} Sidegraph memory tools will fail until it is fixed."
    assert (problem.summary, problem.fix) == ("store cannot be opened", "sidegraph-verify")


def test_store_unreadable_cuts_a_long_message_at_200_characters(tmp_path):
    error = ValueError("x" * 500)
    result = run(
        Inputs(store_dir=tmp_path, now=NOW, open_error=error),
        "session",
        (integrity.STORE_UNREADABLE,),
    )
    assert "x" * 200 + ")" in result.problems[0].notice
    assert "x" * 201 not in result.problems[0].notice


def test_store_unreadable_puts_only_the_first_line_of_a_multi_line_message_in_the_text(tmp_path):
    """A pydantic ``ValidationError`` quotes the record (``input_value=...``) over several lines;
    the notice goes to the user's warning area and the line to the model, so it is the first line,
    whitespace collapsed."""
    try:
        Decision.model_validate({"kind": "SECRET-RECORD-TEXT"})
    except ValidationError as e:
        error = e
    result = run(
        Inputs(store_dir=tmp_path, now=NOW, open_error=error),
        "session",
        (integrity.STORE_UNREADABLE,),
    )
    (problem,) = result.problems
    first = str(error).splitlines()[0]
    assert f"(ValidationError: {first})" in problem.notice
    assert "\n" not in problem.notice and "\n" not in problem.line
    assert "input_value" not in problem.notice and "SECRET-RECORD-TEXT" not in problem.line

    spaced = run(
        Inputs(store_dir=tmp_path, now=NOW, open_error=ValueError("a   b\n  c")),
        "session",
        (integrity.STORE_UNREADABLE,),
    )
    assert "(ValueError: a b)" in spaced.problems[0].notice
    empty = run(
        Inputs(store_dir=tmp_path, now=NOW, open_error=ValueError()),
        "session",
        (integrity.STORE_UNREADABLE,),
    )
    assert "(ValueError: )" in empty.problems[0].notice


def _cannot_open() -> sqlite3.OperationalError:
    error = sqlite3.OperationalError("unable to open database file")
    error.sqlite_errorcode = sqlite3.SQLITE_CANTOPEN
    return error


@pytest.mark.parametrize(
    ("error", "index_exists"),
    [
        (_cannot_open(), True),  # SQLITE_CANTOPEN: the index is not what failed to open
        (sqlite3.DatabaseError("file is not a database"), False),  # no index.db to remove
    ],
)
def test_no_index_to_remove_gets_the_generic_fix_not_an_impossible_one(
    tmp_path, error, index_exists
):
    store_dir = tmp_path / ".sidegraph"
    store_dir.mkdir()
    if index_exists:
        (store_dir / "index.db").write_bytes(b"")
    result = run(
        Inputs(store_dir=store_dir, now=NOW, open_error=error),
        "session",
        (integrity.STORE_UNREADABLE,),
    )
    notice = result.problems[0].notice
    assert notice.endswith("Run `sidegraph-verify` from the repository root for details.")
    assert "index.db" not in notice


def test_store_unreadable_is_clean_when_the_store_opened(tmp_path):
    result = run(Inputs(store_dir=tmp_path, now=NOW), "session", (integrity.STORE_UNREADABLE,))
    assert result.problems == []
    assert result.clean == frozenset({"store-unreadable"})


# -- 1 pending-ratification -----------------------------------------------------------------


def _proposed(store: Store, age_days: int) -> None:
    store.add_decision(
        Decision(
            title="p",
            kind=DecisionKind.GOTCHA,
            status=DecisionStatus.PROPOSED,
            context="c",
            choice="ch",
            valid_from=NOW - timedelta(days=age_days),
            provenance=Provenance(source="manual"),
        )
    )


def test_pending_line_is_todays_text_and_the_notice_waits_for_thirty_days(store):
    _proposed(store, 29)
    problem = detect("pending-ratification", inputs_for(store))
    assert problem.severity == "advisory"
    assert problem.line == (
        "Sidegraph: 1 record(s) awaiting ratification (1 decisions, 0 facts, 0 domains; "
        "oldest 29 days) — review with the ratify MCP tool or sidegraph-ratify."
    )
    assert problem.notice is None


def test_t7_pending_notice_at_exactly_thirty_days(store):
    """Mutation M4 compares with ``>`` instead of ``>=``."""
    _proposed(store, 30)
    problem = detect("pending-ratification", inputs_for(store))
    assert problem.notice == (
        "Sidegraph: 1 record(s) await ratification, the oldest for 30 days. Review them with "
        "`sidegraph-ratify`, or ask the agent to use the ratify tool."
    )


def test_pending_is_clean_for_an_empty_queue(store):
    assert detect("pending-ratification", inputs_for(store)) is None


def test_pending_is_not_run_without_a_store_or_with_the_switch_off(store, monkeypatch):
    _proposed(store, 40)
    with pytest.raises(integrity._NotRun):
        detect("pending-ratification", inputs_for(store, store=None))
    monkeypatch.setenv("SIDEGRAPH_RATIFY_NUDGE", "off")
    with pytest.raises(integrity._NotRun):
        detect("pending-ratification", inputs_for(store))


# -- 2 code-drift ---------------------------------------------------------------------------


def test_drift_line_is_todays_text_for_the_refresh_s_count(tmp_path):
    problem = detect("code-drift", Inputs(store_dir=tmp_path, now=NOW, drift_count=3))
    assert problem.line == (
        "Sidegraph: 3 record(s) are anchored to code that changed after their capture — "
        "task-relevant ones carry a [drifted] tag in retrieval; full list: sidegraph-doctor; "
        "supersede any that no longer hold."
    )
    assert problem.notice is None


def test_drift_zero_is_clean_and_none_is_not_run(tmp_path, monkeypatch):
    assert detect("code-drift", Inputs(store_dir=tmp_path, now=NOW, drift_count=0)) is None
    with pytest.raises(integrity._NotRun):
        detect("code-drift", Inputs(store_dir=tmp_path, now=NOW, drift_count=None))
    monkeypatch.setenv("SIDEGRAPH_DRIFT_NUDGE", "off")
    with pytest.raises(integrity._NotRun):
        detect("code-drift", Inputs(store_dir=tmp_path, now=NOW, drift_count=4))


# -- 3 graph-borrowed -----------------------------------------------------------------------


class _Reader:
    """The slice of ``GraphifyReader`` the registry touches."""

    def __init__(self, path: Path, freshness=None):
        self.path = path
        self._freshness = freshness
        self.calls = 0

    def freshness(self):
        self.calls += 1
        return self._freshness


def test_borrowed_line_names_the_main_checkouts_graph(tmp_path):
    reader = _Reader(tmp_path / "main" / "graph.json")
    inputs = Inputs(store_dir=tmp_path, now=NOW, reader=reader, borrowed_from=tmp_path / "main")
    problem = detect("graph-borrowed", inputs)
    assert problem.line == (
        "Sidegraph: this worktree has no code graph of its own, so memory reads the main "
        f"checkout's ({reader.path}); code that exists only on this branch is not in it."
    )
    assert problem.notice is None
    assert detect("graph-borrowed", Inputs(store_dir=tmp_path, now=NOW, reader=reader)) is None


# -- 4 graph-stale --------------------------------------------------------------------------


def _freshness(state, **over):
    from sidegraph.freshness import GraphFreshness

    base = dict(
        state=state,
        built_at="a" * 40,
        head="b" * 40,
        in_history=True,
        commits_behind=3,
        changed=2,
        sample=["pkg/m.py", "pkg/n.py"],
    )
    base.update(over)
    return GraphFreshness(**base)


def test_stale_graph_texts_own_and_borrowed(tmp_path):
    reader = _Reader(tmp_path / "graphify-out" / "graph.json", _freshness("stale"))
    own = detect("graph-stale", Inputs(store_dir=tmp_path, now=NOW, reader=reader))
    phrase = "built at aaaaaaa, 3 commits behind HEAD, 2 files changed since"
    assert own.severity == "degraded"
    assert own.line == (
        f"Sidegraph: the code graph is stale ({phrase}), so memory cannot see or anchor to "
        "code added after the build. Rebuild it from the repository root: `graphify update .`"
    )
    assert own.notice == own.line
    assert (own.summary, own.fix) == ("code graph stale", "graphify update .")
    assert own.findings == (
        (
            str(reader.path),
            f"the code graph is stale: {phrase} (e.g. pkg/m.py, pkg/n.py); memory cannot see or "
            "anchor to code added after the build — rebuild from the repository root with "
            "`graphify update .`, then run `sidegraph-sync`",
        ),
    )
    main = tmp_path / "main"
    borrowed = detect(
        "graph-stale", Inputs(store_dir=tmp_path, now=NOW, reader=reader, borrowed_from=main)
    )
    assert borrowed.line == (
        f"Sidegraph: the main checkout's code graph ({main}) is stale ({phrase}): rebuild it "
        "there with `graphify update .`"
    )
    assert borrowed.notice == borrowed.line


def test_stale_detail_ends_in_an_ellipsis_when_more_files_changed(tmp_path):
    reader = _Reader(tmp_path / "g.json", _freshness("stale", changed=7))
    problem = detect("graph-stale", Inputs(store_dir=tmp_path, now=NOW, reader=reader))
    assert "(e.g. pkg/m.py, pkg/n.py, …)" in problem.findings[0][1]


def test_fresh_graph_is_clean_and_unknown_or_no_reader_is_not_run(tmp_path):
    fresh = Inputs(store_dir=tmp_path, now=NOW, reader=_Reader(tmp_path, _freshness("fresh")))
    assert detect("graph-stale", fresh) is None
    unknown = Inputs(
        store_dir=tmp_path, now=NOW, reader=_Reader(tmp_path, _freshness("unknown", reason="git"))
    )
    with pytest.raises(integrity._NotRun):
        detect("graph-stale", unknown)
    with pytest.raises(integrity._NotRun):
        detect("graph-stale", Inputs(store_dir=tmp_path, now=NOW))


def test_freshness_is_computed_once_per_inputs_and_a_known_one_is_used(tmp_path):
    reader = _Reader(tmp_path, _freshness("stale"))
    inputs = Inputs(store_dir=tmp_path, now=NOW, reader=reader)
    inputs.freshness()
    inputs.freshness()
    assert reader.calls == 1
    known = Inputs(store_dir=tmp_path, now=NOW, reader=reader, known_freshness=_freshness("fresh"))
    assert known.freshness().state == "fresh"
    assert reader.calls == 1


# -- 6 graph-missing ------------------------------------------------------------------------


def test_t3_missing_graph_names_the_path_and_the_build_command(tmp_path):
    path = tmp_path / "graphify-out" / "graph.json"
    problem = detect("graph-missing", Inputs(store_dir=tmp_path, now=NOW, graph_path=path))
    assert problem.severity == "advisory"
    assert problem.line == (
        f"Sidegraph: no code graph at {path}, so memory cannot match files to records or "
        "anchor new ones. Build it from the repository root: `graphify update .`"
    )
    assert problem.notice is None
    assert (problem.summary, problem.fix) == ("code graph missing", "graphify update .")


def test_an_unreadable_graph_file_gets_the_could_not_be_read_variant(tmp_path):
    path = tmp_path / "graph.json"
    path.write_text("{not json")
    problem = detect("graph-missing", Inputs(store_dir=tmp_path, now=NOW, graph_path=path))
    assert problem.line == (
        f"Sidegraph: the code graph at {path} could not be read, so memory cannot match files "
        "to records or anchor new ones. Rebuild it from the repository root: `graphify update .`"
    )
    assert problem.summary == "code graph unreadable"


def test_graph_missing_is_clean_with_a_reader_and_not_run_without_a_path(tmp_path, monkeypatch):
    reader = _Reader(tmp_path)
    clean = Inputs(store_dir=tmp_path, now=NOW, reader=reader, graph_path=tmp_path / "g.json")
    assert detect("graph-missing", clean) is None
    with pytest.raises(integrity._NotRun):
        detect("graph-missing", Inputs(store_dir=tmp_path, now=NOW))
    monkeypatch.setattr(integrity, "path_state", lambda p: "unknown")
    with pytest.raises(integrity._NotRun):
        detect("graph-missing", Inputs(store_dir=tmp_path, now=NOW, graph_path=tmp_path / "g.json"))


# -- 7 orphaned-records ---------------------------------------------------------------------


def _orphaned(store: Store, **over):
    inputs = inputs_for(store, **over)
    try:
        return detect("orphaned-records", inputs)
    finally:
        inputs.index.close()


def test_t5_three_recent_records_with_every_leaf_orphaned_are_degraded(store):
    for _ in range(3):
        add_record(store, age_days=1)
    problem = _orphaned(store)
    assert problem.severity == "degraded"
    assert problem.line == (
        "Sidegraph: 3 of 3 open record(s) have every code anchor orphaned, so retrieval reaches "
        "them only through their file or domain. If the graph is stale, rebuilding it "
        "re-anchors them; otherwise `sidegraph-doctor` lists them and the heal-anchors skill "
        "repairs them."
    )
    assert problem.notice == problem.line
    assert (problem.summary, problem.fix) == (
        "3 record(s) with every anchor orphaned",
        "sidegraph-doctor",
    )


def test_t5_old_orphans_are_curation_debt_not_an_alarm(store):
    """Mutation M2 ignores record age: seven 60-day-old orphans would warn the human daily."""
    for _ in range(7):
        add_record(store, age_days=60)
    problem = _orphaned(store)
    assert problem.severity == "advisory"
    assert problem.line.startswith("Sidegraph: 7 of 7 open record(s) have every code anchor")
    assert problem.notice is None


def test_t5_a_fact_counts_like_a_decision(store):
    for _ in range(2):
        add_record(store, age_days=1)
    add_record(store, fact=True, age_days=1)
    problem = _orphaned(store)
    assert problem.severity == "degraded"
    assert problem.line.startswith("Sidegraph: 3 of 3 open record(s)")


def test_t5_a_record_with_one_live_leaf_does_not_count(store):
    """Mutation M3 counts records with ANY leaf orphaned."""
    for _ in range(3):
        add_record(store, age_days=1, leaves=("orphaned", "live"))
    assert _orphaned(store) is None


def test_t5_decisions_only_would_miss_the_facts(store):
    """Mutation M3b counts decisions only: three recent orphaned facts must still alarm."""
    for _ in range(3):
        add_record(store, fact=True, age_days=1)
    assert _orphaned(store).severity == "degraded"


def test_orphaned_counts_only_open_records_and_reports_m_as_the_open_records_with_a_leaf(store):
    add_record(store, age_days=1)  # counted
    add_record(store, age_days=1, leaves=("live",))  # open, a leaf, not orphaned: only in m
    add_record(store, age_days=1, status=DecisionStatus.REJECTED)  # terminal: out of both
    problem = _orphaned(store)
    assert problem.line.startswith("Sidegraph: 1 of 2 open record(s)")


def test_a_tier_one_binding_alone_is_not_a_leaf(store):
    rid = add_record(store, age_days=1, leaves=())
    entity = store.upsert_entity(Entity(canonical_name="domain:x"))
    store.add_binding(AnchorBinding(record_id=rid, entity_id=entity.entity_id, tier=1))
    assert _orphaned(store) is None


def test_orphaned_findings_are_one_per_record_sorted_by_id_with_full_paths(store):
    d = add_record(store, age_days=40, leaves=("orphaned", "orphaned"))
    f = add_record(store, fact=True, age_days=50)
    problem = _orphaned(store)
    expected = sorted(
        [
            (str(Path(store.path) / "decisions" / f"{d}.json"), 2),
            (str(Path(store.path) / "facts" / f"{f}.json"), 1),
        ],
        key=lambda pair: Path(pair[0]).stem,
    )
    assert [path for path, _ in problem.findings] == [path for path, _ in expected]
    details = dict(problem.findings)
    assert details[expected[0][0]] == (
        f"every code anchor of this record is orphaned ({expected[0][1]} leaf anchor(s)); "
        "retrieval reaches it only through its file or domain — re-anchor it with add_anchors "
        "or the heal-anchors skill, or supersede it if the code is gone"
    )


def test_t18_orphaned_is_not_run_while_the_statuses_are_not_computed(store):
    """A reloaded index reads every binding live until a sync recomputes the statuses."""
    for _ in range(3):
        add_record(store, age_days=1)
    store.set_meta(VOLATILE_STALE_KEY, "1")
    with pytest.raises(integrity._NotRun):
        _orphaned(store)


def test_orphaned_is_not_run_without_an_index(store):
    with pytest.raises(integrity._NotRun):
        detect("orphaned-records", inputs_for(store, index=None))


def test_orphaned_is_clean_when_nothing_is_orphaned(store):
    add_record(store, age_days=1, leaves=("live",))
    assert _orphaned(store) is None


# -- 8 store-files-skipped ------------------------------------------------------------------


def _skipped(store: Store, value: str | None):
    if value is not None:
        store.set_meta(SKIPPED_CANONICAL_KEY, value)
    inputs = inputs_for(store)
    try:
        return detect("store-files-skipped", inputs)
    finally:
        inputs.index.close()


def test_skipped_files_text_names_the_first_file_and_counts_the_rest(store):
    entries = [
        {"path": "decisions/A.json", "reason": "id 'B' does not match its file name"},
        {"path": "facts/C.json", "reason": "unsafe id"},
        {"path": "facts/D.json", "reason": "unsafe id"},
    ]
    problem = _skipped(store, json.dumps(entries))
    assert problem.severity == "degraded"
    assert problem.line == (
        "Sidegraph: 3 store file(s) could not be indexed and are left out of memory: "
        "decisions/A.json (id 'B' does not match its file name), and 2 more. Run "
        "`sidegraph-verify` to list them, then fix or restore them with git."
    )
    assert problem.notice == problem.line
    assert (problem.summary, problem.fix) == ("3 store file(s) skipped", "sidegraph-verify")


def test_one_skipped_file_has_no_and_more(store):
    problem = _skipped(store, json.dumps([{"path": "decisions/A.json", "reason": "unsafe id"}]))
    assert problem.line == (
        "Sidegraph: 1 store file(s) could not be indexed and are left out of memory: "
        "decisions/A.json (unsafe id). Run `sidegraph-verify` to list them, then fix or "
        "restore them with git."
    )


def test_a_skipped_archive_segment_says_what_the_stderr_warning_says(store):
    """The first listed entry is an archive segment: the notice must not call it a "store
    file" to fix, or tell the user to remove it. It says what ``Store`` warns on stderr: the
    segment has lines that could not be read, the rest of it loaded, restore it with git and
    never delete a segment."""
    entries = [
        {"path": "archive/2020-01-01-1-deadbeef0000.jsonl", "reason": "bad-archive-segment"},
        {"path": "archive/2020-01-02-1-cafe00000000.jsonl", "reason": "bad-archive-segment"},
    ]
    problem = _skipped(store, json.dumps(entries))
    assert problem.line == (
        "Sidegraph: archive segment 2020-01-01-1-deadbeef0000.jsonl has lines that could not be "
        "read; they are left out and the rest of it loaded. Restore it with git; never delete a "
        "segment. Run `sidegraph-verify` to list the lines. 1 more store file(s) were left out "
        "as well."
    )
    assert problem.notice == problem.line
    assert (problem.severity, problem.summary) == ("degraded", "2 store file(s) skipped")


def test_a_skipped_record_file_after_an_archive_segment_keeps_the_record_file_text(store):
    """Only the first entry's reason picks the text: a record file leads, so the text names it
    even when an archive segment follows."""
    entries = [
        {"path": "decisions/A.json", "reason": "parse-error"},
        {"path": "archive/2020-01-01-1-deadbeef0000.jsonl", "reason": "bad-archive-segment"},
    ]
    problem = _skipped(store, json.dumps(entries))
    assert problem.line == (
        "Sidegraph: 2 store file(s) could not be indexed and are left out of memory: "
        "decisions/A.json (parse-error), and 1 more. Run `sidegraph-verify` to list them, "
        "then fix or restore them with git."
    )


@pytest.mark.parametrize("value", [None, "", "[]", "not json", "{}", '["x"]', '[{"reason": "r"}]'])
def test_an_absent_empty_or_malformed_skip_list_is_clean(store, value):
    assert _skipped(store, value) is None


def test_skipped_is_not_run_without_an_index(store):
    with pytest.raises(integrity._NotRun):
        detect("store-files-skipped", inputs_for(store, index=None))
