"""An accepted candidate with no anchor in the graph is skipped by name, so a rerun converges.

see docs/getting-started/bootstrap.md ("Completion, recovery, and hosts")
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from sidegraph.bootstrap.catalog import load_canonical_catalog
from sidegraph.bootstrap.cli import main
from sidegraph.bootstrap.model import WARNING_CONSEQUENCES, WarningCode
from sidegraph.bootstrap.planner import plan_sources
from sidegraph.bootstrap.scan import scan_sources
from sidegraph.engine.reader import GraphifyReader
from sidegraph.profiles import get_profile
from sidegraph.schema import DecisionStatus
from sidegraph.store import Store
from tests.test_bootstrap_cli import write, write_claude_config


def write_adr(root: Path, name: str, mention: str = "") -> None:
    write(
        root / "docs" / "adr" / name,
        "# Policy " + name + "\n\n## Context\n\nSome context for " + name + ".\n\n"
        "## Decision\n\nUse three bounded retries" + mention + ".\n\n"
        "## Rejected\n\nRetrying forever was rejected.\n",
    )


def write_graph_for(root: Path, documented: tuple[str, ...], symbol: str | None = None) -> None:
    nodes: list[dict[str, str]] = [
        {
            "id": f"doc-{name}",
            "label": f"ADR {name}",
            "norm_label": f"adr {name}",
            "file_type": "document",
            "source_file": f"docs/adr/{name}",
            "community": "1",
        }
        for name in documented
    ]
    if symbol is not None:
        nodes.append(
            {
                "id": f"fn-{symbol}",
                "label": symbol,
                "norm_label": symbol,
                "file_type": "code",
                "source_file": "src/retry.py",
                "community": "1",
            }
        )
    write(
        root / "graphify-out" / "graph.json",
        json.dumps({"built_at_commit": "unanchored", "nodes": nodes, "links": []}) + "\n",
    )


def repo(root: Path, adrs: tuple[str, ...], documented: tuple[str, ...]) -> None:
    for name in adrs:
        write_adr(root, name)
    write_graph_for(root, documented)
    write_claude_config(root)


def answers(monkeypatch: pytest.MonkeyPatch, *values: str) -> None:
    iterator = iter(values)
    monkeypatch.setattr("builtins.input", lambda _prompt: next(iterator))


def run(root: Path, capsys: pytest.CaptureFixture[str], *extra: str) -> tuple[int, str]:
    rc = main(["--root", str(root), "--profile", "generic-adr", "--host", "claude-code", *extra])
    return rc, capsys.readouterr().out


def unanchored_lines(out: str) -> list[str]:
    return [line for line in out.splitlines() if line.startswith("UNANCHORED")]


def decisions(root: Path) -> list[str]:
    return [d.provenance.ref or "" for d in load_canonical_catalog(root / ".sidegraph").decisions]


def assert_names_the_graph_rebuild(out: str, root: Path) -> None:
    """The record is durable but unreachable: name the cause and the one remedy, not a resume."""
    (anchors,) = [line for line in out.splitlines() if line.startswith("ANCHORS")]
    assert re.fullmatch(
        r"ANCHORS      incomplete: stored record \S+ for docs/adr/001-retry\.md has no anchor "
        r"in the current graph; run `graphify update \.` and rerun",
        anchors,
    ), anchors
    assert "unresolved or ambiguous" not in out
    assert "RESUME" not in out
    assert f"NEXT         cd {root} && graphify update ." in out
    (proof,) = [line for line in out.splitlines() if line.startswith("PROOF")]
    assert "did not surface" not in proof and "see ANCHORS" in proof, proof


def test_anchorless_accept_converges_with_a_named_skip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo(tmp_path, ("001-retry.md",), documented=())
    answers(monkeypatch, "a", "confirm", "a", "confirm")
    for _ in range(2):
        rc, out = run(tmp_path, capsys)
        assert rc == 0, out
        (line,) = unanchored_lines(out)
        assert re.fullmatch(
            r"UNANCHORED   \S+ docs/adr/001-retry\.md \(no anchor in the graph, nothing written; "
            r"run `graphify update \.` and rerun\)",
            line,
        ), line
        assert "FAILED REF" not in out
        assert "PENDING      <none>" in out
        assert "STORE        partial" not in out and "STORE        incomplete" not in out
    assert decisions(tmp_path) == []


def test_mixed_plan_writes_the_anchored_and_names_the_anchorless(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo(tmp_path, ("001-a.md", "002-b.md"), documented=("002-b.md",))
    answers(monkeypatch, "a", "a", "confirm", "a", "a", "confirm")
    report = tmp_path / "report.md"
    rc, out = run(tmp_path, capsys, "--report", str(report))
    assert rc == 0, out
    (line,) = unanchored_lines(out)
    assert "docs/adr/001-a.md" in line
    assert "ANCHORS      ready" in out
    assert "- skipped unanchorable candidates: 1" in report.read_text(encoding="utf-8")
    assert "PENDING      <none>" in out
    assert "FAILED REF   <none>" in out
    assert decisions(tmp_path) == ["docs/adr/002-b.md"]
    rc, out = run(tmp_path, capsys)  # unchanged rerun converges
    assert rc == 0, out
    assert "PENDING      <none>" in out
    assert decisions(tmp_path) == ["docs/adr/002-b.md"]


def test_review_warns_only_for_the_anchorless_candidate(tmp_path: Path) -> None:
    repo(tmp_path, ("001-a.md", "002-b.md"), documented=("002-b.md",))
    write_adr(tmp_path, "003-c.md", mention=" via `retry_call`")  # a resolved mention, no doc node
    write_graph_for(tmp_path, ("002-b.md",), symbol="retry_call")
    profile = get_profile("generic-adr")
    plan = plan_sources(
        tmp_path,
        scan_sources(tmp_path, profile),
        profile,
        catalog=load_canonical_catalog(tmp_path / ".sidegraph"),
        reader=GraphifyReader(tmp_path / "graphify-out" / "graph.json"),
    )
    by_path = {c.file_path: c for c in plan.candidates}
    assert WarningCode.NO_ANCHOR in by_path["docs/adr/001-a.md"].warnings
    assert WarningCode.NO_ANCHOR not in by_path["docs/adr/002-b.md"].warnings
    assert WarningCode.NO_ANCHOR not in by_path["docs/adr/003-c.md"].warnings
    consequence = WARNING_CONSEQUENCES[WarningCode.NO_ANCHOR]
    assert "graphify update ." in consequence


def test_rebuilt_graph_lets_the_next_run_write_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo(tmp_path, ("001-retry.md",), documented=())
    answers(monkeypatch, "a", "confirm", "a", "confirm")
    rc, _ = run(tmp_path, capsys)
    assert rc == 0 and decisions(tmp_path) == []
    write_graph_for(tmp_path, ("001-retry.md",))
    rc, out = run(tmp_path, capsys)
    assert rc == 0, out
    assert "UNANCHORED" not in out
    assert decisions(tmp_path) == ["docs/adr/001-retry.md"]


def test_identical_content_already_stored_is_ratified_not_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Identical content already stored for the source is ratified in place, not skipped.

    The document loses its graph node after the first (proposed) import; accepting it again
    ratifies the stored record, so no UNANCHORED line appears. The record's own binding no
    longer resolves, so the run exits 2 with the graph rebuild as the remedy (the p5c state).
    """
    repo(tmp_path, ("001-retry.md",), documented=("001-retry.md",))
    answers(monkeypatch, "p", "confirm", "a", "confirm")
    rc, out = run(tmp_path, capsys)
    assert rc == 2, out
    assert decisions(tmp_path) == ["docs/adr/001-retry.md"]
    write_graph_for(tmp_path, ())
    rc, out = run(tmp_path, capsys)
    assert rc == 2, out
    assert_names_the_graph_rebuild(out, tmp_path)
    assert unanchored_lines(out) == [], out
    assert "PENDING      <none>" in out
    assert "FAILED REF   <none>" in out
    (decision,) = load_canonical_catalog(tmp_path / ".sidegraph").decisions
    assert decision.status == DecisionStatus.ACCEPTED


