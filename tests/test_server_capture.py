import json
from pathlib import Path

from sidegraph.engine.reader import GraphifyReader
from sidegraph.retrieval import TOC_CACHE_KEY
from sidegraph.schema import Descriptor, Domain, DomainStatus, Provenance
from sidegraph.server import (
    _add_decision_impl,
    _add_domain_impl,
    _list_proposed_impl,
    _propose_decisions_impl,
    _propose_domains_impl,
    _ratify_decisions_impl,
    _ratify_impl,
    propose_decisions,
)
from sidegraph.store import Store

FIXTURE = Path(__file__).parent / "fixtures" / "mini_graph.json"

DRAFT = {
    "title": "Lock around order placement",
    "kind": "gotcha",
    "context": "races seen",
    "choice": "serialize the calls",
    "anchors": [{"name": "Trader", "file_path": "trader/exec.py"}],
}


def test_propose_list_ratify_roundtrip(tmp_path):
    store = Store(tmp_path / "t.db")
    reader = GraphifyReader(FIXTURE)
    results = _propose_decisions_impl(store, reader, [DRAFT], session_id="s1")
    assert results[0]["status"] == "written"
    did = results[0]["decision_id"]

    listing = _list_proposed_impl(store)
    assert did in listing and "Lock around order placement" in listing

    out = _ratify_decisions_impl(store, accept=[did])
    assert out[did] == "accepted"
    assert "No proposed decisions" in _list_proposed_impl(store)


def test_list_proposed_docstrings_mention_domains_section():
    """M6 review fold-in: both the testable core and the MCP tool docstring should say
    list_proposed covers domains too, not just decisions (§2/§4 review PINNED I2)."""
    from sidegraph.server import list_proposed

    assert "Domains" in _list_proposed_impl.__doc__
    assert "Domains" in list_proposed.__doc__


def test_ratify_impl_reports_errors_per_id(tmp_path):
    store = Store(tmp_path / "t.db")
    out = _ratify_decisions_impl(store, accept=["missing"], drop=["also-missing"])
    assert out["missing"].startswith("error")
    assert out["also-missing"].startswith("error")


def test_ratify_accept_and_drop_same_id_does_not_clobber(tmp_path):
    store = Store(tmp_path / "t.db")
    results = _propose_decisions_impl(store, None, [dict(DRAFT, anchors=[])])
    did = results[0]["decision_id"]

    out = _ratify_decisions_impl(store, accept=[did], drop=[did])
    assert out[did] == "accepted (drop ignored)"
    assert store.get_decision(did).status.value == "accepted"


def test_retrieve_decisions_excludes_rejected(tmp_path):
    from sidegraph.server import _retrieve_decisions_impl

    store = Store(tmp_path / "t.db")
    results = _propose_decisions_impl(store, None, [dict(DRAFT, anchors=[])])
    did = results[0]["decision_id"]
    _ratify_decisions_impl(store, drop=[did])
    # dropped (rejected) records leave the default listing...
    assert did not in {d["id"] for d in _retrieve_decisions_impl(store)}
    # ...but remain visible when superseded/rejected history is requested
    assert did in {d["id"] for d in _retrieve_decisions_impl(store, include_superseded=True)}


# -- unified ratify: routes ids to decisions OR domains, one gate (mind-model layer M2) ----


def test_ratify_impl_accepts_a_decision(tmp_path):
    store = Store(tmp_path / "t.db")
    results = _propose_decisions_impl(store, None, [dict(DRAFT, anchors=[])])
    did = results[0]["decision_id"]
    out = _ratify_impl(store, accept=[did])
    assert out[did] == "accepted"
    assert store.get_decision(did).status.value == "accepted"


def test_ratify_impl_drops_a_decision(tmp_path):
    store = Store(tmp_path / "t.db")
    results = _propose_decisions_impl(store, None, [dict(DRAFT, anchors=[])])
    did = results[0]["decision_id"]
    out = _ratify_impl(store, drop=[did])
    assert out[did] == "dropped"
    assert store.get_decision(did).status.value == "rejected"


