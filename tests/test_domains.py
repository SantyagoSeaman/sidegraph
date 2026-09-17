"""``bootstrap_domains`` — path 1 of Domain authoring (see
docs/concepts/mind-model.md#domain-lifecycle)."""

from __future__ import annotations

import json

import sidegraph.domains as domains_mod
import sidegraph.sync as sync_mod
from sidegraph.capture import RatifyPolicy
from sidegraph.domains import bootstrap_domains, collect_domain_candidates
from sidegraph.engine.reader import GraphifyReader
from sidegraph.schema import Descriptor, Domain, DomainStatus, Provenance
from sidegraph.store import Store
from sidegraph.sync import refresh_domain_communities_now

# Three communities: "10" (size 6, labeled), "20" (size 5, no label -> god-node fallback,
# god node = MainWidget via degree 3), "30" (size 2, below the default min_members=5
# threshold — never proposed regardless of label/paths).
GRAPH = {
    "built_at_commit": "v1",
    "nodes": [
        {
            "id": "n1",
            "label": "Alpha",
            "norm_label": "alpha",
            "file_type": "code",
            "source_file": "domainA/alpha.py",
            "community": 10,
        },
        {
            "id": "n2",
            "label": "Beta",
            "norm_label": "beta",
            "file_type": "code",
            "source_file": "domainA/beta.py",
            "community": 10,
        },
        {
            "id": "n3",
            "label": "Gamma",
            "norm_label": "gamma",
            "file_type": "code",
            "source_file": "domainA/gamma.py",
            "community": 10,
        },
        {
            "id": "n4",
            "label": "Delta",
            "norm_label": "delta",
            "file_type": "code",
            "source_file": "domainA/delta.py",
            "community": 10,
        },
        {
            "id": "n5",
            "label": "Epsilon",
            "norm_label": "epsilon",
            "file_type": "code",
            "source_file": "domainA/epsilon.py",
            "community": 10,
        },
        {
            "id": "n6",
            "label": "Zeta",
            "norm_label": "zeta",
            "file_type": "code",
            "source_file": "domainA/zeta.py",
            "community": 10,
        },
        {
            "id": "m1",
            "label": "MainWidget",
            "norm_label": "mainwidget",
            "file_type": "code",
            "source_file": "domainB/main.py",
            "community": 20,
        },
        {
            "id": "m2",
            "label": "helper_fn",
            "norm_label": "helper_fn",
            "file_type": "code",
            "source_file": "domainB/helper.py",
            "community": 20,
        },
        {
            "id": "m3",
            "label": "util_fn",
            "norm_label": "util_fn",
            "file_type": "code",
            "source_file": "domainC/util.py",
            "community": 20,
        },
        {
            "id": "m4",
            "label": "other_fn",
            "norm_label": "other_fn",
            "file_type": "code",
            "source_file": "domainC/other.py",
            "community": 20,
        },
        {
            "id": "m5",
            "label": "misc_fn",
            "norm_label": "misc_fn",
            "file_type": "code",
            "source_file": "domainB/misc.py",
            "community": 20,
        },
        {
            "id": "x1",
            "label": "Tiny",
            "norm_label": "tiny",
            "file_type": "code",
            "source_file": "domainD/tiny.py",
            "community": 30,
        },
        {
            "id": "x2",
            "label": "Tiny2",
            "norm_label": "tiny2",
            "file_type": "code",
            "source_file": "domainD/tiny2.py",
            "community": 30,
        },
    ],
    "links": [
        {"relation": "calls", "source": "m1", "target": "m2"},
        {"relation": "calls", "source": "m1", "target": "m3"},
        {"relation": "calls", "source": "m1", "target": "m4"},
    ],
}

LABELS = {"10": "Alpha Domain"}


def _write(tmp_path, name, data):
    p = tmp_path / name
    p.write_text(json.dumps(data))
    return p


def _reader(tmp_path, graph=None, labels=None):
    graph = GRAPH if graph is None else graph
    _write(tmp_path, "graph.json", graph)
    if labels is not None:
        _write(tmp_path, ".graphify_labels.json", labels)
    return GraphifyReader(tmp_path / "graph.json")


# -- collector/bootstrap parity (Task 1 refactor: collect_domain_candidates is the shared
# selection body bootstrap_domains and the read-only list_domain_candidates tool both call —
# this characterization test is the regression net proving the extraction changed nothing
# about what bootstrap itself would have proposed) -------------------------------------


def test_collector_matches_bootstrap_selection(tmp_path):
    reader = _reader(tmp_path, labels=LABELS)
    store = Store(tmp_path / "t.db")

    report = bootstrap_domains(store, reader, dry_run=True)
    candidates, stats = collect_domain_candidates(store, reader)

    assert [c["community_id"] for c in report.dry_run] == [c.community_id for c in candidates]
    assert [c["slug"] for c in report.dry_run] == [c.suggested_slug for c in candidates]
    assert [c["title"] for c in report.dry_run] == [c.suggested_title for c in candidates]
    assert [c["path_prefixes"] for c in report.dry_run] == [c.path_prefixes for c in candidates]
    assert [c["summary"] for c in report.dry_run] == [c.summary for c in candidates]

    assert stats.total == len(candidates) == report.proposed
    assert stats.below_threshold == report.below_threshold
    assert stats.filtered == report.filtered
    assert stats.already_claimed == report.skipped_existing


def test_bootstrap_proposes_only_communities_at_or_above_threshold(tmp_path):
    reader = _reader(tmp_path, labels=LABELS)
    store = Store(tmp_path / "t.db")

    report = bootstrap_domains(store, reader)

    assert report.proposed == 2
    assert report.below_threshold == 1
    assert report.skipped_existing == 0

    domains = {d.slug: d for d in store.iter_domains()}
    assert set(domains) == {"alpha-domain", "mainwidget"}
    for d in domains.values():
        assert d.status == DomainStatus.PROPOSED
        assert d.provenance.source == "bootstrap"
        assert d.provenance.graph_version is not None


def test_bootstrap_labeled_community_uses_label_for_title_and_summary(tmp_path):
    reader = _reader(tmp_path, labels=LABELS)
    store = Store(tmp_path / "t.db")
    bootstrap_domains(store, reader)

    d = store.find_domain_by_slug("alpha-domain")
    assert d is not None
    assert d.title == "Alpha Domain"
    assert d.summary == "Alpha Domain"
    assert d.communities == ["10"]
    assert d.path_prefixes == ["domainA"]  # 6/6 members share domainA


def test_bootstrap_unlabeled_community_falls_back_to_god_node(tmp_path):
    reader = _reader(tmp_path, labels=LABELS)
    store = Store(tmp_path / "t.db")
    bootstrap_domains(store, reader)

    d = store.find_domain_by_slug("mainwidget")
    assert d is not None
    assert d.title == "MainWidget"
    assert "5 entities incl. MainWidget, helper_fn, util_fn" in d.summary
    assert "domainB/main.py" in d.summary
    assert d.communities == ["20"]
    assert d.path_prefixes == []  # 3/5 domainB, 2/5 domainC -> 60% < 80%


def test_bootstrap_no_label_file_present_falls_back_for_all(tmp_path):
    reader = _reader(tmp_path, labels=None)  # no .graphify_labels.json at all
    store = Store(tmp_path / "t.db")
    report = bootstrap_domains(store, reader)

    assert report.proposed == 2
    d = store.find_domain_by_slug("alpha")  # god node fallback: no members have higher
    assert d is not None
    assert "6 entities incl." in d.summary


def test_bootstrap_dry_run_writes_nothing(tmp_path):
    reader = _reader(tmp_path, labels=LABELS)
    store = Store(tmp_path / "t.db")

    report = bootstrap_domains(store, reader, dry_run=True)

    assert report.proposed == 2
    assert len(report.dry_run) == 2
    assert list(store.iter_domains()) == []


def test_bootstrap_idempotent_rerun_skips_everything(tmp_path):
    reader = _reader(tmp_path, labels=LABELS)
    store = Store(tmp_path / "t.db")
    bootstrap_domains(store, reader)

    report = bootstrap_domains(store, reader)
    assert report.proposed == 0
    assert report.skipped_existing == 2
    assert len({d.slug for d in store.iter_domains()}) == 2  # no duplicates written


