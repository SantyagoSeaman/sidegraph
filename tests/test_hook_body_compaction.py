"""Within-answer references preserve complete snapshots, recipients and placements."""

from __future__ import annotations

import sqlite3
from copy import deepcopy

import pytest

from sidegraph.host import hooks
from sidegraph.hot_index import HotIndex, Record
from sidegraph.schema import AnchorBinding, Descriptor, Entity, Fact, Provenance
from sidegraph.store_layout import VOLATILE_STALE_KEY
from tests.test_hook_showings import ID_RE, _events, _rows, _shows
from tests.test_host_agent_brief import _agent, _block
from tests.test_host_pretool_records import SESSION, Project, _payload, _run, _text


@pytest.fixture
def project(tmp_path):
    p = Project(tmp_path / "repo")
    yield p
    p.close()


def _shared(project, *, proposed=False):
    status = "proposed" if proposed else "accepted"
    decision = project.record(
        "pkg/a.py",
        "Synthetic retry constraint",
        status=status,
        choice="Keep the measured retry budget. Never generalize beyond this fixture.",
    )
    project.file("pkg/b.py")
    a = project.store.resolve_descriptor("Sym1", "pkg/a.py")
    b = project.store.upsert_entity(
        Entity(canonical_name="Other", descriptor=Descriptor(name="Other", file_path="pkg/b.py"))
    )
    project.store.add_binding(
        AnchorBinding(record_id=decision.id, entity_id=b.entity_id, tier=2, status="live")
    )
    fact = project.store.add_fact(
        Fact(
            statement="Only 12 requests on runtime 3.13 in a local fixture; not a population rate.",
            source="Synthetic bounded measurement, two workers only",
            status=status,
            valid_from=decision.valid_from,
            provenance=Provenance(source="manual"),
        )
    )
    for ent in (a, b):
        project.store.add_binding(
            AnchorBinding(record_id=fact.id, entity_id=ent.entity_id, tier=2, status="live")
        )
    project.store.set_meta(VOLATILE_STALE_KEY, "0")
    return [
        Record(decision.model_dump(mode="json"), proposed),
        Record(fact.model_dump(mode="json"), proposed, "fact"),
    ]


def _bash(monkeypatch, capsys, project, **kw):
    return _text(
        _run(monkeypatch, capsys, project, _payload("Bash", command="cat pkg/a.py pkg/b.py", **kw))
    )


@pytest.mark.parametrize("surface", ["read", "brief"])
@pytest.mark.parametrize("proposed", [False, True])
def test_one_full_copy_per_answer_preserves_claims_and_placements(
    project, monkeypatch, capsys, surface, proposed
):
    records = _shared(project, proposed=proposed)
    claims = []
    real_claim = HotIndex.claim_file

    def claim(self, prefix, rel, value, cap, *, shown=None):
        claims.append((rel, shown))
        return real_claim(self, prefix, rel, value, cap, shown=shown)

    monkeypatch.setattr(HotIndex, "claim_file", claim)
    if surface == "read":
        text = _bash(monkeypatch, capsys, project)
        assert all(
            f"Recorded for {rel} (2 of 2, mistakes first):" in text
            for rel in ("pkg/a.py", "pkg/b.py")
        )
    else:
        prompt = "Inspect pkg/a.py and pkg/b.py."
        out, _ = _agent(monkeypatch, capsys, project, prompt)
        text = _block(out, prompt)
        assert "pkg/a.py:" in text and "pkg/b.py:" in text
    for record in records:
        assert text.count(hooks._record_line(record)) == 1
    trust = " [unratified]" if proposed else ""
    assert f"- [gotcha] See above{trust} (id {records[0].decision['id']})" in text
    assert f"- [fact]{trust} See above (id {records[1].decision['id']})" in text
    ids = [r.decision["id"] for r in records]
    assert ID_RE.findall(text) == ids * 2
    expected = [(rel, rid) for rel in ("pkg/a.py", "pkg/b.py") for rid in ids]
    kind = "inject" if surface == "read" else "inject_brief"
    assert [(row[1], row[2]) for row in _events(project, kind)] == expected
    assert _shows(project) == dict.fromkeys(ids, 2)
    assert (
        _rows(project, "SELECT file_group, record_id FROM delivery_placements ORDER BY id")
        == expected
    )
    assert (
        _rows(project, "SELECT record_type, status FROM delivery_placements ORDER BY id")
        == [(r.record_type, r.decision["status"]) for r in records] * 2
    )
    if surface == "read":
        assert [(rel, list(shown.record_ids)) for rel, shown in claims] == [
            (rel, ids) for rel in ("pkg/a.py", "pkg/b.py")
        ]
        assert all(shown.session_id == SESSION and shown.agent is None for _, shown in claims)
    else:
        assert claims == []


