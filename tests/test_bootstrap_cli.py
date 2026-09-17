from __future__ import annotations

import json
import shlex
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from sidegraph.bootstrap.catalog import load_canonical_catalog
from sidegraph.bootstrap.cli import (
    DomainSuggestions,
    _resolve_graph_path,
    build_parser,
    main,
    render_completion,
)
from sidegraph.bootstrap.model import (
    BootstrapReport,
    HostKind,
    IntegrationResult,
    ProofResult,
    ReviewResult,
    RunStatus,
)
from sidegraph.bootstrap.planner import plan_sources
from sidegraph.bootstrap.scan import scan_sources
from sidegraph.engine.reader import GraphifyReader
from sidegraph.profiles import get_profile
from sidegraph.retrieval import TaskContext
from sidegraph.schema import (
    AnchorBinding,
    Decision,
    DecisionKind,
    DecisionStatus,
    Descriptor,
    Entity,
    EntityKind,
    Provenance,
)
from sidegraph.store import Store


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def write_accepted_adr(root: Path, choice: str = "Use three bounded retries.") -> Path:
    path = root / "docs" / "adr" / "001-retry.md"
    write(
        path,
        "# Retry policy\n\n"
        "## Context\n\n"
        "private source paragraph; token=bootstrap-private-value\n\n"
        f"## Decision\n\n{choice}\n\n"
        "## Rejected\n\nRetrying forever was rejected.\n",
    )
    return path


def rewrite_adr_choice(root: Path, choice: str) -> None:
    write_accepted_adr(root, choice)


def write_graph(root: Path, *, domain_candidate: bool = False) -> Path:
    nodes = [
        {
            "id": "retry-doc",
            "label": "Retry ADR",
            "norm_label": "retry adr",
            "file_type": "document",
            "source_file": "docs/adr/001-retry.md",
            "community": "1",
        }
    ]
    if domain_candidate:
        nodes.extend(
            {
                "id": f"retry-code-{number}",
                "label": f"RetryClient{number}",
                "norm_label": f"retryclient{number}",
                "file_type": "code",
                "source_file": f"lib/retry_{number}.py",
                "community": "1",
            }
            for number in range(1, 5)
        )
    graph = root / "graphify-out" / "graph.json"
    write(
        graph,
        json.dumps({"built_at_commit": "bootstrap-e2e", "nodes": nodes, "links": []}) + "\n",
    )
    return graph


def write_claude_config(root: Path) -> None:
    write(
        root / ".mcp.json",
        json.dumps({"mcpServers": {"sidegraph": {"command": "sidegraph-mcp"}}}) + "\n",
    )
    write(
        root / ".claude" / "settings.json",
        json.dumps(
            {
                "hooks": {
                    "SessionStart": [{"command": "sidegraph-session-start"}],
                    "Stop": [{"command": "sidegraph-stop"}],
                    "PreToolUse": [{"command": "sidegraph-pre-tool-use"}],
                }
            }
        )
        + "\n",
    )


def write_codex_config(root: Path) -> None:
    write(
        root / ".codex" / "config.toml",
        '[mcp_servers.sidegraph]\ncommand = "sidegraph-mcp"\n',
    )
    write(
        root / ".codex" / "hooks" / "hooks.json",
        json.dumps(
            {
                "hooks": {
                    "SessionStart": [{"command": "sidegraph-session-start"}],
                    "Stop": [{"command": "sidegraph-stop"}],
                }
            }
        )
        + "\n",
    )


def write_bootstrap_repo(
    root: Path,
    *,
    host: str,
    complete_host: bool = True,
    domain_candidate: bool = False,
) -> None:
    write_accepted_adr(root)
    write_graph(root, domain_candidate=domain_candidate)
    if complete_host and host == "claude-code":
        write_claude_config(root)
    elif complete_host:
        write_codex_config(root)


def accepted_answers(monkeypatch: pytest.MonkeyPatch) -> None:
    answers = iter(["a", "confirm"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))


def only_candidate_key(root: Path) -> str:
    profile = get_profile("generic-adr")
    plan = plan_sources(
        root,
        scan_sources(root, profile),
        profile,
        catalog=load_canonical_catalog(root / ".sidegraph"),
        reader=GraphifyReader(root / "graphify-out" / "graph.json"),
    )
    assert len(plan.candidates) == 1
    return plan.candidates[0].key


def forbidden_review_input(prompt: str) -> str:
    raise AssertionError(f"review unexpectedly started: {prompt}")