def test_bootstrap_limit_caps_communities_considered(tmp_path):
    reader = _reader(tmp_path, labels=LABELS)
    store = Store(tmp_path / "t.db")

    report = bootstrap_domains(store, reader, limit=1)

    assert report.proposed == 1
    # community "10" sorts before "20" -> the labeled one wins under a limit of 1
    assert {d.slug for d in store.iter_domains()} == {"alpha-domain"}


# -- total_before_limit (BUG B/C: scale-aware default limit + truncation signaling) -------


def _many_communities_graph(n, start=0):
    """``n`` distinct significant (5-member) communities, no labels, no links -- a
    large-scale candidate-count fixture standing in for a monorepo-scale corpus (real
    finding: Airflow returns 2,578 candidates at default settings)."""
    nodes = [
        {
            "id": f"c{c}n{i}",
            "label": f"Thing{c}_{i}",
            "norm_label": f"thing{c}_{i}",
            "file_type": "code",
            "source_file": f"area{c}/f{i}.py",
            "community": c,
        }
        for c in range(start, start + n)
        for i in range(5)
    ]
    return {"built_at_commit": "v1", "nodes": nodes, "links": []}


def test_collect_domain_candidates_total_before_limit_survives_truncation(tmp_path):
    reader = _reader(tmp_path, graph=_many_communities_graph(150))
    store = Store(tmp_path / "t.db")

    candidates, stats = collect_domain_candidates(store, reader, limit=100)

    assert len(candidates) == 100
    assert stats.total == 100
    # the TRUE significant-community count, independent of the limit that truncated it --
    # what list_domain_candidates' `truncated`/`total_significant` signaling is built on.
    assert stats.total_before_limit == 150


def test_collect_domain_candidates_total_before_limit_matches_when_unlimited(tmp_path):
    reader = _reader(tmp_path, graph=_many_communities_graph(150))
    store = Store(tmp_path / "t.db")

    candidates, stats = collect_domain_candidates(store, reader)  # limit=None -> unlimited

    assert len(candidates) == 150
    assert stats.total == stats.total_before_limit == 150


def test_bootstrap_domains_reports_total_before_limit(tmp_path):
    reader = _reader(tmp_path, graph=_many_communities_graph(150))
    store = Store(tmp_path / "t.db")

    report = bootstrap_domains(store, reader, limit=100)

    assert report.proposed == 100
    assert report.total_before_limit == 150


def test_bootstrap_paths_filters_to_matching_communities(tmp_path):
    reader = _reader(tmp_path, labels=LABELS)
    store = Store(tmp_path / "t.db")

    report = bootstrap_domains(store, reader, paths=["domainB"])

    assert report.proposed == 1
    assert report.filtered == 1
    assert {d.slug for d in store.iter_domains()} == {"mainwidget"}


# -- fix: boundary-safe path-prefix filtering (probe: "payments" used to loosely match
# "payments_v2/..." via a bare str.startswith, a sibling directory that merely shares a
# text prefix, not a true subdirectory) -------------------------------------------------


def test_bootstrap_paths_filter_excludes_sibling_prefix(tmp_path):
    graph = {
        "built_at_commit": "v1",
        "nodes": [
            {
                "id": f"p{i}",
                "label": f"fn_{i}",
                "norm_label": f"fn_{i}",
                "file_type": "code",
                "source_file": f"payments_v2/f{i}.py",
                "community": 1,
            }
            for i in range(5)
        ],
        "links": [],
    }
    reader = _reader(tmp_path, graph=graph)
    store = Store(tmp_path / "t.db")

    report = bootstrap_domains(store, reader, paths=["payments"])

    assert report.proposed == 0
    assert report.filtered == 1
    assert list(store.iter_domains()) == []


def test_bootstrap_paths_filter_matches_true_subdirectory(tmp_path):
    graph = {
        "built_at_commit": "v1",
        "nodes": [
            {
                "id": f"p{i}",
                "label": f"fn_{i}",
                "norm_label": f"fn_{i}",
                "file_type": "code",
                "source_file": f"payments/f{i}.py",
                "community": 1,
            }
            for i in range(5)
        ],
        "links": [],
    }
    reader = _reader(tmp_path, graph=graph)
    store = Store(tmp_path / "t.db")

    report = bootstrap_domains(store, reader, paths=["payments"])

    assert report.proposed == 1
    assert report.filtered == 0


def test_bootstrap_skips_community_already_claimed_by_accepted_domain(tmp_path):
    reader = _reader(tmp_path, labels=LABELS)
    store = Store(tmp_path / "t.db")
    pre = store.add_domain(
        Domain(
            slug="widget-area",
            title="Widget Area",
            summary="Manually curated area.",
            communities=["20"],
            provenance=Provenance(source="manual"),
        )
    )
    store.ratify_domains(accept=[pre.domain_id])

    report = bootstrap_domains(store, reader)

    assert report.proposed == 1
    assert report.skipped_existing == 1
    slugs = {d.slug for d in store.iter_domains(status=DomainStatus.PROPOSED)}
    assert slugs == {"alpha-domain"}


def test_bootstrap_slug_collision_within_run_gets_disambiguated(tmp_path):
    """Two communities that both resolve to the same base slug (e.g. two god nodes
    coincidentally sharing a name) must not collide — the second gets a -2 suffix rather
    than failing store.add_domain's live-slug-uniqueness check."""
    graph = {
        "built_at_commit": "v1",
        "nodes": [
            {
                "id": f"a{i}",
                "label": "Widget" if i == 0 else f"a_helper_{i}",
                "norm_label": "widget",
                "file_type": "code",
                "source_file": f"grpA/f{i}.py",
                "community": 1,
            }
            for i in range(5)
        ]
        + [
            {
                "id": f"b{i}",
                "label": "Widget" if i == 0 else f"b_helper_{i}",
                "norm_label": "widget",
                "file_type": "code",
                "source_file": f"grpB/f{i}.py",
                "community": 2,
            }
            for i in range(5)
        ],
        "links": [],
    }
    reader = _reader(tmp_path, graph=graph)
    store = Store(tmp_path / "t.db")

    report = bootstrap_domains(store, reader)

    assert report.proposed == 2
    slugs = {d.slug for d in store.iter_domains()}
    assert slugs == {"widget", "widget-2"}


# -- fix: redact before slugify (probe: secret material leaked into the repo-committed
# slug, and thus the `domain:<slug>` entity name, because the slug was derived from the
# RAW label/god-node name — title/summary were already redacted, slug was not) ----------


def test_bootstrap_label_with_secret_is_redacted_before_slug_and_title(tmp_path):
    secret = "sk-live-abcdefghij1234567890"
    labels = {"10": f"Auth (api_key={secret})"}
    reader = _reader(tmp_path, labels=labels)
    store = Store(tmp_path / "t.db")

    bootstrap_domains(store, reader)

    d = next(d for d in store.iter_domains() if d.communities == ["10"])
    assert secret not in d.slug
    assert secret not in d.title
    assert secret not in d.summary
    assert "[REDACTED]" in d.title
    assert "[REDACTED]" in d.summary


def test_bootstrap_god_node_name_with_secret_is_redacted_before_slug_and_title(tmp_path):
    """Same probe, but via the god-node-name fallback (unlabeled community) rather than
    an engine label — the agent/engine text feeding the slug is untrusted either way."""
    secret = "sk-live-zzzzzzzzzzzzzzzzzzzz"
    graph = {
        "built_at_commit": "v1",
        "nodes": [
            {
                "id": "g0",
                "label": f"Loader (api_key={secret})",
                "norm_label": "loader",
                "file_type": "code",
                "source_file": "svc/loader.py",
                "community": 99,
            },
        ]
        + [
            {
                "id": f"g{i}",
                "label": f"helper_{i}",
                "norm_label": f"helper_{i}",
                "file_type": "code",
                "source_file": "svc/helper.py",
                "community": 99,
            }
            for i in range(1, 5)
        ],
        "links": [{"relation": "calls", "source": "g0", "target": f"g{i}"} for i in range(1, 5)],
    }
    reader = _reader(tmp_path, graph=graph, labels=None)
    store = Store(tmp_path / "t.db")

    bootstrap_domains(store, reader)

    d = next(d for d in store.iter_domains() if d.communities == ["99"])
    assert secret not in d.slug
    assert secret not in d.title
    assert secret not in d.summary


