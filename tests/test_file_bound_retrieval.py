"""Exact file applicability is distinct from verified symbol resolution."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from sidegraph import server
from sidegraph.engine.reader import GraphifyReader
from sidegraph.retrieval import RetrievalBudget, Seed, get_task_context
from sidegraph.schema import (
    AnchorBinding,
    Decision,
    DecisionKind,
    DecisionStatus,
    Descriptor,
    Entity,
    Fact,
    Provenance,
    Scope,
)
from sidegraph.store import Store

TARGET = ".github/workflows/publish.yml"
NEIGHBOR = ".github/workflows/build.yml"


class Project:
    def __init__(self, root: Path, monkeypatch):
        self.root = root
        self.monkeypatch = monkeypatch
        self.now = datetime.now(UTC)
        self.store = Store(root / ".sidegraph")
        self.next_id = 0
        self.entities: dict[str, Entity] = {}
        for path in (TARGET, NEIGHBOR):
            file = root / path
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text("name: synthetic\n")
        monkeypatch.chdir(root)

    def entity(self, path: str) -> Entity:
        if path not in self.entities:
            name = "publish" if path == TARGET else "build"
            self.entities[path] = self.store.upsert_entity(
                Entity(
                    canonical_name=name,
                    descriptor=Descriptor(name=name, file_path=path),
                    last_seen_node_id=name,
                )
            )
        return self.entities[path]

    def record(
        self,
        title: str,
        *,
        path: str = TARGET,
        fact: bool = False,
        status=DecisionStatus.ACCEPTED,
        binding="live",
        age=2,
        expired=False,
        long=False,
    ):
        self.next_id += 1
        common = dict(
            id=f"{self.next_id:026d}",
            status=status,
            valid_from=self.now - timedelta(days=age),
            provenance=Provenance(source="manual"),
        )
        if fact:
            record = self.store.add_fact(
                Fact(
                    statement=title,
                    source="Synthetic measurement",
                    valid_to=self.now - timedelta(days=1) if expired else None,
                    **common,
                )
            )
        else:
            record = self.store.add_decision(
                Decision(
                    title=title,
                    kind=DecisionKind.CONSTRAINT,
                    scope=Scope.REPO,
                    context="context " * (250 if long else 1),
                    choice="Preserve this qualifier",
                    valid_to=self.now - timedelta(days=1) if expired else None,
                    **common,
                )
            )
        entity = self.entity(path)
        self.store.add_binding(
            AnchorBinding(record_id=record.id, entity_id=entity.entity_id, tier=2, status=binding)
        )
        return record

    def reader(self, present=False):
        paths = [TARGET, NEIGHBOR] if present else [NEIGHBOR]
        nodes = [
            {
                "id": "publish" if p == TARGET else "build",
                "label": "publish" if p == TARGET else "build",
                "file_type": "code",
                "source_file": p,
                "community": 1,
            }
            for p in paths
        ]
        graph = self.root / ("present.json" if present else "missing.json")
        graph.write_text(json.dumps({"nodes": nodes, "links": []}) + "\n")
        reader = GraphifyReader(graph)
        self.monkeypatch.setattr(reader, "repo_root", lambda: self.root)
        return reader

    def call(self, surface="get", *, reader="missing", files=None, entities=None, budget=6000):
        r = None if reader is None else self.reader(present=reader == "present")
        files = [TARGET] if files is None else files
        if surface == "get":
            return server._get_task_context_impl(self.store, r, files, entities, 4000, budget)
        return server._query_decisions_impl(self.store, r, files, entities, budget)

    def canonical(self):
        return {
            str(p.relative_to(self.root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in (self.root / ".sidegraph").rglob("*.json")
        }


@pytest.fixture
def project(tmp_path, monkeypatch):
    p = Project(tmp_path, monkeypatch)
    yield p
    p.store.close()


@pytest.mark.parametrize("surface", ["get", "thin"])
@pytest.mark.parametrize("binding", ["live", "degraded", "orphaned"])
def test_missing_file_delivers_its_own_decision_and_fact(project, surface, binding):
    """Catches dropped seed paths, graph-only lookup and live-only fallback."""
    project.record("Direct deployment constraint", binding=binding)
    project.record("Direct deployment measurement", fact=True, binding=binding)
    project.record("Unrelated neighboring rule", path=NEIGHBOR)
    before = project.canonical()
    out = project.call(surface)
    assert "Direct deployment constraint" in out
    assert "Direct deployment measurement" in out
    assert "[file-bound]" in out
    assert "Unrelated neighboring rule" not in out
    assert "No current record is anchored" not in out
    assert project.canonical() == before


def test_present_control_preserves_verified_render(project):
    """Guard: ordinary verified file retrieval must not acquire a fallback warning."""
    project.record("Verified direct rule")
    project.record("Verified direct measurement", fact=True)
    out = project.call(reader="present")
    assert "Verified direct rule" in out and "Verified direct measurement" in out
    assert "[file-bound]" not in out


@pytest.mark.parametrize("surface", ["get", "thin"])
def test_fact_only_file_is_memory_and_does_not_trigger_neighbor_fill(project, surface):
    """Catches a decision-only fallback and facts missing from has_memory."""
    project.record("Only relevant measurement", fact=True, binding="orphaned")
    project.record("Unrelated neighboring rule", path=NEIGHBOR)
    out = project.call(surface)
    assert "Only relevant measurement" in out
    assert "Unrelated neighboring rule" not in out


@pytest.mark.parametrize(
    "reader,files,entities",
    [
        ("present", [TARGET], None),
        ("missing", [], [{"name": "publish", "file_path": TARGET}]),
        (None, [TARGET], None),
    ],
)
def test_file_fallback_does_not_resolve_other_orphan_cases(project, reader, files, entities):
    """Guard: no graph-present symbol resurrection, named-query bypass or graph-free expansion."""
    project.record("Unresolved symbol warning", binding="orphaned")
    assert "Unresolved symbol warning" not in project.call(
        reader=reader, files=files, entities=entities
    )


@pytest.mark.parametrize(
    "path", [TARGET + "/", "../" + TARGET, "/" + TARGET, "C:/" + TARGET, ".github/workflows/"]
)
def test_non_file_authority_does_not_read_exact_fallback(project, path):
    """Guard: normalization must not turn a directory/outside path into exact-file authority."""
    project.record("Exact file secret rule", binding="orphaned")
    assert "Exact file secret rule" not in project.call(files=[path])


@pytest.mark.parametrize(
    "status", [DecisionStatus.REJECTED, DecisionStatus.SUPERSEDED, DecisionStatus.DEPRECATED]
)
def test_terminal_file_record_is_not_current_content(project, status):
    """Guard: bypassing volatile bindings must not bypass record trust/history."""
    project.record("Terminal rule", status=status, binding="orphaned")
    out = project.call()
    # A reverted history one-liner is allowed; accepted direct content is not.
    if status == DecisionStatus.SUPERSEDED and "Terminal rule" in out:
        assert "tried, reverted" in out and "## Related" in out
    else:
        assert "Terminal rule" not in out


@pytest.mark.parametrize(
    "regulated,age,want", [(False, 2, True), (True, 2, False), (False, 45, False)]
)
def test_file_proposal_retains_window_and_regulated_mode(
    project, monkeypatch, regulated, age, want
):
    """Catches proposed records promoted to accepted or aged/regulated leakage."""
    monkeypatch.setenv("SIDEGRAPH_UNRATIFIED", "off" if regulated else "on")
    monkeypatch.setenv("SIDEGRAPH_PROPOSAL_WINDOW_DAYS", "30")
    project.record(
        "Unreviewed file rule", status=DecisionStatus.PROPOSED, binding="orphaned", age=age
    )
    out = project.call()
    assert ("Unreviewed file rule" in out) is want
    if want:
        assert "[unratified]" in out and "[file-bound]" in out


def test_degraded_file_line_keeps_tag_and_actual_placement(project):
    """Catches tags lost on budget fallback and wrong delivered identity."""
    d = project.record("Large direct warning", binding="orphaned", long=True)
    reader = project.reader()
    ctx = get_task_context(
        [Seed(file_path=TARGET)], project.store, reader, RetrievalBudget(memory_chars=230)
    )
    assert ctx.shown_ids == [d.id]
    assert ctx.degraded == 1
    assert "[file-bound]" in ctx.render()
    assert ctx.chars_used <= 230
    assert ctx.delivery_placed == {("decision", d.id)}


def test_duplicate_file_seed_is_delivered_once(project):
    """Catches duplicate counting through explicit file membership and other bindings."""
    project.record("One file warning", binding="orphaned")
    out = project.call(files=[TARGET, TARGET])
    assert out.count("One file warning") == 1


def test_file_route_explanation_survives_the_mcp_cap(project):
    """Catches the cap's drift-only legend filter silently removing the new explanation."""
    project.record("Unverified file warning", binding="orphaned")
    out = project.call()
    assert "[file-bound]" in out and "the graph cannot verify its symbol anchors" in out