def test_rejected_record_is_revived_with_its_bindings_when_the_anchor_is_gone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Accepting an anchorless candidate whose rejected twin has bindings writes a record that
    inherits them (the revival path), instead of skipping it."""
    repo(tmp_path, ("001-retry.md",), documented=("001-retry.md",))
    answers(monkeypatch, "p", "confirm")
    run(tmp_path, capsys)
    store = Store(tmp_path / ".sidegraph")
    try:
        (proposal,) = load_canonical_catalog(tmp_path / ".sidegraph").decisions
        store.drop(proposal.id)
    finally:
        store.close()
    write_graph_for(tmp_path, ())
    answers(monkeypatch, "a", "confirm")
    rc, out = run(tmp_path, capsys)
    assert rc == 2, out
    assert_names_the_graph_rebuild(out, tmp_path)
    assert unanchored_lines(out) == [], out
    assert "FAILED REF   <none>" in out
    dropped, revived = load_canonical_catalog(tmp_path / ".sidegraph").decisions
    assert (dropped.id, dropped.status) == (proposal.id, DecisionStatus.REJECTED)
    assert revived.status == DecisionStatus.ACCEPTED and revived.valid_to is None
    store = Store(tmp_path / ".sidegraph")
    try:
        assert store.bindings_for_record(revived.id), "the new record inherits the old bindings"
    finally:
        store.close()


def test_accepted_anchorless_with_another_kept_proposed_asks_for_an_accepted_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The only accepted candidate was never written: ANCHORS must not blame it."""
    repo(tmp_path, ("001-a.md", "002-b.md"), documented=("002-b.md",))
    answers(monkeypatch, "a", "p", "confirm")
    rc, out = run(tmp_path, capsys)
    assert rc == 2, out
    assert len(unanchored_lines(out)) == 1
    assert "unresolved or ambiguous" not in out
    assert "ANCHORS      incomplete: activation needs at least one accepted candidate" in out


