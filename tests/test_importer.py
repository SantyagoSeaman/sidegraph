import json

from sidegraph.engine.reader import GraphifyReader
from sidegraph.importer import import_rationales
from sidegraph.schema import DecisionStatus
from sidegraph.store import Store


def _write_graph(tmp_path, name, data):
    p = tmp_path / name
    p.write_text(json.dumps(data))
    return p


def _reader(tmp_path, graph, name="g.json"):
    return GraphifyReader(_write_graph(tmp_path, name, graph))


# Code-shape fixture: one rationale, one rationale_for target, both in exec.py.
CODE_GRAPH = {
    "built_at_commit": "v1",
    "nodes": [
        {
            "id": "rat1",
            "label": "Retries are idempotent to survive at-least-once delivery",
            "norm_label": "retries are idempotent to survive at-least-once delivery",
            "file_type": "rationale",
            "source_file": "exec.py",
            "community": 1,
        },
        {
            "id": "fn_submit",
            "label": "submit_order",
            "norm_label": "submit_order",
            "file_type": "code",
            "source_file": "exec.py",
            "community": 1,
        },
    ],
    "links": [
        {"relation": "rationale_for", "source": "rat1", "target": "fn_submit"},
    ],
}


def test_import_accepted_by_default(tmp_path):
    reader = _reader(tmp_path, CODE_GRAPH)
    store = Store(tmp_path / "s.db")
    report = import_rationales(store, reader)
    assert report.imported == 1
    assert report.skipped_existing == 0
    assert report.skipped_unanchorable == 0

    decisions = list(store.iter_decisions())
    assert len(decisions) == 1
    d = decisions[0]
    assert d.status == DecisionStatus.ACCEPTED
    assert d.kind.value == "adr"
    assert d.title == "Retries are idempotent to survive at-least-once delivery"
    assert d.choice == "Retries are idempotent to survive at-least-once delivery"
    assert d.context == "imported from exec.py (rat1)"
    assert d.provenance.source == "import"
    assert d.provenance.author == "sidegraph-import"
    assert d.provenance.graph_version is not None
    assert d.provenance.ref == "exec.py"

    bindings = store.bindings_for_record(d.id)
    leaf = [b for b in bindings if b.tier == 2]
    assert len(leaf) == 1
    entity = store.get_entity(leaf[0].entity_id)
    assert entity.canonical_name == "submit_order"


def test_import_propose_lands_proposed(tmp_path):
    reader = _reader(tmp_path, CODE_GRAPH)
    store = Store(tmp_path / "s.db")
    report = import_rationales(store, reader, propose=True)
    assert report.imported == 1
    d = next(store.iter_decisions())
    assert d.status == DecisionStatus.PROPOSED


def test_import_idempotent_rerun(tmp_path):
    reader = _reader(tmp_path, CODE_GRAPH)
    store = Store(tmp_path / "s.db")
    first = import_rationales(store, reader)
    assert first.imported == 1

    second = import_rationales(store, reader)
    assert second.imported == 0
    assert second.skipped_existing == 1
    assert len(list(store.iter_decisions())) == 1


PATHLESS_GRAPH = {
    "nodes": [
        {
            "id": "rat_p",
            "label": "Cross-cutting rationale with no source file",
            "norm_label": "cross-cutting rationale with no source file",
            "file_type": "rationale",
            "community": 1,
        },  # no source_file -> file_path is None
        {
            "id": "fn_p",
            "label": "handle_thing",
            "norm_label": "handle_thing",
            "file_type": "code",
            "source_file": "thing.py",
            "community": 1,
        },
    ],
    "links": [
        {"relation": "rationale_for", "source": "rat_p", "target": "fn_p"},
    ],
}


def test_import_pathless_rationale_stamps_ref_and_context_with_node_id(tmp_path):
    reader = _reader(tmp_path, PATHLESS_GRAPH)
    store = Store(tmp_path / "s.db")
    report = import_rationales(store, reader)
    assert report.imported == 1

    d = next(store.iter_decisions())
    assert d.provenance.ref == "rat_p"
    assert d.context == "imported from rat_p"
    assert "None" not in d.context