@pytest.mark.parametrize(
    "field,value",
    [
        ("context", "Another context"),
        ("choice", "Keep the measured retry budget. A different hidden second sentence."),
        ("status", "proposed"),
        ("provenance", {"source": "agent"}),
        ("id", "01M00000000000000000000000"),
        ("source", "Different measurement"),
    ],
)
def test_equal_gists_with_different_whole_snapshots_stay_full(project, field, value):
    original = _shared(project)[0]
    changed = deepcopy(original.decision)
    changed[field] = value
    other = Record(changed, original.proposed)
    seen = []
    hooks._records_block("pkg/a.py", [original], seen=seen)
    second = hooks._records_block("pkg/b.py", [other], seen=seen)
    assert hooks._record_line(other) in second and "See above" not in second


@pytest.mark.parametrize("change", ["trust", "type"])
def test_trust_and_type_are_part_of_equality(project, change):
    record = _shared(project)[0]
    payload = {**record.decision, "statement": "Qualified sample", "source": "Synthetic"}
    first = Record(payload, False)
    second = Record(
        deepcopy(payload), change == "trust", "fact" if change == "type" else "decision"
    )
    seen = []
    hooks._records_block("pkg/a.py", [first], seen=seen)
    text = hooks._records_block("pkg/b.py", [second], seen=seen)
    assert hooks._record_line(second) in text and "See above" not in text


def test_short_original_never_grows_and_default_rendering_stays_full():
    record = Record({"id": "x", "kind": "adr", "title": "x", "choice": ""}, False)
    seen = []
    baseline = hooks._records_block("a", [record])
    assert hooks._records_block("a", [record], seen=seen) == baseline
    assert hooks._records_block("a", [record], seen=seen) == baseline
    plan = [(None, "a", [record]), (None, "b", [record])]
    assert hooks._render_brief(plan, compact=True) == hooks._render_brief(plan)


def test_lost_first_claim_cannot_seed_a_reference(project, monkeypatch, capsys):
    records = _shared(project)
    claim = HotIndex.claim_file

    def skip_a(self, prefix, rel, value, cap, *, shown=None):
        return False if rel == "pkg/a.py" else claim(self, prefix, rel, value, cap, shown=shown)

    monkeypatch.setattr(HotIndex, "claim_file", skip_a)
    text = _bash(monkeypatch, capsys, project)
    assert "pkg/a.py" not in text and "See above" not in text
    assert all(hooks._record_line(r) in text for r in records)
    assert [(r[1], r[2]) for r in _events(project, "inject")] == [
        ("pkg/b.py", r.decision["id"]) for r in records
    ]


def test_busy_later_claim_keeps_only_the_successful_full_block(project, monkeypatch, capsys):
    records = _shared(project)
    claim = HotIndex.claim_file

    def busy_b(self, prefix, rel, value, cap, *, shown=None):
        if rel == "pkg/b.py":
            raise sqlite3.OperationalError("database is locked")
        return claim(self, prefix, rel, value, cap, shown=shown)

    monkeypatch.setattr(HotIndex, "claim_file", busy_b)
    text = _bash(monkeypatch, capsys, project)
    assert "pkg/b.py" not in text and "See above" not in text
    assert all(hooks._record_line(r) in text for r in records)
    assert [(r[1], r[2]) for r in _events(project, "inject")] == [
        ("pkg/a.py", r.decision["id"]) for r in records
    ]


