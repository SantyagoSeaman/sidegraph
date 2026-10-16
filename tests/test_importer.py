import json
import textwrap
from datetime import UTC, datetime

import pytest

import sidegraph.importer as importer_module
from sidegraph.capture import RatifyPolicy, redact
from sidegraph.engine.reader import GraphifyReader
from sidegraph.importer import import_rationales
from sidegraph.schema import Decision, DecisionKind, DecisionStatus, Provenance, canonicalize
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
            "auto_ratify_eligible": False,
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


def _rationale_pair(name, source_file, community):
    return [
        {
            "id": f"rat_{name}",
            "label": f"Reason {name}",
            "norm_label": f"reason {name}",
            "file_type": "rationale",
            "source_file": source_file,
            "community": community,
        },
        {
            "id": f"file_{name}",
            "label": source_file,
            "norm_label": source_file,
            "file_type": "document",
            "source_file": source_file,
            "community": community,
        },
    ]


BOUNDARY_GRAPH = {
    "nodes": [
        *_rationale_pair("pay", "payments/a.py", 1),
        *_rationale_pair("pay2", "payments_v2/b.py", 2),
    ],
    "links": [],
}


def test_rationale_path_prefix_respects_directory_boundary(tmp_path):
    reader = _reader(tmp_path, BOUNDARY_GRAPH)
    store = Store(tmp_path / "s.db")
    report = import_rationales(store, reader, path_prefixes=["payments"])
    assert (report.imported, report.filtered) == (1, 1)
    assert [d.title for d in store.iter_decisions()] == ["Reason pay"]

    everything = Store(tmp_path / "all.db")
    report = import_rationales(everything, reader, path_prefixes=[""])
    assert (report.imported, report.filtered) == (2, 0)


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


def _sourced(
    title: str,
    source: str,
    *,
    status: DecisionStatus = DecisionStatus.ACCEPTED,
    supersedes: str | None = None,
) -> Decision:
    now = datetime.now(UTC)
    return Decision(
        title=title,
        kind=DecisionKind.ADR,
        status=status,
        context=f"imported from a.py ({title})",
        choice=title,
        valid_from=now,
        valid_to=now if status == DecisionStatus.DEPRECATED else None,
        supersedes=supersedes,
        provenance=Provenance(source=source, ref="a.py"),
    )


def test_decisions_by_source_keeps_every_status_except_superseded(tmp_path):
    """Missing method is the baseline red; accepted-only/open-only mutants lose real keys."""
    with Store(tmp_path / "s") as store:
        accepted = store.add_decision(_sourced("accepted", "import"))
        proposed = store.add_decision(
            _sourced("proposed", "import", status=DecisionStatus.PROPOSED)
        )
        rejected = store.add_decision(
            _sourced("rejected", "import", status=DecisionStatus.PROPOSED)
        )
        store.drop(rejected.id)
        deprecated = store.add_decision(
            _sourced("deprecated", "import", status=DecisionStatus.DEPRECATED)
        )
        closed = store.add_decision(_sourced("closed", "import"))
        successor = store.add_decision(_sourced("successor", "import", supersedes=closed.id))
        store.add_decision(_sourced("typed by hand", "manual"))
        found = store.decisions_by_source("import")
        by_id = {decision.id: decision.status for decision in found}
        assert by_id == {
            accepted.id: DecisionStatus.ACCEPTED,
            proposed.id: DecisionStatus.PROPOSED,
            rejected.id: DecisionStatus.REJECTED,
            deprecated.id: DecisionStatus.DEPRECATED,
            successor.id: DecisionStatus.ACCEPTED,
        }
        assert closed.id not in by_id
        assert store.get_decision(closed.id).status == DecisionStatus.SUPERSEDED
        assert store.decisions_by_source("unknown") == []


def _cut_096(text: str) -> str:
    """Graphify 0.9.6 _add_rationale: replace line breaks after slicing text[:80], then strip."""
    return text[:80].replace("\r\n", " ").replace("\r", " ").replace("\n", " ").strip()


_DOC = (
    "Retries are idempotent so that a redelivered message never double-charges a customer, "
    "because the broker redelivers on every consumer restart."
)
_LABEL_096 = _cut_096(_DOC)
# Verified newer _shorten_rationale_label form in 0.9.61/0.9.87/0.9.90.
_LABEL_090 = textwrap.shorten(_DOC, width=80, placeholder="…")