@pytest.mark.parametrize("paths", [[TARGET, NEIGHBOR], [NEIGHBOR, TARGET]])
def test_verified_seed_route_wins_marker_precedence(project, paths):
    """Catches a traversal-order-dependent warning when an ordinary seed also reaches the record."""
    d = project.record("Shared verified rule", binding="orphaned")
    e = project.entity(NEIGHBOR)
    project.store.add_binding(
        AnchorBinding(record_id=d.id, entity_id=e.entity_id, tier=2, status="live")
    )
    out = project.call(files=paths)
    assert out.count("Shared verified rule") == 1
    assert "[file-bound]" not in out


def test_pure_thin_wrapper_uses_the_same_file_route(project):
    """Catches the pure wrapper forgetting the new ephemeral seed metadata."""
    from sidegraph.retrieval import query_decisions

    project.record("Pure thin file rule", binding="orphaned")
    out = query_decisions([Seed(file_path=TARGET)], project.store, project.reader())
    assert "Pure thin file rule" in out and "[file-bound]" in out


def test_graph_held_non_anchorable_file_keeps_orphan_guard(project):
    """Guard: nodes_in_file emptiness must not be mistaken for graph absence."""
    project.record("Held config orphan", binding="orphaned")
    graph = project.root / "config.json"
    graph.write_text(
        json.dumps(
            {
                "nodes": [
                    {
                        "id": "publish",
                        "label": "publish",
                        "file_type": "image",
                        "source_file": TARGET,
                    }
                ],
                "links": [],
            }
        )
        + "\n"
    )
    reader = GraphifyReader(graph)
    assert TARGET in reader.source_files() and reader.nodes_in_file(TARGET) == []
    out = get_task_context([Seed(file_path=TARGET)], project.store, reader).render()
    assert "Held config orphan" not in out


