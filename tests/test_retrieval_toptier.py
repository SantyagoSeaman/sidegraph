from datetime import UTC, datetime
from pathlib import Path

from sidegraph.engine.reader import GraphifyReader
from sidegraph.retrieval import top_tier_map
from sidegraph.schema import Decision, DecisionKind, Initiative, Provenance, Scope
from sidegraph.store import Store

FIXTURE = Path(__file__).parent / "fixtures" / "mini_graph.json"


def test_top_tier_map_lists_communities_and_global_mistakes(tmp_path):
    r = GraphifyReader(FIXTURE)
    s = Store(tmp_path / "t.db")
    s.upsert_initiative(Initiative(name="Trading Core"))
    g = Decision(
        title="never block the event loop",
        kind=DecisionKind.CONSTRAINT,
        context="c",
        choice="use async",
        scope=Scope.GLOBAL,
        valid_from=datetime.now(UTC),
        provenance=Provenance(source="manual"),
    )
    s._write_decision(g)
    s._conn.commit()
    text = top_tier_map(s, r)
    # the standing get_task_context instruction now lives at host.hooks.session_start,
    # prepended once regardless of which renderer produced the rest of the text -- this
    # renderer itself only needs to still emit its own header.
    assert "# Sidegraph — project memory" in text
    assert "Trading Core" in text  # initiative
    assert "never block the event loop" in text  # global mistake
    assert "Communities" in text  # community section


def test_top_tier_map_degrades_without_reader(tmp_path):
    s = Store(tmp_path / "t.db")
    text = top_tier_map(s, None)
    assert "# Sidegraph — project memory" in text  # still emits the header + is a str
    assert "Communities" not in text  # no reader -> no community section


def test_top_tier_map_tags_unratified_global_mistakes(tmp_path):
    s = Store(tmp_path / "t.db")
    d = Decision(
        title="watch for stale cache",
        kind=DecisionKind.GOTCHA,
        context="c",
        choice="invalidate on write",
        scope=Scope.GLOBAL,
        valid_from=datetime.now(UTC),
        provenance=Provenance(source="agent"),
    )
    s.add_decision(d)  # default status is PROPOSED
    text = top_tier_map(s, None)
    assert "[unratified]" in text


def test_top_tier_map_surfaces_repo_scoped_records_not_only_global_mistakes(tmp_path):
    """Red target: the shipped map, which listed ONLY `Scope.GLOBAL` decisions of a
    mistake kind — so a store holding real memory advertised none of it.

    Measured on the first live cell (airflow x genkovich-sdd, 2026-07-29). S2 captured a
    `proposed` ADR about `DagFileProcessorManager`, scope `repo`. The store was NOT empty.
    But the corpus had no accepted domains, so SessionStart fell back to this renderer, and
    this renderer's two filters (`Scope.GLOBAL` and `kind in _MISTAKE_KINDS`) excluded that
    record — leaving the agent with the standing instruction "call get_task_context before
    any grep" above a table of contents that mentioned nothing it could possibly want:

        ## Communities
        - BaseModel — 1725 entities (community 0)
        - typing.py — 1156 entities (community 1)

    S1 then grepped, scored FAIL, and was RIGHT to: nothing on screen suggested memory knew
    anything about the class it was asked about. The gap between what the store CONTAINS and
    what the entry map SHOWS is widest exactly when a new corpus has captured its first
    decision and named no domains yet.
    """
    s = Store(tmp_path / "t.db")
    d = Decision(
        title="De-prioritize chronically-failing DAG files",
        kind=DecisionKind.ADR,
        context="c",
        choice="demote in-queue",
        scope=Scope.REPO,
        valid_from=datetime.now(UTC),
        provenance=Provenance(source="agent"),
    )
    s._write_decision(d)
    s._conn.commit()
    text = top_tier_map(s, None)
    assert "De-prioritize chronically-failing DAG files" in text


def test_top_tier_map_does_not_repeat_a_record_it_already_listed(tmp_path):
    """A GLOBAL mistake belongs in the mistakes section and must not also appear under the
    general records section. Red target: a second listing built without deduping by id."""
    s = Store(tmp_path / "t.db")
    g = Decision(
        title="never block the event loop",
        kind=DecisionKind.CONSTRAINT,
        context="c",
        choice="use async",
        scope=Scope.GLOBAL,
        valid_from=datetime.now(UTC),
        provenance=Provenance(source="manual"),
    )
    s._write_decision(g)
    s._conn.commit()
    text = top_tier_map(s, None)
    assert text.count("never block the event loop") == 1
