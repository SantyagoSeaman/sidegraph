"""Replay acceptance for the integrity self-check: the silent degradations one field corpus lived
with for weeks, in one fixture repository, are each said to the human with their fix, and saying
nothing again once they are fixed.

The fixture is a repository whose graph is stale and that has no graph refresh hook, with three
recent records whose every leaf anchor is orphaned, a store file the reload cannot index, and a
31-day-old proposal nobody ratified. One SessionStart emits five notices, each with its fix;
applying the fixes and letting a day pass, the next SessionStart emits none.
see design/superpowers/specs/2026-10-02-integrity-self-check-design.md (T16) and
design/superpowers/specs/2026-10-02-graph-refresh-hook-design.md (D7)
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from sidegraph import githooks
from sidegraph.schema import AnchorBinding, Descriptor, Entity
from sidegraph.store import Store
from tests.test_graph_freshness import T_AFTER, head, stale_repo, write_graph
from tests.test_host_integrity import NOTICE_KEY, _proposal, start
from tests.test_integrity import add_record

NAMES = [("fn_1()", "pkg/new1.py"), ("fn_2()", "pkg/new2.py"), ("fn_3()", "pkg/new3.py")]


def _anchor(store: Store, record_id: str, name: str, file_path: str) -> None:
    entity = store.upsert_entity(
        Entity(canonical_name=name, descriptor=Descriptor(name=name, file_path=file_path))
    )
    store.add_binding(AnchorBinding(record_id=record_id, entity_id=entity.entity_id, tier=2))


def test_t16_five_problems_are_said_once_each_with_a_fix_and_not_again_once_fixed(
    tmp_path, monkeypatch, capsys
):
    fx = stale_repo(tmp_path)
    store_dir = fx.repo / ".sidegraph"  # ignored by the fixture's .gitignore, like a real one
    store = Store(store_dir)
    ids = [add_record(store, age_days=1, leaves=()) for _ in NAMES]
    for rid, (name, file_path) in zip(ids, NAMES, strict=True):
        _anchor(store, rid, name, file_path)
    _proposal(store, 31)
    store.close()
    record = json.loads((store_dir / "decisions" / f"{ids[0]}.json").read_text(encoding="utf-8"))
    bad_file = store_dir / "decisions" / "A.json"
    bad_file.write_text(json.dumps({**record, "id": "B"}), encoding="utf-8")
    (store_dir / "index.db").unlink()  # the next open reloads, and finds the file it must skip
    t0 = datetime.now(UTC)

    first = start(
        monkeypatch, capsys, store_dir, graph=fx.graph, project=fx.repo, session="s1", now=t0
    )

    notices = first["systemMessage"].split("\n")
    assert len(notices) == 5, notices
    graph_stale, orphaned, skipped, pending, refresh = notices
    assert graph_stale.startswith("Sidegraph: the code graph is stale") and (
        graph_stale.endswith("`graphify update .`")
    )
    assert orphaned.startswith("Sidegraph: 3 of 3 open record(s) have every code anchor orphaned")
    assert "`sidegraph-doctor`" in orphaned
    assert skipped.startswith("Sidegraph: 1 store file(s) could not be indexed")
    assert "decisions/A.json" in skipped and "`sidegraph-verify`" in skipped
    assert pending.startswith("Sidegraph: 1 record(s) await ratification, the oldest for 31 days")
    assert "`sidegraph-ratify`" in pending
    assert refresh.startswith("Sidegraph: no git hook keeps the code graph fresh")
    assert "`sidegraph-init --hooks`" in refresh

    # The fixes: a graph rebuilt at HEAD that holds the names, the bad file removed, the
    # proposal ratified, the refresh hook installed. Then a day passes.
    write_graph(
        fx.graph,
        head(fx.repo),
        ["pkg/m.py", *[file_path for _name, file_path in NAMES]],
        mtime_ns=T_AFTER + 100 * 10**9,
    )
    bad_file.unlink()
    info = githooks.repo_info(fx.repo)
    assert info is not None
    githooks.install(info, graphify=None)
    store = Store(store_dir)
    for proposed in list(store.iter_proposed()):
        store.ratify(proposed.id)
    store.close()

    second = start(
        monkeypatch,
        capsys,
        store_dir,
        graph=fx.graph,
        project=fx.repo,
        session="s2",
        now=t0 + timedelta(hours=24),
    )

    assert "systemMessage" not in second
    store = Store(store_dir)
    try:
        assert [
            key
            for key in (
                "graph-stale",
                "orphaned-records",
                "store-files-skipped",
                "pending-ratification",
                "refresh-hook-missing",
            )
            if store.get_meta(NOTICE_KEY + key) is not None
        ] == []
    finally:
        store.close()