def _rationale_graph(*rationales: tuple[str, str, str]) -> dict:
    """One rationale per ``(node_id, file, label)``, in order, each explaining one code node
    of its file (one per file)."""
    nodes: list[dict] = []
    links: list[dict] = []
    targets: dict[str, str] = {}
    for node_id, file_path, label in rationales:
        nodes.append(
            {
                "id": node_id,
                "label": label,
                "file_type": "rationale",
                "source_file": file_path,
                "community": 1,
            }
        )
        if file_path not in targets:
            targets[file_path] = f"fn_{len(targets)}"
            nodes.append(
                {
                    "id": targets[file_path],
                    "label": f"handler_{len(targets)}",
                    "file_type": "code",
                    "source_file": file_path,
                    "community": 1,
                }
            )
        links.append({"relation": "rationale_for", "source": node_id, "target": targets[file_path]})
    return {"built_at_commit": "v1", "nodes": nodes, "links": links}


def _imported_096(tmp_path, name: str, *rationales: tuple[str, str, str]) -> Store:
    store = Store(tmp_path / name)
    graph = _reader(tmp_path, _rationale_graph(*rationales), f"{name}-096.json")
    assert import_rationales(store, graph).imported == len(rationales)
    return store


def _canonical_bytes(root):
    """Read durable records/archives and format, never use the in-memory index as proof."""
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in root.rglob("*")
        if p.is_file() and (p.suffix in {".json", ".jsonl"} or p.name == "format")
    }


@pytest.mark.parametrize("reverse", [False, True], ids=["096-first", "newer-first"])
@pytest.mark.parametrize("dry_run", [False, True], ids=["real", "dry"])
def test_pathless_recut_reimports_only_when_node_identity_changes(tmp_path, reverse, dry_run):
    """Pin the pathless branch of context reconstruction in both cut directions and modes.
    Green on current code; removing context matching for pathless nodes must fail.
    """
    old, new = (_LABEL_090, _LABEL_096) if reverse else (_LABEL_096, _LABEL_090)

    def graph_for(node_id, label):
        return {
            "nodes": [
                {**PATHLESS_GRAPH["nodes"][0], "id": node_id, "label": label},
                PATHLESS_GRAPH["nodes"][1],
            ],
            "links": [{"relation": "rationale_for", "source": node_id, "target": "fn_p"}],
        }

    root = tmp_path / "s"
    store = Store(root)
    assert import_rationales(store, _reader(tmp_path, graph_for("rat_p", old))).imported == 1
    prior = next(store.iter_decisions())
    before = _canonical_bytes(root)
    same = import_rationales(store, _reader(tmp_path, graph_for("rat_p", new)), dry_run=dry_run)
    assert (same.imported, same.skipped_existing) == (0, 1)
    assert _canonical_bytes(root) == before
    assert [d.id for d in store.iter_decisions()] == [prior.id]

    other = import_rationales(
        store, _reader(tmp_path, graph_for("rat_other", new)), dry_run=dry_run
    )
    assert (other.imported, other.skipped_existing) == (1, 0)
    if dry_run:
        assert _canonical_bytes(root) == before
        assert [d.id for d in store.iter_decisions()] == [prior.id]
    else:
        assert {d.provenance.ref for d in store.iter_decisions()} == {"rat_p", "rat_other"}
        assert {d.context for d in store.iter_decisions()} == {
            "imported from rat_p",
            "imported from rat_other",
        }


@pytest.mark.parametrize("reverse", [False, True], ids=["complete-first", "len80-first"])
@pytest.mark.parametrize("dry_run", [False, True], ids=["real", "dry"])
def test_a_complete_prefix_and_a_longer_raw_cut_are_different_rationales(
    tmp_path, reverse, dry_run
):
    """Only the longer observation was cut, so the shorter prefix supplies no cut evidence.
    Green on current code; deleting either shorter-side evidence check must fail.
    """
    short = "Use SQLite for the index."
    extended = _cut_096(
        short + " Keep credentials out of source and retry database writes only when safe."
    )
    assert len(extended) == 80 and extended.startswith(short)
    old, new = (extended, short) if reverse else (short, extended)
    store = _imported_096(tmp_path, "s", ("db_rationale_7", "db.py", old))
    before = _canonical_bytes(tmp_path / "s")
    graph = _reader(tmp_path, _rationale_graph(("db_rationale_7", "db.py", new)))
    result = import_rationales(store, graph, dry_run=dry_run)
    assert (result.imported, result.skipped_existing) == (1, 0)
    if dry_run:
        assert _canonical_bytes(tmp_path / "s") == before
        assert len(list(store.iter_decisions())) == 1
    else:
        assert len(list(store.iter_decisions())) == 2


