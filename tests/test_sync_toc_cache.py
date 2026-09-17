"""``toc_cache`` meta persistence on the sync pass (M4 §5/§6; fix-wave B6): written at the
end of every completed sync, AND on a skipped (already-up-to-date) pass too whenever at
least one accepted domain exists — the cache is volatile state that a content-only change
(e.g. ``add_decision``) can go stale on without ever moving ``graph_version``, so the
version gate must not also gate this refresh. A skipped pass with zero accepted domains
still leaves the cache untouched (nothing to refresh)."""

from __future__ import annotations

import json

from sidegraph.engine.reader import GraphifyReader
from sidegraph.retrieval import TOC_CACHE_KEY, build_toc
from sidegraph.schema import Domain, Provenance
from sidegraph.store import Store
from sidegraph.sync import sync

GRAPH = {
    "built_at_commit": "v1",
    "nodes": [
        {
            "id": "n1",
            "label": "Alpha",
            "norm_label": "alpha",
            "file_type": "code",
            "source_file": "payments/a.py",
            "community": 1,
        },
    ],
    "links": [],
}


def _write_graph(tmp_path, name, data):
    p = tmp_path / name
    p.write_text(json.dumps(data))
    return p


def test_toc_cache_written_on_fresh_sync(tmp_path):
    store = Store(tmp_path / "t.db")
    d = store.add_domain(
        Domain(
            slug="payments",
            title="Payments",
            summary="s",
            path_prefixes=["payments"],
            provenance=Provenance(source="manual"),
        )
    )
    store.ratify_domains(accept=[d.domain_id])
    reader = GraphifyReader(_write_graph(tmp_path, "g.json", GRAPH))

    assert store.get_meta(TOC_CACHE_KEY) is None
    sync(store, reader)

    cached = json.loads(store.get_meta(TOC_CACHE_KEY))
    assert cached["domains"][0]["slug"] == "payments"
    # matches an independent build_toc call over the (now-refreshed) store
    assert cached == build_toc(store, reader)


def test_toc_cache_refreshed_on_skipped_sync_when_accepted_domain_exists(tmp_path):
    # B6 regression: a content-only change (a new accepted domain here, or equally an
    # add_decision bound to an existing domain entity) never moves graph_version, so a
    # follow-up sync is a version-match "skip" — but the cache must still heal, not wait
    # for an unrelated domain ratify/drop or the next real graph rebuild.
    store = Store(tmp_path / "t.db")
    reader = GraphifyReader(_write_graph(tmp_path, "g.json", GRAPH))
    sync(store, reader)  # fresh pass: writes the cache
    first = store.get_meta(TOC_CACHE_KEY)

    d = store.add_domain(
        Domain(  # would change the TOC if recomputed
            slug="late",
            title="Late",
            summary="s",
            provenance=Provenance(source="manual"),
        )
    )
    store.ratify_domains(accept=[d.domain_id])

    report = sync(store, reader)  # same graph_version -> skipped
    assert report.skipped is True
    updated = json.loads(store.get_meta(TOC_CACHE_KEY))
    assert store.get_meta(TOC_CACHE_KEY) != first  # refreshed despite the skip
    assert any(entry["slug"] == "late" for entry in updated["domains"])
    assert updated == build_toc(store, reader)


def test_toc_cache_untouched_on_skipped_sync_with_no_accepted_domains(tmp_path):
    # No accepted domain exists at all -> nothing to refresh; a skipped pass is a true
    # no-op, same as before B6 (avoids a pointless write on every no-op sync call).
    store = Store(tmp_path / "t.db")
    reader = GraphifyReader(_write_graph(tmp_path, "g.json", GRAPH))
    assert store.get_meta(TOC_CACHE_KEY) is None

    sync(store, reader)  # fresh pass (from_version is None) — writes the cache once
    first = store.get_meta(TOC_CACHE_KEY)

    report = sync(store, reader)  # same graph_version -> skipped, no accepted domains
    assert report.skipped is True
    assert store.get_meta(TOC_CACHE_KEY) == first


def test_toc_cache_rewritten_on_forced_sync(tmp_path):
    store = Store(tmp_path / "t.db")
    reader = GraphifyReader(_write_graph(tmp_path, "g.json", GRAPH))
    sync(store, reader)
    first = store.get_meta(TOC_CACHE_KEY)

    d = store.add_domain(
        Domain(
            slug="late",
            title="Late",
            summary="s",
            provenance=Provenance(source="manual"),
        )
    )
    store.ratify_domains(accept=[d.domain_id])

    sync(store, reader, force=True)
    updated = json.loads(store.get_meta(TOC_CACHE_KEY))
    assert updated != json.loads(first)
    assert any(entry["slug"] == "late" for entry in updated["domains"])
