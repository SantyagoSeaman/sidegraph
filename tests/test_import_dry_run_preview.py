"""The import dry run says what auto-ratification would do (both import paths).

``import_rationales`` and ``import_docs`` decide auto-ratification only after a real write, so
``--dry-run`` never said which records a real run would land accepted without a human. The
dry run now builds the same ``AutoEligibility`` signal from the inputs the write uses and
reports the verdict in a separate ``would_auto_ratify`` count; ``auto_ratified`` keeps its
meaning ("transitions that happened") and stays 0.

# see design/superpowers/specs/2026-10-04-import-dry-run-previews-auto-ratification-design.md
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

import pytest

from sidegraph.capture import RatifyPolicy
from sidegraph.cli import import_main
from sidegraph.doc_import import import_docs
from sidegraph.engine.reader import GraphifyReader
from sidegraph.importer import import_rationales
from sidegraph.store import Store
from tests.test_cli_import import _DOC_MENTION_GRAPH, _adr_md
from tests.test_cli_import import GRAPH as _RATIONALE_GRAPH
from tests.test_doc_import import _reader, _write_md
from tests.test_ratify_policy import _importer_graph

# -- code-rationale import -----------------------------------------------------------


def test_importer_dry_run_would_auto_ratify_equals_the_real_runs_count(tmp_path):
    """T1. The kind/policy/propose/anchor fixture whose real run auto-ratifies both records:
    the dry run reports the same count in ``would_auto_ratify``, marks every item eligible,
    and leaves ``auto_ratified`` at 0 (the 2026-09-11 spec's T16 meaning)."""
    reader = _importer_graph(tmp_path)
    with Store(tmp_path / "dry.db") as dry_store, Store(tmp_path / "real.db") as real_store:
        dry = import_rationales(
            dry_store,
            reader,
            kind="gotcha",
            propose=True,
            dry_run=True,
            ratify_policy=RatifyPolicy.AUTO_ALL,
        )
        real = import_rationales(
            real_store, reader, kind="gotcha", propose=True, ratify_policy=RatifyPolicy.AUTO_ALL
        )
    assert real.auto_ratified == 2
    assert dry.would_auto_ratify == real.auto_ratified
    assert dry.auto_ratified == 0
    assert dry.auto_ratify_failures == []
    assert len(dry.dry_run) == 2
    assert all(item["auto_ratify_eligible"] is True for item in dry.dry_run)


def test_importer_dry_run_ineligible_kind_reports_zero(tmp_path):
    """T2 (guard against over-reporting). ``adr`` is outside ``auto-low-risk``'s kinds: the
    real run auto-ratifies nothing, so the dry run must not say it would, per item or in
    total."""
    reader = _importer_graph(tmp_path)
    with Store(tmp_path / "dry.db") as dry_store, Store(tmp_path / "real.db") as real_store:
        dry = import_rationales(
            dry_store,
            reader,
            kind="adr",
            propose=True,
            dry_run=True,
            ratify_policy=RatifyPolicy.AUTO_LOW_RISK,
        )
        real = import_rationales(
            real_store, reader, kind="adr", propose=True, ratify_policy=RatifyPolicy.AUTO_LOW_RISK
        )
    assert real.imported == 2
    assert real.auto_ratified == 0
    assert dry.would_auto_ratify == 0
    assert dry.imported == 2
    assert all(item["auto_ratify_eligible"] is False for item in dry.dry_run)


def test_importer_dry_run_without_propose_reports_zero(tmp_path):
    """T3 (guard). Without ``propose`` a record lands accepted and never reaches a ratify
    transition, whatever the policy and kind: ``auto-all`` + ``gotcha`` is eligible by
    ``auto_ratify_eligible`` alone, so only the importer's own ``propose`` gate keeps the
    dry run from over-reporting."""
    reader = _importer_graph(tmp_path)
    with Store(tmp_path / "dry.db") as dry_store, Store(tmp_path / "real.db") as real_store:
        dry = import_rationales(
            dry_store,
            reader,
            kind="gotcha",
            propose=False,
            dry_run=True,
            ratify_policy=RatifyPolicy.AUTO_ALL,
        )
        real = import_rationales(
            real_store, reader, kind="gotcha", propose=False, ratify_policy=RatifyPolicy.AUTO_ALL
        )
    assert real.imported == 2
    assert real.auto_ratified == 0
    assert dry.would_auto_ratify == 0
    assert all(item["auto_ratify_eligible"] is False for item in dry.dry_run)


_DUPLICATE_KEY_GRAPH: dict = {
    "built_at_commit": "v1",
    "nodes": [
        # rat_a and rat_a2 share the first line AND the file: one identity key
        # (canonical title, provenance.ref), so the real run writes the first and skips the
        # second through ``find_decision_by_title``.
        {
            "id": "rat_a",
            "label": "Retries are idempotent to survive at-least-once delivery",
            "file_type": "rationale",
            "source_file": "exec.py",
            "community": 1,
        },
        {
            "id": "rat_a2",
            "label": "Retries are idempotent to survive at-least-once delivery",
            "file_type": "rationale",
            "source_file": "exec.py",
            "community": 1,
        },
        {
            "id": "fn_submit",
            "label": "submit_order",
            "file_type": "code",
            "source_file": "exec.py",
            "community": 1,
        },
        {
            "id": "rat_b",
            "label": "Backoff caps retries to avoid a thundering herd",
            "file_type": "rationale",
            "source_file": "retry.py",
            "community": 1,
        },
        {
            "id": "fn_retry",
            "label": "schedule_retry",
            "file_type": "code",
            "source_file": "retry.py",
            "community": 1,
        },
    ],
    "links": [
        {"relation": "rationale_for", "source": "rat_a", "target": "fn_submit"},
        {"relation": "rationale_for", "source": "rat_a2", "target": "fn_submit"},
        {"relation": "rationale_for", "source": "rat_b", "target": "fn_retry"},
    ],
}


def test_importer_dry_run_counts_a_key_repeated_within_the_run_once(tmp_path):
    """T5, rationale half. Two nodes with one identity key in one run: a dry run classified
    each against the store as it was before the run and counted both, while the real run
    imports the first and skips the second. The dry run keeps the keys it has already
    counted, so imported / skipped_existing / would_auto_ratify equal the real run's."""
    path = tmp_path / "dup_graph.json"
    path.write_text(json.dumps(_DUPLICATE_KEY_GRAPH))
    reader = GraphifyReader(path)
    with Store(tmp_path / "dry.db") as dry_store, Store(tmp_path / "real.db") as real_store:
        dry = import_rationales(
            dry_store,
            reader,
            kind="gotcha",
            propose=True,
            dry_run=True,
            ratify_policy=RatifyPolicy.AUTO_ALL,
        )
        real = import_rationales(
            real_store, reader, kind="gotcha", propose=True, ratify_policy=RatifyPolicy.AUTO_ALL
        )
    assert (real.imported, real.skipped_existing, real.auto_ratified) == (2, 1, 2)
    assert (dry.imported, dry.skipped_existing, dry.would_auto_ratify) == (
        real.imported,
        real.skipped_existing,
        real.auto_ratified,
    )
    assert len(dry.dry_run) == real.imported


# -- doc import ------------------------------------------------------------------------

_GRAPH_WITH_SYMBOL: dict = {
    "built_at_commit": "v1",
    "nodes": [
        {
            "id": "fn_submit",
            "label": "submit_order",
            "file_type": "code",
            "source_file": "exec.py",
            "community": 1,
        }
    ],
    "links": [],
}
# The symbol the document names left the graph: nothing resolves any more.
_GRAPH_SYMBOL_GONE: dict = {
    "built_at_commit": "v2",
    "nodes": [
        {
            "id": "fn_other",
            "label": "other_fn",
            "file_type": "code",
            "source_file": "other.py",
            "community": 3,
        }
    ],
    "links": [],
}


def _doc(status: str | None) -> str:
    frontmatter = f"---\nstatus: {status}\n---\n" if status else ""
    return (
        f"{frontmatter}# Submit path\n\n## Context\n\nSome context.\n\n## Decision\n\n"
        "Calls `submit_order` on success.\n"
    )


def _import_pair(
    tmp_path: Path,
    *,
    seed_status: str | None,
    run_status: str | None,
    run_graph: dict,
    policy: RatifyPolicy,
    kind: str,
):
    """One dry run and one real run over two byte-identical copies of the same store, so
    the only difference between them is ``dry_run``. ``seed_status`` set means the store
    first holds that import of the same document (manual policy, no ``--propose``)."""
    doc = _write_md(tmp_path, "docs/a.md", _doc(seed_status))
    base = tmp_path / "base.db"
    with Store(base) as seed_store:
        if seed_status is not None:
            import_docs(
                seed_store,
                _reader(tmp_path, _GRAPH_WITH_SYMBOL, "seed.json"),
                [str(doc)],
                kind=kind,
                any_doc=True,
            )
    shutil.copytree(base, tmp_path / "dry.db")
    shutil.copytree(base, tmp_path / "real.db")
    doc.write_text(_doc(run_status))
    reader = _reader(tmp_path, run_graph, "run.json")
    options = {
        "kind": kind,
        "propose": True,
        "any_doc": True,
        "ratify_policy": policy,
    }
    with Store(tmp_path / "dry.db") as dry_store, Store(tmp_path / "real.db") as real_store:
        dry = import_docs(dry_store, reader, [str(doc)], dry_run=True, **options)
        real = import_docs(real_store, reader, [str(doc)], **options)
    return dry, real


@pytest.mark.parametrize(
    ("label", "seed_status", "run_status", "run_graph", "policy", "expected"),
    [
        ("fresh", None, "accepted", _GRAPH_WITH_SYMBOL, RatifyPolicy.AUTO_ALL, 1),
        # Case A: first imported `rejected`, re-imported `accepted` with --propose. The write
        # is `written` yet carries `supersedes` (the rejected record), so auto-low-risk
        # refuses it in the real run.
        ("case-A", "rejected", "accepted", _GRAPH_WITH_SYMBOL, RatifyPolicy.AUTO_LOW_RISK, 0),
        # ... and the same case under auto-all, which admits a superseding draft by shape:
        # the 0 above is the supersedes gate, not some other refusal.
        ("case-A-auto-all", "rejected", "accepted", _GRAPH_WITH_SYMBOL, RatifyPolicy.AUTO_ALL, 1),
        # Case B: the symbol left the graph, so the new record inherits the rejected record's
        # live bindings and has no anchor of its own.
        ("case-B", "rejected", "accepted", _GRAPH_SYMBOL_GONE, RatifyPolicy.AUTO_ALL, 1),
        ("draft", None, "draft", _GRAPH_WITH_SYMBOL, RatifyPolicy.AUTO_ALL, 0),
        ("rejected", None, "rejected", _GRAPH_WITH_SYMBOL, RatifyPolicy.AUTO_ALL, 0),
    ],
)
def test_doc_import_dry_run_would_auto_ratify_equals_the_real_runs_count(
    tmp_path, label, seed_status, run_status, run_graph, policy, expected
):
    """T4. Over identical store copies the dry run's ``would_auto_ratify`` equals the real
    run's ``auto_ratified`` in every case, and the number is not a vacuous 0 == 0 where the
    real run does ratify. The cases that carry ``supersedes`` (A) or inherited bindings (B)
    are the ones a hard-coded signal gets wrong."""
    dry, real = _import_pair(
        tmp_path,
        seed_status=seed_status,
        run_status=run_status,
        run_graph=run_graph,
        policy=policy,
        kind="lesson",
    )
    assert real.auto_ratified == expected, label
    assert dry.would_auto_ratify == real.auto_ratified, label
    assert dry.auto_ratified == 0
    assert dry.auto_ratify_failures == []
    assert [item["auto_ratify_eligible"] for item in dry.dry_run] == [expected == 1], label


def test_doc_import_dry_run_counts_a_ref_repeated_within_the_run_once(tmp_path):
    """T5, document half. The same file named twice on one command line is one identity (the
    ref) twice in one run: the real run writes the first and skips the second as existing, so
    a dry run that classified both against the pre-run store said imported 2. A directory and
    a file inside it, or a live and an archived copy that one profile normalizes to the same
    ref, repeat the key the same way."""
    doc = _write_md(tmp_path, "docs/a.md", _doc("accepted"))
    reader = _reader(tmp_path, _GRAPH_WITH_SYMBOL, "run.json")
    options = {
        "kind": "lesson",
        "propose": True,
        "any_doc": True,
        "ratify_policy": RatifyPolicy.AUTO_ALL,
    }
    with Store(tmp_path / "dry.db") as dry_store, Store(tmp_path / "real.db") as real_store:
        dry = import_docs(dry_store, reader, [str(doc), str(doc)], dry_run=True, **options)
        real = import_docs(real_store, reader, [str(doc), str(doc)], **options)
    assert (real.imported, real.skipped_existing, real.auto_ratified) == (1, 1, 1)
    assert (dry.imported, dry.skipped_existing, dry.would_auto_ratify) == (
        real.imported,
        real.skipped_existing,
        real.auto_ratified,
    )
    assert len(dry.dry_run) == real.imported


def test_doc_import_dry_run_counts_a_document_anchored_only_by_its_file_node(tmp_path, monkeypatch):
    """T4, file-anchor case. The document names nothing the graph resolves, so its only live
    binding is the file node (``_file_node_descriptor``): the dry run must count it, as the
    real run's ``_anchor_signal`` does, or an eligible record reads as unanchored (0 vs 1)."""
    monkeypatch.chdir(tmp_path)
    _write_md(
        tmp_path,
        "docs/a.md",
        "# Plain\n\n## Context\n\nSome context.\n\n## Decision\n\nNothing named here.\n",
    )
    graph = {
        "built_at_commit": "v1",
        "nodes": [
            {
                "id": "doc_a",
                "label": "a.md",
                "file_type": "document",
                "source_file": "docs/a.md",
                "community": 4,
            }
        ],
        "links": [],
    }
    reader = _reader(tmp_path, graph, "run.json")
    options = {
        "kind": "lesson",
        "propose": True,
        "any_doc": True,
        "ratify_policy": RatifyPolicy.AUTO_LOW_RISK,
    }
    with Store(tmp_path / "dry.db") as dry_store, Store(tmp_path / "real.db") as real_store:
        dry = import_docs(dry_store, reader, ["docs/a.md"], dry_run=True, **options)
        real = import_docs(real_store, reader, ["docs/a.md"], **options)
    assert (real.imported, real.auto_ratified) == (1, 1)
    assert dry.would_auto_ratify == real.auto_ratified
    assert [item["auto_ratify_eligible"] for item in dry.dry_run] == [True]


def _proposal(status: str | None, why: str) -> str:
    frontmatter = f"---\nstatus: {status}\n---\n" if status else ""
    return f"{frontmatter}# Zeta\n\n## Why\n\n{why}\n\n## What Changes\n\nCalls `submit_order`.\n"


_ARCHIVED_2024 = "openspec/changes/archive/2024-01-01-zeta/proposal.md"
_ARCHIVED_2025 = "openspec/changes/archive/2025-01-01-zeta/proposal.md"
_LIVE = "openspec/changes/zeta/proposal.md"


def _repeated_ref_pair(
    tmp_path: Path,
    files: dict[str, str],
    *,
    seed: tuple[str, bool] | None = None,
    policy: RatifyPolicy = RatifyPolicy.AUTO_ALL,
):
    """A dry and a real run over identical store copies, with every file named on one command
    line in sorted order. The openspec profile maps an archived copy and the live copy to one
    ref, so one run meets the same ref twice. ``seed`` is a first import of that ref into the
    store: the text of the live file and whether it was imported with ``--propose``, which
    leaves a pending proposal rather than an accepted record."""
    live = tmp_path / _LIVE
    reader = _reader(tmp_path, _GRAPH_WITH_SYMBOL, "run.json")
    with Store(tmp_path / "base.db") as seed_store:
        if seed is not None:
            text, propose = seed
            _write_md(tmp_path, _LIVE, text)
            import_docs(
                seed_store, reader, [str(live)], any_doc=True, profile="openspec", propose=propose
            )
    shutil.copytree(tmp_path / "base.db", tmp_path / "dry.db")
    shutil.copytree(tmp_path / "base.db", tmp_path / "real.db")
    paths = [str(_write_md(tmp_path, rel, text)) for rel, text in sorted(files.items())]
    options = {
        "kind": "lesson",
        "propose": True,
        "any_doc": True,
        "profile": "openspec",
        "ratify_policy": policy,
    }
    with Store(tmp_path / "dry.db") as dry_store, Store(tmp_path / "real.db") as real_store:
        dry = import_docs(dry_store, reader, paths, dry_run=True, **options)
        real = import_docs(real_store, reader, paths, **options)
    return dry, real


_REJECTED_2024 = _proposal("rejected", "Because.")


@pytest.mark.parametrize(
    ("label", "files", "seed", "policy", "expected"),
    [
        # A rejected record is not supersedable, so the live copy is a fresh WRITE the real run
        # auto-ratifies; a bare "seen it" set called it a skipped repeat (dry 0 vs real 1).
        (
            "archived-rejected-then-live",
            {_ARCHIVED_2024: _REJECTED_2024, _LIVE: _proposal(None, "Because v2.")},
            None,
            RatifyPolicy.AUTO_ALL,
            (2, 0, 0, 1),
        ),
        # Identical content, the first copy rejected and the second open: a status flip, so a
        # write that supersedes the rejected record rather than a skipped repeat ...
        (
            "two-archived-first-rejected",
            {_ARCHIVED_2024: _REJECTED_2024, _ARCHIVED_2025: _proposal(None, "Because.")},
            None,
            RatifyPolicy.AUTO_ALL,
            (2, 0, 0, 1),
        ),
        # ... and because it carries `supersedes`, auto-low-risk refuses it in the real run.
        (
            "two-archived-first-rejected-low-risk",
            {_ARCHIVED_2024: _REJECTED_2024, _ARCHIVED_2025: _proposal(None, "Because.")},
            None,
            RatifyPolicy.AUTO_LOW_RISK,
            (2, 0, 0, 0),
        ),
        # Different content, the first copy written and auto-ratified: the live copy supersedes it.
        (
            "archived-then-live-differing",
            {_ARCHIVED_2024: _proposal(None, "Because."), _LIVE: _proposal(None, "Because v2.")},
            None,
            RatifyPolicy.AUTO_ALL,
            (1, 1, 0, 1),
        ),
        # Identical content: the second copy is a skipped repeat.
        (
            "two-archived-identical",
            {
                _ARCHIVED_2024: _proposal(None, "Because."),
                _ARCHIVED_2025: _proposal(None, "Because."),
            },
            None,
            RatifyPolicy.AUTO_ALL,
            (1, 0, 1, 1),
        ),
        # The store holds an accepted record. The rejected archived copy supersedes it and
        # closes it, so the live copy meets only the rejected record and is a fresh write: the
        # closed predecessor must leave the set a later copy is classified against.
        (
            "closed-predecessor-leaves",
            {
                _ARCHIVED_2024: _proposal("rejected", "Other."),
                _LIVE: _proposal(None, "Because v3."),
            },
            (_proposal(None, "Because."), False),
            RatifyPolicy.AUTO_ALL,
            (1, 1, 0, 1),
        ),
        # The store holds a pending proposal. The rejected archived copy drops it, so the live
        # copy meets only rejected records and is a fresh write: the dropped proposal must
        # leave the set too.
        (
            "dropped-proposal-leaves",
            {
                _ARCHIVED_2024: _proposal("rejected", "Other."),
                _LIVE: _proposal(None, "Because v3."),
            },
            (_proposal(None, "Because."), True),
            RatifyPolicy.AUTO_ALL,
            (1, 1, 0, 1),
        ),
    ],
)
def test_doc_import_dry_run_follows_the_records_a_repeated_ref_would_leave(
    tmp_path, label, files, seed, policy, expected
):
    """T5, repeated-ref cases. The dry run classifies a repeat against the records the earlier
    copies would have left at that ref, not against the store as it was: imported, superseded,
    skipped_existing and the eligibility count all equal the real run's."""
    dry, real = _repeated_ref_pair(tmp_path, files, seed=seed, policy=policy)
    assert (real.imported, real.superseded, real.skipped_existing, real.auto_ratified) == expected
    assert (dry.imported, dry.superseded, dry.skipped_existing, dry.would_auto_ratify) == expected
    assert len(dry.dry_run) == real.imported + real.superseded, label


def test_doc_import_dry_run_remembers_a_first_occurrence_that_supersedes(tmp_path):
    """T5, first-occurrence-supersedes case. The store holds an accepted record at the ref and
    the document, edited, is named twice. The first copy supersedes it (a proposal; the ancestor
    stays open), and the second is identical to that proposal, so the real run skips it. A dry
    run that remembers only would-be fresh writes classifies the second copy against the old
    accepted record again and supersedes twice."""
    live = tmp_path / _LIVE
    reader = _reader(tmp_path, _GRAPH_WITH_SYMBOL, "run.json")
    with Store(tmp_path / "base.db") as seed_store:
        _write_md(tmp_path, _LIVE, _proposal(None, "Because."))
        import_docs(seed_store, reader, [str(live)], any_doc=True, profile="openspec")
    shutil.copytree(tmp_path / "base.db", tmp_path / "dry.db")
    shutil.copytree(tmp_path / "base.db", tmp_path / "real.db")
    live.write_text(_proposal(None, "Because v2."))
    options = {
        "kind": "lesson",
        "propose": True,
        "any_doc": True,
        "profile": "openspec",
        "ratify_policy": RatifyPolicy.AUTO_ALL,
    }
    with Store(tmp_path / "dry.db") as dry_store, Store(tmp_path / "real.db") as real_store:
        dry = import_docs(dry_store, reader, [str(live), str(live)], dry_run=True, **options)
        real = import_docs(real_store, reader, [str(live), str(live)], **options)
    assert (real.imported, real.superseded, real.skipped_existing) == (0, 1, 1)
    assert (dry.imported, dry.superseded, dry.skipped_existing) == (0, 1, 1)
    assert dry.would_auto_ratify == real.auto_ratified == 0


# -- the two CLI printers ----------------------------------------------------------------

_RATIONALE_TITLE = "Retries are idempotent to survive at-least-once delivery"


def _rationale_dry_run(tmp_path, capsys, monkeypatch, *, policy: str, kind: str) -> str:
    graph = tmp_path / "g.json"
    graph.write_text(json.dumps(_RATIONALE_GRAPH))
    monkeypatch.setenv("SIDEGRAPH_RATIFY_POLICY", policy)
    argv = ["--db", str(tmp_path / "t.db"), "--graph", str(graph), "--kind", kind]
    assert import_main([*argv, "--propose", "--dry-run"]) == 0
    return capsys.readouterr().out


def test_cli_rationale_dry_run_segment_and_marker_appear_off_manual_only(
    tmp_path, capsys, monkeypatch
):
    """T6, rationale printer. Under a non-manual policy the summary carries ``would
    auto-ratify M`` before ``(skipped:`` and an eligible item ends with ``[auto]``; the
    segment mirrors the real run's, so it prints even at 0, with no marker on an ineligible
    item. Under ``manual`` both lines are exactly what they were."""
    out = _rationale_dry_run(tmp_path, capsys, monkeypatch, policy="auto-low-risk", kind="lesson")
    assert f"exec.py: {_RATIONALE_TITLE} [auto]\n" in out
    assert (
        "would import 1 decision(s), would auto-ratify 1 "
        "(skipped: 0 existing, 0 unanchorable, 0 filtered)" in out
    )

    out = _rationale_dry_run(tmp_path, capsys, monkeypatch, policy="auto-low-risk", kind="adr")
    assert f"exec.py: {_RATIONALE_TITLE}\n" in out
    assert "[auto]" not in out
    assert "would import 1 decision(s), would auto-ratify 0 (skipped:" in out

    out = _rationale_dry_run(tmp_path, capsys, monkeypatch, policy="manual", kind="lesson")
    assert f"exec.py: {_RATIONALE_TITLE}\n" in out
    assert "[auto]" not in out
    assert "auto-ratify" not in out
    assert "would import 1 decision(s) (skipped: 0 existing, 0 unanchorable, 0 filtered)" in out


_DOC_WITH_AN_UNRESOLVED_MENTION = _adr_md(
    "Submit path", "Calls `submit_order` on success, never `ghost_fn`."
)


def _docs_dry_run(tmp_path, capsys, monkeypatch, *, policy: str, kind: str) -> str:
    graph = tmp_path / "g.json"
    graph.write_text(json.dumps(_DOC_MENTION_GRAPH))
    _write_md(tmp_path, "docs/a.md", _DOC_WITH_AN_UNRESOLVED_MENTION)
    monkeypatch.setenv("SIDEGRAPH_RATIFY_POLICY", policy)
    argv = ["--db", str(tmp_path / "t.db"), "--graph", str(graph), "--docs", str(tmp_path / "docs")]
    assert import_main([*argv, "--any-doc", "--kind", kind, "--propose", "--dry-run"]) == 0
    return capsys.readouterr().out


_DOCS_SKIPPED = (
    "(skipped: 0 existing, 0 unanchorable, 0 not-decision-shaped, 0 unparseable, "
    "0 superseded-frontmatter, 0 outside-profile)"
)


def test_cli_docs_dry_run_segment_and_marker_appear_off_manual_only(tmp_path, capsys, monkeypatch):
    """T6, docs printer. ``[auto]`` goes after the title and before the indented ``anchor
    skipped:`` lines; the summary carries ``would auto-ratify M`` after ``supersede S`` and
    before ``(skipped:``. Under ``manual`` both lines stay exactly as they were."""
    out = _docs_dry_run(tmp_path, capsys, monkeypatch, policy="auto-low-risk", kind="lesson")
    assert re.search(
        r"^\S*a\.md: \[imported\] Submit path \[auto\]\n"
        r"    anchor skipped: ghost_fn \(unresolved\)$",
        out,
        re.MULTILINE,
    )
    assert f"would import 1 decision(s), supersede 0, would auto-ratify 1 {_DOCS_SKIPPED}" in out

    out = _docs_dry_run(tmp_path, capsys, monkeypatch, policy="auto-low-risk", kind="adr")
    assert re.search(r"^\S*a\.md: \[imported\] Submit path$", out, re.MULTILINE)
    assert "[auto]" not in out
    assert f"would import 1 decision(s), supersede 0, would auto-ratify 0 {_DOCS_SKIPPED}" in out

    out = _docs_dry_run(tmp_path, capsys, monkeypatch, policy="manual", kind="lesson")
    assert re.search(r"^\S*a\.md: \[imported\] Submit path$", out, re.MULTILINE)
    assert "[auto]" not in out
    assert "auto-ratify" not in out
    assert f"would import 1 decision(s), supersede 0 {_DOCS_SKIPPED}" in out
