"""Twin store fixtures for Portal preparation and wire parity (Portal design, D10)."""

from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import pytest

from sidegraph.engine.reader import GraphifyReader
from sidegraph.schema import (
    AnchorBinding,
    Decision,
    DecisionKind,
    DecisionStatus,
    Descriptor,
    Domain,
    DomainStatus,
    Entity,
    Fact,
    Provenance,
    Scope,
)
from sidegraph.store import Store
from sidegraph.sync import sync
from tests.graph_repo_support import GRAPH, add_worktree, commit, make_main, write_graph

Scenario = Literal["cold", "synced", "moved", "worktree"]
NOW = datetime(2026, 11, 17, 18, tzinfo=UTC)
ENTITY_ID = "01M2B0F19RPYSVTTWG9D6XSHZW"
DECISION_ID = "01M2B0F19RPYSVTTWG9D6XSHZX"
PROPOSAL_ID = "01M2B0F19RPYSVTTWG9D6XSHZY"
FACT_ID = "01M2B0F19RPYSVTTWG9D6XSHZZ"
DOMAIN_ID = "01M2B0F19RPYSVTTWG9D6XSJ00"


def _build_store(root: Path, scenario: Scenario) -> Path:
    old_path = "pkg/old.py" if scenario == "moved" else "pkg/m.py"
    repo = make_main(root, files=(old_path, "util/n.py"))
    provenance = Provenance(source="manual")
    descriptor = Descriptor(name="fn_0()", file_path=old_path)
    with Store(repo / ".sidegraph") as store:
        entity = store.upsert_entity(
            Entity(entity_id=ENTITY_ID, canonical_name="fn_0()", descriptor=descriptor)
        )
        for record_id, status, title in (
            (DECISION_ID, DecisionStatus.ACCEPTED, "Keep a retry budget"),
            (PROPOSAL_ID, DecisionStatus.PROPOSED, "Retry only the pending batch"),
        ):
            store.add_decision(
                Decision(
                    id=record_id,
                    kind=DecisionKind.GOTCHA,
                    status=status,
                    title=title,
                    context="A request may arrive twice.",
                    choice="Track the retries against the batch.",
                    scope=Scope.REPO,
                    valid_from=NOW,
                    provenance=provenance,
                )
            )
            store.add_binding(
                AnchorBinding(record_id=record_id, entity_id=entity.entity_id, tier=2)
            )
        store.add_fact(
            Fact(
                id=FACT_ID,
                statement="Retry count was three.",
                source="Batch test",
                supports=[DECISION_ID],
                status=DecisionStatus.ACCEPTED,
                valid_from=NOW,
                provenance=provenance,
            )
        )
        store.add_binding(AnchorBinding(record_id=FACT_ID, entity_id=entity.entity_id, tier=2))
        store.add_domain(
            Domain(
                domain_id=DOMAIN_ID,
                slug="package",
                title="Package",
                summary="Batch processing.",
                path_prefixes=["pkg/"],
                status=DomainStatus.ACCEPTED,
                provenance=provenance,
            )
        )

    git_head = commit(repo, "Record the batch implementation", {})
    if scenario == "worktree":
        checkout = add_worktree(repo)
    else:
        checkout = repo
        if scenario == "moved":
            git_head = commit(
                repo, "Move batch processing", {old_path: None, "pkg/m.py": "def a():\n    pass\n"}
            )
    if scenario != "worktree":
        write_graph(repo / GRAPH, git_head, ["pkg/m.py", "util/n.py"])
        (repo / ".sidegraph" / "index.db").unlink()
    path = checkout / ".sidegraph"
    if scenario == "synced":
        with Store(path) as store:
            sync(store, GraphifyReader(repo / GRAPH))
    return path


def build_twin_stores(tmp_path: Path, scenario: Scenario) -> tuple[Path, Path]:
    """Build two equivalent stores independently.
    See design/superpowers/specs/2026-11-17-portal-design.md (D10).
    """
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("GIT_AUTHOR_DATE", NOW.isoformat())
        patch.setenv("GIT_COMMITTER_DATE", NOW.isoformat())
        # Equal-length paths leave both twins the same room in bounded path fields.
        return _build_store(tmp_path / "real-mcp", scenario), _build_store(
            tmp_path / "emulated", scenario
        )
