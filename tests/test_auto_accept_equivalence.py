"""``SIDEGRAPH_AUTO_ACCEPT`` x ``SIDEGRAPH_RATIFY_POLICY``: the landing status and the ratifier
stamps every env combination produces through the ``propose_decisions`` and ``propose_domains``
tools. The goldens were captured from the code BEFORE the variable was folded into
``RatifyPolicy`` and must not change with the fold: ``on`` wins over every policy, lands
accepted with no stamp and no live-anchor requirement, and never touches domains; any other
value is off.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

import pytest

from sidegraph import server
from sidegraph.schema import Decision, DecisionKind, DecisionStatus, Provenance
from sidegraph.store import Store
from tests.test_ratify_policy import (
    _eligible_decision_draft,
    _eligible_domain_draft,
    _eligible_fact_draft,
    _feature_graph,
)

AUTO_ACCEPT_VALUES = [None, "0", "1", "off", "on"]
POLICY_VALUES = [None, "manual", "auto-low-risk", "auto-all", "bogus"]


def observe(tmp_path, auto_accept: str | None, policy: str | None) -> dict[str, str]:
    """Run one fixed batch under the env combination; return label -> 'status|stamp|error'."""
    env = {"SIDEGRAPH_GRAPH": str(_feature_graph(tmp_path).path)}
    if auto_accept is not None:
        env["SIDEGRAPH_AUTO_ACCEPT"] = auto_accept
    if policy is not None:
        env["SIDEGRAPH_RATIFY_POLICY"] = policy
    store = Store(tmp_path / "t.db")
    predecessor = Decision(
        title="predecessor",
        kind=DecisionKind.GOTCHA,
        status=DecisionStatus.ACCEPTED,
        context="c",
        valid_from=datetime.now(UTC),
        choice="x",
        provenance=Provenance(source="human"),
    )
    store.add_decision(predecessor)
    with mock.patch.dict(os.environ, env), mock.patch.object(server, "_store", store):
        decisions = server.propose_decisions(
            drafts=[
                _eligible_decision_draft(title="plain"),
                _eligible_decision_draft(
                    title="successor",
                    supersedes=predecessor.id,
                    facts=[_eligible_fact_draft(statement="attached")],
                ),
                _eligible_decision_draft(title="unanchored", anchors=[]),
                _eligible_decision_draft(title="adr", kind="adr"),
            ],
            facts=[_eligible_fact_draft(statement="standalone")],
        )
        [domain] = server.propose_domains(drafts=[_eligible_domain_draft()])
    out: dict[str, str] = {}

    def row(label: str, rec, result_stamp, error) -> None:
        out[label] = f"{rec.status.value}|{rec.ratified_by}|{result_stamp}|{error}"

    plain, successor, unanchored, adr, standalone = decisions
    row("plain", store.get_decision(plain["decision_id"]), plain["ratified_by"], None)
    row(
        "successor",
        store.get_decision(successor["decision_id"]),
        successor["ratified_by"],
        successor["auto_ratify_error"],
    )
    [attached] = successor["facts"]
    row("attached", store.get_fact(attached["fact_id"]), attached["ratified_by"], None)
    row(
        "unanchored", store.get_decision(unanchored["decision_id"]), unanchored["ratified_by"], None
    )
    row("adr", store.get_decision(adr["decision_id"]), adr["ratified_by"], None)
    row("standalone", store.get_fact(standalone["fact_id"]), standalone["ratified_by"], None)
    out["predecessor"] = store.get_decision(predecessor.id).status.value
    dom = next(iter(store.iter_domains()))
    out["domain"] = f"{dom.status.value}|{dom.ratified_by}|{domain['status']}"
    return out


GOLDEN: dict[str, dict[str, str]] = json.loads(
    (
        Path(__file__).parent / "fixtures" / "auto_policy" / "auto_accept_equivalence_goldens.json"
    ).read_text(encoding="utf-8")
)


@pytest.mark.parametrize("auto_accept", AUTO_ACCEPT_VALUES)
@pytest.mark.parametrize("policy", POLICY_VALUES)
def test_env_combination_matches_pre_fold_behaviour(tmp_path, auto_accept, policy):
    assert observe(tmp_path, auto_accept, policy) == GOLDEN[f"{auto_accept}|{policy}"]
