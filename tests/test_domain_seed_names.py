"""A domain authored with a path-named seed stores it under the basename, on every authoring path.
# see design/superpowers/specs/2026-10-14-graphify-upgrade-design.md (D2)"""

from __future__ import annotations

from sidegraph import capture
from sidegraph.schema import Descriptor, file_anchor_descriptors
from sidegraph.server import _add_domain_impl, _supersede_domain_impl
from sidegraph.store import Store

P = "src/sidegraph/store.py"
SEED = {"name": P, "file_path": P}
FOLDED = [Descriptor(name="store.py", file_path=P)]


def test_file_anchor_descriptors_folds_only_path_names():
    plain = Descriptor(name="Store", file_path=P)
    assert file_anchor_descriptors([Descriptor(**SEED), plain]) == [FOLDED[0], plain]


def test_add_and_supersede_domain_store_the_basename(tmp_path):
    store = Store(tmp_path / ".sidegraph")
    added = _add_domain_impl(store, None, "store", "Store", "The store.", seed_anchors=[SEED])
    assert store.get_domain(added["domain_id"]).seed_anchors == FOLDED
    succ = _supersede_domain_impl(
        store, None, "store", "store2", "Store", "The store.", seed_anchors=[SEED]
    )
    assert store.get_domain(succ["domain_id"]).seed_anchors == FOLDED


def test_propose_domains_stores_the_basename(tmp_path):
    store = Store(tmp_path / ".sidegraph")
    (result,) = capture.propose_domains(
        [{"slug": "store", "title": "Store", "summary": "The store.", "seed_anchors": [SEED]}],
        store,
        None,
    )
    assert store.get_domain(result.domain_id).seed_anchors == FOLDED
