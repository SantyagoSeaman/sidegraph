import json
import os
import sqlite3
from pathlib import Path

import pytest

from sidegraph.cli import import_main
from sidegraph.doc_import import import_docs
from sidegraph.engine.reader import GraphifyReader
from sidegraph.store import Store

GRAPH = {
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

UNANCHORABLE_GRAPH = {
    "nodes": [
        {
            "id": "rat1",
            "label": "Orphan rationale with nothing anchorable",
            "norm_label": "orphan rationale with nothing anchorable",
            "file_type": "rationale",
            "source_file": "orphan.py",
            "community": 1,
        },
    ],
    "links": [],
}


def _write_graph(tmp_path, name, data):
    p = tmp_path / name
    p.write_text(json.dumps(data))
    return p


def test_cli_import_reports_and_writes(tmp_path, capsys):
    graph = _write_graph(tmp_path, "g.json", GRAPH)
    db = tmp_path / "t.db"
    assert import_main(["--db", str(db), "--graph", str(graph)]) == 0
    out = capsys.readouterr().out
    assert "imported 1 decision(s) (skipped: 0 existing, 0 unanchorable)" in out

    store = Store(db)
    decisions = list(store.iter_decisions())
    assert len(decisions) == 1
    assert decisions[0].status.value == "accepted"


def test_cli_import_propose_flag_lands_proposed(tmp_path, capsys):
    graph = _write_graph(tmp_path, "g.json", GRAPH)
    db = tmp_path / "t.db"
    assert import_main(["--db", str(db), "--graph", str(graph), "--propose"]) == 0
    store = Store(db)
    d = next(store.iter_decisions())
    assert d.status.value == "proposed"


def test_cli_import_kind_flag(tmp_path, capsys):
    graph = _write_graph(tmp_path, "g.json", GRAPH)
    db = tmp_path / "t.db"
    assert import_main(["--db", str(db), "--graph", str(graph), "--kind", "lesson"]) == 0
    store = Store(db)
    d = next(store.iter_decisions())
    assert d.kind.value == "lesson"


def test_cli_import_dry_run_writes_nothing(tmp_path, capsys):
    graph = _write_graph(tmp_path, "g.json", GRAPH)
    db = tmp_path / "t.db"
    assert import_main(["--db", str(db), "--graph", str(graph), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "exec.py" in out
    assert "Retries are idempotent" in out
    assert "would import 1 decision(s)" in out

    store = Store(db)
    assert list(store.iter_decisions()) == []


def test_cli_import_idempotent_rerun_reports_skipped_existing(tmp_path, capsys):
    graph = _write_graph(tmp_path, "g.json", GRAPH)
    db = tmp_path / "t.db"
    import_main(["--db", str(db), "--graph", str(graph)])
    capsys.readouterr()
    assert import_main(["--db", str(db), "--graph", str(graph)]) == 0
    out = capsys.readouterr().out
    assert "imported 0 decision(s) (skipped: 1 existing, 0 unanchorable)" in out


def test_cli_import_empty_import_exits_zero(tmp_path, capsys):
    graph = _write_graph(tmp_path, "g.json", UNANCHORABLE_GRAPH)
    db = tmp_path / "t.db"
    assert import_main(["--db", str(db), "--graph", str(graph)]) == 0
    out = capsys.readouterr().out
    assert "imported 0 decision(s) (skipped: 0 existing, 1 unanchorable)" in out


def test_cli_import_unreadable_graph_exits_one(tmp_path, capsys):
    assert import_main(["--db", str(tmp_path / "t.db"), "--graph", str(tmp_path / "no.json")]) == 1
    assert "not readable" in capsys.readouterr().out


def test_cli_import_store_open_failure_exits_one(tmp_path, capsys):
    graph = _write_graph(tmp_path, "g.json", GRAPH)
    db = tmp_path / "bad.db"
    Store(db)  # create a valid store first
    # Store(db) creates `db` as a canonical directory with the derived index at
    # db/index.db (git-native store) — corrupt THAT file, not `db` itself.
    conn = sqlite3.connect(str(db / "index.db"))
    conn.execute("UPDATE meta SET value = 'bogus' WHERE key = 'schema_version'")
    conn.commit()
    conn.close()
    assert import_main(["--db", str(db), "--graph", str(graph)]) == 1
    assert "not readable" in capsys.readouterr().out


_MULTI_FILE_GRAPH = {
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
            "source_file": "docs/adr.md",
            "community": 2,
        },
        {
            "id": "file_b",
            "label": "docs/adr.md",
            "norm_label": "docs/adr.md",
            "file_type": "document",
            "source_file": "docs/adr.md",
            "community": 2,
        },
    ],
    "links": [],
}


def test_cli_import_path_flag(tmp_path, capsys):
    graph = _write_graph(tmp_path, "g.json", _MULTI_FILE_GRAPH)
    db = tmp_path / "t.db"
    assert import_main(["--db", str(db), "--graph", str(graph), "--path", "docs/"]) == 0
    store = Store(db)
    decisions = list(store.iter_decisions())
    assert len(decisions) == 1
    assert decisions[0].title == "Reason B"


def test_cli_import_limit_flag(tmp_path, capsys):
    graph = _write_graph(tmp_path, "g.json", _MULTI_FILE_GRAPH)
    db = tmp_path / "t.db"
    assert import_main(["--db", str(db), "--graph", str(graph), "--limit", "1"]) == 0
    store = Store(db)
    decisions = list(store.iter_decisions())
    assert len(decisions) == 1
    assert decisions[0].title == "Reason A"


def test_cli_import_negative_limit_rejected(tmp_path, capsys):
    graph = _write_graph(tmp_path, "g.json", GRAPH)
    db = tmp_path / "t.db"
    assert import_main(["--db", str(db), "--graph", str(graph), "--limit", "-1"]) == 1
    out = capsys.readouterr().out
    assert "--limit" in out
    # A rejected negative --limit must not create a store as a side effect.
    assert not db.exists()


_PATHLESS_GRAPH = {
    "nodes": [
        {
            "id": "rat_p",
            "label": "Pathless rationale for dry-run cosmetics",
            "norm_label": "pathless rationale for dry-run cosmetics",
            "file_type": "rationale",
            "community": 1,
        },
        {
            "id": "fn_p",
            "label": "do_thing",
            "norm_label": "do_thing",
            "file_type": "code",
            "source_file": "thing.py",
            "community": 1,
        },
    ],
    "links": [
        {"relation": "rationale_for", "source": "rat_p", "target": "fn_p"},
    ],
}


def test_cli_import_dry_run_pathless_rationale_prints_node_id_not_none(tmp_path, capsys):
    graph = _write_graph(tmp_path, "g.json", _PATHLESS_GRAPH)
    db = tmp_path / "t.db"
    assert import_main(["--db", str(db), "--graph", str(graph), "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "rat_p: Pathless rationale for dry-run cosmetics" in out
    assert "None:" not in out


# -- --docs mode (doc-import, importer #2) -------------------------------------------------

_DOC_MENTION_GRAPH = {
    "nodes": [
        {
            "id": "fn_submit",
            "label": "submit_order",
            "norm_label": "submit_order",
            "file_type": "code",
            "source_file": "exec.py",
            "community": 1,
        },
    ],
    "links": [],
}

_FREEFORM_MD = "# Random notes\n\nJust some prose. No decision structure here at all.\n"


def _adr_md(title, decision="Calls `submit_order` on success."):
    return f"# {title}\n\n## Context\n\nSome context text.\n\n## Decision\n\n{decision}\n"


def _write_md(tmp_path, rel, text):
    p = tmp_path / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


def _report_state(report):
    return {
        "imported": report.imported,
        "superseded": report.superseded,
        "skipped_existing": report.skipped_existing,
        "status_derived_proposed": report.status_derived_proposed,
        "status_derived_rejected": report.status_derived_rejected,
    }


def _canonical_doc_state(store):
    state = []
    for decision in store.iter_decisions():
        bindings = store.bindings_for_record(decision.id)
        state.append(
            {
                "title": decision.title,
                "status": decision.status.value,
                "ref": decision.provenance.ref,
                "supersedes": decision.supersedes is not None,
                "bindings": sorted(
                    store.get_entity(binding.entity_id).canonical_name for binding in bindings
                ),
            }
        )
    return sorted(state, key=lambda item: (item["ref"], item["status"]))


@pytest.mark.parametrize(
    ("case", "expected_report", "expected_records"),
    [
        (
            "fresh",
            {
                "imported": 1,
                "superseded": 0,
                "skipped_existing": 0,
                "status_derived_proposed": 0,
                "status_derived_rejected": 0,
            },
            [
                (
                    "Submit path",
                    "accepted",
                    "docs/a.md",
                    False,
                    ("community:1", "submit_order"),
                )
            ],
        ),
        (
            "unchanged",
            {
                "imported": 0,
                "superseded": 0,
                "skipped_existing": 1,
                "status_derived_proposed": 0,
                "status_derived_rejected": 0,
            },
            [
                (
                    "Submit path",
                    "accepted",
                    "docs/a.md",
                    False,
                    ("community:1", "submit_order"),
                )
            ],
        ),
        (
            "edited",
            {
                "imported": 0,
                "superseded": 1,
                "skipped_existing": 0,
                "status_derived_proposed": 0,
                "status_derived_rejected": 0,
            },
            [
                (
                    "Submit path",
                    "accepted",
                    "docs/a.md",
                    True,
                    ("community:1", "submit_order"),
                ),
                (
                    "Submit path",
                    "superseded",
                    "docs/a.md",
                    False,
                    ("community:1", "submit_order"),
                ),
            ],
        ),
        (
            "propose",
            {
                "imported": 1,
                "superseded": 0,
                "skipped_existing": 0,
                "status_derived_proposed": 0,
                "status_derived_rejected": 0,
            },
            [
                (
                    "Submit path",
                    "proposed",
                    "docs/a.md",
                    False,
                    ("community:1", "submit_order"),
                )
            ],
        ),
        (
            "draft",
            {
                "imported": 1,
                "superseded": 0,
                "skipped_existing": 0,
                "status_derived_proposed": 1,
                "status_derived_rejected": 0,
            },
            [
                (
                    "Submit path",
                    "proposed",
                    "docs/a.md",
                    False,
                    ("community:1", "submit_order"),
                )
            ],
        ),
        (
            "rejected",
            {
                "imported": 1,
                "superseded": 0,
                "skipped_existing": 0,
                "status_derived_proposed": 0,
                "status_derived_rejected": 1,
            },
            [
                (
                    "Submit path",
                    "rejected",
                    "docs/a.md",
                    False,
                    ("community:1", "submit_order"),
                )
            ],
        ),
        (
            "split",
            {
                "imported": 3,
                "superseded": 0,
                "skipped_existing": 0,
                "status_derived_proposed": 0,
                "status_derived_rejected": 0,
            },
            [
                (
                    "Architecture",
                    "accepted",
                    "docs/a.md",
                    False,
                    ("community:1", "submit_order"),
                ),
                (
                    "AD-1 Retry",
                    "accepted",
                    "docs/a.md#ad-1-retry",
                    False,
                    ("community:1", "submit_order"),
                ),
                (
                    "AD-2 Queue",
                    "accepted",
                    "docs/a.md#ad-2-queue",
                    False,
                    ("community:1", "submit_order"),
                ),
            ],
        ),
        (
            "anchors",
            {
                "imported": 1,
                "superseded": 0,
                "skipped_existing": 0,
                "status_derived_proposed": 0,
                "status_derived_rejected": 0,
            },
            [
                (
                    "Submit path",
                    "accepted",
                    "docs/a.md",
                    False,
                    ("a.md", "community:1", "submit_order", "tag:payments"),
                )
            ],
        ),
    ],
)
def test_import_docs_characterization_snapshot(
    tmp_path, monkeypatch, case, expected_report, expected_records
):
    monkeypatch.chdir(tmp_path)
    graph_data = json.loads(json.dumps(_DOC_MENTION_GRAPH))
    if case == "anchors":
        graph_data["nodes"].append(
            {
                "id": "doc_a",
                "label": "a.md",
                "norm_label": "a.md",
                "file_type": "document",
                "source_file": "docs/a.md",
                "community": 1,
            }
        )
    graph = _write_graph(tmp_path, "g.json", graph_data)
    store = Store(tmp_path / "t.db")
    reader = GraphifyReader(graph)

    body = _adr_md("Submit path")
    if case == "draft":
        body = f"---\nstatus: draft\n---\n{body}"
    elif case == "rejected":
        body = f"---\nstatus: rejected\n---\n{body}"
    elif case == "split":
        body = (
            "# Architecture\n\n## Design Paradigm\n\nKeep execution deterministic.\n\n"
            "## Invariants & Rules\n\nApply every invariant.\n\n"
            "### AD-1 Retry\n\nCall `submit_order`.\n\n"
            "### AD-2 Queue\n\nQueue work through `submit_order`.\n"
        )
    _write_md(tmp_path, "docs/a.md", body)

    options = {
        "any_doc": True,
        "propose": case == "propose",
        "profile": "bmad" if case == "split" else "generic-adr",
        "tags": ["payments"] if case == "anchors" else None,
    }
    if case in {"unchanged", "edited"}:
        import_docs(store, reader, ["docs/a.md"], **options)
    if case == "edited":
        Path("docs/a.md").write_text(
            _adr_md("Submit path", decision="Retry `submit_order` three times."),
            encoding="utf-8",
        )

    report = import_docs(store, reader, ["docs/a.md"], **options)
    records = [
        (
            item["title"],
            item["status"],
            item["ref"],
            item["supersedes"],
            tuple(item["bindings"]),
        )
        for item in _canonical_doc_state(store)
    ]

    assert _report_state(report) == expected_report
    assert records == expected_records


def test_cli_import_docs_reports_and_writes(tmp_path, capsys):
    graph = _write_graph(tmp_path, "g.json", _DOC_MENTION_GRAPH)
    db = tmp_path / "t.db"
    doc = _write_md(tmp_path, "docs/a.md", _adr_md("Submit path"))

    assert (
        import_main(["--db", str(db), "--graph", str(graph), "--docs", str(doc), "--any-doc"]) == 0
    )
    out = capsys.readouterr().out
    assert (
        "imported 1 decision(s), superseded 0 (skipped: 0 existing, 0 unanchorable, "
        "0 not-decision-shaped, 0 unparseable, 0 superseded-frontmatter, 0 outside-profile)" in out
    )

    store = Store(db)
    decisions = list(store.iter_decisions())
    assert len(decisions) == 1
    assert decisions[0].status.value == "accepted"
    assert decisions[0].provenance.source == "doc-import"


def test_cli_import_docs_directory_recursion_and_mixed_files(tmp_path, capsys):
    graph = _write_graph(tmp_path, "g.json", _DOC_MENTION_GRAPH)
    db = tmp_path / "t.db"
    _write_md(tmp_path, "docs/a.md", _adr_md("Doc A"))
    _write_md(tmp_path, "docs/sub/b.md", _adr_md("Doc B"))
    _write_md(tmp_path, "docs/sub/notes.md", _FREEFORM_MD)

    assert (
        import_main(
            ["--db", str(db), "--graph", str(graph), "--docs", str(tmp_path / "docs"), "--any-doc"]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert (
        "imported 2 decision(s), superseded 0 (skipped: 0 existing, 0 unanchorable, "
        "1 not-decision-shaped, 0 unparseable, 0 superseded-frontmatter, 0 outside-profile)" in out
    )
    store = Store(db)
    assert len(list(store.iter_decisions())) == 2


# -- D7.1: import glob enforcement (design/superpowers/specs/
# 2026-07-30-staleness-machinery-design.md) -- the golden red test: the E8 shape (a graph
# report living under graphify-out/) imports 0 without --any-doc -- measured red, it
# imported 1 before this fix. --------------------------------------------------------------


def test_cli_import_docs_graphify_out_report_not_imported_by_default(tmp_path, capsys, monkeypatch):
    graph = _write_graph(tmp_path, "g.json", _DOC_MENTION_GRAPH)
    db = tmp_path / "t.db"
    doc = _write_md(tmp_path, "graphify-out/report.md", _adr_md("Graphify report"))
    monkeypatch.chdir(tmp_path)

    assert import_main(["--db", str(db), "--graph", str(graph), "--docs", str(doc)]) == 0
    out = capsys.readouterr().out
    assert "imported 0 decision(s)" in out
    assert "1 outside-profile" in out
    assert list(Store(db).iter_decisions()) == []


def test_cli_import_docs_any_doc_flag_restores_graphify_out_import(tmp_path, capsys, monkeypatch):
    graph = _write_graph(tmp_path, "g.json", _DOC_MENTION_GRAPH)
    db = tmp_path / "t.db"
    doc = _write_md(tmp_path, "graphify-out/report.md", _adr_md("Graphify report"))
    monkeypatch.chdir(tmp_path)

    assert (
        import_main(["--db", str(db), "--graph", str(graph), "--docs", str(doc), "--any-doc"]) == 0
    )
    out = capsys.readouterr().out
    assert "imported 1 decision(s)" in out
    assert "0 outside-profile" in out
    assert len(list(Store(db).iter_decisions())) == 1


def test_cli_import_docs_and_path_mutually_exclusive(tmp_path, capsys):
    graph = _write_graph(tmp_path, "g.json", _DOC_MENTION_GRAPH)
    db = tmp_path / "t.db"
    doc = _write_md(tmp_path, "docs/a.md", _adr_md("Doc A"))

    rc = import_main(
        [
            "--db",
            str(db),
            "--graph",
            str(graph),
            "--docs",
            str(doc),
            "--path",
            "docs/",
        ]
    )
    assert rc == 1
    out = capsys.readouterr().out
    # Task 4 (--profile) extended this guard/message to also cover --profile; text updated
    # accordingly (behavior — exit 1, no store created — is unchanged).
    assert "--docs/--profile and --path are mutually exclusive" in out
    assert not db.exists()


def test_cli_import_docs_dry_run_writes_nothing(tmp_path, capsys):
    graph = _write_graph(tmp_path, "g.json", _DOC_MENTION_GRAPH)
    db = tmp_path / "t.db"
    doc = _write_md(tmp_path, "docs/a.md", _adr_md("Submit path"))

    rc = import_main(
        ["--db", str(db), "--graph", str(graph), "--docs", str(doc), "--dry-run", "--any-doc"]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "[imported] Submit path" in out
    assert "would import 1 decision(s), supersede 0" in out

    store = Store(db)
    assert list(store.iter_decisions()) == []


def test_cli_import_docs_missing_path_exits_one(tmp_path, capsys):
    graph = _write_graph(tmp_path, "g.json", _DOC_MENTION_GRAPH)
    db = tmp_path / "t.db"

    rc = import_main(
        [
            "--db",
            str(db),
            "--graph",
            str(graph),
            "--docs",
            str(tmp_path / "nope.md"),
        ]
    )
    assert rc == 1
    out = capsys.readouterr().out
    assert "not found" in out
    assert not db.exists()


def test_cli_import_docs_tag_flag(tmp_path, capsys):
    graph = _write_graph(tmp_path, "g.json", _DOC_MENTION_GRAPH)
    db = tmp_path / "t.db"
    doc = _write_md(tmp_path, "docs/a.md", _adr_md("Submit path"))

    assert (
        import_main(
            [
                "--db",
                str(db),
                "--graph",
                str(graph),
                "--docs",
                str(doc),
                "--tag",
                "payments",
                "--tag",
                "retry",
                "--any-doc",
            ]
        )
        == 0
    )
    store = Store(db)
    d = next(store.iter_decisions())
    tag_names = {
        store.get_entity(b.entity_id).canonical_name
        for b in store.bindings_for_record(d.id)
        if b.tier == 0
    }
    assert tag_names == {"tag:payments", "tag:retry"}


def test_cli_import_docs_kind_and_propose_flags(tmp_path, capsys):
    graph = _write_graph(tmp_path, "g.json", _DOC_MENTION_GRAPH)
    db = tmp_path / "t.db"
    doc = _write_md(tmp_path, "docs/a.md", _adr_md("Submit path"))

    assert (
        import_main(
            [
                "--db",
                str(db),
                "--graph",
                str(graph),
                "--docs",
                str(doc),
                "--kind",
                "lesson",
                "--propose",
                "--any-doc",
            ]
        )
        == 0
    )
    store = Store(db)
    d = next(store.iter_decisions())
    assert d.kind.value == "lesson"
    assert d.status.value == "proposed"


def test_cli_import_docs_root_cause_doc_auto_kind_lesson_without_explicit_kind(tmp_path, capsys):
    # B4: --kind omitted entirely -> auto-detect per document (Root cause -> lesson).
    graph = _write_graph(tmp_path, "g.json", _DOC_MENTION_GRAPH)
    db = tmp_path / "t.db"
    doc = _write_md(
        tmp_path,
        "docs/a.md",
        "# Postmortem\n\n## Root cause\n\nA duplicate submit under partition.\n\n"
        "## Decision\n\nCalls `submit_order` on success.\n",
    )

    assert (
        import_main(["--db", str(db), "--graph", str(graph), "--docs", str(doc), "--any-doc"]) == 0
    )
    store = Store(db)
    assert next(store.iter_decisions()).kind.value == "lesson"


def test_cli_import_docs_explicit_kind_overrides_root_cause_auto_detect(tmp_path, capsys):
    graph = _write_graph(tmp_path, "g.json", _DOC_MENTION_GRAPH)
    db = tmp_path / "t.db"
    doc = _write_md(
        tmp_path,
        "docs/a.md",
        "# Postmortem\n\n## Root cause\n\nA duplicate submit under partition.\n\n"
        "## Decision\n\nCalls `submit_order` on success.\n",
    )

    assert (
        import_main(
            [
                "--db",
                str(db),
                "--graph",
                str(graph),
                "--docs",
                str(doc),
                "--kind",
                "adr",
                "--any-doc",
            ]
        )
        == 0
    )
    store = Store(db)
    assert next(store.iter_decisions()).kind.value == "adr"


def test_cli_import_rationale_mode_kind_still_defaults_to_adr_when_omitted(tmp_path, capsys):
    # Rationale mode is unaffected by B4's --docs-only auto-detect.
    graph = _write_graph(tmp_path, "g.json", GRAPH)
    db = tmp_path / "t.db"
    assert import_main(["--db", str(db), "--graph", str(graph)]) == 0
    store = Store(db)
    assert next(store.iter_decisions()).kind.value == "adr"


def test_cli_import_docs_status_derived_proposed_report_line(tmp_path, capsys):
    graph = _write_graph(tmp_path, "g.json", _DOC_MENTION_GRAPH)
    db = tmp_path / "t.db"
    doc = _write_md(
        tmp_path,
        "docs/a.md",
        "---\nstatus: draft\n---\n# Submit path\n\n## Context\n\nc\n\n"
        "## Decision\n\nCalls `submit_order` on success.\n",
    )

    assert (
        import_main(["--db", str(db), "--graph", str(graph), "--docs", str(doc), "--any-doc"]) == 0
    )
    out = capsys.readouterr().out
    assert "1 landed proposed (source status: draft/proposed/pending/under review)" in out
    store = Store(db)
    assert next(store.iter_decisions()).status.value == "proposed"


def test_cli_import_docs_status_derived_proposed_report_line_dry_run(tmp_path, capsys):
    graph = _write_graph(tmp_path, "g.json", _DOC_MENTION_GRAPH)
    db = tmp_path / "t.db"
    doc = _write_md(
        tmp_path,
        "docs/a.md",
        "---\nstatus: draft\n---\n# Submit path\n\n## Context\n\nc\n\n"
        "## Decision\n\nCalls `submit_order` on success.\n",
    )

    assert (
        import_main(
            ["--db", str(db), "--graph", str(graph), "--docs", str(doc), "--dry-run", "--any-doc"]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "1 would land proposed (source status: draft/proposed/pending/under review)" in out


def test_cli_import_docs_section_limit_flag_applied(tmp_path, capsys):
    graph = _write_graph(tmp_path, "g.json", _DOC_MENTION_GRAPH)
    db = tmp_path / "t.db"
    long_choice = "Calls `submit_order` on success. " + ("word " * 100)
    doc = _write_md(tmp_path, "docs/a.md", _adr_md("Submit path", decision=long_choice))

    assert (
        import_main(
            [
                "--db",
                str(db),
                "--graph",
                str(graph),
                "--docs",
                str(doc),
                "--section-limit",
                "200",
                "--any-doc",
            ]
        )
        == 0
    )
    store = Store(db)
    d = next(store.iter_decisions())
    assert d.choice.endswith(" …[truncated]")
    body = d.choice[: -len(" …[truncated]")]
    assert len(body) <= 200


def test_cli_import_docs_section_limit_below_minimum_rejected(tmp_path, capsys):
    graph = _write_graph(tmp_path, "g.json", _DOC_MENTION_GRAPH)
    db = tmp_path / "t.db"
    doc = _write_md(tmp_path, "docs/a.md", _adr_md("Submit path"))

    rc = import_main(
        [
            "--db",
            str(db),
            "--graph",
            str(graph),
            "--docs",
            str(doc),
            "--section-limit",
            "50",
        ]
    )
    assert rc == 1
    out = capsys.readouterr().out
    assert "--section-limit must be >= 200" in out
    assert not db.exists()  # rejected flag must not create a store as a side effect


def test_cli_import_docs_idempotent_rerun_reports_skipped_existing(tmp_path, capsys):
    graph = _write_graph(tmp_path, "g.json", _DOC_MENTION_GRAPH)
    db = tmp_path / "t.db"
    doc = _write_md(tmp_path, "docs/a.md", _adr_md("Submit path"))

    import_main(["--db", str(db), "--graph", str(graph), "--docs", str(doc), "--any-doc"])
    capsys.readouterr()
    assert (
        import_main(["--db", str(db), "--graph", str(graph), "--docs", str(doc), "--any-doc"]) == 0
    )
    out = capsys.readouterr().out
    assert (
        "imported 0 decision(s), superseded 0 (skipped: 1 existing, 0 unanchorable, "
        "0 not-decision-shaped, 0 unparseable, 0 superseded-frontmatter, 0 outside-profile)" in out
    )


def test_cli_import_docs_edited_doc_reports_superseded(tmp_path, capsys):
    graph = _write_graph(tmp_path, "g.json", _DOC_MENTION_GRAPH)
    db = tmp_path / "t.db"
    doc = _write_md(tmp_path, "docs/a.md", _adr_md("Submit path"))

    import_main(["--db", str(db), "--graph", str(graph), "--docs", str(doc), "--any-doc"])
    capsys.readouterr()

    doc.write_text(_adr_md("Submit path", decision="Now retries `submit_order` 3 times."))
    assert (
        import_main(["--db", str(db), "--graph", str(graph), "--docs", str(doc), "--any-doc"]) == 0
    )
    out = capsys.readouterr().out
    assert "imported 0 decision(s), superseded 1" in out

    store = Store(db)
    assert len(list(store.iter_decisions())) == 2


def test_cli_import_docs_unreadable_graph_exits_one(tmp_path, capsys):
    doc = _write_md(tmp_path, "docs/a.md", _adr_md("Submit path"))
    rc = import_main(
        [
            "--db",
            str(tmp_path / "t.db"),
            "--graph",
            str(tmp_path / "no.json"),
            "--docs",
            str(doc),
        ]
    )
    assert rc == 1
    assert "not readable" in capsys.readouterr().out


def test_cli_import_docs_store_open_failure_exits_one(tmp_path, capsys):
    graph = _write_graph(tmp_path, "g.json", _DOC_MENTION_GRAPH)
    doc = _write_md(tmp_path, "docs/a.md", _adr_md("Submit path"))
    db = tmp_path / "bad.db"
    Store(db)  # create a valid store first
    # Store(db) creates `db` as a canonical directory with the derived index at
    # db/index.db (git-native store) — corrupt THAT file, not `db` itself.
    conn = sqlite3.connect(str(db / "index.db"))
    conn.execute("UPDATE meta SET value = 'bogus' WHERE key = 'schema_version'")
    conn.commit()
    conn.close()
    assert import_main(["--db", str(db), "--graph", str(graph), "--docs", str(doc)]) == 1
    assert "not readable" in capsys.readouterr().out


# -- BUG F: absolute --docs path resolved against os.getcwd() silently misses the graph's
# repo-relative source_file when run from the wrong cwd -> "0 imported, N unanchorable". ------

# Only a document node (no code node to mention), so the doc's OWN file-node is the only
# possible anchor — exactly the case the cwd-relativization must get right.
_DOC_NODE_ONLY_GRAPH = {
    "nodes": [
        {
            "id": "file_doc",
            "label": "a.md",
            "norm_label": "a.md",
            "file_type": "document",
            "source_file": "docs/a.md",
            "community": 1,
        },
    ],
    "links": [],
}

# Real, decision-shaped, but with NO backticked mentions — so the ONLY anchor is the doc node.
_MENTIONLESS_ADR = (
    "# Some doc with no mentions\n\n"
    "## Context\n\nReal context prose, with no backticked code mentions at all.\n\n"
    "## Decision\n\nA real decision statement with nothing anchorable inside it.\n"
)


def test_cli_import_docs_absolute_path_from_wrong_cwd_warns_unanchorable(
    tmp_path, capsys, monkeypatch
):
    # BUG F repro: an absolute --docs path is relativized against os.getcwd() to match the
    # graph's repo-relative source_file. Run from a DIFFERENT cwd, that relativization misses,
    # the doc-node anchor fails, and (with no mention anchors) the doc is silently
    # unanchorable. The CLI must WARN on stderr about the abs-path/cwd footgun.
    repo = tmp_path / "repo"
    repo.mkdir()
    graph = _write_graph(repo, "g.json", _DOC_NODE_ONLY_GRAPH)  # source_file "docs/a.md" rel repo
    db = repo / "t.db"
    doc = _write_md(repo, "docs/a.md", _MENTIONLESS_ADR)

    monkeypatch.chdir(tmp_path)  # NOT the repo root — the cwd mismatch
    rc = import_main(["--db", str(db), "--graph", str(graph), "--docs", str(doc), "--any-doc"])
    assert rc == 0
    captured = capsys.readouterr()
    assert "imported 0 decision(s)" in captured.out
    assert "1 unanchorable" in captured.out
    # The actionable warning goes to stderr, naming the abs-path/repo-root fix.
    assert "unanchorable" in captured.err
    assert "repo root" in captured.err
    assert list(Store(db).iter_decisions()) == []


def test_cli_import_docs_absolute_path_from_repo_root_anchors_and_no_warning(
    tmp_path, capsys, monkeypatch
):
    # No-false-positive / workaround guard: the SAME absolute path, run FROM the repo root,
    # relativizes correctly, the doc-node anchor resolves, the doc imports, and NO warning
    # is emitted (stderr stays clean — the common case must be byte-identical).
    repo = tmp_path / "repo"
    repo.mkdir()
    graph = _write_graph(repo, "g.json", _DOC_NODE_ONLY_GRAPH)
    db = repo / "t.db"
    doc = _write_md(repo, "docs/a.md", _MENTIONLESS_ADR)

    monkeypatch.chdir(repo)  # correct cwd
    rc = import_main(["--db", str(db), "--graph", str(graph), "--docs", str(doc), "--any-doc"])
    assert rc == 0
    captured = capsys.readouterr()
    assert "imported 1 decision(s)" in captured.out
    assert captured.err == ""
    assert len(list(Store(db).iter_decisions())) == 1


# -- --profile (Task 4: dialect selection + ingest-glob expansion) ------------------------


def test_import_profile_expands_ingest_globs(tmp_path, monkeypatch, capsys):
    # A superpowers spec discovered purely via the profile's ingest_globs (no explicit path).
    monkeypatch.chdir(tmp_path)
    (tmp_path / "g.json").write_text(
        json.dumps(
            {
                "nodes": [
                    {
                        "id": "file_pay",
                        "label": "payments.py",
                        "norm_label": "payments.py",
                        "file_type": "code",
                        "source_file": "payments.py",
                        "community": 1,
                    }
                ],
                "links": [],
            }
        )
    )
    spec_dir = tmp_path / "docs" / "superpowers" / "specs"
    spec_dir.mkdir(parents=True)
    (spec_dir / "x.md").write_text(
        "---\nstatus: draft\n---\n# Retry policy\n\n## Goal\n\nStop double charges.\n\n"
        "## Architecture\n\nIdempotency key in `payments.py`.\n"
    )
    rc = import_main(
        ["--docs", "--profile", "superpowers", "--db", str(tmp_path / "s.db"), "--graph", "g.json"]
    )
    assert rc == 0
    assert "imported 1 decision" in capsys.readouterr().out


def test_import_unknown_profile_is_a_clean_usage_error(tmp_path, capsys):
    rc = import_main(
        [
            "--docs",
            "--profile",
            "bogus",
            "--db",
            str(tmp_path / "s.db"),
            "--graph",
            str(tmp_path / "g.json"),
        ]
    )
    assert rc == 1
    assert "unknown flow profile 'bogus'" in capsys.readouterr().out


def test_import_help_profile_list_matches_profiles_registry(capsys):
    # `--help`'s "one of: ..." list must be derived from the PROFILES registry, not a
    # hard-coded string -- a literal copy went stale when `bmad` was added (docs-report
    # item 2: `--profile nonsense` correctly listed all five in its error, but `--help`
    # still only named four).
    import re

    from sidegraph.profiles import PROFILES

    with pytest.raises(SystemExit):
        import_main(["--help"])
    # argparse hyphen-wraps help text to the terminal width (COLUMNS), which can split a
    # hyphenated profile name like "spec-kit" across two lines AT the hyphen itself
    # ("spec-\n                  kit", no space after the hyphen) -- a longer profile
    # name would trip the same wrap at width 80 too, and plain whitespace normalization
    # (" ".join(out.split())) does NOT undo this: split() still breaks "spec-" and "kit"
    # into separate tokens, and rejoining with a space yields "spec- kit", still not a
    # substring match. Collapse "-\n<indent>" back to "-" first (undoing exactly the
    # hyphen-wrap, nothing else -- no other hyphen in this text sits at a line break),
    # then normalize remaining whitespace for the ordinary space-wrapped words. False red
    # only either way: a wrapped name that's genuinely missing still fails.
    out = re.sub(r"-\n\s*", "-", capsys.readouterr().out)
    out = " ".join(out.split())
    for name in PROFILES:
        assert name in out, f"--help is missing profile {name!r}"


# -- Task 6 fix pass: F1/F2 — profile-only glob expansion must yield repo-relative paths --

_PAYMENTS_GRAPH = {
    "nodes": [
        {
            "id": "file_pay",
            "label": "payments.py",
            "norm_label": "payments.py",
            "file_type": "code",
            "source_file": "payments.py",
            "community": 1,
        }
    ],
    "links": [],
}

_PAYMENTS_SPEC = (
    "---\nstatus: draft\n---\n# Retry policy\n\n## Goal\n\nStop double charges.\n\n"
    "## Architecture\n\nIdempotency key in `payments.py`.\n"
)


def _write_payments_spec_fixture(tmp_path):
    (tmp_path / "g.json").write_text(json.dumps(_PAYMENTS_GRAPH))
    spec_dir = tmp_path / "docs" / "superpowers" / "specs"
    spec_dir.mkdir(parents=True)
    (spec_dir / "x.md").write_text(_PAYMENTS_SPEC)


def test_import_profile_ref_is_repo_relative(tmp_path, monkeypatch, capsys):
    # F1's core defect: Path.cwd().glob(...) yields ABSOLUTE paths, which flow into
    # provenance.ref/context — non-deterministic across machines. A profile-only run must
    # write a repo-relative ref instead.
    monkeypatch.chdir(tmp_path)
    _write_payments_spec_fixture(tmp_path)
    db = tmp_path / "s.db"

    rc = import_main(["--docs", "--profile", "superpowers", "--db", str(db), "--graph", "g.json"])
    assert rc == 0
    assert "imported 1 decision" in capsys.readouterr().out

    store = Store(db)
    d = next(store.iter_decisions())
    assert not os.path.isabs(d.provenance.ref)
    assert d.provenance.ref == "docs/superpowers/specs/x.md"


def test_import_profile_then_relative_docs_is_idempotent(tmp_path, monkeypatch, capsys):
    # Cross-invocation idempotency: a profile-only run's ref must dedup against a later,
    # equivalent relative `--docs <path>` run — an absolute ref (the pre-F1 bug) would miss
    # the dedup and duplicate a proposed draft. The dialect must match on both invocations
    # (`--profile superpowers` again) for the doc to parse identically either way — the
    # bug/fix under test here is the glob-expansion path's relativization, not the dialect
    # gate, which `--profile` on the first (glob-discovered) run already exercises.
    monkeypatch.chdir(tmp_path)
    _write_payments_spec_fixture(tmp_path)
    db = tmp_path / "s.db"

    rc1 = import_main(["--docs", "--profile", "superpowers", "--db", str(db), "--graph", "g.json"])
    assert rc1 == 0
    assert "imported 1 decision" in capsys.readouterr().out

    rc2 = import_main(
        [
            "--docs",
            "docs/superpowers/specs/x.md",
            "--profile",
            "superpowers",
            "--db",
            str(db),
            "--graph",
            "g.json",
        ]
    )
    assert rc2 == 0
    out2 = capsys.readouterr().out
    assert "imported 0 decision" in out2
    assert "1 existing" in out2
    assert len(list(Store(db).iter_decisions())) == 1


# -- Task 6 fix pass: F3 — bare `--docs` (no PATH, no --profile) contract -----------------


def test_import_bare_docs_uses_default_profile_globs(tmp_path, monkeypatch, capsys):
    # Bare `--docs` (no PATH) is legal even without --profile: it expands the ACTIVE
    # profile's ingest globs — generic-adr's by default (docs/adr/*.md, docs/decisions/*.md).
    monkeypatch.chdir(tmp_path)
    (tmp_path / "g.json").write_text(
        json.dumps(
            {
                "nodes": [
                    {
                        "id": "fn_submit",
                        "label": "submit_order",
                        "norm_label": "submit_order",
                        "file_type": "code",
                        "source_file": "exec.py",
                        "community": 1,
                    }
                ],
                "links": [],
            }
        )
    )
    adr_dir = tmp_path / "docs" / "adr"
    adr_dir.mkdir(parents=True)
    (adr_dir / "0001.md").write_text(
        "# Retry on failure\n\n## Context\n\nOrders can fail transiently.\n\n"
        "## Decision\n\nCalls `submit_order` on success.\n"
    )

    rc = import_main(["--docs", "--db", str(tmp_path / "s.db"), "--graph", "g.json"])
    assert rc == 0
    assert "imported 1 decision" in capsys.readouterr().out


# -- rejected source status (v0.2-scope item 7) ----------------------------------------------
#
# A doc the team TURNED DOWN was landing `accepted`: `_is_draft_like_status` does not match
# "rejected", so it fell through to the --propose-controlled default. The scope note says
# "add the marker", but adding `reject` to the draft markers would label a turned-down ADR
# "proposed, awaiting review" and push it into the human ratification queue — the opposite
# of what happened. It lands `rejected`: closed, and still retrievable as "tried, turned
# down, here is why", which is what the store exists to keep.


def test_cli_import_docs_rejected_status_lands_rejected(tmp_path, capsys):
    graph = _write_graph(tmp_path, "g.json", _DOC_MENTION_GRAPH)
    db = tmp_path / "t.db"
    doc = _write_md(
        tmp_path,
        "docs/a.md",
        "---\nstatus: rejected\n---\n# Submit path\n\n## Context\n\nc\n\n"
        "## Decision\n\nCalls `submit_order` on success.\n",
    )

    assert (
        import_main(["--db", str(db), "--graph", str(graph), "--docs", str(doc), "--any-doc"]) == 0
    )
    out = capsys.readouterr().out
    assert "1 landed rejected (source status: rejected)" in out
    store = Store(db)
    assert next(store.iter_decisions()).status.value == "rejected"


def test_cli_import_docs_crlf_frontmatter_preserves_rejected_status(tmp_path, capsys):
    graph = _write_graph(tmp_path, "g.json", _DOC_MENTION_GRAPH)
    db = tmp_path / "t.db"
    doc = tmp_path / "docs/a.md"
    doc.parent.mkdir(parents=True)
    doc.write_bytes(
        b"---\r\nstatus: rejected\r\n---\r\n# Submit path\r\n\r\n"
        b"## Context\r\n\r\nc\r\n\r\n"
        b"## Decision\r\n\r\nCalls `submit_order` on success.\r\n"
    )

    assert (
        import_main(["--db", str(db), "--graph", str(graph), "--docs", str(doc), "--any-doc"]) == 0
    )
    assert next(Store(db).iter_decisions()).status.value == "rejected"


def test_cli_import_docs_rejected_status_dry_run_line(tmp_path, capsys):
    graph = _write_graph(tmp_path, "g.json", _DOC_MENTION_GRAPH)
    db = tmp_path / "t.db"
    doc = _write_md(
        tmp_path,
        "docs/a.md",
        "---\nstatus: Rejected in favour of ADR-9999\n---\n# Submit path\n\n## Context\n\nc\n\n"
        "## Decision\n\nCalls `submit_order` on success.\n",
    )

    assert (
        import_main(
            ["--db", str(db), "--graph", str(graph), "--docs", str(doc), "--any-doc", "--dry-run"]
        )
        == 0
    )
    assert "1 would land rejected (source status: rejected)" in capsys.readouterr().out


def test_cli_import_docs_rejected_beats_propose_flag(tmp_path, capsys):
    """`--propose` cannot resurrect a turned-down doc as a pending proposal."""
    graph = _write_graph(tmp_path, "g.json", _DOC_MENTION_GRAPH)
    db = tmp_path / "t.db"
    doc = _write_md(
        tmp_path,
        "docs/a.md",
        "---\nstatus: rejected\n---\n# Submit path\n\n## Context\n\nc\n\n"
        "## Decision\n\nCalls `submit_order` on success.\n",
    )
    assert (
        import_main(
            ["--db", str(db), "--graph", str(graph), "--docs", str(doc), "--any-doc", "--propose"]
        )
        == 0
    )
    assert next(Store(db).iter_decisions()).status.value == "rejected"


def test_cli_import_docs_rejected_wins_over_a_draft_marker(tmp_path, capsys):
    """ "Proposed, then rejected" matches BOTH marker sets; the terminal state wins."""
    graph = _write_graph(tmp_path, "g.json", _DOC_MENTION_GRAPH)
    db = tmp_path / "t.db"
    doc = _write_md(
        tmp_path,
        "docs/a.md",
        "---\nstatus: proposed, then rejected\n---\n# Submit path\n\n## Context\n\nc\n\n"
        "## Decision\n\nCalls `submit_order` on success.\n",
    )
    assert (
        import_main(["--db", str(db), "--graph", str(graph), "--docs", str(doc), "--any-doc"]) == 0
    )
    assert next(Store(db).iter_decisions()).status.value == "rejected"


def test_cli_import_docs_superseded_still_skips_a_rejected_doc(tmp_path, capsys):
    """A doc that is BOTH superseded and rejected stays a skip: history docs are not
    re-imported at all, which is the older and stricter rule."""
    graph = _write_graph(tmp_path, "g.json", _DOC_MENTION_GRAPH)
    db = tmp_path / "t.db"
    doc = _write_md(
        tmp_path,
        "docs/a.md",
        "---\nstatus: superseded, rejected\n---\n# Submit path\n\n## Context\n\nc\n\n"
        "## Decision\n\nCalls `submit_order` on success.\n",
    )
    assert (
        import_main(["--db", str(db), "--graph", str(graph), "--docs", str(doc), "--any-doc"]) == 0
    )
    assert list(Store(db).iter_decisions()) == []


def test_cli_import_docs_rejected_is_idempotent(tmp_path, capsys):
    """Review finding 1: a REJECTED record is invisible to the default open-only ref
    lookup, so an unchanged rejected doc was re-imported as a brand-new record on every
    run — three runs, three identical records, and the report line said "0 existing"."""
    graph = _write_graph(tmp_path, "g.json", _DOC_MENTION_GRAPH)
    db = tmp_path / "t.db"
    doc = _write_md(
        tmp_path,
        "docs/a.md",
        "---\nstatus: rejected\n---\n# Submit path\n\n## Context\n\nc\n\n"
        "## Decision\n\nCalls `submit_order` on success.\n",
    )
    argv = ["--db", str(db), "--graph", str(graph), "--docs", str(doc), "--any-doc"]
    for _ in range(3):
        assert import_main(argv) == 0
    capsys.readouterr()
    assert len(list(Store(db).iter_decisions())) == 1


def test_cli_import_docs_status_flip_to_rejected_closes_the_record(tmp_path, capsys):
    """Review finding 2: status is not part of the idempotency content tuple, so flipping
    a doc's frontmatter to `rejected` was `skipped_existing` and the store kept asserting
    the team had adopted what the corpus now says it refused. Real corpora reject an ADR
    after first import far more often than an ADR is born rejected."""
    graph = _write_graph(tmp_path, "g.json", _DOC_MENTION_GRAPH)
    db = tmp_path / "t.db"
    body = "# Submit path\n\n## Context\n\nc\n\n## Decision\n\nCalls `submit_order` on success.\n"
    doc = _write_md(tmp_path, "docs/a.md", f"---\nstatus: accepted\n---\n{body}")
    argv = ["--db", str(db), "--graph", str(graph), "--docs", str(doc), "--any-doc"]
    assert import_main(argv) == 0
    assert next(Store(db).iter_decisions()).status.value == "accepted"

    doc.write_text(f"---\nstatus: rejected\n---\n{body}", encoding="utf-8")
    assert import_main(argv) == 0
    capsys.readouterr()
    records = sorted(Store(db).iter_decisions(), key=lambda d: d.id)
    # Append-only: the adopted record is SUPERSEDED by a rejected successor, never flipped
    # in place — `accepted -> rejected` is not a legal transition (verify.py), and the
    # history ("this was accepted, then the corpus turned it down") is the point.
    assert [d.status.value for d in records] == ["superseded", "rejected"]
    assert records[1].supersedes == records[0].id
    assert records[1].valid_to is not None


def test_cli_import_docs_rejected_matcher_ignores_lookalikes(tmp_path, capsys):
    """Review finding 3, measured false positives on the bare substring: an aside about a
    turned-down ALTERNATIVE, a negation, and a different word all landed `rejected` —
    silently and unrecoverably, since a closed record never surfaces for anyone to notice."""
    graph = _write_graph(tmp_path, "g.json", _DOC_MENTION_GRAPH)
    for i, status in enumerate(
        ("Accepted (rejected alternative: gRPC)", "not rejected", "rejection criteria defined")
    ):
        db = tmp_path / f"t{i}.db"
        doc = _write_md(
            tmp_path,
            f"docs/a{i}.md",
            f"---\nstatus: {status}\n---\n# Submit path\n\n## Context\n\nc\n\n"
            "## Decision\n\nCalls `submit_order` on success.\n",
        )
        assert (
            import_main(["--db", str(db), "--graph", str(graph), "--docs", str(doc), "--any-doc"])
            == 0
        )
        capsys.readouterr()
        assert next(Store(db).iter_decisions()).status.value != "rejected", status


def test_cli_import_docs_rejected_record_carries_valid_to(tmp_path, capsys):
    """Review finding 11: a record that lands already closed must carry its close time,
    like every other terminal record — otherwise it is the only rejected record in the
    store that reads as still-valid to anything checking validity rather than status."""
    graph = _write_graph(tmp_path, "g.json", _DOC_MENTION_GRAPH)
    db = tmp_path / "t.db"
    doc = _write_md(
        tmp_path,
        "docs/a.md",
        "---\nstatus: rejected\n---\n# Submit path\n\n## Context\n\nc\n\n"
        "## Decision\n\nCalls `submit_order` on success.\n",
    )
    assert (
        import_main(["--db", str(db), "--graph", str(graph), "--docs", str(doc), "--any-doc"]) == 0
    )
    capsys.readouterr()
    assert next(Store(db).iter_decisions()).valid_to is not None


def test_cli_import_docs_dry_run_does_not_perform_the_status_flip(tmp_path, capsys):
    """Review R2-1: the flip branch used to write BEFORE the dry-run check, so a dry run
    performed the supersession — unrecoverably, the store being append-only — and the
    human's go/no-go gate in the import-adrs skill was reporting work already done."""
    graph = _write_graph(tmp_path, "g.json", _DOC_MENTION_GRAPH)
    db = tmp_path / "t.db"
    body = "# Submit path\n\n## Context\n\nc\n\n## Decision\n\nCalls `submit_order` on success.\n"
    doc = _write_md(tmp_path, "docs/a.md", f"---\nstatus: accepted\n---\n{body}")
    argv = ["--db", str(db), "--graph", str(graph), "--docs", str(doc), "--any-doc"]
    assert import_main(argv) == 0

    doc.write_text(f"---\nstatus: rejected\n---\n{body}", encoding="utf-8")
    assert import_main([*argv, "--dry-run"]) == 0
    capsys.readouterr()
    records = list(Store(db).iter_decisions())
    assert [d.status.value for d in records] == ["accepted"]  # untouched by the dry run

    assert import_main(argv) == 0  # the real run still has work to do
    capsys.readouterr()
    assert sorted(d.status.value for d in Store(db).iter_decisions()) == ["rejected", "superseded"]


def test_cli_import_docs_flipped_successor_carries_anchors(tmp_path, capsys):
    """Review R2-2: written outside the normal path the successor had zero bindings, so a
    record that exists in the store surfaces nowhere — worse than not importing it."""
    graph = _write_graph(tmp_path, "g.json", _DOC_MENTION_GRAPH)
    db = tmp_path / "t.db"
    body = "# Submit path\n\n## Context\n\nc\n\n## Decision\n\nCalls `submit_order` on success.\n"
    doc = _write_md(tmp_path, "docs/a.md", f"---\nstatus: accepted\n---\n{body}")
    argv = ["--db", str(db), "--graph", str(graph), "--docs", str(doc), "--any-doc"]
    assert import_main(argv) == 0
    doc.write_text(f"---\nstatus: rejected\n---\n{body}", encoding="utf-8")
    assert import_main(argv) == 0
    capsys.readouterr()

    store = Store(db)
    rejected = next(d for d in store.iter_decisions() if d.status.value == "rejected")
    assert store.bindings_for_record(rejected.id), "a rejected successor with no anchors"


def test_cli_import_docs_un_rejecting_a_doc_revives_it(tmp_path, capsys):
    """Review R3-1: handling only accepted→rejected left the mirror hole. Once rejected
    records became visible to the ref lookup (fix 1), a doc un-rejected back to `accepted`
    matched on content and was skipped FOREVER — a regression against the pre-branch
    behaviour, and this branch's own distortion pointed the other way: the corpus says
    adopted, the store keeps saying refused."""
    graph = _write_graph(tmp_path, "g.json", _DOC_MENTION_GRAPH)
    db = tmp_path / "t.db"
    body = "# Submit path\n\n## Context\n\nc\n\n## Decision\n\nCalls `submit_order` on success.\n"
    doc = _write_md(tmp_path, "docs/a.md", f"---\nstatus: rejected\n---\n{body}")
    argv = ["--db", str(db), "--graph", str(graph), "--docs", str(doc), "--any-doc"]
    assert import_main(argv) == 0
    assert [d.status.value for d in Store(db).iter_decisions()] == ["rejected"]

    doc.write_text(f"---\nstatus: accepted\n---\n{body}", encoding="utf-8")
    assert import_main(argv) == 0
    capsys.readouterr()
    statuses = sorted(d.status.value for d in Store(db).iter_decisions())
    assert statuses == ["accepted", "rejected"]  # the refusal stays as history

    assert import_main(argv) == 0  # and the revival is itself idempotent
    capsys.readouterr()
    assert len(list(Store(db).iter_decisions())) == 2


def test_cli_import_docs_status_close_survives_lost_anchors(tmp_path, capsys):
    """Review: a status change was being dropped by `skipped_unanchorable` — an anchoring
    concern blocking the one operation whose job is to remove a false claim, while the
    predecessor already had perfectly good bindings. Reachable whenever someone re-imports
    before rebuilding the graph."""
    graph = _write_graph(tmp_path, "g.json", _DOC_MENTION_GRAPH)
    db = tmp_path / "t.db"
    body = "# Submit path\n\n## Context\n\nc\n\n## Decision\n\nCalls `submit_order` on success.\n"
    doc = _write_md(tmp_path, "docs/a.md", f"---\nstatus: accepted\n---\n{body}")
    argv = ["--db", str(db), "--graph", str(graph), "--docs", str(doc), "--any-doc"]
    assert import_main(argv) == 0

    empty_graph = _write_graph(tmp_path, "empty.json", {"nodes": [], "edges": []})
    doc.write_text(f"---\nstatus: rejected\n---\n{body}", encoding="utf-8")
    assert (
        import_main(["--db", str(db), "--graph", str(empty_graph), "--docs", str(doc), "--any-doc"])
        == 0
    )
    capsys.readouterr()
    store = Store(db)
    statuses = sorted(d.status.value for d in store.iter_decisions())
    assert statuses == ["rejected", "superseded"], "the false claim must not survive"
    rejected = next(d for d in store.iter_decisions() if d.status.value == "rejected")
    assert store.bindings_for_record(rejected.id), "inherited the predecessor's anchors"


def test_cli_import_docs_status_round_trip_is_tracked_every_time(tmp_path, capsys):
    """Review R4-1: preferring the content match whose status AGREES with the source made
    `needs_status_change` false by construction — a re-rejection after a revival selected
    the OLD rejected record and never looked at the open accepted one, so the store kept
    asserting an adoption the corpus had retracted, permanently. The live record's state is
    what matters; history must not shield it."""
    graph = _write_graph(tmp_path, "g.json", _DOC_MENTION_GRAPH)
    db = tmp_path / "t.db"
    body = "# Submit path\n\n## Context\n\nc\n\n## Decision\n\nCalls `submit_order` on success.\n"
    doc = _write_md(tmp_path, "docs/a.md", f"---\nstatus: rejected\n---\n{body}")
    argv = ["--db", str(db), "--graph", str(graph), "--docs", str(doc), "--any-doc"]

    def statuses():
        capsys.readouterr()
        return sorted(d.status.value for d in Store(db).iter_decisions())

    assert import_main(argv) == 0
    assert statuses() == ["rejected"]

    doc.write_text(f"---\nstatus: accepted\n---\n{body}", encoding="utf-8")
    assert import_main(argv) == 0
    assert statuses() == ["accepted", "rejected"]

    doc.write_text(f"---\nstatus: rejected\n---\n{body}", encoding="utf-8")
    assert import_main(argv) == 0
    # the revival is closed and a fresh refusal recorded — no live record claims adoption
    assert statuses() == ["rejected", "rejected", "superseded"]

    assert import_main(argv) == 0  # and the round trip settles: no churn on a re-run
    assert statuses() == ["rejected", "rejected", "superseded"]


def test_cli_import_docs_revival_links_to_what_it_revives(tmp_path, capsys):
    """Review ask 3: `add_decision` leaves an already-terminal predecessor untouched, so
    the link is recorded without an illegal rejected -> superseded transition — and the
    lineage reads correctly instead of stranding an orphan beside its own history."""
    graph = _write_graph(tmp_path, "g.json", _DOC_MENTION_GRAPH)
    db = tmp_path / "t.db"
    body = "# Submit path\n\n## Context\n\nc\n\n## Decision\n\nCalls `submit_order` on success.\n"
    doc = _write_md(tmp_path, "docs/a.md", f"---\nstatus: rejected\n---\n{body}")
    argv = ["--db", str(db), "--graph", str(graph), "--docs", str(doc), "--any-doc"]
    assert import_main(argv) == 0
    doc.write_text(f"---\nstatus: accepted\n---\n{body}", encoding="utf-8")
    assert import_main(argv) == 0
    capsys.readouterr()

    store = Store(db)
    records = {d.status.value: d for d in store.iter_decisions()}
    assert records["accepted"].supersedes == records["rejected"].id
    assert records["rejected"].status.value == "rejected"  # untouched, still history


def test_cli_import_docs_edited_and_flipped_with_dead_anchors_still_closes(tmp_path, capsys):
    """Review R4-2: inheritance keyed on a CONTENT match was unreachable when the doc was
    edited and flipped at once — no match, so the false claim survived behind the same
    misleading cwd warning. The donor is found by ref now. A plain content edit with dead
    anchors is still `skipped_unanchorable`: only a write that changes what the store
    CLAIMS may proceed on borrowed anchors."""
    graph = _write_graph(tmp_path, "g.json", _DOC_MENTION_GRAPH)
    db = tmp_path / "t.db"
    doc = _write_md(
        tmp_path,
        "docs/a.md",
        "---\nstatus: accepted\n---\n# Submit path\n\n## Context\n\nc\n\n"
        "## Decision\n\nCalls `submit_order` on success.\n",
    )
    argv = ["--db", str(db), "--graph", str(graph), "--docs", str(doc), "--any-doc"]
    assert import_main(argv) == 0

    empty = _write_graph(tmp_path, "empty.json", {"nodes": [], "edges": []})
    doc.write_text(
        "---\nstatus: rejected\n---\n# Submit path\n\n## Context\n\nEDITED context\n\n"
        "## Decision\n\nCalls `submit_order` on success, with a caveat.\n",
        encoding="utf-8",
    )
    assert (
        import_main(["--db", str(db), "--graph", str(empty), "--docs", str(doc), "--any-doc"]) == 0
    )
    capsys.readouterr()
    store = Store(db)
    statuses = sorted(d.status.value for d in store.iter_decisions())
    assert statuses == ["rejected", "superseded"], "the retracted claim must not survive"
    rejected = next(d for d in store.iter_decisions() if d.status.value == "rejected")
    assert store.bindings_for_record(rejected.id)


def test_cli_import_docs_propose_flip_leaves_a_linked_chain(tmp_path, capsys):
    """Review R3-2: under `--propose` the flip reached the pre-existing
    edit-again-while-pending path, which drops the proposal itself, leaving two unlinked
    rejected records and a report claiming a supersession of nothing."""
    graph = _write_graph(tmp_path, "g.json", _DOC_MENTION_GRAPH)
    db = tmp_path / "t.db"
    body = "# Submit path\n\n## Context\n\nc\n\n## Decision\n\nCalls `submit_order` on success.\n"
    doc = _write_md(tmp_path, "docs/a.md", f"---\nstatus: accepted\n---\n{body}")
    argv = ["--db", str(db), "--graph", str(graph), "--docs", str(doc), "--any-doc", "--propose"]
    assert import_main(argv) == 0
    assert [d.status.value for d in Store(db).iter_decisions()] == ["proposed"]

    doc.write_text(f"---\nstatus: rejected\n---\n{body}", encoding="utf-8")
    assert import_main(argv) == 0
    capsys.readouterr()
    store = Store(db)
    records = sorted(store.iter_decisions(), key=lambda d: d.id)
    assert len(records) == 2
    assert records[1].supersedes == records[0].id, "the chain must be linked, not two orphans"
    assert records[1].status.value == "rejected"


def test_cli_import_docs_revival_reports_imported_not_superseded(tmp_path, capsys):
    """Review R5-1: reverting the `supersedable` keying leaves the whole suite green while
    a revival reports `superseded 1` for a record that supersedes nothing — this function
    regressed on exactly this kind of thing in three of five review rounds, so the label
    gets an assertion of its own."""
    graph = _write_graph(tmp_path, "g.json", _DOC_MENTION_GRAPH)
    db = tmp_path / "t.db"
    body = "# Submit path\n\n## Context\n\nc\n\n## Decision\n\nCalls `submit_order` on success.\n"
    doc = _write_md(tmp_path, "docs/a.md", f"---\nstatus: rejected\n---\n{body}")
    argv = ["--db", str(db), "--graph", str(graph), "--docs", str(doc), "--any-doc"]
    assert import_main(argv) == 0
    capsys.readouterr()

    doc.write_text(f"---\nstatus: accepted\n---\n{body}", encoding="utf-8")
    assert import_main(argv) == 0
    out = capsys.readouterr().out
    assert "imported 1 decision(s), superseded 0" in out, out