def test_a_reimport_of_the_same_rationale_cut_by_a_newer_engine_writes_nothing(tmp_path):
    """Red against unfixed code: the 0.9.90 cut changes the title, so it imported again."""
    store = _imported_096(tmp_path, "s", ("exec_rationale_12", "exec.py", _LABEL_096))
    graph = _reader(
        tmp_path, _rationale_graph(("exec_rationale_12", "exec.py", _LABEL_090)), "090.json"
    )
    before = _canonical_bytes(tmp_path / "s")
    again = import_rationales(store, graph)
    assert (again.imported, again.skipped_existing) == (0, 1)
    assert _canonical_bytes(tmp_path / "s") == before
    assert len(list(store.iter_decisions())) == 1


def test_the_dry_run_of_that_reimport_counts_what_the_real_run_does(tmp_path):
    """Red against unfixed code: the dry run counted the re-cut rationale as new."""
    graph_090 = _rationale_graph(("exec_rationale_12", "exec.py", _LABEL_090))
    dry_store = _imported_096(tmp_path, "dry", ("exec_rationale_12", "exec.py", _LABEL_096))
    real_store = _imported_096(tmp_path, "real", ("exec_rationale_12", "exec.py", _LABEL_096))
    dry_before = _canonical_bytes(tmp_path / "dry")
    real_before = _canonical_bytes(tmp_path / "real")
    dry = import_rationales(dry_store, _reader(tmp_path, graph_090, "d.json"), dry_run=True)
    real = import_rationales(real_store, _reader(tmp_path, graph_090, "r.json"))
    assert (dry.imported, dry.skipped_existing) == (real.imported, real.skipped_existing)
    assert (real.imported, real.skipped_existing) == (0, 1)
    assert dry.dry_run == []
    assert _canonical_bytes(tmp_path / "dry") == dry_before
    assert _canonical_bytes(tmp_path / "real") == real_before


def test_a_different_docstring_on_a_reused_line_imports(tmp_path):
    """Green on unfixed code (the title differs). Red against a node-id-only key (discarded):
    the same (file, line) slot now holds another docstring."""
    old = "Use SQLite for the index because it ships with Python and needs no server process."
    new = "Use PostgreSQL for the index because the team already runs it and backs it up nightly."
    store = _imported_096(tmp_path, "s", ("db_rationale_7", "db.py", _cut_096(old)))
    label = textwrap.shorten(new, width=80, placeholder="…")
    graph = _reader(tmp_path, _rationale_graph(("db_rationale_7", "db.py", label)), "g.json")
    assert (import_rationales(store, graph).imported, len(list(store.iter_decisions()))) == (1, 2)


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("Use SQLite for the index.", "Use SQLite for the index. Keep credentials in plaintext."),
        (
            "Use SQLite for the index. Keep credentials out of the repo.",
            "Use SQLite for the index.",
        ),
    ],
    ids=["extended", "shortened"],
)
def test_an_uncut_rationale_that_was_edited_imports(tmp_path, old, new):
    """Green on unfixed code. Red against a plain prefix rule (discarded): neither text was cut
    by Graphify, so a longer or shorter one is an edited docstring and a new memory."""
    store = _imported_096(tmp_path, "s", ("db_rationale_7", "db.py", old))
    graph = _reader(tmp_path, _rationale_graph(("db_rationale_7", "db.py", new)), "g.json")
    assert (import_rationales(store, graph).imported, len(list(store.iter_decisions()))) == (1, 2)


def test_one_rationale_listed_twice_with_two_cuts_writes_once(tmp_path):
    """Red against unfixed code (two records), and against a context map read once and never
    updated within the run (discarded). A repeated node id is the only in-run shape the
    context key can meet. The title-key twin is
    test_importer_dry_run_counts_a_key_repeated_within_the_run_once."""
    graph = _rationale_graph(
        ("exec_rationale_12", "exec.py", _LABEL_096), ("exec_rationale_12", "exec.py", _LABEL_090)
    )
    real_store, dry_store = Store(tmp_path / "real"), Store(tmp_path / "dry")
    real = import_rationales(real_store, _reader(tmp_path, graph, "r.json"))
    dry = import_rationales(dry_store, _reader(tmp_path, graph, "d.json"), dry_run=True)
    assert (real.imported, real.skipped_existing) == (dry.imported, dry.skipped_existing) == (1, 1)
    assert len(list(real_store.iter_decisions())) == 1


