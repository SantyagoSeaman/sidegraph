"""The PreToolUse hook journals what it hands the agent as showings.

A Read that prints records writes one ``inject`` row per printed record and the same
``retrieval_shows`` upsert the MCP path runs, in the transaction that claims the file; an
``Agent`` call writes ``inject_brief`` rows for the records its brief printed. Before this, the
hook left one ``touch`` row and nothing else, so ``sidegraph-stats`` and ``sidegraph-doctor``
called a record the hook printed 400 times "never shown".

see design/superpowers/specs/2026-10-11-hook-showings-journal-design.md (D1-D7)
"""

from __future__ import annotations

import re
import sqlite3

import pytest

from sidegraph.hot_index import HotIndex
from sidegraph.schema import DecisionKind
from tests.test_host_agent_brief import (
    LONG_CHOICE,
    LONG_TITLE,
    _agent,
    _block,
)
from tests.test_host_pretool_records import SESSION, Project, _payload, _run, _text

ID_RE = re.compile(r"\(id ([0-9A-Z]{26})\)$", re.M)


@pytest.fixture
def project(tmp_path):
    p = Project(tmp_path / "repo")
    yield p
    p.close()


def _rows(project: Project, sql: str) -> list[tuple]:
    conn = sqlite3.connect(project.store_dir / "index.db")
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def _events(project: Project, kind: str) -> list[tuple]:
    return _rows(
        project,
        "SELECT session_id, key, detail, agent FROM retrieval_events "
        f"WHERE kind = '{kind}' ORDER BY id",
    )


def _shows(project: Project) -> dict[str, int]:
    return dict(_rows(project, "SELECT record_id, shows FROM retrieval_shows"))


def _read(monkeypatch, capsys, project: Project, rel: str, **kw) -> dict:
    return _run(
        monkeypatch, capsys, project, _payload("Read", file_path=str(project.root / rel), **kw)
    )


def _crowded(project: Project, rel: str = "src/a.py", n: int = 5) -> None:
    """``n`` accepted decisions (not mistakes) on one file: the hook prints the top two."""
    for k in range(n):
        project.record(rel, f"Decision {k}", kind=DecisionKind.ADR, age_days=k)


def test_a_read_journals_one_inject_row_per_printed_record(project, monkeypatch, capsys):
    """Red against unfixed code: the hook wrote a touch row and nothing else."""
    _crowded(project)
    out = _read(monkeypatch, capsys, project, "src/a.py", agent="sub-1")
    printed = ID_RE.findall(_text(out))
    assert len(printed) == 2
    rows = _events(project, "inject")
    assert [r[2] for r in rows] == printed
    assert {(r[0], r[1], r[3]) for r in rows} == {(SESSION, "src/a.py", "sub-1")}
    touch = _events(project, "touch")
    assert [(r[0], r[3]) for r in touch] == [(SESSION, "sub-1")]


def test_only_the_printed_slice_is_journaled(project, monkeypatch, capsys):
    """Five records on a file, two printed (three when the top two are mistakes): the journal
    holds the slice, not the file's records. Red against unfixed code (no rows) and against a
    journal of every record."""
    _crowded(project, "src/a.py", 5)
    for k in range(5):
        project.record("src/m.py", f"Gotcha {k}", kind=DecisionKind.GOTCHA, age_days=k)
    a = ID_RE.findall(_text(_read(monkeypatch, capsys, project, "src/a.py")))
    m = ID_RE.findall(_text(_read(monkeypatch, capsys, project, "src/m.py")))
    assert (len(a), len(m)) == (2, 3)
    assert [r[2] for r in _events(project, "inject")] == a + m


def test_a_lost_claim_journals_nothing(project, monkeypatch, capsys):
    """The same agent's second Read prints nothing and adds no row and no show (declared red
    against nothing: a guard against over-journaling); another agent of the session is
    delivered to and journaled under its own id."""
    _crowded(project)
    first = ID_RE.findall(_text(_read(monkeypatch, capsys, project, "src/a.py")))
    assert _read(monkeypatch, capsys, project, "src/a.py") == {}
    assert len(_events(project, "inject")) == 2
    assert set(_shows(project).values()) == {1}
    again = ID_RE.findall(_text(_read(monkeypatch, capsys, project, "src/a.py", agent="sub-2")))
    assert again == first
    assert [r[3] for r in _events(project, "inject")] == [None, None, "sub-2", "sub-2"]
    assert set(_shows(project).values()) == {2}


def test_the_hook_feeds_retrieval_shows(project, monkeypatch, capsys):
    """Red against unfixed code: the table stayed empty."""
    _crowded(project)
    printed = ID_RE.findall(_text(_read(monkeypatch, capsys, project, "src/a.py")))
    assert _shows(project) == {rid: 1 for rid in printed}
    assert project.store.retrieval_shows() == {rid: 1 for rid in printed}