def test_help_has_no_accept_all_switch(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "--profile" in out
    assert "--accept-all" not in out
    assert "--yes" not in out


def test_argparse_usage_error_exits_one() -> None:
    with pytest.raises(SystemExit) as exc:
        main(["--host", "unsupported-host"])
    assert exc.value.code == 1


def test_resolve_graph_path_uses_explicit_env_then_root_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SIDEGRAPH_GRAPH", "env/graph.json")
    assert _resolve_graph_path("chosen.json", tmp_path) == tmp_path / "chosen.json"
    assert _resolve_graph_path(None, tmp_path) == tmp_path / "env/graph.json"
    monkeypatch.delenv("SIDEGRAPH_GRAPH")
    assert _resolve_graph_path(None, tmp_path) == tmp_path / "graphify-out/graph.json"


def test_no_candidates_is_diagnostic_not_activation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write(tmp_path / "docs/adr/notes.md", "# Notes\n\nNo decision sections.\n")
    rc = main(["--root", str(tmp_path), "--profile", "generic-adr"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "DIAGNOSTIC" in out
    assert "PROOF" not in out or "incomplete" in out
    assert not (tmp_path / ".sidegraph").exists()


def test_missing_graph_previews_without_starting_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    write_accepted_adr(tmp_path)
    monkeypatch.setattr("builtins.input", forbidden_review_input)
    rc = main(["--root", str(tmp_path), "--profile", "generic-adr"])
    out = capsys.readouterr().out
    assert rc == 2
    assert "Found 1 candidate" in out
    assert "context: private source paragraph; [REDACTED]" in out
    assert "choice: Use three bounded retries." in out
    assert "rejected: Retrying forever was rejected." in out
    assert "consequences: -" in out
    assert "anchor: -" in out
    assert "bootstrap-private-value" not in out
    assert "graph not readable" in out
    assert f"cd {shlex.quote(str(tmp_path.resolve()))} && graphify update ." in out
    assert not (tmp_path / ".sidegraph").exists()


def test_custom_missing_graph_names_path_without_claiming_graphify_can_write_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    write_accepted_adr(tmp_path)
    custom = tmp_path / "custom output" / "graph.json"
    monkeypatch.setattr("builtins.input", forbidden_review_input)

    assert main(["--root", str(tmp_path), "--graph", str(custom)]) == 2

    out = capsys.readouterr().out
    assert str(custom) in out
    assert "graphify update" not in out
    assert "sidegraph-bootstrap --resume" in out


def test_missing_graph_resume_is_shell_safe_and_pins_resolved_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "repo with ' quote"
    source = write_accepted_adr(root)
    report = root / "reports" / "bootstrap report.md"
    monkeypatch.setattr("builtins.input", forbidden_review_input)

    rc = main(
        [
            "--root",
            str(root),
            "--profile",
            "generic-adr",
            "--docs",
            "docs/adr",
            "--include",
            str(source.relative_to(root)),
            "--report",
            str(report),
        ]
    )

    expected = shlex.join(
        [
            "sidegraph-bootstrap",
            "--resume",
            "--root",
            str(root.resolve()),
            "--db",
            str(root.resolve() / ".sidegraph"),
            "--graph",
            str(root.resolve() / "graphify-out" / "graph.json"),
            "--profile",
            "generic-adr",
            "--host",
            "claude-code",
            "--docs",
            "docs/adr",
            "--include",
            "docs/adr/001-retry.md",
            "--report",
            str(report.resolve()),
        ]
    )
    assert rc == 2
    assert expected in capsys.readouterr().out


def test_malformed_graph_is_operational_error_before_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    write_accepted_adr(tmp_path)
    write(tmp_path / "graphify-out/graph.json", "not json\n")
    monkeypatch.setattr("builtins.input", forbidden_review_input)

    assert main(["--root", str(tmp_path), "--profile", "generic-adr"]) == 1
    assert "ERROR" in capsys.readouterr().err
    assert not (tmp_path / ".sidegraph").exists()


def test_ambiguous_profile_requires_choice_and_explicit_override_works(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    write_accepted_adr(tmp_path)
    (tmp_path / "docs" / "superpowers").mkdir(parents=True)
    (tmp_path / ".specify").mkdir()
    monkeypatch.setattr("builtins.input", forbidden_review_input)

    assert main(["--root", str(tmp_path)]) == 1
    assert "choose --profile" in capsys.readouterr().err
    assert main(["--root", str(tmp_path), "--profile", "generic-adr"]) == 2
    assert "Found 1 candidate" in capsys.readouterr().out


def test_e2e_diagnose_review_apply_reopen_and_proof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    write_bootstrap_repo(tmp_path, host="claude-code")
    accepted_answers(monkeypatch)
    report = tmp_path / "bootstrap-report.md"
    rc = main(
        [
            "--root",
            str(tmp_path),
            "--profile",
            "generic-adr",
            "--host",
            "claude-code",
            "--report",
            str(report),
        ]
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "STORE        ready" in out
    assert "INTEGRATION  Claude Code MCP + hooks verified" in out
    assert "PROOF        production retrieval returned" in out
    assert "PROOF LINE   " in out
    assert "PROOF ANCHOR docs/adr/001-retry.md" in out
    assert "PROOF SOURCE docs/adr/001-retry.md" in out
    assert "PROOF RULE   accepted -> valid -> live tier-2" in out
    assert "HOST PROMPT  Call get_task_context for docs/adr/001-retry.md" in out
    assert "RESUME       " not in out
    rendered_report = report.read_text(encoding="utf-8")
    assert "- elapsed seconds:" in rendered_report
    assert "- candidate precision: 1/1 (100.0%)" in rendered_report
    assert "- next command:" not in rendered_report
    assert "private source paragraph" not in rendered_report


def test_codex_complete_available_checks_exit_zero_as_best_effort(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    write_bootstrap_repo(tmp_path, host="codex")
    accepted_answers(monkeypatch)
    rc = main(["--root", str(tmp_path), "--host", "codex"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "INTEGRATION  Codex best-effort" in out
    assert "Read/Grep PreToolUse unsupported" in out
    assert "fully supported" not in out


def test_all_skip_after_fingerprint_refresh_is_diagnostic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    write_bootstrap_repo(tmp_path, host="claude-code")
    answers = iter(["a", "s"])

    def answer(_prompt: str) -> str:
        value = next(answers)
        if value == "a":
            rewrite_adr_choice(tmp_path, "Use four bounded retries.")
        return value

    monkeypatch.setattr("builtins.input", answer)
    assert main(["--root", str(tmp_path), "--profile", "generic-adr"]) == 0
    out = capsys.readouterr().out
    assert "stale actions discarded" in out
    assert "DIAGNOSTIC" in out
    assert not (tmp_path / ".sidegraph").exists()


def test_source_change_after_refresh_is_incomplete_without_store_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    write_bootstrap_repo(tmp_path, host="claude-code")
    answers = iter(["a", "confirm"])

    def answer(_prompt: str) -> str:
        value = next(answers)
        if value == "confirm":
            rewrite_adr_choice(tmp_path, "Use five bounded retries.")
        return value

    monkeypatch.setattr("builtins.input", answer)
    assert main(["--root", str(tmp_path), "--profile", "generic-adr"]) == 2
    out = capsys.readouterr().out
    assert "source changed after preview" in out
    assert "sidegraph-bootstrap --resume" in out
    assert not (tmp_path / ".sidegraph").exists()


def test_rejected_literal_confirmation_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    write_bootstrap_repo(tmp_path, host="claude-code")
    answers = iter(["a", "yes"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))

    assert main(["--root", str(tmp_path), "--profile", "generic-adr"]) == 2
    assert "CANCELLED" in capsys.readouterr().out
    assert not (tmp_path / ".sidegraph").exists()


def test_eof_during_initial_review_is_actionable_without_writes_or_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    write_bootstrap_repo(tmp_path, host="claude-code")

    def eof(_prompt: str) -> str:
        raise EOFError

    monkeypatch.setattr("builtins.input", eof)

    assert main(["--root", str(tmp_path), "--host", "claude-code"]) == 2
    captured = capsys.readouterr()
    assert "input ended during candidate review; no writes" in captured.out
    assert "sidegraph-bootstrap --resume" in captured.out
    assert "Traceback" not in captured.err
    assert not (tmp_path / ".sidegraph").exists()


def test_eof_during_final_confirmation_is_cancelled_without_writes_or_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    write_bootstrap_repo(tmp_path, host="claude-code")
    answers = iter(["a"])

    def eof_after_review(_prompt: str) -> str:
        try:
            return next(answers)
        except StopIteration:
            raise EOFError from None

    monkeypatch.setattr("builtins.input", eof_after_review)

    assert main(["--root", str(tmp_path), "--host", "claude-code"]) == 2
    captured = capsys.readouterr()
    assert "input ended during final confirmation; no writes" in captured.out
    assert "sidegraph-bootstrap --resume" in captured.out
    assert "Traceback" not in captured.err
    assert not (tmp_path / ".sidegraph").exists()


@pytest.mark.parametrize(
    ("setup", "next_action"),
    [
        (
            "missing-hooks",
            "Configure sidegraph-session-start in .codex/hooks/hooks.json, then rerun "
            "sidegraph-bootstrap",
        ),
        (
            "invalid-mcp",
            "Repair .codex/config.toml, then rerun sidegraph-bootstrap",
        ),
    ],
)
def test_missing_or_invalid_supported_codex_check_is_actionable_incomplete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    setup: str,
    next_action: str,
) -> None:
    write_bootstrap_repo(tmp_path, host="codex", complete_host=False)
    if setup == "missing-hooks":
        write(
            tmp_path / ".codex/config.toml",
            '[mcp_servers.sidegraph]\ncommand = "sidegraph-mcp"\n',
        )
    else:
        write(tmp_path / ".codex/config.toml", "not = [valid\n")
    accepted_answers(monkeypatch)

    assert main(["--root", str(tmp_path), "--host", "codex"]) == 2
    out = capsys.readouterr().out
    assert next_action in out
    assert "fully supported" not in out


def test_codex_misplaced_stop_command_is_actionable_not_best_effort(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    write_bootstrap_repo(tmp_path, host="codex")
    write(
        tmp_path / ".codex" / "hooks" / "hooks.json",
        json.dumps(
            {
                "hooks": {
                    "SessionStart": [
                        {"command": "sidegraph-session-start"},
                        {"command": "sidegraph-stop"},
                    ]
                }
            }
        )
        + "\n",
    )
    accepted_answers(monkeypatch)

    assert main(["--root", str(tmp_path), "--host", "codex"]) == 2
    out = capsys.readouterr().out
    assert "INTEGRATION  codex incomplete" in out
    assert "Configure sidegraph-stop in .codex/hooks/hooks.json" in out
    assert "Codex best-effort" not in out


def test_report_is_opt_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    write_bootstrap_repo(tmp_path, host="claude-code")
    accepted_answers(monkeypatch)

    assert main(["--root", str(tmp_path), "--host", "claude-code"]) == 0
    capsys.readouterr()
    assert not (tmp_path / "bootstrap-report.md").exists()


def assert_pre_review_report_collision(
    root: Path,
    report: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    *extra_args: str,
    protected_label: str,
) -> None:
    graph = root / "graphify-out" / "graph.json"
    graph_before = graph.read_bytes()
    report_before = report.read_bytes() if report.is_file() else None
    monkeypatch.setattr("builtins.input", forbidden_review_input)

    rc = main(["--root", str(root), "--report", str(report), *extra_args])

    captured = capsys.readouterr()
    assert rc == 1
    assert f"--report destination conflicts with protected {protected_label}" in captured.err
    assert "Traceback" not in captured.err
    assert graph.read_bytes() == graph_before
    if report_before is not None:
        assert report.read_bytes() == report_before
    assert not (root / ".sidegraph").exists()


def test_report_cannot_overwrite_graph_input_before_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    write_bootstrap_repo(tmp_path, host="claude-code")
    graph = tmp_path / "graphify-out" / "graph.json"

    assert_pre_review_report_collision(
        tmp_path,
        graph,
        monkeypatch,
        capsys,
        protected_label="graph input",
    )


def test_report_symlink_cannot_overwrite_graph_input_before_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    write_bootstrap_repo(tmp_path, host="claude-code")
    graph = tmp_path / "graphify-out" / "graph.json"
    report_link = tmp_path / "bootstrap-report.md"
    report_link.symlink_to(graph)

    assert_pre_review_report_collision(
        tmp_path,
        report_link,
        monkeypatch,
        capsys,
        protected_label="graph input",
    )


def test_report_cannot_overwrite_scanned_source_before_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    write_bootstrap_repo(tmp_path, host="claude-code")
    source = tmp_path / "docs" / "adr" / "001-retry.md"

    assert_pre_review_report_collision(
        tmp_path,
        source,
        monkeypatch,
        capsys,
        protected_label="scanned source docs/adr/001-retry.md",
    )


def test_report_symlink_cannot_overwrite_scanned_source_before_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    write_bootstrap_repo(tmp_path, host="claude-code")
    source = tmp_path / "docs" / "adr" / "001-retry.md"
    report_link = tmp_path / "bootstrap-report.md"
    report_link.symlink_to(source)

    assert_pre_review_report_collision(
        tmp_path,
        report_link,
        monkeypatch,
        capsys,
        protected_label="scanned source docs/adr/001-retry.md",
    )


def test_report_cannot_target_source_discovered_by_refresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    write_bootstrap_repo(tmp_path, host="claude-code")
    refreshed_source = tmp_path / "docs" / "adr" / "002-new.md"

    def add_source_after_first_action(_prompt: str) -> str:
        write(
            refreshed_source,
            "# New retry limit\n\n## Context\n\nRetries are bounded.\n\n"
            "## Decision\n\nUse four retries.\n",
        )
        return "a"

    monkeypatch.setattr("builtins.input", add_source_after_first_action)

    assert main(["--root", str(tmp_path), "--report", str(refreshed_source)]) == 2
    out = capsys.readouterr().out
    assert "refresh failed" in out
    assert "protected scanned source docs/adr/002-new.md" in out
    assert not (tmp_path / ".sidegraph").exists()


def test_report_destination_is_rechecked_against_sources_immediately_before_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    write_bootstrap_repo(tmp_path, host="claude-code")
    source = tmp_path / "docs" / "adr" / "001-retry.md"
    source_before = source.read_bytes()
    report_link = tmp_path / "bootstrap-report.md"
    accepted_answers(monkeypatch)

    def replace_report_with_source_alias(*args, **kwargs) -> None:
        render_completion(*args, **kwargs)
        report_link.symlink_to(source)

    monkeypatch.setattr(
        "sidegraph.bootstrap.cli.render_completion", replace_report_with_source_alias
    )

    assert main(["--root", str(tmp_path), "--report", str(report_link)]) == 2
    out = capsys.readouterr().out
    assert "report write failed" in out
    assert "protected scanned source docs/adr/001-retry.md" in out
    assert source.read_bytes() == source_before


def test_report_cannot_target_sidegraph_store_tree_before_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    write_bootstrap_repo(tmp_path, host="claude-code")

    assert_pre_review_report_collision(
        tmp_path,
        tmp_path / "state" / "decisions" / "bootstrap.md",
        monkeypatch,
        capsys,
        "--db",
        "state",
        protected_label="Sidegraph store",
    )


def test_report_cannot_overwrite_selected_claude_config_before_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    write_bootstrap_repo(tmp_path, host="claude-code")

    assert_pre_review_report_collision(
        tmp_path,
        tmp_path / ".mcp.json",
        monkeypatch,
        capsys,
        protected_label="Claude Code MCP config",
    )


def test_report_cannot_overwrite_explicit_codex_config_before_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    write_bootstrap_repo(tmp_path, host="codex")
    config = tmp_path / "config" / "codex.toml"
    write(config, '[mcp_servers.sidegraph]\ncommand = "sidegraph-mcp"\n')

    assert_pre_review_report_collision(
        tmp_path,
        config,
        monkeypatch,
        capsys,
        "--host",
        "codex",
        "--codex-config",
        str(config),
        protected_label="Codex config",
    )


def test_user_selected_task_is_used_for_proof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    write_bootstrap_repo(tmp_path, host="claude-code")
    accepted_answers(monkeypatch)
    report = tmp_path / "bootstrap-report.md"

    assert (
        main(
            [
                "--root",
                str(tmp_path),
                "--host",
                "claude-code",
                "--task",
                "docs/unrelated.md",
                "--report",
                str(report),
            ]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "PROOF        production retrieval returned" in out
    assert "TASK         docs/unrelated.md" in out
    assert "TASK PROOF   incomplete: selected decision did not surface" in out
    rendered_report = report.read_text(encoding="utf-8")
    assert "## Proof" in rendered_report
    assert "## Optional task proof" in rendered_report
    assert "- complete: false" in rendered_report


def test_default_proof_rejects_older_global_record_not_accepted_in_current_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    write(
        tmp_path / "docs" / "adr" / "001-retry.md",
        "# Current retry policy\n\n## Context\n\nRetries need a bound.\n\n"
        "## Decision\n\nUse three retries.\n",
    )
    write_graph(tmp_path)
    write_claude_config(tmp_path)
    store = Store(tmp_path / ".sidegraph")
    old_entity = store.upsert_entity(
        Entity(
            canonical_name="Old global rule",
            kind=EntityKind.CONCRETE,
            descriptor=Descriptor(name="Old global rule", file_path="src/old.py"),
        )
    )
    old = store.add_decision(
        Decision(
            title="Old global gotcha",
            kind=DecisionKind.GOTCHA,
            status=DecisionStatus.ACCEPTED,
            context="Old context",
            choice="Old choice",
            valid_from=datetime(2025, 1, 1, tzinfo=UTC),
            provenance=Provenance(source="manual", ref="docs/old.md"),
        )
    )
    store.add_binding(
        AnchorBinding(record_id=old.id, entity_id=old_entity.entity_id, tier=2, status="live")
    )
    store.close()
    accepted_answers(monkeypatch)
    monkeypatch.setattr(
        "sidegraph.bootstrap.proof.get_task_context",
        lambda *args, **kwargs: TaskContext(
            decisions=[f"- old global line (id: {old.id})"],
            shown_ids=[old.id],
        ),
    )

    assert main(["--root", str(tmp_path), "--host", "claude-code"]) == 2
    out = capsys.readouterr().out
    assert "PROOF        incomplete: selected decision did not surface" in out
    assert "old global line" not in out


def test_report_write_failure_after_confirmation_exits_actionable_incomplete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    write_bootstrap_repo(tmp_path, host="claude-code")
    accepted_answers(monkeypatch)
    report_directory = tmp_path / "report-is-a-directory"
    report_directory.mkdir()

    assert (
        main(
            [
                "--root",
                str(tmp_path),
                "--host",
                "claude-code",
                "--report",
                str(report_directory),
            ]
        )
        == 2
    )
    captured = capsys.readouterr()
    assert "report write failed" in captured.out
    assert "sidegraph-bootstrap --resume" in captured.out
    assert "ERROR" not in captured.err


def test_recovery_fields_are_structured_and_terminal_safe(
    capsys: pytest.CaptureFixture[str],
) -> None:
    report = BootstrapReport(
        status=RunStatus.PARTIAL_RECOVERABLE,
        documents_scanned=2,
        candidates=2,
        accepted=2,
        kept_proposed=0,
        skipped=0,
        live_anchors=2,
        review_debt_count=0,
        durable_candidate_keys=("durable\x1b[31m",),
        pending_candidate_keys=("pending\nINJECT",),
        canonical_files=(".sidegraph/decisions/record.json",),
        verification_failures=("SG001\nINJECT",),
        failed_ref="docs/adr/002.md\nINJECT",
        error="private source detail",
        next_command="sidegraph-bootstrap --resume",
    )
    integration = IntegrationResult(
        host=HostKind.CLAUDE_CODE,
        mcp="missing",
        session_start="missing",
        stop="missing",
        pretool_read_grep="missing",
        fully_supported=False,
    )

    render_completion(
        report,
        ReviewResult(items=()),
        integration,
        ProofResult(complete=False, reason="activation is partial-recoverable"),
        DomainSuggestions(),
        task=None,
    )

    out = capsys.readouterr().out
    assert "FAILED REF   docs/adr/002.md\\nINJECT" in out
    assert "DURABLE      durable\\x1b[31m" in out
    assert "PENDING      pending\\nINJECT" in out
    assert "VERIFY       SG001\\nINJECT" in out
    assert "CHANGED      .sidegraph/decisions/record.json" in out
    assert "private source detail" not in out
    assert "\x1b" not in out


def test_anchors_message_names_the_accepted_requirement(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Red against unfixed code: the line said proposals do not block anchor checks, while
    _accepted_anchors_complete requires at least one accept, so a run with no accepted
    candidate could never exit 0."""
    render_completion(
        BootstrapReport(
            status=RunStatus.COMPLETE,
            documents_scanned=1,
            candidates=1,
            accepted=0,
            kept_proposed=1,
            skipped=0,
            live_anchors=0,
            review_debt_count=1,
        ),
        ReviewResult(items=()),
        IntegrationResult(
            host=HostKind.CLAUDE_CODE,
            mcp="verified",
            session_start="verified",
            stop="verified",
            pretool_read_grep="verified",
            fully_supported=True,
        ),
        ProofResult(complete=False, reason="no accepted record"),
        DomainSuggestions(),
        task=None,
    )

    out = capsys.readouterr().out
    assert "activation needs at least one accepted candidate" in out
    assert "proposals do not block anchor checks" not in out


def test_console_script_count_in_cli_reference_matches_pyproject() -> None:
    """Red against unfixed code: the doc says eleven registered, pyproject registers 15."""
    pyproject = Path("pyproject.toml").read_text(encoding="utf-8")
    block = pyproject.split("[project.scripts]", 1)[1].split("\n[", 1)[0]
    registered = [line for line in block.splitlines() if line.startswith("sidegraph-")]
    doc = Path("docs/reference/cli.md").read_text(encoding="utf-8")
    assert f"{len(registered)} console scripts" in doc.replace("Fifteen", "15")


def test_resume_flag_is_documented_as_an_idempotent_marker() -> None:
    """--resume changes no behavior; the help text must say so, not describe a distinct mode."""
    help_text = build_parser().format_help()
    assert "marker for a resumed run" in help_text
    assert "idempotent" in help_text


def test_post_confirmation_fault_renders_durable_recovery_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    write_bootstrap_repo(tmp_path, host="claude-code")
    accepted_answers(monkeypatch)

    def fail_index_write(self, decision) -> None:
        raise RuntimeError("index write failed")

    monkeypatch.setattr(Store, "_index_write_decision", fail_index_write)

    assert main(["--root", str(tmp_path), "--host", "claude-code"]) == 2
    out = capsys.readouterr().out
    assert "FAILED REF   docs/adr/001-retry.md" in out
    assert "DURABLE      9b08bad0d6d48d02" in out
    assert "CHANGED      .sidegraph/decisions/" in out
    assert "RESUME       sidegraph-bootstrap --resume" in out


def test_completion_render_io_failure_after_durable_write_is_partial_recoverable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Red against the swallow: apply_review already wrote canonical state durably before
    render_completion ever runs. An I/O failure in the completion render (e.g. a broken
    output pipe) must not fall through to main()'s catch-all
    `except (OSError, ...) -> return 1` -- exit 1 is reserved for a usage or operational
    error BEFORE review, and this run already has a durable decision file on disk. It must
    be 2 (partial-recoverable), not 1, and it must still try to print a resume line."""
    write_bootstrap_repo(tmp_path, host="claude-code")
    accepted_answers(monkeypatch)

    def dead_pipe(*_args: object, **_kwargs: object) -> None:
        raise OSError("stdout is a dead pipe")

    monkeypatch.setattr("sidegraph.bootstrap.cli.render_completion", dead_pipe)

    rc = main(["--root", str(tmp_path), "--profile", "generic-adr", "--host", "claude-code"])
    assert rc == 2
    decisions = list((tmp_path / ".sidegraph" / "decisions").glob("*.json"))
    assert decisions, "apply_review must have durably written a decision before the render failed"
    out = capsys.readouterr().out
    assert "RESUME       " in out


@pytest.mark.parametrize(
    "raise_exc",
    [
        RecursionError("too much nesting"),
        TypeError("unexpected type"),
        KeyboardInterrupt(),
    ],
    ids=["RecursionError", "TypeError", "KeyboardInterrupt"],
)
def test_completion_escapes_handled_types_after_durable_write_is_partial_recoverable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    raise_exc: BaseException,
) -> None:
    """Red against an incomplete guard: the test above only proves the contract for
    OSError, the one type the handler at cli.py:746 already caught -- it never steps
    outside (OSError, UnicodeError, ValueError). RecursionError is the real, uninjected
    reachability path (a deeply nested .mcp.json makes the unguarded recursion in
    integrations._command_strings raise inside verify_integration, which apply_review
    runs before); TypeError stands in for any other ordinary bug in this tail; and
    KeyboardInterrupt is a BaseException that a plain `except Exception` would still
    miss. All three currently escape both this handler and main()'s matching catch-all
    (`except (OSError, UnicodeError, ValueError) -> return 1`), reaching the test as an
    uncaught exception instead of a clean exit code -- while apply_review already wrote
    a canonical decision to disk. The contract must hold for every exception class that
    can happen here, not just the one the existing test happens to inject."""
    write_bootstrap_repo(tmp_path, host="claude-code")
    accepted_answers(monkeypatch)

    def broken_render(*_args: object, **_kwargs: object) -> None:
        raise raise_exc

    monkeypatch.setattr("sidegraph.bootstrap.cli.render_completion", broken_render)

    rc = main(["--root", str(tmp_path), "--profile", "generic-adr", "--host", "claude-code"])
    assert rc == 2
    decisions = list((tmp_path / ".sidegraph" / "decisions").glob("*.json"))
    assert decisions, "apply_review must have durably written a decision before completion failed"
    out = capsys.readouterr().out
    assert "RESUME       " in out


def test_report_write_escapes_handled_types_after_durable_write_is_partial_recoverable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Same defect, second site: the report-write handler at cli.py:770 guards the same
    three types as the completion handler above and misses everything else. A plain
    TypeError from write_report_only_when_requested must still resolve to exit 2 with a
    resume line, not escape to main()'s catch-all or an uncaught traceback, given the
    decision was already durably written by apply_review."""
    write_bootstrap_repo(tmp_path, host="claude-code")
    accepted_answers(monkeypatch)

    def broken_report_write(*_args: object, **_kwargs: object) -> None:
        raise TypeError("unexpected type")

    monkeypatch.setattr(
        "sidegraph.bootstrap.cli.write_report_only_when_requested", broken_report_write
    )

    rc = main(
        [
            "--root",
            str(tmp_path),
            "--profile",
            "generic-adr",
            "--host",
            "claude-code",
            "--report",
            str(tmp_path / "bootstrap-report.md"),
        ]
    )
    assert rc == 2
    decisions = list((tmp_path / ".sidegraph" / "decisions").glob("*.json"))
    assert decisions, "apply_review must have durably written a decision before the report failed"
    out = capsys.readouterr().out
    assert "RESUME       " in out


def test_report_write_failure_with_dead_output_channel_is_still_partial_recoverable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Red one layer in: the report-write handler above now catches BaseException, but
    its own two print() calls were left unwrapped -- unlike the completion handler's,
    which wraps its prints in suppress(BaseException). This is exactly the scenario that
    handler exists for: a dead output channel. When write_report_only_when_requested
    fails AND stdout is dead, the handler's own `print(...)` raises OSError, which
    escapes the except block, out of _main, into main()'s own catch-all -- which still
    catches OSError and returns 1 -- misreporting a run that already durably wrote a
    canonical decision as having failed before writing anything. Must be 2, not 1, with
    no traceback. (Cannot assert a RESUME line here -- the whole point is the channel it
    would be printed to is dead -- so this test only asserts the exit code and the
    durable write, not captured output.)"""
    write_bootstrap_repo(tmp_path, host="claude-code")
    accepted_answers(monkeypatch)

    class DeadPipe:
        def write(self, _s: str) -> int:
            raise OSError("stdout is a dead pipe")

        def flush(self) -> None:
            raise OSError("stdout is a dead pipe")

    def broken_report_write(*_args: object, **_kwargs: object) -> None:
        monkeypatch.setattr(sys, "stdout", DeadPipe())
        raise TypeError("unexpected type")

    monkeypatch.setattr(
        "sidegraph.bootstrap.cli.write_report_only_when_requested", broken_report_write
    )

    rc = main(
        [
            "--root",
            str(tmp_path),
            "--profile",
            "generic-adr",
            "--host",
            "claude-code",
            "--report",
            str(tmp_path / "bootstrap-report.md"),
        ]
    )
    assert rc == 2
    decisions = list((tmp_path / ".sidegraph" / "decisions").glob("*.json"))
    assert decisions, "apply_review must have durably written a decision before the report failed"


def test_completion_failure_diagnostic_identifies_the_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """{error} alone renders str() only -- for KeyboardInterrupt() (an empty message)
    the ACTIONABLE line used to print 'completion failed after durable write: ' with
    NOTHING after the colon, so a real Ctrl-C after the write and a genuine internal bug
    with no message were indistinguishable: an empty reason either way. The exception's
    type name must appear on stdout's ACTIONABLE line, and the full traceback must land
    on stderr (never stdout, which downstream tooling parses for ACTIONABLE/RESUME) so a
    genuine bug is still post-mortem debuggable."""
    write_bootstrap_repo(tmp_path, host="claude-code")
    accepted_answers(monkeypatch)

    def broken_render(*_args: object, **_kwargs: object) -> None:
        raise KeyboardInterrupt()

    monkeypatch.setattr("sidegraph.bootstrap.cli.render_completion", broken_render)

    rc = main(["--root", str(tmp_path), "--profile", "generic-adr", "--host", "claude-code"])
    assert rc == 2
    captured = capsys.readouterr()
    assert "ACTIONABLE   completion failed after durable write: KeyboardInterrupt" in captured.out
    assert "Traceback (most recent call last)" in captured.err
    assert "KeyboardInterrupt" in captured.err


def test_report_write_failure_diagnostic_identifies_the_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Same defect, second site: the report-write handler's ACTIONABLE line also rendered
    only str(error), losing the exception's identity for anything with a thin or empty
    message. Type name on stdout, full traceback on stderr, same as the completion
    handler above."""
    write_bootstrap_repo(tmp_path, host="claude-code")
    accepted_answers(monkeypatch)

    def broken_report_write(*_args: object, **_kwargs: object) -> None:
        raise ValueError("boom")

    monkeypatch.setattr(
        "sidegraph.bootstrap.cli.write_report_only_when_requested", broken_report_write
    )

    rc = main(
        [
            "--root",
            str(tmp_path),
            "--profile",
            "generic-adr",
            "--host",
            "claude-code",
            "--report",
            str(tmp_path / "bootstrap-report.md"),
        ]
    )
    assert rc == 2
    captured = capsys.readouterr()
    assert "ACTIONABLE   report write failed: ValueError: boom" in captured.out
    assert "Traceback (most recent call last)" in captured.err
    assert "ValueError: boom" in captured.err


def test_unknown_candidate_key_is_usage_error_without_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    write_bootstrap_repo(tmp_path, host="claude-code")
    monkeypatch.setattr("builtins.input", forbidden_review_input)

    assert main(["--root", str(tmp_path), "--candidate", "not-a-key"]) == 1
    assert "unknown candidate key" in capsys.readouterr().err
    assert not (tmp_path / ".sidegraph").exists()


def test_candidate_selection_tracks_source_across_refresh_key_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    write_bootstrap_repo(tmp_path, host="claude-code")
    initial_key = only_candidate_key(tmp_path)
    answers = iter(["a", "a", "confirm"])

    def edit_after_initial_action(_prompt: str) -> str:
        answer = next(answers)
        if answer == "a" and not hasattr(edit_after_initial_action, "edited"):
            edit_after_initial_action.edited = True
            rewrite_adr_choice(tmp_path, "Use four bounded retries.")
        return answer

    monkeypatch.setattr("builtins.input", edit_after_initial_action)

    assert main(["--root", str(tmp_path), "--candidate", initial_key]) == 0
    out = capsys.readouterr().out
    assert "stale actions discarded" in out
    catalog = load_canonical_catalog(tmp_path / ".sidegraph")
    assert len(catalog.decisions) == 1
    assert catalog.decisions[0].choice == "Use four bounded retries."


def test_refresh_resume_command_pins_refreshed_candidate_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    write_bootstrap_repo(tmp_path, host="claude-code")
    initial_key = only_candidate_key(tmp_path)
    answers = iter(["a", "a", "not-confirmed"])

    def edit_before_refresh(_prompt: str) -> str:
        answer = next(answers)
        if answer == "a" and not hasattr(edit_before_refresh, "edited"):
            edit_before_refresh.edited = True
            rewrite_adr_choice(tmp_path, "Use five bounded retries.")
        return answer

    monkeypatch.setattr("builtins.input", edit_before_refresh)

    assert main(["--root", str(tmp_path), "--candidate", initial_key]) == 2
    refreshed_key = only_candidate_key(tmp_path)
    out = capsys.readouterr().out
    resume = next(line for line in out.splitlines() if line.startswith("RESUME       "))
    assert f"--candidate {refreshed_key}" in resume
    assert initial_key not in resume
    assert not (tmp_path / ".sidegraph").exists()


def test_domain_suggestions_are_read_only_and_do_not_gate_proof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    write_bootstrap_repo(
        tmp_path,
        host="claude-code",
        domain_candidate=True,
    )
    accepted_answers(monkeypatch)

    assert main(["--root", str(tmp_path), "--host", "claude-code"]) == 0
    out = capsys.readouterr().out
    assert "PROOF        production retrieval returned" in out
    assert "Optional orientation follow-up: sidegraph-domains bootstrap --dry-run" in out
    assert not tuple((tmp_path / ".sidegraph" / "domains").glob("*.json"))


def test_resume_reruns_review_and_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Three runs from identical store state: run 1 populates the store (its stdout is
    discarded — imports differ legitimately from converged reruns); run 2 is the converged
    baseline; run 3 passes --resume and must match run 2 exactly, since --resume is a marker
    only and changes no behavior."""
    write_bootstrap_repo(tmp_path, host="claude-code")
    answers = iter(["a", "confirm", "a", "confirm", "a", "confirm"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))
    base = ["--root", str(tmp_path), "--host", "claude-code"]

    assert main(base) == 0
    capsys.readouterr()

    converged_exit = main(base)
    converged_out = capsys.readouterr().out
    assert "RESUME       " not in converged_out

    resumed_exit = main([*base, "--resume"])
    resumed_out = capsys.readouterr().out

    assert resumed_exit == converged_exit
    assert resumed_out == converged_out

    catalog = load_canonical_catalog(tmp_path / ".sidegraph")
    assert len(catalog.decisions) == 1


def test_resume_accepts_unchanged_proposal_without_duplicate_or_pending_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Treating an unchanged proposal as already done prevents resume convergence."""
    write_bootstrap_repo(tmp_path, host="claude-code")
    answers = iter(["p", "confirm", "a", "confirm"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))
    candidate_key = only_candidate_key(tmp_path)
    report_path = tmp_path / "reports" / "proposal.md"
    base = [
        "--root",
        str(tmp_path),
        "--db",
        "state",
        "--graph",
        "graphify-out/graph.json",
        "--profile",
        "generic-adr",
        "--docs",
        "docs",
        "--include",
        "docs/adr/001-retry.md",
        "--host",
        "claude-code",
        "--candidate",
        candidate_key,
        "--report",
        "reports/proposal.md",
    ]
    exact_resume = (
        f"sidegraph-bootstrap --resume --root {tmp_path} --db {tmp_path / 'state'} "
        f"--graph {tmp_path / 'graphify-out/graph.json'} --profile generic-adr "
        "--host claude-code --docs docs --include docs/adr/001-retry.md "
        f"--candidate {candidate_key} --report {report_path}"
    )

    assert main(base) == 2
    first_out = capsys.readouterr().out
    assert f"RESUME       {exact_resume}" in first_out
    assert f"- next command: `{exact_resume}`" in report_path.read_text(encoding="utf-8")
    assert main([*base, "--resume"]) == 0
    out = capsys.readouterr().out

    catalog = load_canonical_catalog(tmp_path / "state")
    assert len(catalog.decisions) == 1
    assert catalog.decisions[0].status.value == "accepted"
    assert "STORE        ready" in out
    assert "PROOF        production retrieval returned" in out
    assert "RESUME       " not in out


def test_canonical_complete_integration_failure_renders_and_reports_exact_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A complete store still needs a replay command when the selected host is incomplete."""
    write_bootstrap_repo(tmp_path, host="codex", complete_host=False)
    codex_config = tmp_path / "config" / "codex.toml"
    write(codex_config, '[mcp_servers.sidegraph]\ncommand = "sidegraph-mcp"\n')
    accepted_answers(monkeypatch)
    report_path = tmp_path / "reports" / "integration.md"
    argv = [
        "--root",
        str(tmp_path),
        "--db",
        "state",
        "--graph",
        "graphify-out/graph.json",
        "--profile",
        "generic-adr",
        "--host",
        "codex",
        "--codex-config",
        "config/codex.toml",
        "--task",
        "docs/adr/001-retry.md",
        "--report",
        "reports/integration.md",
    ]
    exact_resume = (
        f"sidegraph-bootstrap --resume --root {tmp_path} --db {tmp_path / 'state'} "
        f"--graph {tmp_path / 'graphify-out/graph.json'} --profile generic-adr "
        "--host codex --codex-config config/codex.toml --task docs/adr/001-retry.md "
        f"--report {report_path}"
    )

    assert main(argv) == 2
    out = capsys.readouterr().out
    rendered_report = report_path.read_text(encoding="utf-8")

    assert "STORE        ready" in out
    assert "INTEGRATION  codex incomplete" in out
    assert "PROOF        production retrieval returned" in out
    assert f"RESUME       {exact_resume}" in out
    assert f"- next command: `{exact_resume}`" in rendered_report