def test_ratify_impl_accepts_a_domain(tmp_path):
    store = Store(tmp_path / "t.db")
    out_domain = _add_domain_impl(store, None, slug="payments", title="Payments", summary="s.")
    domain_id = out_domain["domain_id"]
    out = _ratify_impl(store, accept=[domain_id])
    assert out[domain_id] == "accepted"
    assert store.get_domain(domain_id).status == DomainStatus.ACCEPTED
    assert store.find_abstract_entity("domain:payments") is not None


def test_ratify_impl_drops_a_domain(tmp_path):
    store = Store(tmp_path / "t.db")
    out_domain = _add_domain_impl(store, None, slug="payments", title="Payments", summary="s.")
    domain_id = out_domain["domain_id"]
    out = _ratify_impl(store, drop=[domain_id])
    assert out[domain_id] == "dropped"
    assert store.get_domain(domain_id).status == DomainStatus.DROPPED
    assert store.find_abstract_entity("domain:payments") is None


def test_ratify_impl_mixed_batch_decisions_and_domains(tmp_path):
    store = Store(tmp_path / "t.db")
    dec_results = _propose_decisions_impl(store, None, [dict(DRAFT, anchors=[])])
    did = dec_results[0]["decision_id"]
    dom_out = _add_domain_impl(store, None, slug="payments", title="Payments", summary="s.")
    domain_id = dom_out["domain_id"]

    out = _ratify_impl(store, accept=[did, domain_id])
    assert out[did] == "accepted"
    assert out[domain_id] == "accepted"
    assert store.get_decision(did).status.value == "accepted"
    assert store.get_domain(domain_id).status == DomainStatus.ACCEPTED


def test_ratify_impl_unknown_id_reports_error(tmp_path):
    store = Store(tmp_path / "t.db")
    out = _ratify_impl(store, accept=["totally-unknown-id"])
    assert out["totally-unknown-id"].startswith("error")


def test_ratify_impl_accept_and_drop_same_domain_id_does_not_clobber(tmp_path):
    store = Store(tmp_path / "t.db")
    dom_out = _add_domain_impl(store, None, slug="payments", title="Payments", summary="s.")
    domain_id = dom_out["domain_id"]
    out = _ratify_impl(store, accept=[domain_id], drop=[domain_id])
    assert out[domain_id] == "accepted (drop ignored)"
    assert store.get_domain(domain_id).status == DomainStatus.ACCEPTED


# -- ratify activates the TOC cache immediately (fix: lazy sync alone never rebuilds it,
# it only keeps last_synced current -- "bootstrap -> ratify -> TOC comes alive" needs the
# ratify surface itself to rebuild the cache) -------------------------------------------


def test_ratify_impl_domain_accept_refreshes_toc_cache(tmp_path):
    store = Store(tmp_path / "t.db")
    out_domain = _add_domain_impl(store, None, slug="payments", title="Payments", summary="s.")
    domain_id = out_domain["domain_id"]
    assert store.get_meta(TOC_CACHE_KEY) is None

    _ratify_impl(store, accept=[domain_id])

    cached = json.loads(store.get_meta(TOC_CACHE_KEY))
    assert any(d["slug"] == "payments" for d in cached["domains"])


def test_ratify_impl_domain_drop_refreshes_toc_cache(tmp_path):
    store = Store(tmp_path / "t.db")
    out_domain = _add_domain_impl(store, None, slug="payments", title="Payments", summary="s.")
    domain_id = out_domain["domain_id"]
    assert store.get_meta(TOC_CACHE_KEY) is None

    _ratify_impl(store, drop=[domain_id])

    # dropped, never accepted -> absent from the rebuilt TOC, but the cache IS rebuilt
    cached = json.loads(store.get_meta(TOC_CACHE_KEY))
    assert cached["domains"] == []