# Same title, different files (and different rationale_for targets) -> both must import: the
# idempotency key is (canonical title, provenance.ref, provenance.source), not title alone.
SAME_TITLE_DIFFERENT_FILE_GRAPH = {
    "nodes": [
        {
            "id": "rat_a",
            "label": "Retries are idempotent",
            "norm_label": "retries are idempotent",
            "file_type": "rationale",
            "source_file": "a.py",
            "community": 1,
        },
        {
            "id": "file_a",
            "label": "a.py",
            "norm_label": "a.py",
            "file_type": "document",
            "source_file": "a.py",
            "community": 1,
        },
        {
            "id": "rat_b",
            "label": "Retries are idempotent",
            "norm_label": "retries are idempotent",
            "file_type": "rationale",
            "source_file": "b.py",
            "community": 2,
        },
        {
            "id": "file_b",
            "label": "b.py",
            "norm_label": "b.py",
            "file_type": "document",
            "source_file": "b.py",
            "community": 2,
        },
    ],
    "links": [],
}


def test_import_same_title_different_files_both_import(tmp_path):
    reader = _reader(tmp_path, SAME_TITLE_DIFFERENT_FILE_GRAPH)
    store = Store(tmp_path / "s.db")
    report = import_rationales(store, reader)
    assert report.imported == 2
    assert report.skipped_existing == 0

    decisions = list(store.iter_decisions())
    assert len(decisions) == 2
    refs = {d.provenance.ref for d in decisions}
    assert refs == {"a.py", "b.py"}

    # Rerunning is still idempotent per-file: neither re-imports.
    rerun = import_rationales(store, reader)
    assert rerun.imported == 0
    assert rerun.skipped_existing == 2
    assert len(list(store.iter_decisions())) == 2


REDACT_GRAPH = {
    "nodes": [
        {
            "id": "rat1",
            "label": "Uses api_key=AKIAABCDEFGHIJKLMNOP for the vendor call",
            "norm_label": "uses api_key=akiaabcdefghijklmnop for the vendor call",
            "file_type": "rationale",
            "source_file": "exec.py",
            "community": 1,
        },
        {
            "id": "fn_submit",
            "label": "submit_order",
            "norm_label": "submit_order",
            "file_type": "code",
            "source_file": "exec.py",
            "community": 1,
        },
    ],
    "links": [
        {"relation": "rationale_for", "source": "rat1", "target": "fn_submit"},
    ],
}


# Regression for "redact before truncate": the secret token starts at index 110 and is 20
# chars long (ends at 130), so a naive text[:120] truncation cuts it at its 10th char,
# leaving a partial, non-matching fragment ("AKIAABCDEF" is only 6 chars past "AKIA", not the
# 16 the pattern requires) that a redact-after-truncate pass would miss entirely.
_SECRET_TOKEN = "AKIAABCDEFGHIJKLMNOP"
_STRADDLING_TEXT = (
    "z" * 110
    + _SECRET_TOKEN
    + " end of the sentence explaining the rationale in full for context and completeness."
)

STRADDLING_SECRET_GRAPH = {
    "nodes": [
        {
            "id": "rat1",
            "label": _STRADDLING_TEXT,
            "norm_label": _STRADDLING_TEXT.lower(),
            "file_type": "rationale",
            "source_file": "exec.py",
            "community": 1,
        },
        {
            "id": "fn_submit",
            "label": "submit_order",
            "norm_label": "submit_order",
            "file_type": "code",
            "source_file": "exec.py",
            "community": 1,
        },
    ],
    "links": [
        {"relation": "rationale_for", "source": "rat1", "target": "fn_submit"},
    ],
}


def test_import_redact_before_truncate_no_partial_secret_in_title(tmp_path):
    reader = _reader(tmp_path, STRADDLING_SECRET_GRAPH)
    store = Store(tmp_path / "s.db")
    report = import_rationales(store, reader)
    assert report.imported == 1

    d = next(store.iter_decisions())
    assert len(d.title) == 120
    assert _SECRET_TOKEN[:10] not in d.title  # the raw token prefix a truncate-first bug leaks
    assert "AKIA" not in d.title
    assert "[REDACTED]" in d.title
    assert "AKIA" not in d.choice


