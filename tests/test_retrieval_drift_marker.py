"""Drift→supersede wave D3: the `` [drifted]`` marker on detailed-tier decision lines,
the one legend line per rendered surface, and the defensively-parsed, live-filtered
``drifted_record_ids`` cache read.
# see design/superpowers/specs/2026-07-30-drift-supersede-affordance-design.md (D3)
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from sidegraph.retrieval import (
    _DRIFT_LEGEND,
    DRIFT_CACHE_KEY,
    RetrievalBudget,
    TaskContext,
    _fmt_decision,
    drifted_record_ids,
    drill_down,
    rank_decisions,
)
from sidegraph.schema import (
    AnchorBinding,
    Decision,
    DecisionKind,
    DecisionStatus,
    Domain,
    Entity,
    Provenance,
)
from sidegraph.store import Store


@pytest.fixture
def store(tmp_path) -> Store:
    return Store(tmp_path / ".sidegraph")


def _mk_decision(store: Store, **overrides) -> Decision:
    base = dict(
        title="Watch the cache",
        kind=DecisionKind.GOTCHA,
        context="ctx",
        choice="always flush",
        status=DecisionStatus.ACCEPTED,
        valid_from=datetime.now(UTC),
        provenance=Provenance(source="manual"),
    )
    base.update(overrides)
    return store.add_decision(Decision(**base))


def _bind(store: Store, d: Decision, name: str = "Seed") -> Entity:
    e = store.upsert_entity(Entity(canonical_name=name))
    store.add_binding(AnchorBinding(record_id=d.id, entity_id=e.entity_id, tier=2, status="live"))
    return e


def _set_cache(store: Store, by_commit: dict[str, list[str]]) -> None:
    store.set_meta(
        DRIFT_CACHE_KEY,
        json.dumps(
            {"head": "abc123", "computed_at": datetime.now(UTC).isoformat(), "by_commit": by_commit}
        ),
    )


# -- drifted_record_ids ---------------------------------------------------------------------


def test_missing_cache_is_empty(store):
    assert drifted_record_ids(store) == frozenset()


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        json.dumps(["wrong", "shape"]),
        json.dumps({"by_commit": "not-a-dict"}),
        json.dumps({"by_commit": {"c1": "not-a-list"}}),
        json.dumps({"by_commit": {"c1": [42]}}),
    ],
)
def test_malformed_cache_is_empty(store, raw):
    store.set_meta(DRIFT_CACHE_KEY, raw)
    assert drifted_record_ids(store) == frozenset()


def test_cached_live_id_is_returned(store):
    d = _mk_decision(store)
    _set_cache(store, {"c1": [d.id]})
    assert drifted_record_ids(store) == frozenset({d.id})


def test_superseded_id_is_filtered_out(store):
    """An in-session repair supersedes a cached record without any commit — the read-side
    live filter is what keeps markers and hook counts honest (spec D2/D3)."""
    d = _mk_decision(store)
    d2 = _mk_decision(store, title="Successor", supersedes=d.id)
    _set_cache(store, {"c1": [d.id, d2.id]})
    assert drifted_record_ids(store) == frozenset({d2.id})


def test_unknown_id_is_filtered_out(store):
    _set_cache(store, {"c1": ["01UNKNOWNULID0000000000000"]})
    assert drifted_record_ids(store) == frozenset()


# -- the marker in _fmt_decision ------------------------------------------------------------


def test_marker_renders_in_tag_cluster_at_both_tiers():
    d = Decision(
        kind=DecisionKind.GOTCHA,
        status=DecisionStatus.ACCEPTED,
        title="T",
        context="c",
        choice="do X",
        valid_from=datetime.now(UTC),
        provenance=Provenance(source="manual"),
    )
    detailed = _fmt_decision(d, detailed=True, drifted=True)
    tight = _fmt_decision(d, detailed=False, drifted=True)
    assert detailed.startswith("- [gotcha] [drifted] ")
    assert tight.startswith("- [gotcha] [drifted] ")
    assert "[drifted]" not in _fmt_decision(d, detailed=True)


# -- rank_decisions threading ---------------------------------------------------------------


def test_seed_anchored_drifted_decision_carries_marker_and_legend(store):
    d = _mk_decision(store)
    e = _bind(store, d)
    _set_cache(store, {"c1": [d.id]})
    ctx = rank_decisions([e], [], [], store, RetrievalBudget())
    line = next(ln for ln in ctx.mistakes if d.id in ln)
    assert "[drifted]" in line
    assert ctx.drift_shown
    assert _DRIFT_LEGEND in ctx.render()


def test_clean_store_renders_no_marker_no_legend(store):
    d = _mk_decision(store)
    e = _bind(store, d)
    ctx = rank_decisions([e], [], [], store, RetrievalBudget())
    assert not any("[drifted]" in ln for ln in ctx.mistakes)
    assert not ctx.drift_shown
    assert _DRIFT_LEGEND not in ctx.render()


def test_related_tight_tier_never_carries_marker(store):
    """Bucket C/D (related) lines have no id and no supersede consumer — the marker is
    detailed-selected records only (spec D3)."""
    d = _mk_decision(store)
    e = _bind(store, d)
    _set_cache(store, {"c1": [d.id]})
    ctx = rank_decisions([], [e], [], store, RetrievalBudget())
    assert ctx.related
    assert not any("[drifted]" in ln for ln in ctx.related)
    assert not ctx.drift_shown


def test_degraded_line_keeps_marker_in_cluster_position(store):
    """Degrade-before-drop re-renders a detailed-selected line at the tight tier keeping
    its id (design D4) — the marker rides the same ruling, in the cluster, never after
    the id suffix (spec I3)."""
    d = _mk_decision(
        store,
        title="A very long gotcha title about caches",
        context="x" * 400,
        rejected="Negative: " + "y" * 400,
    )
    e = _bind(store, d)
    _set_cache(store, {"c1": [d.id]})
    ctx = rank_decisions([e], [], [], store, RetrievalBudget(memory_chars=200))
    line = next(ln for ln in ctx.mistakes if d.id in ln)
    assert "(context:" not in line  # actually degraded, not just short
    assert "[drifted]" in line
    assert line.index("[drifted]") < line.index("(id:")
    assert ctx.drift_shown


def test_legend_render_appends_before_supersede_hint():
    ctx = TaskContext(mistakes=["- [gotcha] [drifted] X (id: 01A)"], drift_shown=True)
    rendered = ctx.render()
    assert _DRIFT_LEGEND in rendered
    assert rendered.index(_DRIFT_LEGEND) < rendered.index("supersede_decision with its")


# -- drill_down -----------------------------------------------------------------------------


def _accepted_domain_with(store: Store, d: Decision) -> str:
    dom = store.add_domain(
        Domain(
            slug="trading",
            title="Trading",
            summary="All trading logic",
            provenance=Provenance(source="manual"),
        )
    )
    store.ratify_domains(accept=[dom.domain_id])
    entity = store.find_abstract_entity("domain:trading")
    store.add_binding(
        AnchorBinding(record_id=d.id, entity_id=entity.entity_id, tier=1, status="live")
    )
    return "trading"


def test_drill_down_marks_and_returns_legend_key(store):
    d = _mk_decision(store)
    slug = _accepted_domain_with(store, d)
    _set_cache(store, {"c1": [d.id]})
    result = drill_down(slug, store, None)
    assert result["found"]
    assert any("[drifted]" in ln for ln in result["decisions"])
    assert result["legend"] == _DRIFT_LEGEND
    # The no-reader note is untouched by the legend (spec N3: "note" stays owned by it).
    assert result["note"] == "no graph reader available — members omitted"


def test_drill_down_clean_store_has_no_legend_key(store):
    d = _mk_decision(store)
    slug = _accepted_domain_with(store, d)
    result = drill_down(slug, store, None)
    assert result["found"]
    assert "legend" not in result
    assert not any("[drifted]" in ln for ln in result["decisions"])