# -- fix: whitespace-only label/god-name no longer crashes mid-run ----------------------


def test_bootstrap_whitespace_label_falls_back_to_god_node_title(tmp_path):
    """A label that's present but whitespace-only is truthy in Python, so it used to
    survive `label or god_name or base_name` and reach `Domain(title=...)`, which raises
    on blank text — mid-run, after any earlier proposals in the batch already committed.
    It must instead behave exactly like "no label": fall back to the god-node title."""
    labels = {"10": "   "}
    reader = _reader(tmp_path, labels=labels)
    store = Store(tmp_path / "t.db")

    report = bootstrap_domains(store, reader)

    assert report.proposed == 2  # run completes despite the blank label
    d = store.find_domain_by_slug("alpha")  # god-node fallback, same as the no-label case
    assert d is not None
    assert d.title == "Alpha"
    assert "6 entities incl." in d.summary  # member-digest fallback, since label is blank


def test_bootstrap_whitespace_god_node_name_falls_back_to_digest(tmp_path):
    """Belt-and-braces: even when the god node's own name is whitespace-only (no label at
    all here either), the run must complete — title falls back to the deterministic
    `community-<id>` name, summary to the member digest."""
    graph = {
        "built_at_commit": "v1",
        "nodes": [
            {
                "id": "w0",
                "label": "   ",
                "norm_label": "w0",
                "file_type": "code",
                "source_file": "svc/w0.py",
                "community": 77,
            },
        ]
        + [
            {
                "id": f"w{i}",
                "label": f"helper_{i}",
                "norm_label": f"helper_{i}",
                "file_type": "code",
                "source_file": "svc/w.py",
                "community": 77,
            }
            for i in range(1, 5)
        ],
        "links": [{"relation": "calls", "source": "w0", "target": f"w{i}"} for i in range(1, 5)],
    }
    reader = _reader(tmp_path, graph=graph, labels=None)
    store = Store(tmp_path / "t.db")

    report = bootstrap_domains(store, reader)

    assert report.proposed == 1  # run completes despite the blank god-node name
    d = store.find_domain_by_slug("community-77")
    assert d is not None
    assert d.title == "community-77"
    assert "entities incl." in d.summary  # member-digest fallback


# -- fix: derivation guard (Gate-5 blocker) — a winning >=80%-majority top-level directory
# is still discarded back to path_prefixes=[] when it's a well-known shared/infra dir name
# (guard a), or already reaches too many of the graph's OTHER communities (guard b). See
# domains._SHARED_DIR_NAMES / domains._PREFIX_BREADTH_CAP and
# docs/guides/naming-your-domains.md. --------------------------------------------------


def test_bootstrap_skips_deriving_prefix_for_well_known_shared_dir(tmp_path):
    """A community that clusters with its own tests/ (8/9 ~= 89% of its anchorable
    members) must NOT get "tests" derived as its path_prefixes stabilizer — the exact
    Gate-5 shape ("≥80% of its members were test files", true of any god-node community
    that ships tests alongside its source)."""
    graph = {
        "built_at_commit": "v1",
        "nodes": [
            {
                "id": "eb0",
                "label": "ExchangeBackend",
                "norm_label": "exchangebackend",
                "file_type": "code",
                "source_file": "src/exchange_backend.py",
                "community": 1,
            },
        ]
        + [
            {
                "id": f"eb{i}",
                "label": f"test_exchange_{i}",
                "norm_label": f"test_exchange_{i}",
                "file_type": "code",
                "source_file": f"tests/test_exchange_{i}.py",
                "community": 1,
            }
            for i in range(1, 9)
        ],
        "links": [{"relation": "calls", "source": "eb0", "target": f"eb{i}"} for i in range(1, 9)],
    }
    reader = _reader(tmp_path, graph=graph, labels=None)
    store = Store(tmp_path / "t.db")

    bootstrap_domains(store, reader)

    d = store.find_domain_by_slug("exchangebackend")
    assert d is not None
    assert d.communities == ["1"]
    assert d.path_prefixes == []  # 8/9 share "tests" — but it's a well-known shared dir


def test_bootstrap_skips_deriving_prefix_when_shared_with_over_20pct_of_other_communities(tmp_path):
    """A candidate prefix outside the well-known-shared-dir list is still skipped when it
    ALSO shows up in more than 20% of the graph's OTHER communities — evidence of a
    cross-cutting directory pattern (vendored deps, generated code, ...), not this
    community's own home, even though it's also this community's own majority dir."""
    nodes = [
        {
            "id": f"v{i}",
            "label": f"VendorLib{i}",
            "norm_label": f"vendorlib{i}",
            "file_type": "code",
            "source_file": f"vendor/v{i}.py",
            "community": 1,
        }
        for i in range(5)
    ] + [
        # communities "2" and "3" also ship a vendor/ file of their own -> "vendor" reaches
        # 2 of the 5 TOTAL communities (40% > 20%; the guard's denominator is ALL
        # communities in the graph, not just the "other" ones).
        {
            "id": "c2v",
            "label": "C2Vendor",
            "norm_label": "c2vendor",
            "file_type": "code",
            "source_file": "vendor/extra2.py",
            "community": 2,
        },
        {
            "id": "c3v",
            "label": "C3Vendor",
            "norm_label": "c3vendor",
            "file_type": "code",
            "source_file": "vendor/extra3.py",
            "community": 3,
        },
        {
            "id": "c4",
            "label": "C4",
            "norm_label": "c4",
            "file_type": "code",
            "source_file": "domain4/f.py",
            "community": 4,
        },
        {
            "id": "c5",
            "label": "C5",
            "norm_label": "c5",
            "file_type": "code",
            "source_file": "domain5/f.py",
            "community": 5,
        },
    ]
    graph = {"built_at_commit": "v1", "nodes": nodes, "links": []}
    reader = _reader(tmp_path, graph=graph, labels=None)
    store = Store(tmp_path / "t.db")

    bootstrap_domains(store, reader, min_members=1)

    d = store.find_domain_by_slug("vendorlib0")
    assert d is not None
    assert d.communities == ["1"]
    assert d.path_prefixes == []  # "vendor" reaches 2/5 total communities (40%) -> skipped


def test_bootstrap_derives_prefix_unique_to_its_own_community(tmp_path):
    """Control: a majority directory that does NOT reach any other community (and isn't a
    well-known shared name) is still derived normally — the breadth guard must not punish
    a domain's genuinely own home directory just for existing in a graph with other
    communities."""
    nodes = [
        {
            "id": f"v{i}",
            "label": f"VendorLib{i}",
            "norm_label": f"vendorlib{i}",
            "file_type": "code",
            "source_file": f"vendor/v{i}.py",
            "community": 1,
        }
        for i in range(5)
    ] + [
        {
            "id": "c4",
            "label": "C4",
            "norm_label": "c4",
            "file_type": "code",
            "source_file": "domain4/f.py",
            "community": 4,
        },
        {
            "id": "c5",
            "label": "C5",
            "norm_label": "c5",
            "file_type": "code",
            "source_file": "domain5/f.py",
            "community": 5,
        },
    ]
    graph = {"built_at_commit": "v1", "nodes": nodes, "links": []}
    reader = _reader(tmp_path, graph=graph, labels=None)
    store = Store(tmp_path / "t.db")

    bootstrap_domains(store, reader, min_members=1)

    d = store.find_domain_by_slug("vendorlib0")
    assert d is not None
    assert d.path_prefixes == ["vendor"]