def test_import_redacts_secrets(tmp_path):
    reader = _reader(tmp_path, REDACT_GRAPH)
    store = Store(tmp_path / "s.db")
    import_rationales(store, reader)
    d = next(store.iter_decisions())
    assert "AKIA" not in d.choice
    assert "AKIA" not in d.title
    assert "[REDACTED]" in d.choice


# One rationale_for target ("helper") whose name collides ambiguously (no file_path on the
# rationale's own target node, and a second "helper" elsewhere) -> per-target resolve must
# come back "ambiguous", never guessed; the importer must then fall back to the file's own
# node (file_node: label == source_file, the doc-corpus file-anchor pattern).
UNRESOLVABLE_TARGET_GRAPH = {
    "nodes": [
        {
            "id": "rat1",
            "label": "Handles retries defensively",
            "norm_label": "handles retries defensively",
            "file_type": "rationale",
            "source_file": "ADR-001.md",
            "community": 1,
        },
        {
            "id": "dup1",
            "label": "helper",
            "norm_label": "helper",
            "file_type": "code",
            "community": 1,
        },
        {
            "id": "dup2",
            "label": "helper",
            "norm_label": "helper",
            "file_type": "code",
            "source_file": "other.py",
            "community": 2,
        },
        {
            "id": "file_node",
            "label": "ADR-001.md",
            "norm_label": "adr-001.md",
            "file_type": "document",
            "source_file": "ADR-001.md",
            "community": 1,
        },
    ],
    "links": [
        {"relation": "rationale_for", "source": "rat1", "target": "dup1"},
    ],
}


def test_import_unresolvable_target_falls_back_to_file_node(tmp_path):
    reader = _reader(tmp_path, UNRESOLVABLE_TARGET_GRAPH)
    store = Store(tmp_path / "s.db")
    report = import_rationales(store, reader)
    assert report.imported == 1
    assert report.skipped_unanchorable == 0

    d = next(store.iter_decisions())
    bindings = store.bindings_for_record(d.id)
    leaf = [b for b in bindings if b.tier == 2]
    assert len(leaf) == 1
    entity = store.get_entity(leaf[0].entity_id)
    assert entity.canonical_name == "ADR-001.md"
    # The ambiguous "helper" target was never bound.
    assert store.find_entity("helper", None) is None


# Same ambiguous target, but no file-level node exists at all -> nothing resolves.
NOTHING_RESOLVABLE_GRAPH = {
    "nodes": [
        {
            "id": "rat1",
            "label": "Handles retries defensively",
            "norm_label": "handles retries defensively",
            "file_type": "rationale",
            "source_file": "orphan.py",
            "community": 1,
        },
        {
            "id": "dup1",
            "label": "helper",
            "norm_label": "helper",
            "file_type": "code",
            "community": 1,
        },
        {
            "id": "dup2",
            "label": "helper",
            "norm_label": "helper",
            "file_type": "code",
            "source_file": "other.py",
            "community": 2,
        },
    ],
    "links": [
        {"relation": "rationale_for", "source": "rat1", "target": "dup1"},
    ],
}


def test_import_nothing_resolvable_is_skipped_unanchorable(tmp_path):
    reader = _reader(tmp_path, NOTHING_RESOLVABLE_GRAPH)
    store = Store(tmp_path / "s.db")
    report = import_rationales(store, reader)
    assert report.imported == 0
    assert report.skipped_unanchorable == 1
    assert list(store.iter_decisions()) == []


def test_import_dry_run_writes_nothing(tmp_path):
    reader = _reader(tmp_path, CODE_GRAPH)
    store = Store(tmp_path / "s.db")
    report = import_rationales(store, reader, dry_run=True)
    assert report.imported == 1
    assert report.dry_run == [
        {
            "file_path": "exec.py",
            "title": "Retries are idempotent to survive at-least-once delivery",
            "node_id": "rat1",
        }
    ]
    assert list(store.iter_decisions()) == []
    assert list(store.iter_concrete_entities()) == []


