"""Synthetic public-boundary RED cases; no task/response contents stored in ledger."""

from datetime import UTC, datetime

import pytest

from sidegraph import server
from sidegraph.schema import AnchorBinding, Decision, Descriptor, Provenance, Scope
from sidegraph.store import Store


def rows(store, table):
    if (
        store._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type=? AND name=?", ("table", table)
        ).fetchone()
        is None
    ):
        return []
    return [dict(r) for r in store._conn.execute(f"SELECT * FROM {table}")]


def test_repeat_calls_have_distinct_occurrences_not_anchor_fanout(tmp_path, monkeypatch):
    monkeypatch.setenv("SIDEGRAPH_TELEMETRY", "on")
    store = Store(tmp_path / "s")
    d = store.add_decision(
        Decision(
            title="Synthetic global guard",
            kind="gotcha",
            scope=Scope.GLOBAL,
            status="accepted",
            context="Synthetic context",
            choice="Keep synthetic guard.",
            valid_from=datetime.now(UTC),
            provenance=Provenance(source="manual"),
        )
    )
    for path in ["a.py", "b.py", "c.py"]:
        entity = store.get_or_create_entity(Descriptor(name=path, file_path=path))
        store.add_binding(AnchorBinding(record_id=d.id, entity_id=entity.entity_id, tier=2))
    for _ in range(2):
        answer = server._query_decisions_impl(store, None, None, None, 6000)
        assert "Synthetic global guard" in answer
    occurrences = rows(store, "delivery_occurrences")
    assert len(occurrences) == 2, "two prepared answers need two observations"
    assert len({r["occurrence_id"] for r in occurrences}) == 2
    assert [r["placement_count"] for r in occurrences] == [1, 1]
    assert len(rows(store, "delivery_placements")) == 2
    assert len(rows(store, "delivery_anchor_links")) == 6
    store.close()


@pytest.mark.parametrize("surface,want", [("query", "valid_empty"), ("drill", "unresolved")])
def test_empty_and_unknown_domain_are_observed_without_body_placements(
    tmp_path, monkeypatch, surface, want
):
    monkeypatch.setenv("SIDEGRAPH_TELEMETRY", "on")
    store = Store(tmp_path / "s")
    if surface == "query":
        server._query_decisions_impl(store, None, None, None, 6000)
    else:
        assert server._drill_down_impl(store, None, "missing-synthetic-domain")["found"] is False
    occurrences = rows(store, "delivery_occurrences")
    assert len(occurrences) == 1, "empty content is still a prepared answer"
    assert occurrences[0]["outcome"] == want
    assert rows(store, "delivery_placements") == []
    store.close()


@pytest.mark.parametrize("tool", ["Read", "Agent"])
def test_hooks_observe_only_the_winning_final_placements(tmp_path, monkeypatch, capsys, tool):
    from tests.test_host_pretool_records import Project, _payload, _run

    project = Project(tmp_path / "repo")
    d = project.record("src/a.py", "Synthetic hook guard")
    data = _payload(
        tool,
        **(
            {"file_path": str(project.root / "src/a.py")}
            if tool == "Read"
            else {"prompt": "Work on src/a.py"}
        ),
    )
    data["tool_use_id"] = "synthetic-host-call"
    out = _run(monkeypatch, capsys, project, data, {"SIDEGRAPH_TELEMETRY": "on"})
    assert out["hookSpecificOutput"]
    headers = rows(project.store, "delivery_occurrences")
    assert len(headers) == 1
    assert headers[0]["surface"] == ("hook_read" if tool == "Read" else "hook_brief")
    assert headers[0]["host_call_id"] == "synthetic-host-call"
    assert headers[0]["considered"] is None
    placements = rows(project.store, "delivery_placements")
    assert [(p["record_id"], p["status"], p["file_group"]) for p in placements] == [
        (d.id, "accepted", "src/a.py")
    ]
    project.close()


def global_decision(store, title="Synthetic guard", **extra):
    return store.add_decision(
        Decision(
            title=title,
            kind="gotcha",
            scope=Scope.GLOBAL,
            status="accepted",
            context="Synthetic context",
            choice="Keep synthetic guard.",
            valid_from=datetime.now(UTC),
            provenance=Provenance(source="manual"),
            **extra,
        )
    )


def test_snapshot_retains_emitted_status_after_store_changes(tmp_path, monkeypatch):
    monkeypatch.setenv("SIDEGRAPH_TELEMETRY", "on")
    store = Store(tmp_path / "s")
    d = global_decision(store)
    original = server._hard_cap

    def cap_then_supersede(*args, **kwargs):
        answer = original(*args, **kwargs)
        global_decision(store, "Synthetic successor", supersedes=d.id)
        return answer

    monkeypatch.setattr(server, "_hard_cap", cap_then_supersede)
    assert "Synthetic guard" in server._query_decisions_impl(store, None, None, None, 6000)
    assert store.get_decision(d.id).status.value == "superseded"
    assert [(p["record_id"], p["status"]) for p in rows(store, "delivery_placements")] == [
        (d.id, "accepted")
    ]
    store.close()