# -- fix: derive's breadth guard aligned with sync's refresh-time claim cap (Option A,
# owner-approved 2026-09-14) -- the two guards used to count DIFFERENT numerators over the
# SAME 0.2 figure: derive excluded the candidate's own community from "other communities
# sharing the dir", refresh counts the full claimed set with the own community INCLUDED.
# The gap let bootstrap derive a stabilizer that the very next refresh rejected as overbroad
# -- "path rule too broad" reported to a human about a rule bootstrap itself wrote. See
# tests/test_ratify_policy.py::test_bootstrap_never_derives_a_prefix_the_refresh_cap_would_
# reject (Ruling HH) for the instance the round-0 fix removed. -------------------------
#
# Fix round 1 (review) found the round-0 fix incomplete on two fronts:
# - Major 1: derive's OWN numerator (``_prefix_community_breadth``) skipped any
#   ``file_path`` with fewer than two path segments, so a node whose path was the bare
#   directory name ("trader") or that name plus a trailing slash ("trader/") was
#   invisible to derive but still visible to refresh's ``matches_path_prefix`` -- refresh
#   could still cap a rule derive never saw coming. Fixed by indexing
#   ``_prefix_community_breadth`` with the SAME single-segment boundary rule
#   ``matches_path_prefix`` uses.
# - Major 2: the invariant test below ran its loop ZERO times against the round-0 fix
#   (nothing was derived in its 10-community fixture), so its own assertion never
#   executed -- guarding nothing. Fixed by adding an exclusive ``risk/`` community so the
#   loop is provably non-vacuous, plus a direct ``checked >= 1`` assertion so a future
#   fixture/``min_members`` change can't quietly empty it again.
# Both guarantees are also now scoped honestly: "never" holds against the graph a rule was
# derived from, not across a later rebuild -- a directory one community owns exclusively
# today can be shared by five tomorrow, and refresh capping the rule then is the design
# working as intended. ------------------------------------------------------------------


def _assert_bootstrap_never_derives_a_rule_the_refresh_cap_rejects(
    tmp_path, nodes, *, min_members: int = 5
) -> int:
    """Shared assertion for the invariant this whole section pins: bootstrap the given
    graph, then for EVERY domain it produces with a non-empty ``path_prefixes``, assert an
    immediate refresh over the SAME reader never reports it overbroad. Returns the number
    of domains actually checked so a caller can assert non-vacuity (fix round 1, Major 2
    -- a fixture that quietly derives nothing guards nothing, and a bare `for` loop over
    zero domains does not fail on its own)."""
    graph = {"built_at_commit": "v1", "nodes": nodes, "links": []}
    reader = _reader(tmp_path, graph=graph, labels=None)
    store = Store(tmp_path / "t.db")
    bootstrap_domains(store, reader, min_members=min_members)
    checked = 0
    for domain in store.iter_domains():
        if not domain.path_prefixes:
            continue
        checked += 1
        _communities, overbroad = refresh_domain_communities_now(domain, store, reader)
        assert overbroad is None, f"{domain.slug}: derived rule tripped refresh cap: {overbroad}"
    return checked


def test_bootstrap_derived_prefixes_never_trip_the_refresh_claim_cap(tmp_path):
    """Invariant, not one arithmetic instance: for EVERY domain bootstrap proposes with a
    non-empty ``path_prefixes``, an immediate refresh over the SAME reader must never
    report that domain overbroad -- a rule bootstrap derives can never trip
    ``sync._DOMAIN_CLAIM_CAP`` AGAINST THE GRAPH IT WAS DERIVED FROM (fix round 1: not a
    claim about a LATER rebuild -- a directory one community owns exclusively today can be
    shared by five tomorrow, and refresh capping the rule then is the design working, not
    this invariant failing).

    Eleven communities: the original ten ("trader/" is c1's own home dir, 5 members, also
    shared by c2/c3, 1 member each; 7 single-member filler communities) PLUS a ``risk``
    community (5 members, its own exclusive ``risk/``) -- fix round 1, Major 2: the
    original 10-community fixture ALONE derives nothing post-fix (c1's claimed set
    ``{c1,c2,c3}`` = 3/11 > 0.2, the same veto as before), so the loop below ran ZERO
    times against that fixture and the assertion inside it never executed -- confirmed by
    running it (``checked == 0``, red against the ``assert checked >= 1`` below). ``risk``
    derives ``["risk"]`` (claimed ``{risk}`` = 1, the single-community floor exempts it
    from the cap regardless of ratio) and refresh never caps a 1-community claim either --
    the loop now runs once, non-vacuously, and green."""
    nodes = (
        [
            {
                "id": f"c1n{i}",
                "label": f"C1N{i}",
                "norm_label": f"c1n{i}",
                "file_type": "code",
                "source_file": f"trader/c1n{i}.py",
                "community": "c1",
            }
            for i in range(5)
        ]
        + [
            {
                "id": "c2n0",
                "label": "C2N0",
                "norm_label": "c2n0",
                "file_type": "code",
                "source_file": "trader/c2n0.py",
                "community": "c2",
            },
            {
                "id": "c3n0",
                "label": "C3N0",
                "norm_label": "c3n0",
                "file_type": "code",
                "source_file": "trader/c3n0.py",
                "community": "c3",
            },
        ]
        + [
            {
                "id": f"filler{i}",
                "label": f"Filler{i}",
                "norm_label": f"filler{i}",
                "file_type": "code",
                "source_file": f"misc/filler{i}.py",
                "community": f"filler-{i}",
            }
            for i in range(7)
        ]
        + [
            {
                "id": f"riskn{i}",
                "label": f"RiskN{i}",
                "norm_label": f"riskn{i}",
                "file_type": "code",
                "source_file": f"risk/riskn{i}.py",
                "community": "risk",
            }
            for i in range(5)
        ]
    )
    checked = _assert_bootstrap_never_derives_a_rule_the_refresh_cap_rejects(tmp_path, nodes)
    assert checked >= 1  # non-vacuity (fix round 1, Major 2): the loop above must have run


def test_bootstrap_floor_does_not_exempt_a_two_community_claim_from_the_cap(tmp_path):
    """Pins the single-community floor's EXACT threshold, and how close derive's ratio
    sits to the cap (fix round 1, Major 2 + fix round 2, NEW Minor A): the review's
    in-memory mutant F5 (the floor's ``len(claimed) > 1`` widened to ``> 2``) survived all
    294 bootstrap tests, because none of them exercised a claim of EXACTLY 2 communities
    that the cap should still reject. Round 1's fix used 6 total communities (2/6 = 0.33)
    -- comfortably clear of the 0.2 boundary, which left two MORE mutants unpinned: F7
    (the cap raised to 0.25) and F10 (the denominator off by one) both also passed all
    298 bootstrap tests, since nothing sat close enough to 0.2 to notice either drifting.
    Nine communities instead: "c1" (5 members, home dir "trader/") and "c2" (1 member,
    also under "trader/") -- claimed ``{c1, c2}`` = 2/9 = 0.222, just OVER 0.2 -- plus 7
    single-member filler communities padding the total to 9 (the review asked for a claim
    of exactly 2 over AT MOST 9 communities; 9 is the boundary itself). Correct code
    (floor ``> 1``, cap ``0.2``, denominator ``total_communities``) rejects: c1 derives
    ``path_prefixes == []``. F5, F7 and F10 each independently let this exact claim
    through instead -- see the fix-round-2 report for the mutant re-run confirming all
    three die on this fixture."""
    nodes = (
        [
            {
                "id": f"c1n{i}",
                "label": f"C1N{i}",
                "norm_label": f"c1n{i}",
                "file_type": "code",
                "source_file": f"trader/c1n{i}.py",
                "community": "c1",
            }
            for i in range(5)
        ]
        + [
            {
                "id": "c2n0",
                "label": "C2N0",
                "norm_label": "c2n0",
                "file_type": "code",
                "source_file": "trader/c2n0.py",
                "community": "c2",
            }
        ]
        + [
            {
                "id": f"filler{i}",
                "label": f"Filler{i}",
                "norm_label": f"filler{i}",
                "file_type": "code",
                "source_file": f"misc/filler{i}.py",
                "community": f"filler-{i}",
            }
            for i in range(7)
        ]
    )
    graph = {"built_at_commit": "v1", "nodes": nodes, "links": []}
    reader = _reader(tmp_path, graph=graph, labels=None)
    store = Store(tmp_path / "t.db")

    bootstrap_domains(store, reader, min_members=5)

    d = next(dm for dm in store.iter_domains() if dm.communities == ["c1"])
    assert d.path_prefixes == []  # claimed {c1,c2}=2/9=0.222>0.2 -- floor exempts only claim==1