def test_all_anchorless_run_on_a_fresh_repo_reports_the_files_it_created(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Opening the store creates its layout files: the diagnostic names them, not "no writes"."""
    repo(tmp_path, ("001-retry.md",), documented=())
    answers(monkeypatch, "a", "confirm")
    rc, out = run(tmp_path, capsys)
    assert rc == 0, out
    (diagnostic,) = [line for line in out.splitlines() if line.startswith("DIAGNOSTIC")]
    assert diagnostic.endswith("0 skipped in review, 1 unanchored); no records written"), diagnostic
    assert "no writes" not in out
    changed = [line for line in out.splitlines() if line.startswith("CHANGED")]
    assert changed, out
    created = {
        path.relative_to(tmp_path).as_posix() for path in (tmp_path / ".sidegraph").rglob("*")
    }
    assert ".sidegraph/format" in created
    assert any(".sidegraph/format" in line for line in changed), out
    assert decisions(tmp_path) == []


def test_all_skip_diagnostic_line_is_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo(tmp_path, ("001-retry.md",), documented=())
    answers(monkeypatch, "s")
    rc, out = run(tmp_path, capsys)
    assert rc == 0, out
    assert "no activation (1 documents, 1 candidates, 1 skipped); no writes" in out


def test_unchanged_rerun_of_the_unreachable_state_names_the_rebuild_not_a_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo(tmp_path, ("001-retry.md",), documented=("001-retry.md",))
    answers(monkeypatch, "p", "confirm", "a", "confirm", "a", "confirm")
    run(tmp_path, capsys)
    write_graph_for(tmp_path, ())
    for _ in range(2):
        rc, out = run(tmp_path, capsys)
        assert rc == 2, out
        assert_names_the_graph_rebuild(out, tmp_path)
        assert "PENDING      <none>" in out


def test_the_unreachable_state_converges_once_the_graph_regains_the_node(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo(tmp_path, ("001-retry.md",), documented=("001-retry.md",))
    answers(monkeypatch, "p", "confirm", "a", "confirm", "a", "confirm")
    run(tmp_path, capsys)
    write_graph_for(tmp_path, ())
    rc, _ = run(tmp_path, capsys)
    assert rc == 2
    write_graph_for(tmp_path, ("001-retry.md",))
    rc, out = run(tmp_path, capsys)
    assert rc == 0, out
    assert "ANCHORS      ready" in out
    assert "incomplete" not in out and "RESUME" not in out
    assert "PROOF        production retrieval returned" in out
    (decision,) = load_canonical_catalog(tmp_path / ".sidegraph").decisions
    assert decision.status == DecisionStatus.ACCEPTED


def test_an_unresolved_mention_keeps_its_own_anchors_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A written record whose backticked mention names nothing in the graph is a different
    cause with a different remedy: its message and exit code are unchanged."""
    write_adr(tmp_path, "001-retry.md", mention=" via `ghost_fn`")
    write_graph_for(tmp_path, ("001-retry.md",))
    write_claude_config(tmp_path)
    answers(monkeypatch, "a", "confirm")
    rc, out = run(tmp_path, capsys)
    assert rc == 2, out
    assert "ANCHORS      incomplete: accepted candidate has unresolved or ambiguous anchors" in out
    assert "has no anchor in the current graph" not in out


def write_nodes(
    root: Path, nodes: list[dict[str, str]], rel: str = "graphify-out/graph.json"
) -> None:
    write(
        root / rel,
        json.dumps({"built_at_commit": "unanchored", "nodes": nodes, "links": []}) + "\n",
    )


def doc_node(**over: str) -> dict[str, str]:
    node = {
        "id": "doc-001-retry.md",
        "label": "ADR 001-retry.md",
        "norm_label": "adr 001-retry.md",
        "file_type": "document",
        "source_file": "docs/adr/001-retry.md",
        "community": "1",
    }
    return {**node, **over}


def stored_then_graph(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    nodes: list[dict[str, str]],
    *extra: str,
) -> tuple[int, str]:
    """Reach the p5c state (record ratified in place), then run once on a graph with ``nodes``."""
    repo(tmp_path, ("001-retry.md",), documented=("001-retry.md",))
    answers(monkeypatch, "p", "confirm", "a", "confirm")
    run(tmp_path, capsys, *extra)
    write_nodes(tmp_path, nodes)
    return run(tmp_path, capsys, *extra)


@pytest.mark.parametrize(
    "nodes",
    [
        pytest.param(
            [
                doc_node(file_type="rationale"),
                doc_node(id="doc-other", source_file="docs/other.md", label="Other"),
            ],
            id="only-a-rationale-node-in-the-file",
        ),
        pytest.param([doc_node(source_file="docs/other.md")], id="node-id-moved-to-another-file"),
        pytest.param([doc_node(file_type="image")], id="node-id-survives-as-an-image"),
    ],
)
def test_anchors_and_proof_agree_when_the_graph_only_half_reaches_the_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    nodes: list[dict[str, str]],
) -> None:
    """ANCHORS ready exactly when the proof surfaces the record; never ready + RESUME."""
    rc, out = stored_then_graph(tmp_path, monkeypatch, capsys, nodes)
    ready = "ANCHORS      ready" in out
    surfaced = "PROOF        production retrieval returned" in out
    assert ready == surfaced, out
    if ready:
        assert rc == 0 and "RESUME" not in out, out
    else:
        assert rc == 2, out
        assert_names_the_graph_rebuild(out, tmp_path)