def test_pathless_inline_fact_has_placement_without_anchor_fanout(tmp_path, monkeypatch):
    from sidegraph.schema import Fact

    monkeypatch.setenv("SIDEGRAPH_TELEMETRY", "on")
    store = Store(tmp_path / "s")
    d = global_decision(store)
    fact = store.add_fact(
        Fact(
            statement="Synthetic 8-worker observation.",
            source="Synthetic test.",
            supports=[d.id],
            status="accepted",
            valid_from=datetime.now(UTC),
            provenance=Provenance(source="manual"),
        )
    )
    answer = server._query_decisions_impl(store, None, None, None, 6000)
    assert fact.statement in answer
    header = rows(store, "delivery_occurrences")[0]
    assert (
        header["considered"],
        header["unique_record_count"],
        header["placement_count"],
        header["anchor_link_count"],
    ) == (2, 2, 2, 0)
    assert {(p["record_type"], p["record_id"]) for p in rows(store, "delivery_placements")} == {
        ("decision", d.id),
        ("fact", fact.id),
    }
    store.close()


@pytest.mark.parametrize("same_id", [False, True])
def test_final_atom_queue_counts_actual_placements_even_for_identical_lines(same_id):
    from sidegraph.delivery import Snapshot
    from sidegraph.retrieval import TaskContext

    line = "- [adr] Same: " + "x" * 300
    ids = ["one", "one" if same_id else "two"]
    snapshots = [Snapshot(i, "decision", "adr", "accepted") for i in ids]
    ctx = TaskContext(
        related=[line, line],
        shown_ids=ids,
        line_ids={line: ids},
        emitted=2,
        delivery_atoms={line: snapshots},
        delivery_considered={x.key for x in snapshots},
        delivery_placed={x.key for x in snapshots},
    )
    body, _, _ = ctx.render_parts()
    report = server._CapReport()
    server._hard_cap(body, [], "narrow", cap=len(line) + 250, atoms=ctx.line_ids, report=report)
    assert report.cut and len([x for x in report.kept if x == line]) == 1
    delivered = server._delivered(ctx, report)
    assert [p.record.record_id for p in delivered.delivery_placements] == ["one"]
    # Existing ID-level legacy accounting is intentionally unchanged.
    assert delivered.emitted == (2 if same_id else 1)


def test_hard_cut_structure_without_surviving_records_is_unknown(tmp_path, monkeypatch):
    from sidegraph.retrieval import TaskContext

    monkeypatch.setenv("SIDEGRAPH_TELEMETRY", "on")
    store = Store(tmp_path / "s")
    monkeypatch.setattr(
        server,
        "_retrieve",
        lambda *a, **k: TaskContext(
            structure=["Synthetic structure " + "x" * 45000], delivery_unresolved=False
        ),
    )
    server._get_task_context_impl(store, object(), None, None, 4000, 6000)
    header = rows(store, "delivery_occurrences")[0]
    assert header["outcome"] == "unknown"
    assert header["structure_present"] is None
    assert header["reason"] == "structure_cut"
    store.close()


def test_records_and_unresolved_seed_keep_both_observations(tmp_path, monkeypatch):
    monkeypatch.setenv("SIDEGRAPH_TELEMETRY", "on")
    store = Store(tmp_path / "s")
    global_decision(store)
    server._query_decisions_impl(store, None, None, [{"name": "MissingSyntheticEntity"}], 6000)
    header = rows(store, "delivery_occurrences")[0]
    assert header["outcome"] == "records" and header["unresolved"] == 1
    store.close()


def test_core_error_is_observed_without_error_text_and_rethrown(tmp_path, monkeypatch):
    monkeypatch.setenv("SIDEGRAPH_TELEMETRY", "on")
    store = Store(tmp_path / "s")
    error = RuntimeError("PRIVATE ERROR BODY CANARY")

    def fail(*a, **k):
        raise error

    monkeypatch.setattr(server, "_retrieve", fail)
    with pytest.raises(RuntimeError) as raised:
        server._query_decisions_impl(store, None, None, None, 6000)
    assert raised.value is error
    assert rows(store, "delivery_occurrences")[0]["outcome"] == "error"
    assert "PRIVATE ERROR BODY CANARY" not in "\n".join(store._conn.iterdump())
    store.close()