def test_accepted_support_of_proposed_file_parent_inherits_route(project, monkeypatch):
    """Catches unmarked accepted evidence rendered standalone from an unverified proposed parent."""
    monkeypatch.setenv("SIDEGRAPH_UNRATIFIED", "on")
    parent = project.record("Proposed parent", status=DecisionStatus.PROPOSED, binding="orphaned")
    project.store.add_fact(
        Fact(
            id="00000000000000000000000099",
            statement="Standalone supported observation",
            source="Synthetic observation",
            supports=[parent.id],
            status=DecisionStatus.ACCEPTED,
            valid_from=project.now,
            provenance=Provenance(source="manual"),
        )
    )
    out = project.call()
    line = next(line for line in out.splitlines() if "Standalone supported observation" in line)
    assert "[file-bound]" in line and "[unratified]" not in line
    assert "[unratified] [file-bound] Proposed parent" in out


def test_remaining_pointer_names_both_working_listing_surfaces(project):
    """Catches the mixed population being advertised through a decision-only follow-up."""
    d = project.record("Listed decision", binding="orphaned")
    f = project.record("Listed measurement", fact=True, binding="orphaned")
    out = project.call(budget=0)
    assert "has 2 more record(s)" in out
    assert f'retrieve_decisions(files=["{TARGET}"])' in out
    assert f'list_facts(files=["{TARGET}"])' in out
    decisions = json.loads(
        server._decision_listing(project.store, project.reader(), files=[TARGET])
    )
    facts = json.loads(server._fact_listing(project.store, project.reader(), files=[TARGET]))
    assert [row["id"] for row in decisions["decisions"]] == [d.id]
    assert [row["id"] for row in facts["facts"]] == [f.id]