def test_ratify_impl_decisions_only_leaves_toc_cache_untouched(tmp_path):
    store = Store(tmp_path / "t.db")
    results = _propose_decisions_impl(store, None, [dict(DRAFT, anchors=[])])
    did = results[0]["decision_id"]
    store.set_meta(TOC_CACHE_KEY, "sentinel")

    _ratify_impl(store, accept=[did])

    assert store.get_meta(TOC_CACHE_KEY) == "sentinel"


def test_ratify_decisions_alias_still_ratifies_decisions(tmp_path):
    """ratify_decisions is now an alias for the unified _ratify_impl — same behavior for
    decision ids (existing callers must not notice a difference)."""
    store = Store(tmp_path / "t.db")
    results = _propose_decisions_impl(store, None, [dict(DRAFT, anchors=[])])
    did = results[0]["decision_id"]
    out = _ratify_impl(store, accept=[did])
    assert out[did] == "accepted"


def test_ratify_decisions_tool_body_also_covers_domains(tmp_path):
    """The MCP `ratify_decisions` tool now dispatches to the same routing impl as `ratify`
    — verified here by calling _ratify_impl directly with a domain id, exactly what the
    tool's body does (fastmcp dispatch itself is exercised by test_server_mcp_smoke.py)."""
    store = Store(tmp_path / "t.db")
    dom_out = _add_domain_impl(store, None, slug="payments", title="Payments", summary="s.")
    domain_id = dom_out["domain_id"]
    out = _ratify_impl(store, accept=[domain_id])
    assert out[domain_id] == "accepted"


# -- add_decision: per-anchor ambiguous feedback (Gate-5 finding S3) ----------------------


def _ambiguous_reader(tmp_path):
    """Two nodes named "run()" sharing a community -- resolve() comes back ambiguous with
    the shared community still known (same shape as test_anchoring's real-reader case)."""
    data = {
        "built_at_commit": "x",
        "nodes": [
            {
                "id": "a",
                "label": "run()",
                "norm_label": "run()",
                "file_type": "code",
                "source_file": "m.py",
                "community": "7",
            },
            {
                "id": "b",
                "label": "run()",
                "norm_label": "run()",
                "file_type": "code",
                "source_file": "m.py",
                "community": "7",
            },
        ],
        "links": [],
    }
    graph_path = tmp_path / "g.json"
    graph_path.write_text(json.dumps(data))
    return GraphifyReader(graph_path)


def test_add_decision_impl_reports_ambiguous_anchor_in_anchors_skipped(tmp_path):
    store = Store(tmp_path / "t.db")
    reader = _ambiguous_reader(tmp_path)

    out = _add_decision_impl(
        store,
        reader,
        "title",
        "adr",
        "ctx",
        "choice",
        anchors=[{"name": "run"}],
    )
    assert out["anchors_skipped"] == [
        {"name": "run", "reason": "ambiguous", "candidates": ["a", "b"]}
    ]
    # no Tier-2 leaf was created for the ambiguous anchor -- only the Tier-1 community binding
    assert out["bindings"] == 1


def test_add_decision_impl_resolved_anchor_unaffected(tmp_path):
    store = Store(tmp_path / "t.db")
    reader = GraphifyReader(FIXTURE)

    out = _add_decision_impl(
        store,
        reader,
        "title",
        "adr",
        "ctx",
        "choice",
        anchors=[{"name": "Trader", "file_path": "trader/exec.py"}],
    )
    assert out["anchors_skipped"] == []
    assert out["bindings"] == 2  # leaf + community, both resolved cleanly


def test_add_decision_impl_ambiguous_candidates_capped_at_five(tmp_path):
    nodes = [
        {
            "id": f"n{i}",
            "label": "run()",
            "norm_label": "run()",
            "file_type": "code",
            "source_file": "m.py",
            "community": "7",
        }
        for i in range(7)
    ]
    graph_path = tmp_path / "g.json"
    graph_path.write_text(json.dumps({"built_at_commit": "x", "nodes": nodes, "links": []}))
    reader = GraphifyReader(graph_path)
    store = Store(tmp_path / "t.db")

    out = _add_decision_impl(
        store,
        reader,
        "title",
        "adr",
        "ctx",
        "choice",
        anchors=[{"name": "run"}],
    )
    assert len(out["anchors_skipped"]) == 1
    assert len(out["anchors_skipped"][0]["candidates"]) == 5