def test_repeated_read_loses_claim_and_adds_no_occurrence(tmp_path, monkeypatch, capsys):
    from tests.test_host_pretool_records import Project, _payload, _run

    project = Project(tmp_path / "repo")
    project.record("src/a.py", "Synthetic guard")
    payload = _payload("Read", file_path=str(project.root / "src/a.py"))
    first = _run(monkeypatch, capsys, project, payload, {"SIDEGRAPH_TELEMETRY": "on"})
    second = _run(monkeypatch, capsys, project, payload)
    assert first.get("hookSpecificOutput") and second == {}
    assert len(rows(project.store, "delivery_occurrences")) == 1
    project.close()


def test_drill_down_private_snapshots_do_not_affect_public_budget(tmp_path, monkeypatch):
    from sidegraph.schema import DecisionKind
    from tests.test_retrieval_drilldown import _accept, _bind_domain, _decision, _domain

    monkeypatch.setenv("SIDEGRAPH_TELEMETRY", "on")
    store = Store(tmp_path / "s")
    _accept(store, _domain(store, "synthetic"))
    for i in range(3):
        d = _decision(store, DecisionKind.GOTCHA, title=f"Synthetic {i}", status="accepted")
        _bind_domain(store, d.id, "synthetic")
    result = server._drill_down_impl(store, None, "synthetic", limit=1, budget_chars=1000)
    assert len(result["decisions"]) == 1
    assert "_delivery_snapshots" not in result and "decision_ids" not in result
    assert len(server._dump(result)) <= 1000
    placements = rows(store, "delivery_placements")
    assert len(placements) == 1 and placements[0]["record_id"] in result["decisions"][0]
    assert rows(store, "delivery_occurrences")[0]["considered"] is None
    store.close()


def test_ambiguous_entity_candidates_do_not_claim_the_seed_was_resolved(tmp_path):
    from types import SimpleNamespace

    from sidegraph.retrieval import Seed, resolve_seeds

    class AmbiguousReader:
        def resolve(self, descriptor, *, collapse):
            return SimpleNamespace(node_id=None, candidates=["one", "two"])

        def get_node(self, node_id):
            return SimpleNamespace(
                node_id=node_id, community="1", name=node_id, file_path=node_id + ".py"
            )

    store = Store(tmp_path / "s")
    resolution = resolve_seeds([Seed(name="Synthetic")], AmbiguousReader(), store)
    assert resolution.seed_node_ids == ["one", "two"]
    assert resolution.unresolved, "candidate structure is not an unambiguous resolution"
    store.close()


def test_brief_document_path_and_prompt_do_not_enter_new_ledger(tmp_path, monkeypatch, capsys):
    from tests.test_host_pretool_records import Project, _payload, _run

    project = Project(tmp_path / "repo")
    project.record("src/a.py", "Synthetic guard")
    doc = project.root / "private-review-plan.md"
    doc.write_text("Read src/a.py\n")
    prompt = "PRIVATE PROMPT CANARY; follow private-review-plan.md"
    out = _run(
        monkeypatch,
        capsys,
        project,
        _payload("Agent", prompt=prompt),
        {"SIDEGRAPH_TELEMETRY": "on"},
    )
    assert "named in private-review-plan.md" in out["hookSpecificOutput"]["updatedInput"]["prompt"]
    assert len(rows(project.store, "delivery_occurrences")) == 1
    dump = "\n".join(project.store._conn.iterdump())
    assert "private-review-plan.md" not in dump
    assert "PRIVATE PROMPT CANARY" not in dump
    project.close()


@pytest.mark.parametrize(
    "files,want", [(["missing.py"], "unresolved"), (["pkg/m.py", "missing.py"], "structure")]
)
def test_ladder_rejected_original_seeds_retain_unresolved_evidence(
    tmp_path, monkeypatch, files, want
):
    from tests.test_seed_ladder import make_reader

    monkeypatch.setenv("SIDEGRAPH_TELEMETRY", "on")
    reader = make_reader(tmp_path, ["pkg/m.py"])
    store = Store(tmp_path / ".sidegraph")
    answer = server._get_task_context_impl(store, reader, files, None, 4000, 6000)
    assert "Not in the code graph" in answer
    header = rows(store, "delivery_occurrences")[0]
    assert header["outcome"] == want
    assert header["unresolved"] == 1
    store.close()


def test_real_input_validation_error_has_neutral_core_failure_reason(tmp_path, monkeypatch):
    from pydantic import ValidationError

    from tests.test_seed_ladder import make_reader

    monkeypatch.setenv("SIDEGRAPH_TELEMETRY", "on")
    store = Store(tmp_path / ".sidegraph")
    reader = make_reader(tmp_path, ["pkg/m.py"])
    with pytest.raises(ValidationError):
        server._query_decisions_impl(store, reader, None, [{"name": 7}], 6000)
    header = rows(store, "delivery_occurrences")[0]
    assert header["outcome"] == "error"
    assert header["reason"] == "core_error"
    store.close()