def test_the_same_text_in_another_file_still_imports_after_the_cut_change(tmp_path):
    """Red against unfixed code (a.py imported twice), and against a context key without the
    file (discarded: b.py would be skipped). The over-dedup guard for the new key."""
    store = _imported_096(tmp_path, "s", ("a_rationale_3", "a.py", _LABEL_096))
    graph = _rationale_graph(
        ("a_rationale_3", "a.py", _LABEL_090), ("b_rationale_3", "b.py", _LABEL_090)
    )
    again = import_rationales(store, _reader(tmp_path, graph, "g.json"))
    assert (again.imported, again.skipped_existing) == (1, 1)
    assert sorted(d.provenance.ref for d in store.iter_decisions()) == ["a.py", "b.py"]


_COMPLETE = "Use SQLite for local state because it ships with Python and needs no server."
_LONG = _COMPLETE[:-1] + " administration or a separate daemon."


@pytest.mark.parametrize(("old", "new"), [(_LONG, _COMPLETE), (_COMPLETE, _LONG)])
def test_unequal_same_engine_ellipsis_and_complete_edits_import(tmp_path, old, new):
    """Both labels use the same newer engine form. Green on title-only baseline, red
    against the discarded any-evidence-prefix helper; this is distinct from equal-label
    ambiguity, which the pure helper tests intentionally accept."""
    old_label = textwrap.shorten(old, width=80, placeholder="…")
    new_label = textwrap.shorten(new, width=80, placeholder="…")
    assert old_label != new_label
    store = _imported_096(tmp_path, "s", ("db_rationale_7", "db.py", old_label))
    graph = _reader(tmp_path, _rationale_graph(("db_rationale_7", "db.py", new_label)), "new.json")
    result = import_rationales(store, graph)
    assert (result.imported, result.skipped_existing) == (1, 0)
    assert len(list(store.iter_decisions())) == 2


@pytest.mark.parametrize("reverse", [False, True], ids=["ellipsis-first", "len80-first"])
def test_a_bare_ellipsis_does_not_match_a_different_reliable_80_label(tmp_path, reverse):
    """Green on baseline; expected red when the unequal-empty-body guard is deleted.
    A non-80 counterpart is already rejected by the ELL/NONE class rule; this case
    specifically reaches the otherwise valid cross-class prefix branch.
    """
    assert len(_LABEL_096) == 80
    stored, new = (_LABEL_096, "…") if reverse else ("…", _LABEL_096)
    assert canonicalize(stored) != canonicalize(new)
    store = _imported_096(tmp_path, "s", ("exec_rationale_12", "exec.py", stored))
    graph = _reader(tmp_path, _rationale_graph(("exec_rationale_12", "exec.py", new)), "new.json")
    result = import_rationales(store, graph)
    assert (result.imported, result.skipped_existing) == (1, 0)
    assert len(list(store.iter_decisions())) == 2


def test_a_different_node_id_in_the_same_file_is_a_different_context(tmp_path):
    """Green on baseline; red against a context map keyed only by ref/file."""
    assert canonicalize(_LABEL_096) != canonicalize(_LABEL_090)
    store = _imported_096(tmp_path, "s", ("exec_rationale_12", "exec.py", _LABEL_096))
    graph = _reader(
        tmp_path, _rationale_graph(("exec_rationale_99", "exec.py", _LABEL_090)), "other.json"
    )
    result = import_rationales(store, graph)
    assert (result.imported, result.skipped_existing) == (1, 0)
    assert {d.context for d in store.iter_decisions()} == {
        "imported from exec.py (exec_rationale_12)",
        "imported from exec.py (exec_rationale_99)",
    }


def test_context_reconstruction_does_not_parse_parentheses_in_a_path(tmp_path):
    file_path = "src/generated (compat)/exec.py"
    store = _imported_096(tmp_path, "s", ("exec_rationale_12", file_path, _LABEL_096))
    before = _canonical_bytes(tmp_path / "s")
    graph = _reader(
        tmp_path, _rationale_graph(("exec_rationale_12", file_path, _LABEL_090)), "new.json"
    )
    result = import_rationales(store, graph)
    assert (result.imported, result.skipped_existing) == (0, 1)
    assert _canonical_bytes(tmp_path / "s") == before