def test_bootstrap_and_sync_breadth_caps_are_pinned_to_the_same_constant():
    """One-line guard the review suggested (fix round 2, NEW Minor A): the whole point of
    Option A is that ``domains._PREFIX_BREADTH_CAP`` and ``sync._DOMAIN_CLAIM_CAP`` are
    ONE ratio, applied on both sides of the same invariant -- so a future edit to either
    constant alone, without the other, should fail a test loudly rather than silently
    reopening the gap this whole fix round exists to close."""
    assert domains_mod._PREFIX_BREADTH_CAP == sync_mod._DOMAIN_CLAIM_CAP


# -- fix round 3, NEW Minor B (review): the invariant test suite pinned only the DERIVE
# side of the boundary and the anchorable-type filter -- two refresh-side knobs
# (sync._recompute_domain_communities's own `>` and its own ANCHORABLE_FILE_TYPES filter)
# were unpinned and could drift without any test noticing, breaking the invariant even
# though derive's own guard stayed correct. ---------------------------------------------


def test_bootstrap_boundary_claim_at_exactly_the_cap_is_not_capped_by_refresh(tmp_path):
    """Pins the refresh-side boundary the invariant depends on, not just derive's own
    (fix round 3, review NEW Minor B): mutant S2 (refresh's `>` widened to `>=`) survives
    all 411 tests, because the one fixture that sits exactly at the 0.2 boundary (the
    dedupe test, ``{trbig, trsmall}`` = 2/10 = 0.20) never runs a refresh -- it only
    checks what derive itself produced. Same claim, run through the invariant helper this
    time: "c1" (5 members, home dir "trader/") and "c2" (1 member, also under "trader/")
    -- claimed ``{c1, c2}`` = 2/10 = 0.20 exactly, plus 8 single-member filler
    communities. Correct code (both derive's and refresh's cap check use a STRICT ``>``):
    0.20 is not OVER 0.20, so derive proposes ``["trader"]`` for c1 and the immediate
    refresh agrees it is not capped. S2 would cap it instead (``0.20 >= 0.20``), breaking
    the invariant on a perfectly ordinary boundary claim no existing fixture reached
    through an actual refresh."""
    nodes = (
        [
            {
                "id": f"c1n{i}",
                "label": f"C1N{i}",
                "norm_label": f"c1n{i}",
                "file_type": "code",
                "source_file": f"trader/c1n{i}.py",
                "community": "c1",
            }
            for i in range(5)
        ]
        + [
            {
                "id": "c2n0",
                "label": "C2N0",
                "norm_label": "c2n0",
                "file_type": "code",
                "source_file": "trader/c2n0.py",
                "community": "c2",
            }
        ]
        + [
            {
                "id": f"filler{i}",
                "label": f"Filler{i}",
                "norm_label": f"filler{i}",
                "file_type": "code",
                "source_file": f"misc/filler{i}.py",
                "community": f"filler-{i}",
            }
            for i in range(8)
        ]
    )
    checked = _assert_bootstrap_never_derives_a_rule_the_refresh_cap_rejects(tmp_path, nodes)
    assert checked >= 1  # non-vacuity: derive must actually produce a rule to check here


def test_bootstrap_refresh_ignores_a_non_anchorable_neighbor_like_derive_does(tmp_path):
    """Pins the anchorable-type filter on BOTH sides of the invariant (fix round 3,
    review NEW Minor B): mutant S4 (refresh drops its ``file_type in
    ANCHORABLE_FILE_TYPES`` filter when matching ``path_prefixes``) survives all 411
    tests. "c1" (5 ``code`` members, home dir "trader/") and "c2" (a single ``image``
    node at ``"trader/logo.png"`` -- NOT anchorable, so it never counts toward derive's
    breadth via ``_prefix_community_breadth``'s own ``ANCHORABLE_FILE_TYPES`` filter)
    plus 4 single-member filler communities (6 total). Correct code: derive sees claimed
    ``{c1}`` only (c2's image node is invisible to breadth), the single-community floor
    exempts it, and "trader" is derived; refresh applies the IDENTICAL anchorable filter
    to its own match, so it also sees only ``{c1}`` and never caps it. S4 would let c2's
    image node into refresh's match anyway, capping a claim derive never even saw as
    broader than one community."""
    nodes = (
        [
            {
                "id": f"c1n{i}",
                "label": f"C1N{i}",
                "norm_label": f"c1n{i}",
                "file_type": "code",
                "source_file": f"trader/c1n{i}.py",
                "community": "c1",
            }
            for i in range(5)
        ]
        + [
            {
                "id": "c2img0",
                "label": "C2Img0",
                "norm_label": "c2img0",
                "file_type": "image",
                "source_file": "trader/logo.png",
                "community": "c2",
            }
        ]
        + [
            {
                "id": f"filler{i}",
                "label": f"Filler{i}",
                "norm_label": f"filler{i}",
                "file_type": "code",
                "source_file": f"misc/filler{i}.py",
                "community": f"filler-{i}",
            }
            for i in range(4)
        ]
    )
    checked = _assert_bootstrap_never_derives_a_rule_the_refresh_cap_rejects(tmp_path, nodes)
    assert checked >= 1  # non-vacuity: derive must actually produce a rule to check here


def test_bootstrap_derive_counts_a_bare_directory_named_path_like_refresh_does(tmp_path):
    """Major 1 (review, ``probe_w2.py`` shape (a)): before this fix, ``_prefix_community_
    breadth`` indexed only ``PurePosixPath`` parts of length >= 2, so a node whose
    ``file_path`` is the bare string "trader" -- unreachable on a real filesystem next to
    a directory of the same name, but reachable for a non-file node -- was invisible to
    derive's breadth count while ``schema.matches_path_prefix`` (``fp == prefix or
    fp.startswith(prefix + "/")``) still matched it at refresh time. Five communities:
    "ca" (5 members, home dir "trader/") and "cb" (1 member, ``file_path == "trader"``
    exactly) plus 3 filler communities. RED at the pre-fix tree (reproduced read-only in a
    scratchpad probe against a ``git archive`` of the pre-fix commit, never the checkout;
    see the fix-round-1 report): derive's breadth count excluded cb (the wrong index), so
    "trader" WAS derived for ca and the immediate refresh (claimed ``{ca, cb}`` = 2/5 =
    0.4 > 0.2) capped it. GREEN after: derive's breadth index now sees cb too (claimed
    ``{ca, cb}`` = 2/5 > 0.2), so "trader" is never derived for ca in the first place --
    nothing is left for refresh to cap."""
    nodes = (
        [
            {
                "id": f"can{i}",
                "label": f"CAN{i}",
                "norm_label": f"can{i}",
                "file_type": "code",
                "source_file": f"trader/can{i}.py",
                "community": "ca",
            }
            for i in range(5)
        ]
        + [
            {
                "id": "cbn0",
                "label": "CBN0",
                "norm_label": "cbn0",
                "file_type": "code",
                "source_file": "trader",
                "community": "cb",
            }
        ]
        + [
            {
                "id": f"filler{i}",
                "label": f"Filler{i}",
                "norm_label": f"filler{i}",
                "file_type": "code",
                "source_file": f"misc/filler{i}.py",
                "community": f"filler-{i}",
            }
            for i in range(3)
        ]
    )
    graph = {"built_at_commit": "v1", "nodes": nodes, "links": []}
    reader = _reader(tmp_path, graph=graph, labels=None)
    store = Store(tmp_path / "t.db")

    bootstrap_domains(store, reader, min_members=5)

    d = next(dm for dm in store.iter_domains() if dm.communities == ["ca"])
    assert d.path_prefixes == []  # claimed {ca,cb} = 2/5 = 0.4 > 0.2 once cb's bare path counts
    _communities, overbroad = refresh_domain_communities_now(d, store, reader)
    assert overbroad is None  # nothing was derived, so nothing is left for refresh to cap


