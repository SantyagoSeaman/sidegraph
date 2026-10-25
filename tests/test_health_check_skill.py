"""Read-only health-check skill contract, using only isolated synthetic inputs."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from sidegraph.schema import Decision, DecisionKind, DecisionStatus, Provenance
from sidegraph.store import Store
from sidegraph.store_layout import SCHEMA_VERSION

_ROOT = Path(__file__).resolve().parents[1]
_COLLECTOR = _ROOT / "plugin/sidegraph/skills/health-check/scripts/collect.py"
_GIT = shutil.which("git")


def _test_path() -> str:
    git_dir = str(Path(_GIT).parent) if _GIT else ""
    return os.pathsep.join(part for part in (git_dir, os.defpath) if part)


def _collector():
    spec = importlib.util.spec_from_file_location("sidegraph_health_check", _COLLECTOR)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    project = tmp_path / "project"
    project.mkdir()
    store = project / ".sidegraph"
    store.mkdir()
    (store / "format").write_text(f"sidegraph-store {SCHEMA_VERSION}\n", encoding="utf-8")
    for name in ("decisions", "facts", "domains", "entities", "bindings", "archive", "initiatives"):
        (store / name).mkdir()
    (project / "graphify-out").mkdir()
    (project / "graphify-out/graph.json").write_text(
        '{"graph": {"graphify_version": "0.9.90"}, "built_at_commit": "unknown", '
        '"nodes": [], "links": []}\n',
        encoding="utf-8",
    )
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home / ".claude"))
    monkeypatch.setenv("PATH", _test_path())
    for name in (
        "SIDEGRAPH_DIR",
        "SIDEGRAPH_DB",
        "SIDEGRAPH_GRAPH",
        "PLUGIN_ROOT",
        "CLAUDE_PLUGIN_ROOT",
    ):
        monkeypatch.delenv(name, raising=False)
    subprocess.run(["git", "init", "-q"], cwd=project, check=True, capture_output=True)
    subprocess.run(["git", "add", "-A"], cwd=project, check=True, capture_output=True)
    return project, store


def _manifest(
    root: Path, *, allow_locked_shm: bool = False
) -> dict[str, bytes | tuple[str, int, int]]:
    result: dict[str, bytes | tuple[str, int, int]] = {}
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        name = str(path.relative_to(root))
        try:
            result[name] = path.read_bytes()
        except PermissionError:
            if not allow_locked_shm or path.name != "index.db-shm":
                raise
            stat = path.stat()
            result[name] = ("locked_sqlite_shm_bytes_unverified", stat.st_size, stat.st_mtime_ns)
    return result


def test_collect_returns_six_sections_and_unknown_active_mcp_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, store = _sandbox(tmp_path, monkeypatch)

    report = _collector().collect(
        project=project, store=store, graph=project / "graphify-out/graph.json"
    )

    assert [section["name"] for section in report["sections"]] == [
        "plugin_and_wiring",
        "versions",
        "graphify",
        "store_integrity",
        "anchors_and_entities",
        "inventory_and_usage",
    ]
    assert {section["status"] for section in report["sections"]} <= {
        "OK",
        "warning",
        "broken",
    }
    assert report["overall"] == "warning"
    assert report["sections"][0]["evidence"]["active_mcp"]["status"] == "UNKNOWN"


@pytest.mark.parametrize("unsafe_state", ["open_wal", "nonempty_journal", "corrupt_header"])
def test_other_unsafe_indexes_are_not_opened(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, unsafe_state: str
) -> None:
    project, store = _sandbox(tmp_path, monkeypatch)
    index = store / "index.db"
    live_connection: sqlite3.Connection | None = None
    if unsafe_state == "open_wal":
        live_connection = sqlite3.connect(index)
        live_connection.execute("CREATE TABLE fixture (value TEXT)")
        live_connection.execute("PRAGMA journal_mode=WAL")
        live_connection.execute("INSERT INTO fixture VALUES ('held-open')")
        assert (store / "index.db-wal").is_file()
        assert (store / "index.db-shm").is_file()
    elif unsafe_state == "nonempty_journal":
        connection = sqlite3.connect(index)
        try:
            connection.execute("CREATE TABLE fixture (value TEXT)")
        finally:
            connection.close()
        (store / "index.db-journal").write_bytes(b"synthetic nonempty journal")
    else:
        index.write_bytes(b"not a recognized sqlite database")
    before = _manifest(project, allow_locked_shm=unsafe_state == "open_wal")
    events: list[str] = []
    active = [True]

    def capture(event: str, args: tuple[object, ...]) -> None:
        if active[0] and event == "sqlite3.connect" and args and "index.db" in str(args[0]):
            events.append(str(args[0]))

    sys.addaudithook(capture)

    try:
        audit_probe_dir = tmp_path / "audit probe with spaces"
        audit_probe_dir.mkdir()
        audit_probe_path = audit_probe_dir / "index.db"
        audit_probe_uri = f"{audit_probe_path.as_uri()}?mode=rwc"
        probe_connection = sqlite3.connect(audit_probe_uri, uri=True)
        probe_connection.close()
        assert len(events) == 1
        events.clear()

        report = _collector().collect(
            project=project, store=store, graph=project / "graphify-out/graph.json"
        )
        assert report["sections"][4]["evidence"]["doctor_advisories"]["counts_by_code"] == "UNKNOWN"
        assert report["sections"][5]["evidence"]["statistics"]["status"] == "UNKNOWN"
        after = _manifest(project, allow_locked_shm=unsafe_state == "open_wal")
        assert after == before
        if any(
            isinstance(value, tuple) and value[0] == "locked_sqlite_shm_bytes_unverified"
            for value in before.values()
        ):
            assert report["preservation"]["status"] == "unstable_or_unverified"
            assert report["preservation"]["unreadable_input_count"] >= 1
    finally:
        active[0] = False
        if live_connection is not None:
            live_connection.close()
    assert events == []


def test_wal_guard_does_not_attempt_a_sqlite_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unsafe (WAL-mode) index is never opened, by any doctor or stats path, and no source
    file changes. The audit hook has a positive control, so it cannot pass vacuously."""
    project, store = _sandbox(tmp_path, monkeypatch)
    index = store / "index.db"
    connection = sqlite3.connect(index)
    try:
        connection.execute("CREATE TABLE anchor_bindings (value TEXT)")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("INSERT INTO anchor_bindings VALUES ('fixture')")
    finally:
        connection.close()
    assert index.read_bytes()[18:20] == b"\x02\x02"
    assert not (store / "index.db-wal").exists()
    assert not (store / "index.db-shm").exists()
    before = _manifest(project)
    events: list[str] = []
    active = [True]

    def capture(event: str, args: tuple[object, ...]) -> None:
        if active[0] and event == "sqlite3.connect" and args and "index.db" in str(args[0]):
            events.append(str(args[0]))

    sys.addaudithook(capture)
    try:
        audit_probe_dir = tmp_path / "audit probe with spaces"
        audit_probe_dir.mkdir()
        audit_probe_uri = f"{(audit_probe_dir / 'index.db').as_uri()}?mode=rwc"
        sqlite3.connect(audit_probe_uri, uri=True).close()
        assert len(events) == 1, "the audit hook must see an index.db open"
        events.clear()

        report = _collector().collect(
            project=project,
            store=store,
            graph=project / "graphify-out/graph.json",
            host="claude",
        )
    finally:
        active[0] = False

    assert _manifest(project) == before
    assert report["sections"][4]["evidence"]["doctor_advisories"]["counts_by_code"] == "UNKNOWN"
    assert report["sections"][4]["evidence"]["doctor_advisories"]["status"] == "UNKNOWN"
    assert report["sections"][5]["evidence"]["statistics"]["status"] == "UNKNOWN"
    assert report["overall"] == "warning"
    assert events == []