@pytest.mark.parametrize(
    "prior_status",
    [
        DecisionStatus.ACCEPTED,
        DecisionStatus.PROPOSED,
        DecisionStatus.REJECTED,
        DecisionStatus.DEPRECATED,
    ],
)
@pytest.mark.parametrize("dry_run", [False, True], ids=["real", "dry"])
def test_a_recut_retains_every_prior_status_without_write_or_transition(
    tmp_path, monkeypatch, prior_status, dry_run
):
    root = tmp_path / "s"
    store = Store(root)
    initial_status = (
        DecisionStatus.PROPOSED if prior_status == DecisionStatus.REJECTED else prior_status
    )
    now = datetime.now(UTC)
    prior = store.add_decision(
        Decision(
            title=_LABEL_096,
            kind=DecisionKind.ADR,
            status=initial_status,
            context="imported from exec.py (exec_rationale_12)",
            choice=_LABEL_096,
            valid_from=now,
            valid_to=now if initial_status == DecisionStatus.DEPRECATED else None,
            provenance=Provenance(source="import", ref="exec.py"),
        )
    )
    if prior_status == DecisionStatus.REJECTED:
        store.drop(prior.id)
    assert store.get_decision(prior.id).status == prior_status
    before = _canonical_bytes(root)

    def unexpected_transition(*args, **kwargs):
        raise AssertionError("a skipped existing import must not transition")

    monkeypatch.setattr(importer_module, "_auto_ratify", unexpected_transition)
    monkeypatch.setattr(Store, "ratify", unexpected_transition)
    monkeypatch.setattr(Store, "drop", unexpected_transition)
    graph = _reader(
        tmp_path, _rationale_graph(("exec_rationale_12", "exec.py", _LABEL_090)), "new.json"
    )
    result = import_rationales(
        store, graph, propose=True, dry_run=dry_run, ratify_policy=RatifyPolicy.AUTO_ALL
    )
    assert (result.imported, result.skipped_existing) == (0, 1)
    assert (result.auto_ratified, result.would_auto_ratify, result.auto_ratify_failures) == (
        0,
        0,
        [],
    )
    assert result.dry_run == []
    assert _canonical_bytes(root) == before
    assert store.get_decision(prior.id).status == prior_status
    assert [d.id for d in store.iter_decisions()] == [prior.id]


@pytest.mark.parametrize("reverse", [False, True], ids=["forward", "reverse"])
def test_redaction_manufactured_80_does_not_skip_an_edited_rationale(tmp_path, reverse):
    """Green on baseline; expected red against trusting the marker-bearing side's length.
    Reverse first stores the later raw-cut observation (80 raw, 83 redacted), then
    receives the earlier complete observation (77 raw, 80 redacted). Both are edited
    observations of this context, and their different titles cannot rescue the skip.
    """
    old = "Use token=x for database access. Keep delay bounded and verify results first."
    new = _cut_096(old + " Never cache credentials.")
    assert len(old) == 77
    assert len(redact(old)[0]) == len(new) == 80
    assert len(redact(new)[0]) == 83
    stored_raw, received_raw = (new, old) if reverse else (old, new)
    assert (len(redact(stored_raw)[0]), len(redact(received_raw)[0])) == (
        (83, 80) if reverse else (80, 83)
    )
    assert canonicalize(redact(stored_raw)[0]) != canonicalize(redact(received_raw)[0])
    store = _imported_096(tmp_path, "s", ("db_rationale_7", "db.py", stored_raw))
    graph = _reader(
        tmp_path, _rationale_graph(("db_rationale_7", "db.py", received_raw)), "new.json"
    )
    result = import_rationales(store, graph)
    assert (result.imported, result.skipped_existing) == (1, 0)
    assert len(list(store.iter_decisions())) == 2
    assert all("token=x" not in d.choice for d in store.iter_decisions())