def test_add_decision_impl_no_reader_no_anchors_skipped(tmp_path):
    store = Store(tmp_path / "t.db")
    out = _add_decision_impl(
        store,
        None,
        "title",
        "adr",
        "ctx",
        "choice",
        anchors=[{"name": "run"}],
    )
    assert out["anchors_skipped"] == []


# -- I1 (R1 improvement wave §1): _add_decision_impl gains the D7.3 session_id fallback ----
# Measured defect: the "add" pair (_add_decision_impl/_add_fact_impl) never applied the
# fallback at all, unlike the "propose" pair -- no design rationale on record for the
# asymmetry, and it's what makes the git-bindings join (task #23) trustworthy.


def test_add_decision_impl_falls_back_to_telemetry_session_key_when_fresh(tmp_path):
    from datetime import UTC, datetime

    from sidegraph.config import TELEMETRY_SESSION_KEY

    store = Store(tmp_path / "t.db")
    store.set_meta(TELEMETRY_SESSION_KEY, f"session-xyz|{datetime.now(UTC).isoformat()}")

    out = _add_decision_impl(store, None, "title", "adr", "ctx", "choice")

    assert store.get_decision(out["id"]).provenance.session_id == "session-xyz"


def test_add_decision_impl_explicit_session_id_wins_over_fallback(tmp_path):
    from datetime import UTC, datetime

    from sidegraph.config import TELEMETRY_SESSION_KEY

    store = Store(tmp_path / "t.db")
    store.set_meta(TELEMETRY_SESSION_KEY, f"marker-session|{datetime.now(UTC).isoformat()}")

    out = _add_decision_impl(
        store, None, "title", "adr", "ctx", "choice", session_id="explicit-session"
    )

    assert store.get_decision(out["id"]).provenance.session_id == "explicit-session"


def test_add_decision_impl_ignores_stale_telemetry_session_marker(tmp_path):
    """Window-expired twin (declared exception): passes before AND after."""
    from datetime import UTC, datetime, timedelta

    from sidegraph.config import TELEMETRY_SESSION_KEY

    store = Store(tmp_path / "t.db")
    stale = (datetime.now(UTC) - timedelta(hours=25)).isoformat()
    store.set_meta(TELEMETRY_SESSION_KEY, f"stale-session|{stale}")

    out = _add_decision_impl(store, None, "title", "adr", "ctx", "choice")

    assert store.get_decision(out["id"]).provenance.session_id is None


# -- propose_decisions: tags + layer carry through (recovered FR1.4, §2/§3) ----------------


def test_propose_decisions_tags_and_layer_round_trip(tmp_path):
    store = Store(tmp_path / "t.db")
    results = _propose_decisions_impl(
        store,
        None,
        [
            dict(
                DRAFT,
                anchors=[],
                tags=["Security", "  needs review "],
                layer="business",
            )
        ],
    )
    did = results[0]["decision_id"]
    decision = store.get_decision(did)
    assert decision.layer == "business"

    security = store.find_abstract_entity("tag:security")
    needs_review = store.find_abstract_entity("tag:needs-review")
    assert security is not None and needs_review is not None
    bound_decisions = {d.id for d in store.valid_decisions_for_entity(security.entity_id)}
    assert did in bound_decisions

    # tags survive ratification unchanged (drafts carry through)
    _ratify_decisions_impl(store, accept=[did])
    assert store.get_decision(did).layer == "business"
    assert did in {d.id for d in store.valid_decisions_for_entity(needs_review.entity_id)}