def test_actual_hard_cut_keeps_both_explanations_and_final_telemetry(project, monkeypatch):
    """Catches drift-only legend retention and telemetry credit for a cut file record."""
    from sidegraph import retrieval
    from tests.test_delivery_surfaces import rows

    monkeypatch.setenv("SIDEGRAPH_TELEMETRY", "on")
    verified = project.record("Verified drifting warning", path=NEIGHBOR)
    file = project.record("Small file warning", binding="orphaned")
    cut = project.record("Large file warning", binding="orphaned", long=True)
    monkeypatch.setattr(retrieval, "drifted_record_ids", lambda _: frozenset([verified.id]))
    original = server._hard_cap
    monkeypatch.setattr(server, "_hard_cap", lambda *a, **k: original(*a, **k, cap=1800))
    out = project.call(files=[NEIGHBOR, TARGET])
    assert len(out) <= 1800 and "Large file warning" not in out
    assert "Verified drifting warning" in out and "Small file warning" in out
    assert "[drifted] =" in out and "[file-bound] =" in out
    assert {p["record_id"] for p in rows(project.store, "delivery_placements")} == {
        verified.id,
        file.id,
    }
    assert cut.id not in out
    assert "more record(s)" not in out  # accepted pre-cap zero-remainder parity


def test_partial_file_pointer_is_a_floor_when_pre_cap_delivery_can_be_cut(project, monkeypatch):
    """Catches an exact remainder derived from a file record later removed by the final cap."""
    project.record("Small warning", binding="orphaned")
    project.record("Measurement " * 70, fact=True, binding="orphaned")
    original = server._hard_cap
    monkeypatch.setattr(server, "_hard_cap", lambda *a, **k: original(*a, **k, cap=1100))
    out = project.call(budget=200)
    assert len(out) <= 1100
    assert "has at least 1 more record(s)" in out


@pytest.mark.parametrize("surface", ["get", "thin"])
def test_normalized_bare_file_keeps_its_authority(project, surface):
    """Catches dropping normalized unresolved paths before the exact file fallback."""
    project.record("Normalized file rule", binding="orphaned")
    # The MCP thin tool deliberately has no ladder; its exact spelling is the contract.
    path = "./" + TARGET if surface == "get" else TARGET
    assert "Normalized file rule" in project.call(surface, files=[path])


def test_failed_named_seed_and_missing_directory_do_not_acquire_file_authority(project):
    """Guard: named failures and removed trailing slash cannot broaden the query."""
    project.record("Must not broaden", binding="orphaned")
    assert "Must not broaden" not in project.call(
        files=[], entities=[{"name": "Missing", "file_path": TARGET}]
    )
    (project.root / TARGET).unlink()
    assert "Must not broaden" not in project.call(files=[TARGET + "/"])


@pytest.mark.parametrize("fact", [False, True])
def test_expired_file_records_stay_hidden(project, fact):
    """Guard: file applicability removes only the volatile binding reach restriction."""
    project.record("Expired applicability", fact=fact, binding="orphaned", expired=True)
    assert "Expired applicability" not in project.call()


