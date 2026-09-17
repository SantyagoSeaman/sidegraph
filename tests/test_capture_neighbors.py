"""Staleness machinery D2: capture-time neighbors — `_live_neighbors` factored out of
`_is_duplicate`'s shared-anchor walk, reported back via `ProposeResult.neighbors` so an
agent proposing a new record can consider superseding an existing one instead of leaving a
fresh, possibly-contradicting record alongside it.
# see design/superpowers/specs/2026-07-30-staleness-machinery-design.md (D2)
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sidegraph.capture import propose
from sidegraph.engine.reader import GraphifyReader
from sidegraph.store import Store

FIXTURE = Path(__file__).parent / "fixtures" / "mini_graph.json"


@pytest.fixture(autouse=True)
def _no_ambient_initiative(monkeypatch):
    monkeypatch.setattr("sidegraph.capture._derive_initiative", lambda: None)


def _draft(**over):
    base = dict(
        title="Use locks in Trader",
        kind="gotcha",
        context="races seen",
        choice="lock around order placement",
        anchors=[{"name": "Trader", "file_path": "trader/exec.py"}],
    )
    base.update(over)
    return base


def test_second_propose_on_shared_anchor_reports_first_as_neighbor(tmp_path):
    """A test that proposes twice on one anchor: the second result's neighbors contain the
    first record exactly once, and never the second (not-yet-written, being-written) record
    itself — the pre-write placement's whole point (design D2, red against a post-write
    placement)."""
    store = Store(tmp_path / "t.db")
    reader = GraphifyReader(FIXTURE)
    first = propose([_draft(kind="lesson", title="first lesson")], store, reader)
    store.ratify(first[0].decision_id)

    second = propose([_draft(kind="adr", title="a different title entirely")], store, reader)
    assert second[0].status == "written"
    neighbor_ids = [n["id"] for n in second[0].neighbors]
    assert neighbor_ids == [first[0].decision_id]


def test_neighbors_deduped_by_id_not_once_per_shared_anchor(tmp_path):
    """A decision bound to TWO of the draft's anchors must appear once in neighbors, not
    twice — dedupe-by-id before the cap (design D2, red against the concatenating design
    B1a rejected)."""
    store = Store(tmp_path / "t.db")
    first = propose(
        [
            _draft(
                kind="lesson",
                title="shared across two anchors",
                anchors=[
                    {"name": "Trader", "file_path": "trader/exec.py"},
                    {"name": "run", "file_path": "m.py"},
                ],
            )
        ],
        store,
        GraphifyReader(_two_anchor_fixture(tmp_path)),
    )
    store.ratify(first[0].decision_id)

    second = propose(
        [
            _draft(
                kind="adr",
                title="new one",
                anchors=[
                    {"name": "Trader", "file_path": "trader/exec.py"},
                    {"name": "run", "file_path": "m.py"},
                ],
            )
        ],
        store,
        GraphifyReader(_two_anchor_fixture(tmp_path)),
    )
    assert second[0].status == "written"
    neighbor_ids = [n["id"] for n in second[0].neighbors]
    assert neighbor_ids == [first[0].decision_id]  # exactly once, not twice


def _two_anchor_fixture(tmp_path) -> Path:
    import json

    data = {
        "built_at_commit": "x",
        "nodes": [
            {
                "id": "trader",
                "label": "Trader",
                "norm_label": "trader",
                "file_type": "code",
                "source_file": "trader/exec.py",
                "community": "1",
            },
            {
                "id": "run",
                "label": "run",
                "norm_label": "run",
                "file_type": "code",
                "source_file": "m.py",
                "community": "2",
            },
        ],
        "links": [],
    }
    p = tmp_path / "two_anchor.json"
    p.write_text(json.dumps(data))
    return p


def test_neighbors_walk_uses_draft_anchors_not_tags_or_domains(tmp_path):
    """A decision reachable only via a shared TAG (never one of the draft's own concrete
    `anchors`) must not surface as a neighbor — the walk is anchors-only (design D2, red
    against the flooding design the cap-3/dedup machinery exists to avoid)."""
    store = Store(tmp_path / "t.db")
    reader = GraphifyReader(FIXTURE)
    first = propose(
        [_draft(kind="lesson", title="tagged one", tags=["security"], anchors=[])],
        store,
        reader,
    )
    store.ratify(first[0].decision_id)

    second = propose([_draft(kind="adr", title="new one", tags=["security"])], store, reader)
    assert second[0].status == "written"
    assert second[0].neighbors == []


def test_zero_anchor_draft_has_empty_neighbors(tmp_path):
    store = Store(tmp_path / "t.db")
    results = propose([_draft(anchors=[])], store, None)
    assert results[0].status == "written"
    assert results[0].neighbors == []


def test_deduped_result_carries_the_dup_as_its_single_neighbor(tmp_path):
    """The dedup early-return means the general neighbors walk never runs on that path —
    yet the dup itself must ride as the ONE neighbor (design D2 -- this replaces rev 1's
    now-unreachable "excluding the exact-dup match" clause)."""
    store = Store(tmp_path / "t.db")
    reader = GraphifyReader(FIXTURE)
    first = propose([_draft()], store, reader)
    store.ratify(first[0].decision_id)

    second = propose([_draft(context="different words")], store, reader)
    assert second[0].status == "deduped"
    assert len(second[0].neighbors) == 1
    assert second[0].neighbors[0]["id"] == first[0].decision_id


def test_neighbors_capped_at_three_newest_first(tmp_path):
    store = Store(tmp_path / "t.db")
    reader = GraphifyReader(FIXTURE)
    written_ids = []
    for i in range(4):
        r = propose([_draft(kind="lesson", title=f"lesson {i}")], store, reader)
        store.ratify(r[0].decision_id)
        written_ids.append(r[0].decision_id)

    fifth = propose([_draft(kind="adr", title="the fifth")], store, reader)
    assert fifth[0].status == "written"
    neighbor_ids = [n["id"] for n in fifth[0].neighbors]
    assert len(neighbor_ids) == 3
    # newest-first (ULID descending) among the 4 live neighbors sharing the anchor
    assert neighbor_ids == sorted(written_ids, reverse=True)[:3]