def test_an_unchanged_secret_containing_legacy_cut_can_import_once_more(tmp_path):
    """Accepted private residue: no raw length can be recovered from the stored marker.
    The changed title must not accidentally rescue this case through the legacy key."""
    synthetic_key = "AKIA" + "A" * 16
    text = (
        "Use " + synthetic_key + " for retries.\n      Retries remain idempotent "
        "because the broker may redeliver after every restart."
    )
    old, new = _cut_096(text), textwrap.shorten(text, width=80, placeholder="…")
    assert len(old) == 80
    assert "[REDACTED]" in redact(old)[0]
    assert canonicalize(redact(old)[0]) != canonicalize(redact(new)[0])
    store = _imported_096(tmp_path, "s", ("exec_rationale_12", "exec.py", old))
    graph = _reader(tmp_path, _rationale_graph(("exec_rationale_12", "exec.py", new)), "new.json")
    first = import_rationales(store, graph)
    assert (first.imported, first.skipped_existing) == (1, 0)
    second = import_rationales(store, graph)
    assert (second.imported, second.skipped_existing) == (0, 1)
    assert synthetic_key.encode() not in b"".join(_canonical_bytes(tmp_path / "s").values())


def test_an_unanchorable_occurrence_does_not_consume_a_context_key(tmp_path, monkeypatch):
    graph = _reader(
        tmp_path,
        _rationale_graph(
            ("exec_rationale_12", "exec.py", _LABEL_096),
            ("exec_rationale_12", "exec.py", _LABEL_090),
        ),
        "both.json",
    )
    original = importer_module._select_anchors
    seen = 0

    def first_unanchorable(node, reader):
        nonlocal seen
        seen += 1
        return [] if seen == 1 else original(node, reader)

    monkeypatch.setattr(importer_module, "_select_anchors", first_unanchorable)
    store = Store(tmp_path / "s")
    result = import_rationales(store, graph)
    assert (result.imported, result.skipped_unanchorable, result.skipped_existing) == (1, 1, 0)


def test_import_reads_existing_source_keys_once(tmp_path, monkeypatch):
    store = Store(tmp_path / "s")
    graph = _reader(
        tmp_path,
        _rationale_graph(
            ("a_rationale_1", "a.py", "Use SQLite for the index."),
            ("b_rationale_1", "b.py", "Use retries for delivery."),
        ),
    )
    original = Store.decisions_by_source
    calls = []

    def counted(self, source):
        calls.append(source)
        return original(self, source)

    monkeypatch.setattr(Store, "decisions_by_source", counted)
    assert import_rationales(store, graph).imported == 2
    assert calls == ["import"]


@pytest.mark.parametrize("file_path", ['src/token="abc/a.py', "src/api_key='x/a.py"])
@pytest.mark.parametrize("dry_run", [False, True], ids=["real", "dry"])
@pytest.mark.parametrize("same_run", [False, True], ids=["stored", "in-run"])
def test_context_redaction_cannot_merge_different_nodes(tmp_path, file_path, dry_run, same_run):
    """Red on the initial candidate: an unclosed quote swallows the node id.

    The two labels deliberately miss the legacy title key, so this distinguishes the
    unsafe context-key lookup for both persisted and in-run keys.
    """
    first = ("exec_rationale_12", file_path, _LABEL_096)
    second = ("exec_rationale_99", file_path, _LABEL_090)
    assert canonicalize(_LABEL_096) != canonicalize(_LABEL_090)
    old_context, old_redactions = redact(f"imported from {file_path} ({first[0]})")
    new_context, new_redactions = redact(f"imported from {file_path} ({second[0]})")
    assert old_redactions and new_redactions
    assert old_context == new_context
    if same_run:
        store = Store(tmp_path / "s")
        graph_data = _rationale_graph(first, second)
    else:
        store = _imported_096(tmp_path, "s", first)
        graph_data = _rationale_graph(second)
    before = _canonical_bytes(tmp_path / "s")
    result = import_rationales(store, _reader(tmp_path, graph_data, "next.json"), dry_run=dry_run)
    assert (result.imported, result.skipped_existing) == (2 if same_run else 1, 0)
    assert len(list(store.iter_decisions())) == (0 if dry_run and same_run else 1 if dry_run else 2)
    if dry_run:
        assert _canonical_bytes(tmp_path / "s") == before