def test_propose_decisions_redacts_tag_text(tmp_path):
    store = Store(tmp_path / "t.db")
    results = _propose_decisions_impl(
        store,
        None,
        [
            dict(
                DRAFT,
                anchors=[],
                tags=["api_key=sk-live-abc123"],
            )
        ],
    )
    assert results[0]["redactions"] >= 1
    did = results[0]["decision_id"]
    bindings = store.bindings_for_record(did)
    tag_names = {store.get_entity(b.entity_id).canonical_name for b in bindings}
    assert not any("sk-live-abc123" in name for name in tag_names)


# -- list_proposed: Domains section parity (M5, M2 review PINNED I2) ---------------------


def test_list_proposed_includes_domains_section(tmp_path):
    store = Store(tmp_path / "t.db")
    out = _add_domain_impl(
        store,
        None,
        slug="payments",
        title="Payments",
        summary="Order settlement.",
    )
    listing = _list_proposed_impl(store)
    assert "Domains:" in listing
    assert out["domain_id"] in listing
    assert "payments" in listing
    assert "Payments" in listing
    assert "Order settlement." in listing


def test_list_proposed_decisions_and_domains_both_sectioned(tmp_path):
    store = Store(tmp_path / "t.db")
    dec = _propose_decisions_impl(store, None, [dict(DRAFT, anchors=[])])
    did = dec[0]["decision_id"]
    dom = _add_domain_impl(store, None, slug="payments", title="Payments", summary="s.")
    domain_id = dom["domain_id"]

    listing = _list_proposed_impl(store)
    assert "Decisions:" in listing and did in listing
    assert "Domains:" in listing and domain_id in listing
    # Decisions section precedes the Domains section (task: "after the decisions listing").
    assert listing.index("Decisions:") < listing.index("Domains:")


def test_list_proposed_decisions_only_has_no_domains_section(tmp_path):
    store = Store(tmp_path / "t.db")
    _propose_decisions_impl(store, None, [dict(DRAFT, anchors=[])])
    listing = _list_proposed_impl(store)
    assert "Domains:" not in listing


def test_list_proposed_empty_message_mentions_domains(tmp_path):
    store = Store(tmp_path / "t.db")
    listing = _list_proposed_impl(store)
    assert "No proposed decisions" in listing
    assert "domains" in listing


# -- Gate-5 fix 3: list_proposed's Domains section shows the membership rule too, not just
# the CLI listing — the same human gate (`ratify`) reads either surface.


def test_list_proposed_domain_line_shows_paths_and_communities(tmp_path):
    store = Store(tmp_path / "t.db")
    store.add_domain(
        Domain(
            slug="payments",
            title="Payments",
            summary="s.",
            communities=["1", "2"],
            path_prefixes=["payments"],
            provenance=Provenance(source="manual"),
        )
    )
    listing = _list_proposed_impl(store)
    assert "paths:" in listing and "payments/" in listing
    assert "communities:" in listing and "1" in listing and "2" in listing


# -- Gate-6 finding: seed_anchors (the name-domains skill's PRIMARY membership shape) is
# invisible at the ratify gate unless the anchors themselves are rendered -- a domain
# authored with ONLY seed_anchors previously showed "paths: (none); communities: (none)"
# with no sign of the rule being approved.


def test_list_proposed_domain_line_shows_seed_anchors_when_paths_and_communities_empty(
    tmp_path,
):
    store = Store(tmp_path / "t.db")
    store.add_domain(
        Domain(
            slug="payments",
            title="Payments",
            summary="s.",
            seed_anchors=[Descriptor(name="OrderBook", file_path="trader/order_book.py")],
            provenance=Provenance(source="manual"),
        )
    )
    listing = _list_proposed_impl(store)
    assert "anchors:" in listing
    assert "OrderBook@trader/order_book.py" in listing


# -- BEH-1 finding: add_decision is the human-asked path (same rationale add_fact/
# supersede_fact already stamp "human" for -- the asking human was the gate), but it was
# stamping provenance.source="agent" -- the value reserved for the agent-initiated
# propose_decisions pipeline. Only propose_decisions (capture.py) may stamp "agent".