def test_a_retyped_node_in_the_same_file_still_reaches_the_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Same id and file, document -> concept: no plan-time anchor, but the stored one resolves."""
    rc, out = stored_then_graph(tmp_path, monkeypatch, capsys, [doc_node(file_type="concept")])
    assert rc == 0, out
    assert "ANCHORS      ready" in out
    assert "incomplete" not in out and "RESUME" not in out and "NEXT" not in out


def test_a_failed_write_keeps_the_exact_resume_beside_an_unreachable_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A partial-recoverable run needs its resume whatever else is wrong with the graph."""
    from sidegraph.bootstrap import cli
    from sidegraph.bootstrap.model import RunStatus

    repo(tmp_path, ("001-retry.md",), documented=("001-retry.md",))
    answers(monkeypatch, "p", "confirm", "a", "confirm")
    run(tmp_path, capsys)
    write_nodes(tmp_path, [])
    real = cli.apply_review

    def partial(*args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        report = real(*args, **kwargs)  # type: ignore[arg-type]
        return report.model_copy(
            update={
                "status": RunStatus.PARTIAL_RECOVERABLE,
                "failed_ref": "docs/adr/002-x.md",
                "next_command": "sidegraph-bootstrap --resume",
            }
        )

    monkeypatch.setattr(cli, "apply_review", partial)
    rc, out = run(tmp_path, capsys)
    assert rc == 2, out
    assert "PROOF        incomplete: activation is partial-recoverable" in out
    assert "(see ANCHORS)" not in out
    (resume,) = [line for line in out.splitlines() if line.startswith("RESUME")]
    for part in ("--resume", "--root", "--db", "--graph", "--profile generic-adr"):
        assert part in resume, resume
    assert "stored record" in out  # the unreachable record is still named on ANCHORS


def test_a_custom_graph_is_named_not_rebuilt_by_a_wrong_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    graph = tmp_path / "alt" / "g.json"
    write_adr(tmp_path, "001-retry.md")
    write_claude_config(tmp_path)
    write_nodes(tmp_path, [doc_node()], "alt/g.json")
    answers(monkeypatch, "p", "confirm", "a", "confirm")
    extra = ("--graph", str(graph))
    run(tmp_path, capsys, *extra)
    write_nodes(tmp_path, [], "alt/g.json")
    rc, out = run(tmp_path, capsys, *extra)
    assert rc == 2, out
    (anchors,) = [line for line in out.splitlines() if line.startswith("ANCHORS")]
    assert anchors.endswith(f"rebuild the graph at {graph} and rerun"), anchors
    tail = [line for line in out.splitlines() if re.match(r"(ANCHORS|PROOF|NEXT|RESUME)", line)]
    assert not any("graphify update" in line for line in tail), tail
    assert not any(line.startswith(("NEXT", "RESUME")) for line in tail), tail


def test_a_fresh_run_lists_the_creation_marker_as_changed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo(tmp_path, ("001-retry.md",), documented=())
    answers(monkeypatch, "a", "confirm")
    rc, out = run(tmp_path, capsys)
    assert rc == 0, out
    changed = [line for line in out.splitlines() if line.startswith("CHANGED")]
    assert any(line.endswith(".sidegraph/stamping_live_since") for line in changed), changed


def test_the_unanchored_line_names_the_graph_in_use(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    graph = tmp_path / "alt" / "g.json"
    write_adr(tmp_path, "001-retry.md")
    write_claude_config(tmp_path)
    write_nodes(tmp_path, [], "alt/g.json")
    answers(monkeypatch, "a", "confirm")
    rc, out = run(tmp_path, capsys, "--graph", str(graph))
    assert rc == 0, out
    (line,) = unanchored_lines(out)
    assert line.endswith(f"nothing written; rebuild the graph at {graph} and rerun)"), line
    assert "graphify update" not in line


def test_the_report_carries_the_remedy_for_an_unreachable_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    report = tmp_path / "report.md"
    rc, out = stored_then_graph(tmp_path, monkeypatch, capsys, [], "--report", str(report))
    assert rc == 2, out
    text = report.read_text(encoding="utf-8")
    assert "- stored record has no anchor in the current graph: run `graphify update .`" in text
    assert "001-retry" not in text, "the report stays free of source names"


def test_a_mixed_run_names_the_unreachable_record_and_the_proof_keeps_a_reachable_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """001 is stored but unreachable, 002 is new and anchored: exit 2 for 001 only, no RESUME."""
    repo(tmp_path, ("001-retry.md",), documented=("001-retry.md",))
    answers(monkeypatch, "p", "confirm")
    run(tmp_path, capsys)
    write(  # no rejected text: the proof prefers 001's record, which is the unreachable one
        tmp_path / "docs" / "adr" / "002-cache.md",
        "# Cache\n\n## Context\n\nCtx.\n\n## Decision\n\nUse a cache.\n",
    )
    write_nodes(tmp_path, [doc_node(id="doc-002", source_file="docs/adr/002-cache.md")])
    answers(monkeypatch, "a", "a", "confirm")
    rc, out = run(tmp_path, capsys)
    assert rc == 2, out
    (anchors,) = [line for line in out.splitlines() if line.startswith("ANCHORS")]
    assert "stored record" in anchors and "docs/adr/001-retry.md" in anchors, anchors
    assert "RESUME" not in out
    assert "NEXT         cd " in out
    (proof,) = [line for line in out.splitlines() if line.startswith("PROOF  ")]
    assert "see ANCHORS" in proof, proof


def test_another_reason_for_exit_2_keeps_the_exact_resume_beside_an_unreachable_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The unreachable record is not the sole cause (the host wiring is gone too): a resume
    can still help, so it stays exact and the graph rebuild is named beside it."""
    repo(tmp_path, ("001-retry.md",), documented=("001-retry.md",))
    answers(monkeypatch, "p", "confirm", "a", "confirm")
    run(tmp_path, capsys)
    write_nodes(tmp_path, [])
    (tmp_path / ".mcp.json").unlink()
    rc, out = run(tmp_path, capsys)
    assert rc == 2, out
    assert "stored record" in out and "INTEGRATION  Claude Code MCP + hooks verified" not in out
    (resume,) = [line for line in out.splitlines() if line.startswith("RESUME")]
    assert "--root" in resume and "--graph" in resume, resume


def labels(out: str) -> list[str]:
    return [line.split()[0] for line in out.splitlines() if line and not line[0].isspace()]


def report_status(report: Path) -> str:
    (line,) = [
        line for line in report.read_text(encoding="utf-8").splitlines() if "- status: " in line
    ]
    return line.split("- status: ")[1]


def test_all_anchorless_run_writes_the_requested_report_as_a_diagnostic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo(tmp_path, ("001-retry.md",), documented=())
    answers(monkeypatch, "a", "confirm")
    report = tmp_path / "report.md"
    rc, out = run(tmp_path, capsys, "--report", str(report))
    assert rc == 0, out
    assert report_status(report) == "diagnostic"
    assert "- skipped unanchorable candidates: 1" in report.read_text(encoding="utf-8")
    assert "INTEGRATION  Claude Code MCP + hooks verified" in out
    assert "ANCHORS" not in labels(out) and "PROOF" not in labels(out)
    assert "STORE" not in labels(out) and "RESUME" not in labels(out)


def test_all_anchorless_run_still_needs_the_host_integration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    write_adr(tmp_path, "001-retry.md")
    write_graph_for(tmp_path, ())  # no host wiring
    answers(monkeypatch, "a", "confirm")
    report = tmp_path / "report.md"
    rc, out = run(tmp_path, capsys, "--report", str(report))
    assert rc == 2, out
    assert len(unanchored_lines(out)) == 1
    assert "STORE        incomplete" in out
    assert "DIAGNOSTIC" not in labels(out), "a DIAGNOSTIC line means exit 0"
    assert "ANCHORS" not in labels(out) and "PROOF" not in labels(out)
    assert "INTEGRATION  claude-code incomplete" in out
    assert "ACTION" in out
    (resume,) = [line for line in out.splitlines() if line.startswith("RESUME")]
    assert "sidegraph-bootstrap" in resume and "--root" in resume
    assert report_status(report) == "incomplete"
    assert decisions(tmp_path) == []


def test_all_anchorless_run_fails_on_a_store_violation_the_open_tolerates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo(tmp_path, ("001-retry.md",), documented=())
    store = Store(tmp_path / ".sidegraph")
    store.close()
    (tmp_path / ".sidegraph" / "facts" / "01ABCDEFGHJKMNPQRSTVWXYZ00.json").symlink_to(
        tmp_path / "missing.json"
    )
    answers(monkeypatch, "a", "confirm")
    report = tmp_path / "report.md"
    rc, out = run(tmp_path, capsys, "--report", str(report))
    assert rc == 2, out
    assert len(unanchored_lines(out)) == 1
    assert "STORE        incomplete" in out
    assert "DIAGNOSTIC" not in labels(out)
    assert "ANCHORS" not in labels(out) and "PROOF" not in labels(out)
    (verify_line,) = [line for line in out.splitlines() if line.startswith("VERIFY")]
    assert "01ABCDEFGHJKMNPQRSTVWXYZ00.json" in verify_line
    (resume,) = [line for line in out.splitlines() if line.startswith("RESUME")]
    assert resume == f"RESUME       sidegraph-verify --db {tmp_path / '.sidegraph'}"
    assert "INTEGRATION  Claude Code MCP + hooks verified" in out
    assert report_status(report) == "incomplete"


def test_all_anchorless_run_with_a_task_reports_no_proof_that_does_not_apply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo(tmp_path, ("001-retry.md",), documented=())
    answers(monkeypatch, "a", "confirm")
    report = tmp_path / "report.md"
    rc, out = run(tmp_path, capsys, "--report", str(report), "--task", "retry policy")
    assert rc == 0, out
    assert "TASK" not in labels(out) and "PROOF" not in labels(out)
    text = report.read_text(encoding="utf-8")
    assert "Proof" not in text and "proof" not in text