def test_bootstrap_derive_counts_a_trailing_slash_path_like_refresh_does(tmp_path):
    """Major 1 (review, ``probe_w2.py`` shape (b)): the same gap as the bare-path test
    above, for a node whose ``file_path`` is the winning directory name plus a trailing
    slash ("trader/", no filename) instead of the bare name -- also matched by
    ``matches_path_prefix`` (the prefix's own trailing slash is stripped before
    comparing) and also invisible to the pre-fix breadth index. Same graph as the bare-
    path test, only ``cb``'s ``file_path`` differs. RED at the pre-fix tree (same
    scratchpad probe as the bare-path test, both shapes in one run; see the fix-round-1
    report), GREEN after, for the identical reason."""
    nodes = (
        [
            {
                "id": f"can{i}",
                "label": f"CAN{i}",
                "norm_label": f"can{i}",
                "file_type": "code",
                "source_file": f"trader/can{i}.py",
                "community": "ca",
            }
            for i in range(5)
        ]
        + [
            {
                "id": "cbn0",
                "label": "CBN0",
                "norm_label": "cbn0",
                "file_type": "code",
                "source_file": "trader/",
                "community": "cb",
            }
        ]
        + [
            {
                "id": f"filler{i}",
                "label": f"Filler{i}",
                "norm_label": f"filler{i}",
                "file_type": "code",
                "source_file": f"misc/filler{i}.py",
                "community": f"filler-{i}",
            }
            for i in range(3)
        ]
    )
    graph = {"built_at_commit": "v1", "nodes": nodes, "links": []}
    reader = _reader(tmp_path, graph=graph, labels=None)
    store = Store(tmp_path / "t.db")

    bootstrap_domains(store, reader, min_members=5)

    d = next(dm for dm in store.iter_domains() if dm.communities == ["ca"])
    assert d.path_prefixes == []  # claimed {ca,cb} = 2/5 = 0.4 > 0.2 once cb's "trader/" counts
    _communities, overbroad = refresh_domain_communities_now(d, store, reader)
    assert overbroad is None  # nothing was derived, so nothing is left for refresh to cap


# -- fix round 2, NEW Major A: _majority_top_dir (PurePosixPath) and
# _prefix_community_breadth (split("/", 1)[0], fix round 1) decomposed file_path
# differently, so derive could pick a majority top directory breadth's own dict had no
# entry for at all -- the single-community floor then waved it through unchecked. Only
# an ABSOLUTE file_path exposes this: PurePosixPath("/repo/a/n.py").parts[0] is "/", the
# breadth key for the same path is "". At refresh, matches_path_prefix strips "/"'s
# trailing slash to an empty-string prefix, which matches every absolute path AND every
# node whose file_path is the empty string "" -- reproducing the original wave-2 symptom:
# derive proposes a rule the very next refresh rejects as overbroad. Fixed by putting
# _majority_top_dir on the SAME str.partition("/") decomposition breadth already used,
# with a member counting toward the majority vote only when there IS a separator AND a
# non-empty head -- see both functions' own docstrings. ------------------------------


def test_bootstrap_never_derives_an_absolute_path_as_a_prefix(tmp_path):
    """NEW Major A, shape 1 (review, ``probe_w2f.py``): five communities, all-absolute
    paths (5 members each under ``"/repo/<c>/"``). RED at the pre-round-2 tree
    (reproduced read-only in a scratchpad probe against a ``git archive`` of the pre-fix
    commit, never the checkout; see the fix-round-2 report): community ``"c0"`` derived
    ``path_prefixes=["/"]`` and the immediate refresh capped it (claimed = all 5
    communities, since an empty prefix matches every absolute path); under ``auto-all``
    the domain was ``accepted`` and then reported ``"activation: path rule too broad: '/'
    match 5/5 communities"`` -- the exact original wave-2 symptom, reintroduced by round
    1's own fix. GREEN after: ``_majority_top_dir`` now skips every member whose
    decomposition has an empty head (an absolute path), so no community in this fixture
    ever has a majority directory at all -- nothing is derived, so ``_domain_anchored``
    never holds (no ``path_prefixes``, no ``seed_anchors``) and ``auto-all`` never even
    attempts auto-ratify: every domain stays ``proposed``, with no `auto_ratify_failures`
    entry."""
    nodes = [
        {
            "id": f"c{c}n{i}",
            "label": f"C{c}N{i}",
            "norm_label": f"c{c}n{i}",
            "file_type": "code",
            "source_file": f"/repo/c{c}/n{i}.py",
            "community": f"c{c}",
        }
        for c in range(5)
        for i in range(5)
    ]
    graph = {"built_at_commit": "v1", "nodes": nodes, "links": []}
    reader = _reader(tmp_path, graph=graph, labels=None)
    store = Store(tmp_path / "t.db")

    report = bootstrap_domains(store, reader, min_members=5, ratify_policy=RatifyPolicy.AUTO_ALL)

    domains = list(store.iter_domains())
    assert len(domains) == 5
    assert report.auto_ratify_failures == []
    for d in domains:
        assert d.path_prefixes == []  # an absolute path has no directory component to vote
        assert d.status == DomainStatus.PROPOSED  # never _domain_anchored -> never even attempted
        _communities, overbroad = refresh_domain_communities_now(d, store, reader)
        assert overbroad is None


def test_bootstrap_absolute_path_does_not_collide_with_an_empty_file_path_node(tmp_path):
    """NEW Major A, shape 2 (review, ``probe_w2f.py``): pre-existing before round 2, only
    surfaced now -- the empty-prefix collision the previous test guards against ALSO
    matches any node whose OWN ``file_path`` is the empty string ``""`` (``schema.
    matches_path_prefix("", "")`` is ``True`` via the ``fp == prefix`` branch). One
    absolute-path community (``"a"``, 5 members under ``"/repo/a/"``) plus three ordinary
    relative communities (``b0``/``b1``/``b2``, each under its own exclusive directory,
    unaffected controls) plus a fifth community (``"e"``) holding a single node whose
    ``file_path`` is ``""``. RED at the pre-round-2 tree (same scratchpad probe as the
    previous test; see the fix-round-2 report): ``"a"`` derived ``["/"]`` and refresh
    capped it 2/5 (matching both ``"a"`` and ``"e"``). GREEN after: ``"a"`` derives
    nothing, for the identical reason as the previous test; ``b0``/``b1``/``b2`` are
    unaffected, confirming the fix does not disturb ordinary relative-path derivation."""
    nodes = (
        [
            {
                "id": f"an{i}",
                "label": f"AN{i}",
                "norm_label": f"an{i}",
                "file_type": "code",
                "source_file": f"/repo/a/n{i}.py",
                "community": "a",
            }
            for i in range(5)
        ]
        + [
            {
                "id": f"b{i}n{j}",
                "label": f"B{i}N{j}",
                "norm_label": f"b{i}n{j}",
                "file_type": "code",
                "source_file": f"relb{i}/n{j}.py",
                "community": f"b{i}",
            }
            for i in range(3)
            for j in range(5)
        ]
        + [
            {
                "id": "emptyn0",
                "label": "EmptyN0",
                "norm_label": "emptyn0",
                "file_type": "code",
                "source_file": "",
                "community": "e",
            }
        ]
    )
    graph = {"built_at_commit": "v1", "nodes": nodes, "links": []}
    reader = _reader(tmp_path, graph=graph, labels=None)
    store = Store(tmp_path / "t.db")

    bootstrap_domains(store, reader, min_members=5)

    d_a = next(dm for dm in store.iter_domains() if dm.communities == ["a"])
    assert d_a.path_prefixes == []  # no directory component survives an absolute path
    _communities, overbroad = refresh_domain_communities_now(d_a, store, reader)
    assert overbroad is None
    for tag in ("b0", "b1", "b2"):
        d = next(dm for dm in store.iter_domains() if dm.communities == [tag])
        assert d.path_prefixes == [f"rel{tag}"]  # ordinary relative derivation, unaffected
        _communities, overbroad = refresh_domain_communities_now(d, store, reader)
        assert overbroad is None