def test_add_decision_stamps_human_provenance(tmp_path):
    store = Store(tmp_path / "t.db")
    out = _add_decision_impl(store, None, title="t", kind="adr", context="c", choice="ch")

    assert store.get_decision(out["id"]).provenance.source == "human"


# -- SIDEGRAPH_AUTO_ACCEPT: env plumbing through the propose_decisions tool shell; domains
# are exempt regardless (see design/superpowers/specs/
# 2026-07-10-ratification-ux-and-mcp-gaps-design.md) -------------------------------------


def test_auto_accept_env_resolution(monkeypatch):
    from sidegraph.server import _auto_accept

    monkeypatch.delenv("SIDEGRAPH_AUTO_ACCEPT", raising=False)
    assert _auto_accept() is False
    monkeypatch.setenv("SIDEGRAPH_AUTO_ACCEPT", "1")
    assert _auto_accept() is False  # only "on" enables
    monkeypatch.setenv("SIDEGRAPH_AUTO_ACCEPT", "on")
    assert _auto_accept() is True


def test_propose_decisions_impl_auto_accept_lands_accepted(tmp_path):
    store = Store(tmp_path / "t.db")
    results = _propose_decisions_impl(store, None, [dict(DRAFT, anchors=[])], auto_accept=True)
    did = results[0]["decision_id"]
    assert store.get_decision(did).status.value == "accepted"


def test_propose_domains_ignores_auto_accept(tmp_path, monkeypatch):
    monkeypatch.setenv("SIDEGRAPH_AUTO_ACCEPT", "on")
    store = Store(tmp_path / "srv.db")
    out = _propose_domains_impl(store, None, [{"slug": "risk", "title": "Risk", "summary": "s"}])
    dom = next(iter(store.iter_domains(status=DomainStatus.PROPOSED)), None)
    assert dom is not None and dom.slug == "risk"
    # _propose_domain_one's real status literal on success (capture.py) -- never "ok".
    assert out[0]["status"] == "proposed"


# -- D7.4: deterministic domain lint (design/superpowers/specs/
# 2026-07-30-staleness-machinery-design.md) -------------------------------------------------


def test_propose_domains_warns_on_dead_path_prefix(tmp_path):
    reader = GraphifyReader(FIXTURE)
    store = Store(tmp_path / "t.db")
    out = _propose_domains_impl(
        store,
        reader,
        [{"slug": "ghost", "title": "Ghost", "summary": "s", "path_prefixes": ["nonexistent-dir"]}],
    )
    assert out[0]["status"] == "proposed"
    assert any("dead prefix" in w for w in out[0]["warnings"])


def test_propose_domains_no_dead_prefix_warning_when_prefix_matches_a_real_file(tmp_path):
    reader = GraphifyReader(FIXTURE)
    store = Store(tmp_path / "t.db")
    out = _propose_domains_impl(
        store,
        reader,
        [{"slug": "trading", "title": "Trading", "summary": "s", "path_prefixes": ["trader"]}],
    )
    assert out[0]["status"] == "proposed"
    assert out[0]["warnings"] == []


def test_propose_domains_dead_prefix_check_is_a_noop_without_a_reader(tmp_path):
    """Best-effort: no reader -> nothing to check the dead-prefix half against, so it
    contributes no warning (never a rejection, never a crash)."""
    store = Store(tmp_path / "t.db")
    out = _propose_domains_impl(
        store,
        None,
        [{"slug": "ghost", "title": "Ghost", "summary": "s", "path_prefixes": ["nonexistent-dir"]}],
    )
    assert out[0]["status"] == "proposed"
    assert out[0]["warnings"] == []