def test_a_record_printed_under_two_files_of_one_line_is_two_showings(project, monkeypatch, capsys):
    """+1 per printed block, not per call (the MCP path dedupes per call)."""
    rec = project.record("src/a.py", "Shared", kind=DecisionKind.ADR)
    # one decision, bound to a second file through a second entity
    from sidegraph.schema import AnchorBinding, Descriptor, Entity

    project.file("src/b.py")
    ent = project.store.upsert_entity(
        Entity(canonical_name="Other", descriptor=Descriptor(name="Other", file_path="src/b.py"))
    )
    project.store.add_binding(
        AnchorBinding(record_id=rec.id, entity_id=ent.entity_id, tier=2, status="live")
    )
    out = _run(
        monkeypatch,
        capsys,
        project,
        _payload("Bash", command="cat src/a.py src/b.py"),
    )
    assert ID_RE.findall(_text(out)) == [rec.id, rec.id]
    assert _shows(project) == {rec.id: 2}
    assert [(r[1], r[2]) for r in _events(project, "inject")] == [
        ("src/a.py", rec.id),
        ("src/b.py", rec.id),
    ]


def test_telemetry_off_journals_no_showing(project, monkeypatch, capsys):
    """The opt-out promise: delivery is identical, nothing is written. Declared red against
    nothing until the gate is wrong; the mutant that drops the gate turns it red."""
    _crowded(project)
    on = _text(_read(monkeypatch, capsys, project, "src/a.py", session="on"))
    off = _text(
        _run(
            monkeypatch,
            capsys,
            project,
            _payload("Read", session="off", file_path=str(project.root / "src/a.py")),
            {"SIDEGRAPH_TELEMETRY": "off"},
        )
    )
    assert off == on
    assert all(r[0] != "off" for r in _events(project, "inject"))
    assert set(_shows(project).values()) == {1}, "only the telemetry-on Read counted"


@pytest.mark.parametrize("ledger", [False, True])
def test_claim_and_showing_commit_together(project, monkeypatch, capsys, ledger):
    """Legacy claim/showings stay atomic; the optional delivery ledger commits separately.

    An older index has only the claim/showings and touch commits. The new ledger may add
    one final commit, without moving the legacy showings into that transaction.
    """
    _crowded(project)
    if not ledger:
        for table in ("delivery_anchor_links", "delivery_placements", "delivery_occurrences"):
            project.store._conn.execute(f"DROP TABLE {table}")
        project.store._conn.commit()
    trace: list[str] = []
    real_open = HotIndex.open

    def opening(store_path, **kwargs):
        index = real_open(store_path, **kwargs)
        if index is not None:
            index._conn.set_trace_callback(trace.append)
        return index

    monkeypatch.setattr(HotIndex, "open", staticmethod(opening))
    _read(monkeypatch, capsys, project, "src/a.py")
    assert len(_events(project, "inject")) == 2
    transactions: list[list[str]] = []
    current: list[str] | None = None
    for sql in trace:
        if sql.startswith("BEGIN"):
            current = []
        elif sql == "COMMIT":
            assert current is not None, trace
            transactions.append(current)
            current = None
        elif sql == "ROLLBACK":
            current = None
        elif current is not None:
            current.append(sql)
    showing_transactions = [
        statements
        for statements in transactions
        if any("INSERT INTO retrieval_events" in sql and "'inject'" in sql for sql in statements)
    ]
    assert len(showing_transactions) == 1, trace
    assert any("INSERT INTO meta" in sql for sql in showing_transactions[0]), trace
    assert not any("delivery_occurrences" in sql for sql in showing_transactions[0]), trace
    ledger_transactions = [
        statements
        for statements in transactions
        if any("INSERT INTO delivery_occurrences" in sql for sql in statements)
    ]
    assert len(ledger_transactions) == int(ledger), trace
    assert trace.count("COMMIT") == 2 + int(ledger), trace


# -- the Agent brief ---------------------------------------------------------------------------


def _clip_records(project: Project, rel: str, count: int = 2) -> None:
    for k in range(count):
        project.record(rel, f"{LONG_TITLE} {k}", choice=LONG_CHOICE + ".")


def _brief_ids(block: str) -> list[str]:
    return ID_RE.findall(block)


def _journaled_brief(project: Project) -> list[tuple]:
    return _events(project, "inject_brief")