@pytest.mark.parametrize("surface", ["get", "thin"])
@pytest.mark.parametrize("support", [False, True])
def test_proposed_file_fact_keeps_marker_and_explanation(project, monkeypatch, surface, support):
    """Sol F1 / Opus I1: proposals must preserve the unverified file route too."""
    monkeypatch.setenv("SIDEGRAPH_UNRATIFIED", "on")
    if support:
        parent = project.record(
            "Proposed file parent", status=DecisionStatus.PROPOSED, binding="orphaned"
        )
        project.store.add_fact(
            Fact(
                id="00000000000000000000000098",
                statement="Proposed file observation",
                source="Synthetic measurement",
                status=DecisionStatus.PROPOSED,
                valid_from=project.now,
                provenance=Provenance(source="manual"),
                supports=[parent.id],
            )
        )
    else:
        project.record(
            "Proposed file observation",
            fact=True,
            status=DecisionStatus.PROPOSED,
            binding="orphaned",
        )
    out = project.call(surface)
    line = next(line for line in out.splitlines() if "Proposed file observation" in line)
    assert "fact [unratified] [file-bound]:" in line
    assert "the graph cannot verify its symbol anchors" in out


@pytest.mark.parametrize("surface", ["get", "thin"])
def test_same_entity_keeps_named_live_and_file_orphan_routes_separate(project, surface):
    """Sol F2: file membership must not overwrite ordinary reachability of the same entity."""
    project.record("Normally named rule", binding="live")
    project.record("File-only orphan rule", binding="orphaned")
    named = [{"name": "publish", "file_path": TARGET}]
    normal = project.call(surface, files=[], entities=named)
    assert "Normally named rule" in normal and "[file-bound]" not in normal
    assert "File-only orphan rule" not in normal
    out = project.call(surface, files=[TARGET], entities=named)
    ordinary = next(line for line in out.splitlines() if "Normally named rule" in line)
    fallback = next(line for line in out.splitlines() if "File-only orphan rule" in line)
    assert "[file-bound]" not in ordinary
    assert "[file-bound]" in fallback


@pytest.mark.parametrize("reverse", [False, True])
def test_same_entity_precedence_does_not_depend_on_seed_order(project, reverse):
    project.record("Ordinary same-entity rule", binding="live")
    seeds = [Seed(file_path=TARGET), Seed(name="publish", file_path=TARGET)]
    if reverse:
        seeds.reverse()
    out = get_task_context(seeds, project.store, project.reader()).render()
    assert "Ordinary same-entity rule" in out and "[file-bound]" not in out


@pytest.mark.parametrize("exists", [False, True])
def test_padded_directory_intent_never_acquires_file_authority(project, exists):
    """Sol F3 / Opus I2: the original spelling's stripped slash intent is authoritative."""
    project.record("Directory authority guard", binding="orphaned")
    if not exists:
        (project.root / TARGET).unlink()
    out = project.call(files=[TARGET + "/ "])
    assert "Directory authority guard" not in out


@pytest.mark.parametrize("surface", ["get", "thin"])
def test_file_only_reverted_history_keeps_unverified_marker(project, surface):
    """Sol F4 / Opus M1: historical content needs a route marker without becoming current."""
    project.record("Reverted file rule", status=DecisionStatus.SUPERSEDED, binding="orphaned")
    out = project.call(surface)
    assert "tried, reverted: [constraint] [file-bound] Reverted file rule" in out
    assert "the graph cannot verify its symbol anchors" in out
    assert "## ⚠ Known mistakes" not in out


def test_ordinary_named_history_route_wins_over_redundant_file_route(project):
    project.record("Ordinary named history", status=DecisionStatus.SUPERSEDED, binding="orphaned")
    out = project.call(files=[TARGET], entities=[{"name": "publish", "file_path": TARGET}])
    assert "Ordinary named history" in out and "[file-bound]" not in out