def test_propose_domains_warns_when_prefix_subsumes_sibling_seed_anchor(tmp_path):
    """A path_prefix that would swallow another ACCEPTED domain's own seed_anchors file
    (design D7.4's second mechanical half) -- store-only, no reader needed."""
    store = Store(tmp_path / "t.db")
    existing = store.add_domain(
        Domain(
            slug="payments",
            title="Payments",
            summary="Order settlement.",
            seed_anchors=[Descriptor(name="Trader", file_path="trader/exec.py")],
            provenance=Provenance(source="manual"),
        )
    )
    store.ratify_domains(accept=[existing.domain_id])

    out = _propose_domains_impl(
        store,
        None,
        [{"slug": "trading", "title": "Trading", "summary": "s", "path_prefixes": ["trader"]}],
    )
    assert out[0]["status"] == "proposed"
    assert any(
        "subsumes" in w and "payments" in w and "trader/exec.py" in w for w in out[0]["warnings"]
    )


def test_propose_domains_subsume_warning_deduped_per_domain_and_prefix(tmp_path):
    """NIT-3 (code review): a domain with SEVERAL seed anchors all falling under the same
    prefix must warn once per (domain, prefix), not once per matching anchor."""
    store = Store(tmp_path / "t.db")
    existing = store.add_domain(
        Domain(
            slug="payments",
            title="Payments",
            summary="Order settlement.",
            seed_anchors=[
                Descriptor(name="Trader", file_path="trader/exec.py"),
                Descriptor(name="Ledger", file_path="trader/ledger.py"),
            ],
            provenance=Provenance(source="manual"),
        )
    )
    store.ratify_domains(accept=[existing.domain_id])

    out = _propose_domains_impl(
        store,
        None,
        [{"slug": "trading", "title": "Trading", "summary": "s", "path_prefixes": ["trader"]}],
    )
    assert out[0]["status"] == "proposed"
    subsume_warnings = [w for w in out[0]["warnings"] if "subsumes" in w]
    assert len(subsume_warnings) == 1


def test_propose_domains_no_subsume_warning_against_a_proposed_not_accepted_domain(tmp_path):
    """ "Live" = ACCEPTED only -- a merely-PROPOSED sibling domain's seed anchor is not yet
    an addressable domain, so it can't be subsumed."""
    store = Store(tmp_path / "t.db")
    store.add_domain(
        Domain(
            slug="payments",
            title="Payments",
            summary="Order settlement.",
            seed_anchors=[Descriptor(name="Trader", file_path="trader/exec.py")],
            provenance=Provenance(source="manual"),
        )
    )  # never ratified -- stays PROPOSED

    out = _propose_domains_impl(
        store,
        None,
        [{"slug": "trading", "title": "Trading", "summary": "s", "path_prefixes": ["trader"]}],
    )
    assert out[0]["status"] == "proposed"
    assert out[0]["warnings"] == []


def test_propose_domains_no_warnings_when_path_prefixes_empty(tmp_path):
    reader = GraphifyReader(FIXTURE)
    store = Store(tmp_path / "t.db")
    out = _propose_domains_impl(store, reader, [{"slug": "misc", "title": "Misc", "summary": "s"}])
    assert out[0]["status"] == "proposed"
    assert out[0]["warnings"] == []