def test_an_agent_brief_journals_the_records_it_printed_at_the_line_cap(
    project, monkeypatch, capsys
):
    """Counts 1, 2, 2, 2: the line cap leaves the fourth file one line, its first record. The
    journal equals the ids parsed from the printed block, `agent` is NULL (the recipient does
    not exist yet) and the kind is its own. Red against unfixed code (no rows) and against a
    journal of `records[:2]` per file."""
    counts = {"src/f0.py": 1, "src/f1.py": 2, "src/f2.py": 2, "src/f3.py": 2}
    for rel, count in counts.items():
        _clip_records(project, rel, count)
    prompt = "Touch " + ", ".join(counts) + "."
    out = _run(
        monkeypatch,
        capsys,
        project,
        _payload("Agent", agent="parent", description="d", prompt=prompt),
    )
    ids = _brief_ids(_block(out, prompt))
    assert len(ids) == 6
    rows = _journaled_brief(project)
    assert [r[2] for r in rows] == ids
    assert {r[0] for r in rows} == {SESSION}
    assert {r[3] for r in rows} == {None}, "the parent is not the recipient"
    assert [r[1] for r in rows] == ["src/f0.py"] + ["src/f1.py"] * 2 + ["src/f2.py"] * 2 + [
        "src/f3.py"
    ]
    assert _events(project, "inject") == []
    assert _shows(project) == {rid: 1 for rid in ids}


def test_an_agent_brief_journals_only_what_the_character_cap_kept(project, monkeypatch, capsys):
    """Deep paths: the 3,000-character cap drops whole files from the end. The journal holds
    exactly the printed ids."""
    deep = "/".join(chr(97 + i) * 45 for i in range(5))
    rels = [f"src/{deep}/f{i}.py" for i in range(6)]
    for rel in rels:
        _clip_records(project, rel)
    prompt = "Touch " + ", ".join(rels) + "."
    out, _ = _agent(monkeypatch, capsys, project, prompt)
    ids = _brief_ids(_block(out, prompt))
    assert len(ids) == 4
    assert [r[2] for r in _journaled_brief(project)] == ids


def test_an_agent_brief_with_no_session_or_telemetry_off_journals_nothing(
    project, monkeypatch, capsys
):
    _crowded(project)
    out = _run(
        monkeypatch,
        capsys,
        project,
        {"tool_name": "Agent", "tool_input": {"prompt": "Fix src/a.py."}},
    )
    assert "updatedInput" in out["hookSpecificOutput"], "the brief is still delivered"
    out, _ = _agent(
        monkeypatch, capsys, project, "Fix src/a.py.", env={"SIDEGRAPH_TELEMETRY": "off"}
    )
    assert "updatedInput" in out["hookSpecificOutput"]
    assert _journaled_brief(project) == []
    assert _shows(project) == {}


def test_a_failing_journal_never_costs_the_agent_its_brief(project, monkeypatch, capsys):
    _crowded(project)

    def boom(self, *a, **kw):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(HotIndex, "record_shows", boom)
    out, _ = _agent(monkeypatch, capsys, project, "Fix src/a.py.")
    assert "updatedInput" in out["hookSpecificOutput"]


def test_a_record_printed_under_two_files_of_one_brief_is_two_showings(
    project, monkeypatch, capsys
):
    """+1 each time it is printed, in a brief as on a Bash line."""
    from sidegraph.schema import AnchorBinding, Descriptor, Entity

    rec = project.record("src/a.py", "Shared", kind=DecisionKind.ADR)
    project.file("src/b.py")
    ent = project.store.upsert_entity(
        Entity(canonical_name="Other", descriptor=Descriptor(name="Other", file_path="src/b.py"))
    )
    project.store.add_binding(
        AnchorBinding(record_id=rec.id, entity_id=ent.entity_id, tier=2, status="live")
    )
    prompt = "Touch src/a.py and src/b.py."
    out, _ = _agent(monkeypatch, capsys, project, prompt)
    assert _brief_ids(_block(out, prompt)) == [rec.id, rec.id]
    assert _shows(project) == {rec.id: 2}


# -- doctor and the stats memory line ----------------------------------------------------------


def test_doctor_never_surfaced_skips_a_record_the_hook_showed(project, monkeypatch, capsys):
    """A record whose area is queried but whose show counter is zero is a finding; after the
    hook printed it there is none. Red against unfixed code."""
    from sidegraph.doctor import NEVER_SURFACED, curate

    project.record("src/a.py", "Only decision", kind=DecisionKind.ADR)
    for _ in range(12):
        project.store.record_retrieval([], ["src/a.py"])
    flagged = [f for f in curate(project.store_dir).findings if f.code == NEVER_SURFACED]
    assert len(flagged) == 1
    _read(monkeypatch, capsys, project, "src/a.py")
    assert [f for f in curate(project.store_dir).findings if f.code == NEVER_SURFACED] == []


def test_no_recorded_showing_drops_for_a_hook_shown_record(project, monkeypatch, capsys):
    """Red against unfixed code: the counter only saw MCP renders."""
    from datetime import UTC, datetime

    from sidegraph.stats.model import build_report

    _crowded(project, "src/a.py", 3)
    now = datetime.now(UTC)
    before = build_report(project.store_dir, None, now=now).memory.no_recorded_showing
    shown = ID_RE.findall(_text(_read(monkeypatch, capsys, project, "src/a.py")))
    after = build_report(project.store_dir, None, now=now).memory.no_recorded_showing
    assert before - after == len(shown) == 2