def test_bootstrap_majority_ignores_a_root_level_file_like_a_missing_vote(tmp_path):
    """Pins the majority/breadth asymmetry (fix round 3, review NEW Minor C): mutant F15
    (letting a bare-name, no-separator member vote in ``_majority_top_dir``'s majority
    calc -- dropping just the ``not sep`` half of its skip condition, see that function's
    own docstring for why the two skips differ) survives all 411 tests, and matters on
    real corpora: it would change the majority for 14 communities in this repo alone
    (e.g. proposing ``"pyproject.toml"`` as a stabilizer, or losing ``"docs"``), 55 in
    openspec, 16 in xgboost, and a handful more across other corpora surveyed (fix round
    2 review, measured; see the fix-round-3 report for the corpus-by-corpus counts --
    this file ships to the public repo and never names a NEVER_PUBLIC corpus).

    One community, 5 members: 3 under ``"a/"`` and 2 at the repo root with no directory
    component at all (``"root1.py"``, ``"root2.py"``). Correct code: only the 3 ``"a/"``
    members vote -- a bare filename has no directory to vote FOR, unlike
    ``_prefix_community_breadth``, which still indexes it under its own name because
    that function asks a different question (what would refresh MATCH, not where do this
    community's members LIVE) -- so majority is ``"a"`` at 3/3 = 100% >= 80%, and
    ``path_prefixes == ["a"]``. F15 lets all 5 vote, each root file becoming its own
    one-vote "directory", diluting ``"a"`` to 3/5 = 60% < 80% -- no majority survives,
    ``path_prefixes == []``."""
    nodes = [
        {
            "id": f"an{i}",
            "label": f"AN{i}",
            "norm_label": f"an{i}",
            "file_type": "code",
            "source_file": f"a/n{i}.py",
            "community": "c1",
        }
        for i in range(3)
    ] + [
        {
            "id": "root1",
            "label": "Root1",
            "norm_label": "root1",
            "file_type": "code",
            "source_file": "root1.py",
            "community": "c1",
        },
        {
            "id": "root2",
            "label": "Root2",
            "norm_label": "root2",
            "file_type": "code",
            "source_file": "root2.py",
            "community": "c1",
        },
    ]
    graph = {"built_at_commit": "v1", "nodes": nodes, "links": []}
    reader = _reader(tmp_path, graph=graph, labels=None)
    store = Store(tmp_path / "t.db")

    bootstrap_domains(store, reader, min_members=5)

    d = next(dm for dm in store.iter_domains() if dm.communities == ["c1"])
    assert d.path_prefixes == ["a"]


# -- fix: within-run dedup of an identical derived path_prefixes set (live finding) -------


def test_bootstrap_dedupes_identical_prefix_set_keeping_only_the_larger_community(tmp_path):
    """Live finding: two communities in the SAME bootstrap run that each independently
    derive the IDENTICAL non-empty path_prefixes stabilizer (both >= 80% under 'trader/')
    must not BOTH keep it -- a later sync REPLACE refresh would then let two different
    domains' path_prefixes match the same code, the same "silently-overlapping memory"
    shape the Gate-5 breadth guard exists to prevent, just triggered by two domains racing
    for the same directory within one run instead of one rule alone reaching too broad.
    Only the community with the MOST anchorable members keeps the derived prefix; the
    smaller one is still proposed, just with no derived stabilizer (label/digest/title/
    summary untouched)."""

    def _trader_nodes(tag: str, community: str, count: int) -> list[dict]:
        return [
            {
                "id": f"{tag}{i}",
                "label": f"Trader{tag.capitalize()}{i}",
                "norm_label": f"trader{tag}{i}",
                "file_type": "code",
                "source_file": f"trader/{tag}{i}.py",
                "community": community,
            }
            for i in range(count)
        ]

    # "trbig" (8 members) and "trsmall" (5 members) both derive path_prefixes=["trader"]
    # on their own -- 8 filler communities (2 members each, below min_members, so never
    # candidates themselves) pad the graph to 10 total communities so "trader"'s claimed
    # set (own community included) is {trbig, trsmall} = 2/10 = 0.20 for each -- sits
    # exactly on the breadth cap's boundary (not > 0.20), so the breadth guard alone would
    # never veto either one.
    nodes = _trader_nodes("big", "trbig", 8) + _trader_nodes("small", "trsmall", 5)
    for i in range(8):
        nodes += [
            {
                "id": f"filler{i}a",
                "label": f"Filler{i}A",
                "norm_label": f"filler{i}a",
                "file_type": "code",
                "source_file": f"filler{i}/a.py",
                "community": f"filler{i}",
            },
            {
                "id": f"filler{i}b",
                "label": f"Filler{i}B",
                "norm_label": f"filler{i}b",
                "file_type": "code",
                "source_file": f"filler{i}/b.py",
                "community": f"filler{i}",
            },
        ]
    graph = {"built_at_commit": "v1", "nodes": nodes, "links": []}
    reader = _reader(tmp_path, graph=graph, labels=None)
    store = Store(tmp_path / "t.db")

    report = bootstrap_domains(store, reader, min_members=5)

    big = next(d for d in store.iter_domains() if d.communities == ["trbig"])
    small = next(d for d in store.iter_domains() if d.communities == ["trsmall"])
    assert big.path_prefixes == ["trader"]  # larger community (8 members) keeps it
    assert small.path_prefixes == []  # smaller (5 members) loses it
    assert report.proposed == 2  # both are still proposed either way


# -- fix: a label naming an entity that lives in a DIFFERENT community is rejected ------
#
# Found on a real corpus (independent evaluation, 2026-07-08): 8 of 15 accepted domains
# stored a community that does NOT contain the domain's own namesake god node (e.g. a
# domain titled "OrderBook" stored the community that TA strategies live in; the actual
# `OrderBook` god node lived in a different, numerically adjacent community). Root cause
# is NOT an index/ordering bug in this module -- `community_id`, `anchorable`, and
# `god_node` are carried as one tuple/record end to end and never re-derived by index (see
# the `candidates`/`pending` construction above). It is that `.graphify_labels.json`'s
# community-id keys do not reliably correspond to the SAME partition as the CURRENT
# graph.json's embedded `community` field -- verified on the evaluation store: the
# mismatch was present even though the domain's stamped `provenance.graph_version`
# matched the current graph.json byte-for-byte (no drift between bootstrap-time and
# analysis-time), and the label/community disagreement was spread uniformly across ~48%
# of all communities in that corpus, not clustered near any min_members/limit boundary --
# inconsistent with a positional bug, consistent with the engine's label pass numbering
# communities differently than whatever produced graph.json's `community` field.


def test_bootstrap_label_naming_a_different_communitys_entity_is_rejected(tmp_path):
    """Community "5"'s label names `Bar1` -- an entity graph.json places in community
    "6", not "5" (verifiable straight from `graph`, below). Trusting the label anyway
    would store a domain titled/slugged after `Bar1` whose `communities` is `["5"]`,
    i.e. a domain that does not contain the entity it is named after. The label must be
    rejected and the god-node fallback used instead, so the domain built from a community
    always names something that community actually contains."""
    graph = {
        "built_at_commit": "v1",
        "nodes": [
            {
                "id": f"foo{i}",
                "label": f"Foo{i}",
                "norm_label": f"foo{i}",
                "file_type": "code",
                "source_file": f"foo/f{i}.py",
                "community": 5,
            }
            for i in range(1, 6)
        ]
        + [
            {
                "id": f"bar{i}",
                "label": f"Bar{i}",
                "norm_label": f"bar{i}",
                "file_type": "code",
                "source_file": f"bar/f{i}.py",
                "community": 6,
            }
            for i in range(1, 6)
        ],
        "links": [
            {"relation": "calls", "source": "foo1", "target": "foo2"},
            {"relation": "calls", "source": "bar1", "target": "bar2"},
        ],
    }
    labels = {"5": "Bar1"}
    reader = _reader(tmp_path, graph=graph, labels=labels)
    store = Store(tmp_path / "t.db")

    report = bootstrap_domains(store, reader)

    assert report.proposed == 2
    five = next(d for d in store.iter_domains() if d.communities == ["5"])
    six = next(d for d in store.iter_domains() if d.communities == ["6"])
    # community "5"'s own god node is Foo1 (the only member with any degree) -- the
    # rejected label must fall back to it, never to Bar1.
    assert five.title == "Foo1"
    assert five.summary != "Bar1"
    # community "6" -- Bar1's real home -- is unaffected, and still gets to use Bar1 as
    # its own (unlabeled, god-node-derived) name.
    assert six.title == "Bar1"