def test_calls_children_briefs_and_full_context_do_not_share_seen_state(
    project, monkeypatch, capsys
):
    records = _shared(project)
    for rel in ("pkg/a.py", "pkg/b.py"):
        text = _text(_run(monkeypatch, capsys, project, _payload("Read", file_path=rel)))
        assert "See above" not in text
        assert all(hooks._record_line(r) in text for r in records)
    child = _bash(monkeypatch, capsys, project, agent="child")
    assert all(child.count(hooks._record_line(r)) == 1 for r in records)
    prompt = "Inspect pkg/a.py and pkg/b.py."
    for _ in range(2):
        out, _ = _agent(monkeypatch, capsys, project, prompt)
        assert all(_block(out, prompt).count(hooks._record_line(r)) == 1 for r in records)
    assert _run(monkeypatch, capsys, project, _payload("Read", file_path="pkg/a.py")) == {}
    from sidegraph.server import _get_task_context_impl

    full = _get_task_context_impl(
        project.store, None, None, [{"name": "Sym1", "file_path": "pkg/a.py"}], 4000, 6000
    )
    assert "Never generalize beyond this fixture." in full
    assert records[1].decision["statement"] in full and records[1].decision["source"] in full
    assert "See above" not in full


def test_brief_compacts_only_after_full_cost_selection_without_backfill():
    records = [
        Record(
            {
                "id": str(i),
                "statement": str(i) + "q" * 424,
                "source": "Only a synthetic fixture",
                "status": "accepted",
            },
            False,
            "fact",
        )
        for i in range(2)
    ]
    assert all(len(hooks._record_line(r)) <= 512 for r in records)
    chosen = [(None, f"pkg/{i}.py", records) for i in range(3)]
    assert len(hooks._render_brief(chosen)) > hooks._BRIEF_MAX_CHARS
    plan = hooks._brief_plan(chosen)
    assert [rel for _, rel, _ in plan] == ["pkg/0.py", "pkg/1.py"]
    text = hooks._brief_block(plan)
    assert "pkg/2.py" not in text
    assert 'More: get_task_context(files=["pkg/0.py", "pkg/1.py"]).' in text
    assert all(text.count(hooks._record_line(r)) == 1 for r in records)
    assert len(text) < len(hooks._render_brief(plan)) <= hooks._BRIEF_MAX_CHARS


def test_telemetry_opt_out_still_compacts_without_writes(project, monkeypatch, capsys):
    records = _shared(project)
    text = _text(
        _run(
            monkeypatch,
            capsys,
            project,
            _payload("Bash", command="cat pkg/a.py pkg/b.py"),
            {"SIDEGRAPH_TELEMETRY": "off"},
        )
    )
    assert all(text.count(hooks._record_line(r)) == 1 for r in records)
    assert _events(project, "inject") == []
    assert _shows(project) == {}
    assert _rows(project, "SELECT count(*) FROM delivery_occurrences") == [(0,)]


@pytest.mark.parametrize("mistakes,shown", [(1, 2), (3, 3)])
def test_compaction_keeps_read_slice_counts_and_more_links(mistakes, shown):
    records = [
        Record(
            {
                "id": f"{i:026d}",
                "kind": "gotcha" if i < mistakes else "adr",
                "title": f"Synthetic record {i}",
                "choice": "Keep this complete selected gist.",
            },
            False,
        )
        for i in range(shown + 1)
    ]
    seen = []
    first = hooks._records_block("pkg/a.py", records, seen=seen)
    second = hooks._records_block("pkg/b.py", records, seen=seen)
    assert f"({shown} of {len(records)}, mistakes first):" in first
    assert f"({shown} of {len(records)}, mistakes first):" in second
    assert 'More: get_task_context(files=["pkg/a.py"]).' in first
    assert 'More: get_task_context(files=["pkg/b.py"]).' in second
    assert (
        ID_RE.findall(first) == ID_RE.findall(second) == [r.decision["id"] for r in records[:shown]]
    )
    assert second.count("See above") == shown
    assert all(hooks._record_line(r) in first for r in records[:shown])
    assert records[-1] not in seen
