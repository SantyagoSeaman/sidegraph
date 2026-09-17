"""``sidegraph-sync --json``/``--check``: CI-consumable report shape shared with the
``sync_anchors`` MCP tool (``sync.report_as_dict``/``sync.report_has_findings``). See
design/superpowers/specs/2026-07-11-ci-integrity-design.md ruling 1 (the report shape) and
design/superpowers/specs/2026-07-11-ci-live-findings-design.md ruling 1 (the failing-classes
refinement below). Reuses the store+graph fixture approach from tests/test_sync_run.py."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from sidegraph.cli import sync_main
from sidegraph.engine.reader import GraphifyReader
from sidegraph.schema import (
    AnchorBinding,
    Decision,
    DecisionKind,
    Descriptor,
    Domain,
    DomainStatus,
    Entity,
    Provenance,
)
from sidegraph.store import Store

GRAPH = {
    "built_at_commit": "vA",
    "nodes": [
        # stable: same id, same file -> unchanged (no findings)
        {
            "id": "s1",
            "label": "f_stable()",
            "norm_label": "f_stable()",
            "file_type": "code",
            "source_file": "a.py",
            "community": 1,
        },
    ],
    "links": [],
}


def _entity(store, name, file, node_id):
    e = Entity(
        canonical_name=name,
        descriptor=Descriptor(name=name, file_path=file),
        last_seen_node_id=node_id,
    )
    return store.upsert_entity(e)


def _leaf(store, entity_id):
    """A decision whose only anchor is a tier-2 (leaf) binding to ``entity_id`` -- see
    tests/test_sync_run.py's ``_leaf``. When the entity orphans, this binding orphans with
    it (``rebind_entity``'s ``_set_leaf_status``), which is exactly what makes the decision
    show up in ``SyncReport.stale_decisions``."""
    d = store.add_decision(
        Decision(
            title=f"about {entity_id}",
            kind=DecisionKind.LESSON,
            context="c",
            choice="ch",
            valid_from=datetime.now(UTC),
            provenance=Provenance(source="manual"),
        )
    )
    store.add_binding(AnchorBinding(record_id=d.id, entity_id=entity_id, tier=2, status="live"))
    return d


@pytest.fixture
def clean_store_env(tmp_path, monkeypatch):
    """A store whose one tracked entity resolves cleanly against the graph -- no
    findings, ``--check`` should exit 0."""
    graph_path = tmp_path / "graph.json"
    graph_path.write_text(json.dumps(GRAPH))
    store_dir = tmp_path / ".sidegraph"
    store = Store(store_dir)
    _entity(store, "f_stable", "a.py", "s1")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SIDEGRAPH_DIR", str(store_dir))
    monkeypatch.setenv("SIDEGRAPH_GRAPH", str(graph_path))


@pytest.fixture
def orphaning_store_env(tmp_path, monkeypatch):
    """A store whose tracked entity's node is gone from the graph -> orphaned outcome +
    its sole anchor decision goes stale (all tier-2 leaves orphaned) -- under the current
    gate (design/superpowers/specs/2026-07-11-ci-live-findings-design.md ruling 1) the
    orphaned outcome alone is informational; ``--check`` exits 2 because the stale decision
    fires, not because of the outcome."""
    graph_path = tmp_path / "graph.json"
    graph_path.write_text(json.dumps(GRAPH))
    store_dir = tmp_path / ".sidegraph"
    store = Store(store_dir)
    gone = _entity(store, "gone_fn", "b.py", "r1")
    _leaf(store, gone.entity_id)

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SIDEGRAPH_DIR", str(store_dir))
    monkeypatch.setenv("SIDEGRAPH_GRAPH", str(graph_path))


@pytest.fixture
def healed_store_env(tmp_path, monkeypatch):
    """The healed shape design/superpowers/specs/2026-07-11-ci-live-findings-design.md
    ruling 1 exists for: a decision with TWO tier-2 anchors -- one (``f_stable``/``s1``)
    resolves cleanly, the other (``renamed_away_fn``/``r9``, standing in for a function a
    legitimate rename moved out from under its old anchor) has no node in the graph and
    orphans. The decision keeps its other live anchor, so it never goes stale --
    ``--check`` must exit 0 even though the orphaned outcome is still reported
    (informational only)."""
    graph_path = tmp_path / "graph.json"
    graph_path.write_text(json.dumps(GRAPH))
    store_dir = tmp_path / ".sidegraph"
    store = Store(store_dir)
    kept = _entity(store, "f_stable", "a.py", "s1")
    renamed_away = _entity(store, "renamed_away_fn", "b.py", "r9")
    d = store.add_decision(
        Decision(
            title="about both",
            kind=DecisionKind.LESSON,
            context="c",
            choice="ch",
            valid_from=datetime.now(UTC),
            provenance=Provenance(source="manual"),
        )
    )
    store.add_binding(
        AnchorBinding(record_id=d.id, entity_id=kept.entity_id, tier=2, status="live")
    )
    store.add_binding(
        AnchorBinding(record_id=d.id, entity_id=renamed_away.entity_id, tier=2, status="live")
    )

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SIDEGRAPH_DIR", str(store_dir))
    monkeypatch.setenv("SIDEGRAPH_GRAPH", str(graph_path))


@pytest.fixture
def domain_failure_store_env(tmp_path, monkeypatch):
    """CLI-level parity with ``orphaning_store_env`` for the ``domain_failures`` finding
    class (final-review.md ledger #2 / Important-3's sibling gap: no CLI-level ``--check``
    exit-2 test existed for this finding, and this is the same family whose gating lives on
    exactly the path Important-2 fixed). An accepted domain's ``seed_anchors`` resolve
    raises unconditionally, isolated to that one domain by ``_refresh_domains``'s own
    per-domain try -- no tracked entities exist here, so this exercises the domain-refresh
    path in isolation from the entity rebind ladder."""
    graph_path = tmp_path / "graph.json"
    graph_path.write_text(json.dumps(GRAPH))
    store_dir = tmp_path / ".sidegraph"
    store = Store(store_dir)
    store.add_domain(
        Domain(
            slug="broken",
            title="broken",
            summary="broken summary",
            status=DomainStatus.ACCEPTED,
            seed_anchors=[{"name": "f_stable", "file_path": "a.py"}],
            provenance=Provenance(source="manual"),
        )
    )

    def boom(self, desc):
        raise RuntimeError("malformed descriptor")

    monkeypatch.setattr(GraphifyReader, "resolve", boom)

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SIDEGRAPH_DIR", str(store_dir))
    monkeypatch.setenv("SIDEGRAPH_GRAPH", str(graph_path))


def test_sync_json_prints_tool_shape(clean_store_env, capsys):
    rc = sync_main(["--json", "--force"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert set(out) == {
        "synced",
        "from_version",
        "to_version",
        "counts",
        "repointed",
        "outcomes",
        "stale_decisions",
        "empty_domains",
        "overbroad_domains",
        "slug_conflicts",
        "domains_refreshed",
        "domain_failures",
    }


def test_sync_check_exit_2_on_orphaned(orphaning_store_env, capsys):
    rc = sync_main(["--json", "--check", "--force"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 2
    assert any(o["status"] == "orphaned" for o in out["outcomes"])
    # pins the fixture's own claim: the orphaned entity's sole anchor decision goes stale
    assert out["stale_decisions"]


def test_sync_check_exit_0_on_clean(clean_store_env):
    assert sync_main(["--check", "--force"]) == 0


def test_sync_check_exit_0_when_orphan_has_live_sibling(healed_store_env, capsys):
    # The healed shape: orphaned outcome still present (informational), but the decision
    # keeps a live anchor so it never goes stale -- ruling 1 turns this green.
    rc = sync_main(["--json", "--check", "--force"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert any(o["status"] == "orphaned" for o in out["outcomes"])
    assert out["stale_decisions"] == []


def test_sync_check_json_compose(orphaning_store_env, capsys):
    rc = sync_main(["--json", "--check", "--force"])
    out = json.loads(capsys.readouterr().out)  # stdout is still pure JSON
    assert rc == 2 and any(o["status"] == "orphaned" for o in out["outcomes"])


def test_sync_check_exit_2_on_domain_failure(domain_failure_store_env, capsys):
    """CLI-level parity with ``test_sync_check_exit_2_on_orphaned`` for the
    ``domain_failures`` finding class (ledger #2). This is the test the whole-branch
    review named as most likely to have caught Important-2 (`--check` silently
    pre-empted by an earlier caller's one-shot heal): it exercises the same
    ``sync_main`` -> ``sync(force=...)`` path the CLI actually runs, not a direct
    ``sync()`` unit call."""
    rc = sync_main(["--json", "--check", "--force"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 2
    assert out["domain_failures"]
    assert out["domain_failures"][0]["slug"] == "broken"
    assert "malformed descriptor" in out["domain_failures"][0]["error"]


def test_sync_json_skip_is_exit_0_without_check(clean_store_env, capsys):
    """A PLAIN sync (no ``--check``) legitimately reports a version-match skip -- that
    is unaffected by Important-2's fix, which only changes ``--check``'s own force
    behaviour (see the sibling test below)."""
    sync_main(["--force"])  # establish version
    capsys.readouterr()  # discard the (non-json) prose from the establishing run above --
    # only the --json call's stdout below is asserted to be pure JSON
    rc = sync_main(["--json"])  # version match, no --check -> skip
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["synced"] is False


def test_sync_check_forces_a_real_pass_even_on_version_match(clean_store_env, capsys):
    """Important-2: ``--check`` must never be silently pre-empted by an earlier
    caller's one-shot cold-reload heal. Pin it at the level the bug actually lived at --
    a version match that would otherwise gate a plain sync must still run a REAL pass
    under ``--check`` (``synced: True``, not the empty skipped-report shape), even
    though no ``--force`` was passed."""
    sync_main(["--force"])  # establish version
    capsys.readouterr()  # discard the (non-json) prose from the establishing run above
    rc = sync_main(["--json", "--check"])  # version match, but --check implies force
    out = json.loads(capsys.readouterr().out)
    assert rc == 0  # clean store -- no findings
    assert out["synced"] is True