@pytest.mark.parametrize("dry_run", [False, True], ids=["real", "dry"])
def test_a_literal_redaction_marker_is_not_evidence_of_node_identity(tmp_path, dry_run):
    """A literal marker may equal a previously redacted node id even with zero new redactions."""
    synthetic_key = "AKIA" + "A" * 16
    old_context, n_old = redact(f"imported from exec.py ({synthetic_key})")
    new_context, n_new = redact("imported from exec.py ([REDACTED])")
    assert (n_old, n_new) == (1, 0)
    assert old_context == new_context
    store = _imported_096(tmp_path, "s", (synthetic_key, "exec.py", _LABEL_096))
    before = _canonical_bytes(tmp_path / "s")
    graph = _reader(tmp_path, _rationale_graph(("[REDACTED]", "exec.py", _LABEL_090)), "next.json")
    result = import_rationales(store, graph, dry_run=dry_run)
    assert (result.imported, result.skipped_existing) == (1, 0)
    if dry_run:
        assert _canonical_bytes(tmp_path / "s") == before
    else:
        assert len(list(store.iter_decisions())) == 2


@pytest.mark.parametrize("reverse", [False, True], ids=["stored-marker", "new-marker"])
@pytest.mark.parametrize("dry_run", [False, True], ids=["real", "dry"])
def test_only_one_redacted_label_keeps_its_own_length_trust(tmp_path, reverse, dry_run):
    """Green on the candidate; swapped/shared flag mutations must fail these directions.

    Existing manufactured-80 cases put markers on both sides, so they cannot distinguish
    using the wrong side's marker to compute a reliability flag.
    """
    marked_raw = "Use retries for delivery and keep them bounded. Set token=x in vault........."
    clean = "Use retries for delivery and keep them bounded.…"
    marked, count = redact(marked_raw)
    assert count == 1 and len(marked) == 80 and len(marked_raw) == 77
    assert "[REDACTED]" not in clean
    stored, new = (clean, marked_raw) if reverse else (marked_raw, clean)
    assert canonicalize(redact(stored)[0]) != canonicalize(redact(new)[0])
    store = _imported_096(tmp_path, "s", ("exec_rationale_12", "exec.py", stored))
    before = _canonical_bytes(tmp_path / "s")
    graph = _reader(tmp_path, _rationale_graph(("exec_rationale_12", "exec.py", new)), "next.json")
    result = import_rationales(store, graph, dry_run=dry_run)
    assert (result.imported, result.skipped_existing) == (1, 0)
    if dry_run:
        assert _canonical_bytes(tmp_path / "s") == before
    else:
        assert len(list(store.iter_decisions())) == 2


@pytest.mark.parametrize("dry_run", [False, True], ids=["real", "dry"])
def test_an_unanchorable_occurrence_does_not_consume_a_title_key(tmp_path, monkeypatch, dry_run):
    """Green on the candidate; moving only the title-key insert before anchors must fail."""
    graph = _reader(
        tmp_path,
        _rationale_graph(
            ("exec_rationale_12", "exec.py", _LABEL_096),
            ("exec_rationale_99", "exec.py", _LABEL_096),
        ),
        "both.json",
    )
    original = importer_module._select_anchors
    calls = 0

    def first_unanchorable(node, reader):
        nonlocal calls
        calls += 1
        return [] if calls == 1 else original(node, reader)

    monkeypatch.setattr(importer_module, "_select_anchors", first_unanchorable)
    store = Store(tmp_path / "s")
    before = _canonical_bytes(tmp_path / "s")
    result = import_rationales(store, graph, dry_run=dry_run)
    assert (result.imported, result.skipped_unanchorable, result.skipped_existing) == (1, 1, 0)
    assert calls == 2
    if dry_run:
        assert _canonical_bytes(tmp_path / "s") == before
    else:
        assert len(list(store.iter_decisions())) == 1


@pytest.mark.parametrize("dry_run", [False, True], ids=["real", "dry"])
def test_a_redacted_context_still_uses_the_legacy_title_key(tmp_path, dry_run):
    """Disabling untrusted context identity must not disable the independent legacy key."""
    file_path = 'src/token="abc/a.py'
    store = _imported_096(tmp_path, "s", ("exec_rationale_12", file_path, _LABEL_096))
    before = _canonical_bytes(tmp_path / "s")
    graph = _reader(
        tmp_path, _rationale_graph(("exec_rationale_99", file_path, _LABEL_096)), "next.json"
    )
    result = import_rationales(store, graph, dry_run=dry_run)
    assert (result.imported, result.skipped_existing) == (0, 1)
    assert _canonical_bytes(tmp_path / "s") == before