def test_missing_index_keeps_canonical_doctor_curation_available(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, store = _sandbox(tmp_path, monkeypatch)

    report = _collector().collect(
        project=project, store=store, graph=project / "graphify-out/graph.json"
    )

    doctor = report["sections"][4]["evidence"]["doctor_advisories"]
    assert doctor["status"] == "available"
    assert "binding-status" in doctor["skipped"]
    assert not (store / "index.db").exists()
    assert any(
        item["priority"] == "usage" and item["argv"][:2] == ["sidegraph-sync", "--json"]
        for item in report["recommendations"]
    )


def test_missing_index_still_reports_a_canonical_advisory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, store = _sandbox(tmp_path, monkeypatch)
    record = Decision(
        title="synthetic decision",
        kind=DecisionKind.ADR,
        status=DecisionStatus.ACCEPTED,
        context="private fixture context",
        choice="synthetic fixture choice",
        valid_from=datetime.now(UTC),
        provenance=Provenance(source="manual"),
    )
    (store / "decisions/synthetic.json").write_text(record.model_dump_json(), encoding="utf-8")

    report = _collector().collect(
        project=project, store=store, graph=project / "graphify-out/graph.json"
    )

    doctor = report["sections"][4]["evidence"]["doctor_advisories"]
    assert doctor["findings_by_code"]["dangling-record"] == 1
    assert doctor["skipped"]
    assert not (store / "index.db").exists()


def test_cold_index_status_is_preserved_in_stats_and_doctor_skips(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, store = _sandbox(tmp_path, monkeypatch)
    initialized = Store(store)
    initialized.close()

    report = _collector().collect(
        project=project, store=store, graph=project / "graphify-out/graph.json"
    )

    stats = report["sections"][5]["evidence"]["statistics"]
    assert stats["status"] == "available"
    assert stats["binding_statuses_computed"] is False
    assert "orphaned-records" in report["sections"][4]["evidence"]["doctor_advisories"]["skipped"]


def test_malformed_config_and_record_canary_do_not_reach_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, store = _sandbox(tmp_path, monkeypatch)
    secret = "health-check-canary-do-not-print"
    (project / ".claude").mkdir()
    (project / ".claude/settings.json").write_text(f"{{broken {secret}", encoding="utf-8")
    (store / "decisions/corrupt.json").write_text(f"{{broken {secret}", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=project, check=True, capture_output=True)

    plugin = tmp_path / "plugin"
    plugin.mkdir()
    (plugin / ".mcp.json").write_text(
        '{"mcpServers":{"sidegraph":{"command":"hidden"}}}', encoding="utf-8"
    )
    (plugin / "hooks").mkdir()
    (plugin / "hooks/hooks.json").write_text(f"{{{secret}", encoding="utf-8")
    report = _collector().collect(
        project=project, store=store, graph=project / "graphify-out/graph.json", plugin_root=plugin
    )

    serialized = json.dumps(report)
    assert secret not in serialized
    assert report["sections"][3]["status"] == "broken"
    assert report["sections"][3]["evidence"]["verification"]["status"] == "broken"
    wiring = report["sections"][0]["evidence"]["static_wiring"]
    assert wiring["status"] == "warning"
    assert wiring["files"][".mcp.json"]["server_names"] == ["sidegraph"]
    assert wiring["files"][".mcp.json"]["sidegraph_declared"] is True
    assert "hooks/hooks.json" in wiring["malformed_files"]


def test_claude_project_only_scope_is_visible(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, store = _sandbox(tmp_path, monkeypatch)
    (project / ".claude").mkdir()
    (project / ".claude/settings.json").write_text(
        '{"enabledPlugins":{"sidegraph@sidegraph":true}}', encoding="utf-8"
    )
    subprocess.run(["git", "add", "-A"], cwd=project, check=True, capture_output=True)
    monkeypatch.setenv("SIDEGRAPH_HOST", "claude")

    report = _collector().collect(
        project=project, store=store, graph=project / "graphify-out/graph.json", host="claude"
    )

    reach = report["sections"][0]["evidence"]["claude_reach"]
    assert reach["status"] == "available"
    assert reach["project_only"] is True


def test_nested_claude_off_scope_and_graph_refresh_guard_are_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, store = _sandbox(tmp_path, monkeypatch)
    (project / ".claude").mkdir()
    (project / ".claude/settings.json").write_text(
        '{"enabledPlugins":{"sidegraph@sidegraph":true}}', encoding="utf-8"
    )
    nested = project / "packages/nested"
    (nested / ".claude").mkdir(parents=True)
    (nested / ".claude/settings.json").write_text(
        '{"enabledPlugins":{"sidegraph@sidegraph":false}}', encoding="utf-8"
    )
    (nested / "README.md").write_text("nested project fixture\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=project, check=True, capture_output=True)
    plugin = tmp_path / "plugin"
    (plugin / "hooks").mkdir(parents=True)
    (plugin / "hooks/hooks.json").write_text(
        '{"hooks":{"SessionStart":[{"command":"GRAPHIFY_NO_AUTO_REFRESH=1"}]}}',
        encoding="utf-8",
    )
    monkeypatch.setenv("SIDEGRAPH_HOST", "claude")

    report = _collector().collect(
        project=project,
        store=store,
        graph=project / "graphify-out/graph.json",
        plugin_root=plugin,
        host="claude",
    )

    assert report["sections"][0]["evidence"]["claude_reach"]["nested_off"] == 1
    assert report["sections"][2]["evidence"]["refresh_hook_guard"] == {"status": "declared"}
    assert report["sections"][5]["evidence"]["canonical_counts"]["entities"] == 0
    activation_advice = [
        item for item in report["recommendations"] if item["priority"] == "activation"
    ]
    assert activation_advice
    assert activation_advice[0]["argv"] == [
        "sidegraph-doctor",
        "--json",
        "--db",
        str(store),
        "--graph",
        str(project / "graphify-out/graph.json"),
    ]
    assert activation_advice[0]["scope"] == (
        f"project={project}; launch={project}; settings=.claude/settings.json"
    )


def test_subprocess_invocation_preserves_project_and_git_index_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, store = _sandbox(tmp_path, monkeypatch)
    before = _manifest(project)
    env = os.environ.copy()
    env.update(
        {"HOME": str(tmp_path / "home"), "CLAUDE_CONFIG_DIR": str(tmp_path / "home/.claude")}
    )
    env["PATH"] = _test_path()
    env["SIDEGRAPH_HOST"] = "unknown"
    for name in (
        "SIDEGRAPH_DIR",
        "SIDEGRAPH_DB",
        "SIDEGRAPH_GRAPH",
        "PLUGIN_ROOT",
        "CLAUDE_PLUGIN_ROOT",
    ):
        env.pop(name, None)

    result = subprocess.run(
        [sys.executable, "-B", str(_COLLECTOR), "--project", str(project), "--store", str(store)],
        cwd=project,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )

    report = json.loads(result.stdout)
    assert len(report["sections"]) == 6
    assert report["preservation"]["status"] == "verified"
    assert _manifest(project) == before


def test_plugin_version_mismatch_is_reported_without_restart_advice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, store = _sandbox(tmp_path, monkeypatch)
    plugin = tmp_path / "plugin"
    (plugin / ".claude-plugin").mkdir(parents=True)
    (plugin / ".claude-plugin/plugin.json").write_text(
        '{"name":"sidegraph","version":"99.0.0"}', encoding="utf-8"
    )

    report = _collector().collect(
        project=project, store=store, graph=project / "graphify-out/graph.json", plugin_root=plugin
    )

    versions = report["sections"][1]
    assert versions["status"] == "warning"
    assert versions["evidence"]["collector_manifest_match"] is False
    assert all("restart" not in item["action"].lower() for item in report["recommendations"])
    assert any("metadata" in item["action"].lower() for item in report["recommendations"])


def test_missing_graph_is_a_warning_with_all_sections_preserved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, store = _sandbox(tmp_path, monkeypatch)

    report = _collector().collect(
        project=project,
        store=store,
        graph=project / "graphify-out/missing.json",
    )

    assert report["sections"][2]["status"] == "warning"
    assert report["sections"][2]["evidence"]["status"] == "UNKNOWN"
    assert len(report["sections"]) == 6


def test_stale_graph_is_reported_without_rebuild(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, store = _sandbox(tmp_path, monkeypatch)
    subprocess.run(
        ["git", "rm", "--cached", "-qr", "graphify-out"],
        cwd=project,
        check=True,
        capture_output=True,
    )
    (project / ".gitignore").write_text("graphify-out/\n", encoding="utf-8")
    (project / "tracked.py").write_text("value = 0\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=project, check=True, capture_output=True)
    git_identity = ["-c", "user.name=Health Check Fixture", "-c", "user.email=fixture@example.test"]
    subprocess.run(["git", *git_identity, "commit", "-qm", "initial"], cwd=project, check=True)
    built_at = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=project, check=True, capture_output=True, text=True
    ).stdout.strip()
    graph = project / "graphify-out/graph.json"
    graph.write_text(
        json.dumps(
            {
                "graph": {"graphify_version": "0.9.90"},
                "built_at_commit": built_at,
                "nodes": [
                    {
                        "id": "tracked-function",
                        "label": "tracked_function",
                        "norm_label": "tracked_function",
                        "file_type": "code",
                        "source_file": "tracked.py",
                        "source_location": "L1",
                        "community": 1,
                    }
                ],
                "links": [],
            }
        ),
        encoding="utf-8",
    )
    (project / "tracked.py").write_text("value = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.py"], cwd=project, check=True, capture_output=True)
    subprocess.run(
        ["git", *git_identity, "commit", "-qm", "change source"], cwd=project, check=True
    )

    source = project / "tracked.py"
    graph_mtime_ns = graph.stat().st_mtime_ns
    # The reader treats equality as fresh; stale evidence requires a strictly later source mtime.
    os.utime(source, ns=(graph_mtime_ns, graph_mtime_ns))
    reader = _collector()._imports()["GraphifyReader"](str(graph))
    equal_mtime = reader.freshness(root=project)
    assert equal_mtime.state == "fresh"
    assert equal_mtime.changed == 0

    later_mtime_ns = graph_mtime_ns + 1_000_000_000
    os.utime(source, ns=(later_mtime_ns, later_mtime_ns))

    report = _collector().collect(project=project, store=store, graph=graph)

    assert report["sections"][2]["status"] == "warning"
    assert report["sections"][2]["evidence"]["freshness"]["state"] == "stale"
    assert any(item["argv"] == ["graphify", "update", "."] for item in report["recommendations"])


def test_dirty_worktree_and_stale_stats_preserve_git_index_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, store = _sandbox(tmp_path, monkeypatch)
    initialized = Store(store)
    initialized.close()
    tracked = project / "tracked.txt"
    tracked.write_text("staged content\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=project, check=True, capture_output=True)
    tracked.write_text("dirty content\n", encoding="utf-8")
    record = Decision(
        title="synthetic dirty-index decision",
        kind=DecisionKind.ADR,
        status=DecisionStatus.ACCEPTED,
        context="private fixture context",
        choice="synthetic fixture choice",
        valid_from=datetime.now(UTC),
        provenance=Provenance(source="manual"),
    )
    (store / "decisions/dirty-index.json").write_text(record.model_dump_json(), encoding="utf-8")
    before = _manifest(project)

    report = _collector().collect(
        project=project,
        store=store,
        graph=project / "graphify-out/graph.json",
    )

    after = _manifest(project)
    assert after == before
    assert report["sections"][3]["evidence"]["git"]["dirty"] is True
    assert report["sections"][5]["evidence"]["statistics"]["index_stale"] is True
    assert report["preservation"]["status"] == "verified"
    assert any(
        item["priority"] == "usage" and item["argv"][:2] == ["sidegraph-sync", "--json"]
        for item in report["recommendations"]
    )


def test_unsupported_path_cli_is_unknown_not_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    collector = _collector()
    modules = collector._imports()
    monkeypatch.setattr(modules["hooks"], "_path_install", lambda: None)
    monkeypatch.setattr(
        collector.shutil, "which", lambda _name, path=None: str(tmp_path / "sidegraph-mcp")
    )

    result = collector._path_install_metadata(modules["hooks"])

    assert result["status"] == "UNKNOWN"
    monkeypatch.setattr(collector.shutil, "which", lambda _name, path=None: None)
    assert collector._path_install_metadata(modules["hooks"]) == {
        "status": "absent",
        "optional": True,
    }


def test_bundled_mcp_config_reports_nested_sidegraph_declaration() -> None:
    wiring = _collector()._wiring_declarations(_ROOT / "plugin/sidegraph")

    assert wiring["status"] == "available"
    assert wiring["files"][".mcp.json"]["server_names"] == ["sidegraph"]
    assert wiring["files"][".mcp.json"]["sidegraph_declared"] is True


def test_explicit_host_and_structured_scoped_recommendations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, store = _sandbox(tmp_path, monkeypatch)
    custom_store = tmp_path / "store ;$(echo not-a-command)"
    store.rename(custom_store)
    plugin = tmp_path / "plugin"
    (plugin / ".claude-plugin").mkdir(parents=True)
    (plugin / ".claude-plugin/plugin.json").write_text(
        '{"name":"sidegraph","version":"99.0.0"}', encoding="utf-8"
    )
    monkeypatch.delenv("SIDEGRAPH_HOST", raising=False)

    report = _collector().collect(
        project=project,
        store=custom_store,
        graph=project / "graphify-out/graph.json",
        plugin_root=plugin,
        host="claude",
    )

    assert report["sections"][0]["evidence"]["host"] == "claude"
    recommendations = report["recommendations"]
    assert recommendations
    assert all({"argv", "cwd", "env", "scope", "reason"} <= item.keys() for item in recommendations)
    assert all("command" not in item for item in recommendations)
    assert any(str(custom_store) in item["argv"] for item in recommendations)
    assert len({(tuple(item["argv"]), item["scope"]) for item in recommendations}) == len(
        recommendations
    )


def test_verify_and_git_unknown_are_warnings_not_false_ok_or_broken(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, store = _sandbox(tmp_path, monkeypatch)
    collector = _collector()
    modules = collector._imports()

    def failed_verify(_store: Path) -> list[object]:
        raise OSError("synthetic verify failure")

    monkeypatch.setattr(modules["verify"], "verify_snapshot", failed_verify)
    original_git = collector._git

    def missing_status(args: list[str], cwd: Path, git_env: object) -> str | None:
        if args == ["status", "--porcelain"]:
            return None
        return original_git(args, cwd, git_env)

    monkeypatch.setattr(collector, "_git", missing_status)
    report = collector.collect(
        project=project, store=store, graph=project / "graphify-out/graph.json"
    )

    integrity = report["sections"][3]
    assert integrity["status"] == "warning"
    assert integrity["evidence"]["verification"]["status"] == "UNKNOWN"
    assert integrity["evidence"]["git"]["dirty"] == "UNKNOWN"


def test_inventory_reports_invalid_pending_ulid_as_unknown_age() -> None:
    class FakeDoctor:
        _ulid_datetime = staticmethod(_collector()._imports()["doctor"]._ulid_datetime)

        @staticmethod
        def _iter_records(_store: Path, directory: str) -> list[tuple[Path, dict[str, str]]]:
            if directory == "decisions":
                return [(Path("record.json"), {"id": "not-a-ulid", "status": "proposed"})]
            return []

        @staticmethod
        def _check_archive_dir(_path: Path) -> SimpleNamespace:
            return SimpleNamespace(decisions=[], domains=[])

    inventory = _collector()._inventory(Path("/synthetic/store"), FakeDoctor)

    assert inventory["proposed_count"] == 1
    assert inventory["oldest_pending_age_days"] == "UNKNOWN"
    assert inventory["pending_age_unknown_count"] == 1
    assert "pending_age_unparseable_id" in inventory["unknown_reasons"]


def test_failed_inventory_reader_withholds_partial_pending_totals_and_keeps_sections(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, store = _sandbox(tmp_path, monkeypatch)
    initialized = Store(store)
    initialized.set_meta("volatile_stale", "0")
    initialized.close()
    collector = _collector()
    doctor = collector._imports()["doctor"]
    original_iter_records = doctor._iter_records

    def fail_decisions(store_path: Path, directory: str):
        if directory == "decisions":
            raise OSError("synthetic decisions read failure")
        return original_iter_records(store_path, directory)

    monkeypatch.setattr(doctor, "_iter_records", fail_decisions)

    report = collector.collect(
        project=project, store=store, graph=project / "graphify-out/graph.json"
    )

    assert len(report["sections"]) == 6
    inventory = report["sections"][5]
    assert inventory["status"] == "warning"
    assert inventory["evidence"]["statistics"]["status"] == "available"
    assert inventory["evidence"]["proposed_count"] == "UNKNOWN"
    assert inventory["evidence"]["oldest_pending_age_days"] == "UNKNOWN"
    assert inventory["evidence"]["pending_age_unknown_count"] == "UNKNOWN"
    assert "decisions_unreadable" in inventory["evidence"]["unknown_reasons"]


def test_unexpected_inventory_exception_preserves_other_sections(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, store = _sandbox(tmp_path, monkeypatch)
    collector = _collector()

    def fail_inventory(_store: Path, _doctor: object) -> dict[str, object]:
        raise RuntimeError("synthetic inventory implementation failure")

    monkeypatch.setattr(collector, "_inventory", fail_inventory)

    report = collector.collect(
        project=project, store=store, graph=project / "graphify-out/graph.json"
    )

    assert len(report["sections"]) == 6
    assert report["sections"][5]["status"] == "warning"
    assert report["sections"][5]["evidence"]["unknown_reasons"] == ["inventory_read_failed"]


def test_symlink_fingerprint_tracks_referent_changes(tmp_path: Path) -> None:
    target = tmp_path / "target.json"
    target.write_text("first", encoding="utf-8")
    link = tmp_path / "settings.json"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"file symlink unavailable: {type(exc).__name__}")
    collector = _collector()

    before, before_errors = collector._fingerprint([("settings", link)])
    target.write_text("second", encoding="utf-8")
    after, after_errors = collector._fingerprint([("settings", link)])

    assert before_errors == after_errors == []
    assert before["settings"] != after["settings"]


def test_recursive_protected_directory_fingerprint_tracks_symlink_referent(
    tmp_path: Path,
) -> None:
    protected_dir = tmp_path / "store"
    protected_dir.mkdir()
    target = tmp_path / "record.json"
    target.write_text('{"value":"before"}', encoding="utf-8")
    try:
        (protected_dir / "linked-record.json").symlink_to(target)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"file symlink unavailable: {type(exc).__name__}")
    collector = _collector()

    before, before_errors = collector._fingerprint([("store", protected_dir)])
    target.write_text('{"value":"after"}', encoding="utf-8")
    after, after_errors = collector._fingerprint([("store", protected_dir)])

    assert before_errors == after_errors == []
    assert before != after


def test_candidate_settings_directories_do_not_escape_selected_root(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    collector = _collector()
    modules = collector._imports()

    candidates, complete = collector._candidate_settings_dirs(
        project,
        project,
        None,
        modules["git_env"],
        modules["claude_settings"],
    )

    assert complete is True
    assert candidates == [project]


def test_git_root_and_default_store_preserve_trailing_space(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if os.name == "nt":
        pytest.skip("Windows does not preserve trailing spaces in path names")
    project, _store = _sandbox(tmp_path, monkeypatch)
    spaced_project = tmp_path / "project "
    project.rename(spaced_project)
    (tmp_path / "project").mkdir()
    collector = _collector()
    modules = collector._imports()
    assert collector._git_root(spaced_project, modules["git_env"]) == spaced_project

    resolved_roots: list[str] = []
    resolve_store_path = modules["resolve_store_path"]

    def capture_default_store(store: str | None = None, *, root: str, warn_on_create: bool):
        resolved_roots.append(root)
        return resolve_store_path(store, root=root, warn_on_create=warn_on_create)

    modules["resolve_store_path"] = capture_default_store
    monkeypatch.setattr(collector, "_imports", lambda: modules)
    report = collector.collect(project=spaced_project, host="unknown")

    assert resolved_roots == [str(spaced_project)]
    assert any(item["cwd"] == str(spaced_project) for item in report["recommendations"])
    assert (
        collector._git_index(spaced_project, spaced_project, modules["git_env"])
        == spaced_project / ".git/index"
    )

    external_git_index = tmp_path / "external git dir " / "index"

    def git_path(args: list[str], _cwd: Path, _git_env: object) -> str | None:
        if args == ["rev-parse", "--git-path", "index"]:
            return f"{external_git_index}\n"
        return None

    monkeypatch.setattr(collector, "_git", git_path)
    assert collector._git_index(spaced_project, None, modules["git_env"]) == external_git_index

    def empty_git_path(_args: list[str], _cwd: Path, _git_env: object) -> str:
        return "\n"

    monkeypatch.setattr(collector, "_git", empty_git_path)
    assert collector._git_index(spaced_project, None, modules["git_env"]) is None

    monkeypatch.setattr(collector, "_git", lambda *_args: None)
    assert collector._git_index(spaced_project, None, modules["git_env"]) is None


def test_cached_git_listing_preserves_leading_and_trailing_path_spaces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    collector = _collector()
    modules = collector._imports()

    def listing(args: list[str], _cwd: Path, _git_env: object) -> str | None:
        return (
            " leading and trailing /file.txt\0" if args == ["ls-files", "--cached", "-z"] else None
        )

    monkeypatch.setattr(collector, "_git", listing)
    candidates, complete = collector._candidate_settings_dirs(
        root, root, root, modules["git_env"], modules["claude_settings"]
    )

    assert complete is True
    assert root / " leading and trailing " in candidates


def test_failed_settings_directory_discovery_withholds_verified_preservation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, store = _sandbox(tmp_path, monkeypatch)
    collector = _collector()
    original_git = collector._git

    def fail_settings_listing(args: list[str], cwd: Path, git_env: object) -> str | None:
        if args == ["ls-files", "--cached", "-z"]:
            return None
        return original_git(args, cwd, git_env)

    monkeypatch.setattr(collector, "_git", fail_settings_listing)

    report = collector.collect(
        project=project, store=store, graph=project / "graphify-out/graph.json"
    )

    assert report["preservation"]["status"] == "unstable_or_unverified"
    assert report["preservation"]["unreadable_input_count"] >= 1
    assert report["preservation"]["unverified_reasons"] == [
        "claude_settings_directory_listing_unavailable"
    ]


def test_nested_settings_change_during_collection_invalidates_preservation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, store = _sandbox(tmp_path, monkeypatch)
    (project / ".claude").mkdir()
    (project / ".claude/settings.json").write_text(
        '{"enabledPlugins":{"sidegraph@sidegraph":true}}', encoding="utf-8"
    )
    nested_settings = project / "packages/nested/.claude/settings.json"
    nested_settings.parent.mkdir(parents=True)
    nested_settings.write_text('{"enabledPlugins":{"sidegraph@sidegraph":true}}', encoding="utf-8")
    (project / "packages/nested/tracked.py").write_text("# tracked candidate\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=project, check=True, capture_output=True)
    collector = _collector()
    original_fingerprint = collector._fingerprint
    calls = [0]
    snapshots: list[dict[str, str]] = []
    mutated = False

    def mutate_after_before_snapshot(
        paths: list[tuple[str, Path]],
    ) -> tuple[dict[str, str], list[str]]:
        nonlocal mutated
        result = original_fingerprint(paths)
        if any(label in {"store", "graph", "git-index"} for label, _path in paths):
            snapshots.append(result[0])
            calls[0] += 1
        if not mutated and any(label == "store" for label, _path in paths):
            mutated = True
            nested_settings.write_text(
                '{"enabledPlugins":{"sidegraph@sidegraph":false}}', encoding="utf-8"
            )
        return result

    monkeypatch.setattr(collector, "_fingerprint", mutate_after_before_snapshot)

    report = collector.collect(
        project=project,
        store=store,
        graph=project / "graphify-out/graph.json",
        host="claude",
    )

    assert calls[0] >= 2
    nested_settings_label = f"claude-settings-{nested_settings.parent.parent}-settings.json"
    assert nested_settings_label in snapshots[0]
    assert snapshots[0] != snapshots[1]
    assert report["preservation"]["status"] == "unstable_or_unverified"
    assert report["preservation"]["changed_input_count"] >= 1


def test_graph_location_mismatch_gets_inspection_advice_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, store = _sandbox(tmp_path, monkeypatch)
    foreign = tmp_path / "foreign"
    graph = foreign / "graphify-out/graph.json"
    graph.parent.mkdir(parents=True)
    graph.write_text(
        '{"graph":{"graphify_version":"0.9.90"},"built_at_commit":"unknown","nodes":[],"links":[]}',
        encoding="utf-8",
    )
    subprocess.run(["git", "init", "-q"], cwd=foreign, check=True, capture_output=True)

    report = _collector().collect(project=project, store=store, graph=graph)

    evidence = report["sections"][2]["evidence"]
    assert evidence["location_root_match"] is False
    assert evidence["freshness_root"] == str(project)
    assert report["sections"][2]["status"] == "warning"
    advice = [item for item in report["recommendations"] if item["priority"] == "freshness"]
    assert advice and advice[0]["argv"] == [
        "sidegraph-doctor",
        "--json",
        "--db",
        str(store),
        "--graph",
        str(graph),
    ]


def test_healthy_fresh_graph_and_computed_index_need_no_graph_advice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, store = _sandbox(tmp_path, monkeypatch)
    identity = ["-c", "user.name=Health Check Fixture", "-c", "user.email=fixture@example.test"]
    subprocess.run(["git", *identity, "commit", "-qm", "healthy baseline"], cwd=project, check=True)
    built_at = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=project, check=True, capture_output=True, text=True
    ).stdout.strip()
    graph = project / "graphify-out/graph.json"
    graph.write_text(
        json.dumps(
            {
                "graph": {"graphify_version": "0.9.90"},
                "built_at_commit": built_at,
                "nodes": [],
                "links": [],
            }
        ),
        encoding="utf-8",
    )
    running_release = ".".join(
        package for package in _collector()._imports()["__version__"].split(".")[:3]
    )
    (store / "format").write_text(
        f"sidegraph-store 1.0.0\nrequires-sidegraph >= {running_release}\n",
        encoding="utf-8",
    )
    initialized = Store(store)
    initialized.set_meta("volatile_stale", "0")
    initialized.close()
    collector = _collector()
    modules = collector._imports()
    package_version = modules["__version__"]
    graphify_package = modules["GRAPHIFY_PACKAGE"]
    plugin = tmp_path / "plugin"
    (plugin / ".claude-plugin").mkdir(parents=True)
    (plugin / ".claude-plugin/plugin.json").write_text(
        json.dumps({"name": "sidegraph", "version": package_version}), encoding="utf-8"
    )
    (plugin / ".mcp.json").write_text('{"mcpServers":{"sidegraph":{}}}', encoding="utf-8")
    (plugin / "hooks").mkdir()
    (plugin / "hooks/hooks.json").write_text(
        '{"hooks":{"SessionStart":[{"command":"GRAPHIFY_NO_AUTO_REFRESH=1"}]}}',
        encoding="utf-8",
    )
    original_version = collector.importlib.metadata.version

    def known_graphify_version(name: str) -> str:
        if name == graphify_package:
            return "0.9.90"
        return original_version(name)

    monkeypatch.setattr(collector.importlib.metadata, "version", known_graphify_version)

    report = collector.collect(project=project, store=store, graph=graph, plugin_root=plugin)

    assert report["sections"][2]["status"] == "OK"
    assert report["sections"][2]["evidence"]["freshness"]["state"] == "fresh"
    assert report["sections"][3]["status"] == "OK"
    assert report["sections"][4]["status"] == "OK", report["sections"][4]
    assert report["sections"][4]["evidence"]["doctor_advisories"]["skipped"] == []
    assert not any(item["priority"] == "freshness" for item in report["recommendations"])


def test_missing_store_subprocess_does_not_create_store_or_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, store = _sandbox(tmp_path, monkeypatch)
    shutil.rmtree(store)
    before = _manifest(project)
    env = os.environ.copy()
    env.update(
        {"HOME": str(tmp_path / "home"), "CLAUDE_CONFIG_DIR": str(tmp_path / "home/.claude")}
    )
    env["PATH"] = _test_path()
    for name in (
        "SIDEGRAPH_DIR",
        "SIDEGRAPH_DB",
        "SIDEGRAPH_GRAPH",
        "PLUGIN_ROOT",
        "CLAUDE_PLUGIN_ROOT",
    ):
        env.pop(name, None)

    result = subprocess.run(
        [sys.executable, "-B", str(_COLLECTOR), "--project", str(project)],
        cwd=project,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )

    report = json.loads(result.stdout)
    assert report["preservation"]["status"] == "verified"
    assert not store.exists()
    assert _manifest(project) == before


def test_copied_skill_uses_existing_interpreter_without_provisioning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, _store = _sandbox(tmp_path, monkeypatch)
    copied_skill = tmp_path / "copy/plugin/sidegraph/skills/health-check"
    shutil.copytree(_ROOT / "plugin/sidegraph/skills/health-check", copied_skill)
    before = _manifest(tmp_path)
    env = os.environ.copy()
    env.update(
        {"HOME": str(tmp_path / "home"), "CLAUDE_CONFIG_DIR": str(tmp_path / "home/.claude")}
    )
    env["PATH"] = _test_path()

    result = subprocess.run(
        [sys.executable, "-B", str(copied_skill / "scripts/collect.py"), "--project", str(project)],
        cwd=tmp_path,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )

    report = json.loads(result.stdout)
    runtime = report["sections"][1]["evidence"]["collector_runtime"]
    assert len(report["sections"]) == 6
    assert runtime["interpreter"] == str(Path(sys.executable).resolve())
    assert "sidegraph" in Path(runtime["package_file"]).parts
    assert runtime["source_root"] is None
    assert _manifest(tmp_path) == before


def test_skill_lists_all_sections_when_runtime_cannot_be_identified() -> None:
    skill = (_ROOT / "plugin/sidegraph/skills/health-check/SKILL.md").read_text()
    assert "no existing interpreter" in skill
    assert "nonzero" in skill and "PowerShell" in skill
    for title in (
        "Plugin and wiring",
        "CLI and runtime versions",
        "Graphify",
        "Store integrity",
        "Anchors and entities",
        "Inventory and usage",
    ):
        assert title in skill


def test_skill_and_docs_name_direct_existing_interpreter_and_manual_only_actions() -> None:
    skill = (_ROOT / "plugin/sidegraph/skills/health-check/SKILL.md").read_text()

    assert "-B" in skill
    assert "already-existing" in skill and "Python interpreter" in skill
    assert '--project "$LAUNCH_DIR"' in skill
    assert "six sections" in skill
    assert "Do not execute them during this health check" in " ".join(skill.split())
