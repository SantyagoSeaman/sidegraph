"""``toc_cache`` meta persistence on the sync pass (M4 §5/§6; fix-wave B6): written at the
end of every completed sync, AND on a skipped (already-up-to-date) pass too whenever at
least one accepted domain exists — the cache is volatile state that a content-only change
(e.g. ``add_decision``) can go stale on without ever moving ``graph_version``, so the
version gate must not also gate this refresh. A skipped pass with zero accepted domains
still leaves the cache untouched (nothing to refresh)."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import sidegraph.sync as sync_module
from sidegraph.engine.reader import GraphifyReader
from sidegraph.retrieval import TOC_CACHE_KEY, build_toc
from sidegraph.schema import (
    AnchorBinding,
    Decision,
    DecisionKind,
    DecisionStatus,
    Domain,
    Provenance,
)
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


def _without_digest(cache: dict) -> dict:
    """The cached TOC as ``build_toc`` returns it: the sync pass stamps the store's digest
    into the cache beside it (see ``sync.TOC_DIGEST_FIELD``)."""
    return {k: v for k, v in cache.items() if k != sync_module.TOC_DIGEST_FIELD}


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
    # matches an independent build_toc call over the (now-refreshed) store, and records the
    # digest of the store it was built over
    assert _without_digest(cached) == build_toc(store, reader)
    assert cached[sync_module.TOC_DIGEST_FIELD] == store.canonical_digest()


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
    assert _without_digest(updated) == build_toc(store, reader)


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


# -- the skipped pass rebuilds only when the store changed (D2b) ------------------------


def _accepted_payments(store: Store) -> Domain:
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
    return store.get_domain(d.domain_id)


def _count_toc_builds(monkeypatch) -> list[int]:
    """A spy on the ``build_toc`` the sync pass calls; each build appends one entry."""
    builds: list[int] = []
    real = sync_module.build_toc

    def spy(*args, **kwargs):
        builds.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(sync_module, "build_toc", spy)
    return builds


def test_skipped_sync_on_an_unchanged_store_does_not_rebuild_the_toc(tmp_path, monkeypatch):
    """Every ``get_task_context`` call runs this skipped pass, and a TOC build counts every
    domain's decisions (0.2 s on a real 15-domain store). The cache records the store's
    canonical digest, so an unchanged store keeps the cache it has; a canonical write moves
    the digest and the next pass rebuilds, so the count a session sees is never older than
    the store."""
    store = Store(tmp_path / "t.db")
    _accepted_payments(store)
    reader = GraphifyReader(_write_graph(tmp_path, "g.json", GRAPH))
    sync(store, reader)  # the real pass: builds and records the digest
    cached = store.get_meta(TOC_CACHE_KEY)
    builds = _count_toc_builds(monkeypatch)

    assert sync(store, reader).skipped is True
    assert builds == []
    assert store.get_meta(TOC_CACHE_KEY) == cached

    mistake = store.add_decision(
        Decision(
            title="watch the retry loop",
            kind=DecisionKind.GOTCHA,
            status=DecisionStatus.ACCEPTED,
            context="c",
            choice="ch",
            valid_from=datetime.now(UTC),
            provenance=Provenance(source="manual"),
        )
    )
    entity = store.find_abstract_entity("domain:payments")
    store.add_binding(
        AnchorBinding(record_id=mistake.id, entity_id=entity.entity_id, tier=1, status="live")
    )

    assert sync(store, reader).skipped is True
    assert builds == [1]
    assert json.loads(store.get_meta(TOC_CACHE_KEY))["domains"][0]["mistakes"] == 1

    assert sync(store, reader).skipped is True
    assert builds == [1]  # the rebuilt cache is current again


def test_skipped_sync_rebuilds_a_cache_that_carries_no_digest(tmp_path, monkeypatch):
    """The ratify and capture paths write the cache with no graph reader, so their copy
    misses the document-anchored decisions a reader would add. It carries no digest, so the
    next pass that has a reader rebuilds it once instead of trusting it."""
    store = Store(tmp_path / "t.db")
    _accepted_payments(store)
    reader = GraphifyReader(_write_graph(tmp_path, "g.json", GRAPH))
    sync(store, reader)
    store.set_meta(TOC_CACHE_KEY, json.dumps(build_toc(store)))
    builds = _count_toc_builds(monkeypatch)

    assert sync(store, reader).skipped is True
    assert builds == [1]
    assert "store_digest" in json.loads(store.get_meta(TOC_CACHE_KEY))

    sync(store, reader)
    assert builds == [1]


def test_skipped_sync_rebuilds_when_the_store_has_no_digest_to_compare(tmp_path, monkeypatch):
    """A refused stamp clears the store's digest ("cannot tell"). A cache that carries no
    digest either, as the ratify and capture paths write it, must not read as current just
    because the two absences are equal."""
    store = Store(tmp_path / "t.db")
    _accepted_payments(store)
    reader = GraphifyReader(_write_graph(tmp_path, "g.json", GRAPH))
    sync(store, reader)
    with store._mutation():
        store._conn.execute("DELETE FROM meta WHERE key = 'canonical_digest'")
    assert store.canonical_digest() is None
    store.set_meta(TOC_CACHE_KEY, json.dumps(build_toc(store)))
    assert sync_module.TOC_DIGEST_FIELD not in json.loads(store.get_meta(TOC_CACHE_KEY))
    builds = _count_toc_builds(monkeypatch)

    assert sync(store, reader).skipped is True
    assert builds == [1]