def test_add_decision_impl_reports_unresolved_anchor_as_orphaned(tmp_path):
    """Red against the shipped write path: an anchor naming something that is NOT in the
    graph is still bound -- as an ``orphaned`` Tier-2 leaf, deliberately (anchoring.py's
    "created when resolved or unresolved") -- but every field of the return said success.
    ``anchors_skipped`` covered only ``ambiguous``, so the ONE outcome that produces memory
    which can never be delivered was the one nothing reported.

    Measured consequence (design/testing/2026-08-03-delivery-gap-remeasure.md follow-up):
    on the airflow corpus 29% of Tier-2 bindings are orphaned-at-birth against 0-4% on
    every other corpus, 11 of 35 decisions are unreachable through any live concrete
    binding, and ``_titles_for_path`` -- which skips orphaned bindings -- therefore falls
    back to the generic PreToolUse nudge on that corpus's most-read files.

    The reported shape mirrors ``add_anchors``'s own ``orphaned`` bucket rather than
    inventing a second vocabulary for the same fact.
    """
    store = Store(tmp_path / "t.db")
    reader = GraphifyReader(FIXTURE)

    out = _add_decision_impl(
        store,
        reader,
        "title",
        "adr",
        "ctx",
        "choice",
        anchors=[{"name": "no_such_symbol_anywhere", "file_path": "trader/exec.py"}],
    )

    assert out["anchors_orphaned"] == [
        {
            "entity_id": out["entities"][0]["entity_id"],
            "canonical_name": "no_such_symbol_anywhere",
            "tier": 2,
            "reason": "name-not-in-file",
        }
    ]
    # ambiguous stays its own outcome: nothing was ambiguous here
    assert out["anchors_skipped"] == []
    # and the binding really was written (orphaned), not dropped -- unchanged behaviour
    assert out["bindings"] == 1


def test_add_decision_impl_resolved_anchor_reports_no_orphans(tmp_path):
    """The other half of the guard: a resolved anchor must not appear in the new bucket,
    or the field would be noise the caller learns to ignore."""
    store = Store(tmp_path / "t.db")
    reader = GraphifyReader(FIXTURE)

    out = _add_decision_impl(
        store,
        reader,
        "title",
        "adr",
        "ctx",
        "choice",
        anchors=[{"name": "Trader", "file_path": "trader/exec.py"}],
    )
    assert out["anchors_orphaned"] == []
    assert out["anchors_skipped"] == []
    assert out["bindings"] == 2


def test_orphaned_anchor_reason_distinguishes_a_graph_that_lacks_the_file(tmp_path):
    """Red against the shipped field: ``anchors_orphaned`` said an anchor was dead but not
    WHY, and the two causes need opposite fixes.

    Measured on the airflow corpus while implementing the file-community fallback: 9 of its
    15 orphans name a file that exists on disk and is simply ABSENT from the graph -- the
    graph was built 2026-07-29, the files were written 2026-07-30. The anchor was right and
    the graph was stale. Only 2 name a file the graph does have. Telling an author to fix
    the name, when the real fix is `graphify update .`, sends them at the wrong problem.
    """
    store = Store(tmp_path / "t.db")
    reader = GraphifyReader(FIXTURE)

    lacks_file = _add_decision_impl(
        store,
        reader,
        "t",
        "adr",
        "c",
        "ch",
        anchors=[{"name": "derive_hint", "file_path": "not/in/the/graph/at_all.py"}],
    )
    assert [e["reason"] for e in lacks_file["anchors_orphaned"]] == ["file-not-in-graph"]

    has_file = _add_decision_impl(
        store,
        reader,
        "t",
        "adr",
        "c",
        "ch",
        anchors=[{"name": "no_such_symbol_anywhere", "file_path": "trader/exec.py"}],
    )
    assert [e["reason"] for e in has_file["anchors_orphaned"]] == ["name-not-in-file"]

    no_path = _add_decision_impl(
        store,
        reader,
        "t",
        "adr",
        "c",
        "ch",
        anchors=[{"name": "Dev Environment & Release Tooling (Breeze)"}],
    )
    assert [e["reason"] for e in no_path["anchors_orphaned"]] == ["no-file-path"]


def test_propose_decisions_docstring_enumerates_relation_literals():
    """I5 (R1 improvement wave §4-bis, review Minor 4): propose_decisions is the
    capture-side tool the R1 relation-guess failure actually used; add_decision already
    enumerates the five relation literals in its own docstring (server.py:371-372). Red
    against the current docstring, which mentions ``anchors``' ``relation`` key but never
    enumerates its valid values."""
    doc = propose_decisions.__doc__ or ""
    for literal in ("creates", "modifies", "affects", "deprecates", "considered"):
        assert literal in doc, f"{literal!r} missing from propose_decisions' docstring"
