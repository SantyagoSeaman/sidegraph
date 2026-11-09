"""A file named exactly ``.json`` (or ``.jsonl`` under ``archive/``) is listed by the index
rebuild's ``*.json`` glob, so the digest walk must list it too: otherwise the index holds a row
for a file the digest never sees, and every digest recheck that races a writer rebuilds.

# see design/superpowers/specs/2026-11-05-hook-wait-and-digest-recheck.md (D3)
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from sidegraph.schema import Decision, DecisionKind, DecisionStatus, Provenance
from sidegraph.store import Store


def _decision(title: str) -> Decision:
    return Decision(
        title=title,
        kind=DecisionKind.ADR,
        status=DecisionStatus.ACCEPTED,
        context="c",
        choice="x",
        valid_from=datetime.now(UTC),
        provenance=Provenance(source="manual"),
    )


@pytest.mark.parametrize(("subdir", "name"), [("decisions", ".json"), ("archive", ".jsonl")])
def test_a_stray_dot_json_is_in_the_digest_walk_and_the_recheck_keeps_the_index(
    tmp_path, subdir, name
):
    path = tmp_path / "store"
    with Store(path) as store:
        store.add_decision(_decision("one"))
    (path / subdir).mkdir(exist_ok=True)
    (path / subdir / name).write_text("{}\n")
    with Store(path) as other:
        other.add_decision(_decision("two"))  # a real change, so the next open reloads
    with Store(path) as store:
        digest, observed = store._compute_canonical_digest()
        store._reload_index_from_canonical(digest)  # the reload that loads the stray
        digest, observed = store._compute_canonical_digest()
        loaded = {
            (row["subdir"], row["stem"])
            for row in store._conn.execute("SELECT subdir, stem FROM canonical_stat")
        }
        assert loaded <= observed.keys()

        rebuilds: list[str] = []
        for attr in ("_rebuild_format1_in_transaction", "_rebuild_index_in_transaction"):
            original = getattr(store, attr)

            def counted(*args, _original=original, _attr=attr, **kwargs):
                rebuilds.append(_attr)
                return _original(*args, **kwargs)

            setattr(store, attr, counted)
        store._reload_index_from_canonical(digest, recheck=True)
        assert rebuilds == []