def test_bootstrap_label_matching_its_own_communitys_entity_is_kept(tmp_path):
    """Sanity complement: a label that DOES name a member of the community it is keyed to
    (the common, non-drifted case) must still be trusted and used verbatim -- the new
    guard only rejects a label when it demonstrably belongs elsewhere, never a label that
    is silent (free-form prose, e.g. "Alpha Domain" in the shared LABELS fixture) or that
    correctly names its own community's own entity."""
    graph = {
        "built_at_commit": "v1",
        "nodes": [
            {
                "id": f"foo{i}",
                "label": f"Foo{i}",
                "norm_label": f"foo{i}",
                "file_type": "code",
                "source_file": f"foo/f{i}.py",
                "community": 5,
            }
            for i in range(1, 6)
        ],
        "links": [{"relation": "calls", "source": "foo1", "target": "foo2"}],
    }
    labels = {"5": "Foo1"}
    reader = _reader(tmp_path, graph=graph, labels=labels)
    store = Store(tmp_path / "t.db")

    bootstrap_domains(store, reader)

    d = next(d for d in store.iter_domains() if d.communities == ["5"])
    assert d.title == "Foo1"


def test_bootstrap_ambiguous_label_with_no_candidate_in_own_community_is_rejected(tmp_path):
    """Real-corpus finding (review round 2): an AMBIGUOUS label -- several same-named
    nodes scattered across several DIFFERENT communities -- used to be kept outright
    whenever those communities didn't all collapse to one shared value (`resolve()`
    returns `community=None` for a true multi-community ambiguity, and the first version
    of this guard only rejected when `result.community` was set). That let a label
    through even when NONE of its same-named candidates live in the community it's keyed
    to -- exactly as provably wrong as the unique-match case this guard already rejects
    (measured on the evaluation corpus: community "27" titled "Strategy" while every
    actual "Strategy" node lives in communities 41/286).

    Here "Strategy" exists twice, in communities "6" and "7" -- never in "5", the
    community the label is keyed to. The label must be rejected and community "5" falls
    back to its own god node, "Foo1"."""
    graph = {
        "built_at_commit": "v1",
        "nodes": [
            {
                "id": f"foo{i}",
                "label": f"Foo{i}",
                "norm_label": f"foo{i}",
                "file_type": "code",
                "source_file": f"foo/f{i}.py",
                "community": 5,
            }
            for i in range(1, 6)
        ]
        + [
            {
                "id": "s6",
                "label": "Strategy",
                "norm_label": "strategy",
                "file_type": "code",
                "source_file": "strategies/s6.py",
                "community": 6,
            },
            {
                "id": "s7",
                "label": "Strategy",
                "norm_label": "strategy",
                "file_type": "code",
                "source_file": "strategies/s7.py",
                "community": 7,
            },
        ],
        "links": [{"relation": "calls", "source": "foo1", "target": "foo2"}],
    }
    labels = {"5": "Strategy"}
    reader = _reader(tmp_path, graph=graph, labels=labels)
    store = Store(tmp_path / "t.db")

    bootstrap_domains(store, reader)

    five = next(d for d in store.iter_domains() if d.communities == ["5"])
    assert five.title == "Foo1"
    assert five.summary != "Strategy"


def test_bootstrap_ambiguous_label_with_one_candidate_in_own_community_is_kept(tmp_path):
    """Complement of the above: when the SAME ambiguous name also has a candidate that
    genuinely lives in the community the label is keyed to, the label is plausibly
    correct (some entity by that name really is a member here) and must still be kept --
    the guard only rejects when EVERY candidate is provably elsewhere."""
    graph = {
        "built_at_commit": "v1",
        "nodes": [
            {
                "id": "strat5",
                "label": "Strategy",
                "norm_label": "strategy",
                "file_type": "code",
                "source_file": "foo/strategy.py",
                "community": 5,
            }
        ]
        + [
            {
                "id": f"foo{i}",
                "label": f"Foo{i}",
                "norm_label": f"foo{i}",
                "file_type": "code",
                "source_file": f"foo/f{i}.py",
                "community": 5,
            }
            for i in range(2, 6)
        ]
        + [
            {
                "id": "s6",
                "label": "Strategy",
                "norm_label": "strategy",
                "file_type": "code",
                "source_file": "strategies/s6.py",
                "community": 6,
            },
        ],
        "links": [],
    }
    labels = {"5": "Strategy"}
    reader = _reader(tmp_path, graph=graph, labels=labels)
    store = Store(tmp_path / "t.db")

    bootstrap_domains(store, reader)

    five = next(d for d in store.iter_domains() if d.communities == ["5"])
    assert five.title == "Strategy"


# -- D7.4: deterministic domain lint (design/superpowers/specs/
# 2026-07-30-staleness-machinery-design.md) -------------------------------------------------


def test_bootstrap_warns_when_derived_prefix_subsumes_sibling_seed_anchor(tmp_path):
    """Community "10" derives path_prefixes=["domainA"] (all 6 members share it) -- an
    ACCEPTED sibling domain's own seed_anchors file under domainA/ means this bootstrap
    candidate's derived prefix would subsume it (design D7.4's second mechanical half;
    the dead-prefix half can't fire here -- see this module's own docstring)."""
    reader = _reader(tmp_path, labels=LABELS)
    store = Store(tmp_path / "t.db")
    sibling = store.add_domain(
        Domain(
            slug="alpha-notes",
            title="Alpha notes",
            summary="Hand-curated notes on Alpha.",
            seed_anchors=[Descriptor(name="Alpha", file_path="domainA/alpha.py")],
            provenance=Provenance(source="manual"),
        )
    )
    store.ratify_domains(accept=[sibling.domain_id])

    report = bootstrap_domains(store, reader)

    ten = next(d for d in store.iter_domains() if d.communities == ["10"])
    assert ten.path_prefixes == ["domainA"]
    entry = next(w for w in report.warnings if w["slug"] == ten.slug)
    assert any("subsumes" in w and "alpha-notes" in w for w in entry["warnings"])


def test_bootstrap_no_warnings_in_the_common_case(tmp_path):
    reader = _reader(tmp_path, labels=LABELS)
    store = Store(tmp_path / "t.db")
    report = bootstrap_domains(store, reader)
    assert report.warnings == []


def test_bootstrap_dry_run_carries_warnings_per_item(tmp_path):
    reader = _reader(tmp_path, labels=LABELS)
    store = Store(tmp_path / "t.db")
    sibling = store.add_domain(
        Domain(
            slug="alpha-notes",
            title="Alpha notes",
            summary="Hand-curated notes on Alpha.",
            seed_anchors=[Descriptor(name="Alpha", file_path="domainA/alpha.py")],
            provenance=Provenance(source="manual"),
        )
    )
    store.ratify_domains(accept=[sibling.domain_id])

    report = bootstrap_domains(store, reader, dry_run=True)

    ten = next(item for item in report.dry_run if item["path_prefixes"] == ["domainA"])
    assert any("subsumes" in w for w in ten["warnings"])
    # dry-run must not have written anything, warnings included -- the candidate is only
    # ever a listing entry.
    assert list(store.iter_domains(status=DomainStatus.PROPOSED)) == []