MULTI_FILE_GRAPH = {
    "nodes": [
        {
            "id": "rat_a",
            "label": "Reason A",
            "norm_label": "reason a",
            "file_type": "rationale",
            "source_file": "a.py",
            "community": 1,
        },
        {
            "id": "file_a",
            "label": "a.py",
            "norm_label": "a.py",
            "file_type": "document",
            "source_file": "a.py",
            "community": 1,
        },
        {
            "id": "rat_b",
            "label": "Reason B",
            "norm_label": "reason b",
            "file_type": "rationale",
            "source_file": "b.py",
            "community": 2,
        },
        {
            "id": "file_b",
            "label": "b.py",
            "norm_label": "b.py",
            "file_type": "document",
            "source_file": "b.py",
            "community": 2,
        },
        {
            "id": "rat_c",
            "label": "Reason C",
            "norm_label": "reason c",
            "file_type": "rationale",
            "source_file": "docs/adr.md",
            "community": 3,
        },
        {
            "id": "file_c",
            "label": "docs/adr.md",
            "norm_label": "docs/adr.md",
            "file_type": "document",
            "source_file": "docs/adr.md",
            "community": 3,
        },
    ],
    "links": [],
}


def test_import_path_prefix_filters_and_counts(tmp_path):
    reader = _reader(tmp_path, MULTI_FILE_GRAPH)
    store = Store(tmp_path / "s.db")
    report = import_rationales(store, reader, path_prefixes=["docs/"])
    assert report.filtered == 2
    assert report.imported == 1
    d = next(store.iter_decisions())
    assert d.title == "Reason C"


def test_import_limit_caps_processed_nodes(tmp_path):
    reader = _reader(tmp_path, MULTI_FILE_GRAPH)
    store = Store(tmp_path / "s.db")
    report = import_rationales(store, reader, limit=1)
    assert report.imported == 1
    d = next(store.iter_decisions())
    assert d.title == "Reason A"


FOUR_TARGETS_GRAPH = {
    "nodes": [
        {
            "id": "rat1",
            "label": "Central retry policy explanation",
            "norm_label": "central retry policy explanation",
            "file_type": "rationale",
            "source_file": "exec.py",
            "community": 1,
        },
        {
            "id": "fn1",
            "label": "fn_one",
            "norm_label": "fn_one",
            "file_type": "code",
            "source_file": "exec.py",
            "community": 1,
        },
        {
            "id": "fn2",
            "label": "fn_two",
            "norm_label": "fn_two",
            "file_type": "code",
            "source_file": "exec.py",
            "community": 1,
        },
        {
            "id": "fn3",
            "label": "fn_three",
            "norm_label": "fn_three",
            "file_type": "code",
            "source_file": "exec.py",
            "community": 1,
        },
        {
            "id": "fn4",
            "label": "fn_four",
            "norm_label": "fn_four",
            "file_type": "code",
            "source_file": "exec.py",
            "community": 1,
        },
    ],
    "links": [
        {"relation": "rationale_for", "source": "rat1", "target": "fn1"},
        {"relation": "rationale_for", "source": "rat1", "target": "fn2"},
        {"relation": "rationale_for", "source": "rat1", "target": "fn3"},
        {"relation": "rationale_for", "source": "rat1", "target": "fn4"},
    ],
}


def test_import_anchors_capped_at_three(tmp_path):
    reader = _reader(tmp_path, FOUR_TARGETS_GRAPH)
    store = Store(tmp_path / "s.db")
    report = import_rationales(store, reader)
    assert report.imported == 1

    d = next(store.iter_decisions())
    leaf_entity_ids = {b.entity_id for b in store.bindings_for_record(d.id) if b.tier == 2}
    assert len(leaf_entity_ids) == 3
    assert store.find_entity("fn_four", "exec.py") is None
