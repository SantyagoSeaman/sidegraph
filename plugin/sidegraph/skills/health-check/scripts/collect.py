"""Collect a read-only, bounded report about the current Sidegraph installation."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib.metadata
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tomllib
from collections import Counter
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def _source_root() -> Path | None:
    """Return source only for a verified Sidegraph checkout containing this skill."""
    source = Path(__file__).resolve().parents[5] / "src"
    try:
        metadata = tomllib.loads((source.parent / "pyproject.toml").read_text(encoding="utf-8"))
    except (OSError, ValueError, tomllib.TOMLDecodeError):
        return None
    project = metadata.get("project", {})
    return source if isinstance(project, dict) and project.get("name") == "sidegraph" else None


def _imports() -> dict[str, Any]:
    source = _source_root()
    if source is not None and source.is_dir() and str(source) not in sys.path:
        sys.path.insert(0, str(source))
    import sidegraph
    from sidegraph import doctor, verify
    from sidegraph.adr_cli import _avoid_index_open
    from sidegraph.config import default_graph_path, resolve_store_path
    from sidegraph.engine.reader import GRAPHIFY_PACKAGE, TESTED_ENGINE_VERSIONS, GraphifyReader
    from sidegraph.gitenv import git_env
    from sidegraph.host import claude_settings, hooks
    from sidegraph.integrity import binding_statuses_computed
    from sidegraph.stats.model import build_report

    return {
        "__version__": sidegraph.__version__,
        "package_file": str(Path(sidegraph.__file__).resolve()),
        "interpreter": str(Path(sys.executable).resolve()),
        "source_root": str(source.resolve()) if source is not None else None,
        "doctor": doctor,
        "verify": verify,
        "_avoid_index_open": _avoid_index_open,
        "default_graph_path": default_graph_path,
        "resolve_store_path": resolve_store_path,
        "GraphifyReader": GraphifyReader,
        "TESTED_ENGINE_VERSIONS": TESTED_ENGINE_VERSIONS,
        "GRAPHIFY_PACKAGE": GRAPHIFY_PACKAGE,
        "git_env": git_env,
        "claude_settings": claude_settings,
        "hooks": hooks,
        "binding_statuses_computed": binding_statuses_computed,
        "build_report": build_report,
    }


def _git_env(git_env: Any) -> dict[str, str]:
    env = git_env()
    env["GIT_OPTIONAL_LOCKS"] = "0"
    return env


def _git(args: list[str], cwd: Path, git_env: Any) -> str | None:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            env=_git_env(git_env),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="strict",
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError, UnicodeError, ValueError):
        return None
    return result.stdout if result.returncode == 0 else None


def _git_root(project: Path, git_env: Any) -> Path | None:
    value = _git(["rev-parse", "--show-toplevel"], project, git_env)
    path = value.removesuffix("\n") if value is not None else ""
    return Path(path).resolve() if path else None


def _git_index(project: Path, root: Path | None, git_env: Any) -> Path | None:
    value = _git(["rev-parse", "--git-path", "index"], project, git_env)
    if value is None:
        return None
    value = value.removesuffix("\n")
    if not value:
        return None
    path = Path(value)
    return (root / path).absolute() if not path.is_absolute() and root else path.absolute()


def _file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 128), b""):
            digest.update(block)
    return digest.hexdigest()


def _fingerprint(paths: list[tuple[str, Path]]) -> tuple[dict[str, str], list[str]]:
    """Hash inspected files and directory membership, retaining expected missing paths."""
    values: dict[str, str] = {}
    errors: list[str] = []
    for label, path in paths:
        try:
            if path.is_symlink():
                link = os.readlink(path)
                try:
                    target = path.resolve(strict=True)
                    if target.is_file():
                        values[label] = f"SYMLINK:{link}:{_file_hash(target)}"
                    else:
                        values[label] = f"SYMLINK:{link}:DIRECTORY_OR_SPECIAL_UNVERIFIED"
                        errors.append(label)
                except OSError:
                    values[label] = f"SYMLINK:{link}:UNRESOLVED"
                    errors.append(label)
            elif path.is_dir():
                names = sorted(p.name for p in path.iterdir())
                values[f"{label}/"] = hashlib.sha256(
                    json.dumps(names, ensure_ascii=False).encode("utf-8")
                ).hexdigest()
                for child in sorted(path.iterdir(), key=lambda p: p.name):
                    nested, nested_errors = _fingerprint([(f"{label}/{child.name}", child)])
                    values.update(nested)
                    errors.extend(nested_errors)
            elif path.is_file() and not path.is_symlink():
                values[label] = _file_hash(path)
            else:
                values[label] = "MISSING"
        except OSError:
            values[label] = "UNREADABLE"
            errors.append(label)
    return values, errors


def _protected_paths(
    project: Path,
    store: Path,
    graph: Path,
    plugin_root: Path | None,
    git_index: Path | None,
    candidate_dirs: list[Path] | None = None,
) -> list[tuple[str, Path]]:
    paths: list[tuple[str, Path]] = [("store", store), ("graph", graph)]
    if git_index is not None:
        paths.append(("git-index", git_index))
    settings_dirs = {project, *(candidate_dirs or [])}
    for name in ("settings.json", "settings.local.json"):
        for directory in sorted(settings_dirs):
            paths.append((f"claude-settings-{directory}-{name}", directory / ".claude" / name))
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    user_settings = (
        Path(config_dir) / "settings.json" if config_dir else Path.home() / ".claude/settings.json"
    )
    paths.append(("user-claude-settings", user_settings))
    if plugin_root:
        paths.extend(
            (f"plugin-manifest-{name}", plugin_root / name / "plugin.json")
            for name in (".claude-plugin", ".codex-plugin")
        )
        paths.extend(
            (f"plugin-config-{name}", plugin_root / name)
            for name in (".mcp.json", "codex/mcp.json", "codex/hooks.json", "hooks/hooks.json")
        )
    paths.extend(
        (f"graph-sidecar-{name}", graph.parent / name)
        for name in ("manifest.json", ".graphify_labels.json", ".graphify_labels.pending.json")
    )
    return paths


def _candidate_settings_dirs(
    project: Path, root: Path, git_root: Path | None, git_env: Any, claude_settings: Any
) -> tuple[list[Path], bool]:
    """Settings directories read by Claude reach: launch ancestors and indexed ancestors."""
    candidates = {project}
    for parent in project.parents:
        if parent == root:
            candidates.add(parent)
            break
        if root in parent.parents:
            candidates.add(parent)
            continue
        break
    complete = True
    if git_root is not None:
        listing = _git(["ls-files", "--cached", "-z"], git_root, git_env)
        if listing is None:
            complete = False
        else:
            try:
                raw_listing = listing.encode("utf-8", errors="strict")
            except UnicodeError:
                complete = False
            else:
                for relative in claude_settings._listed_directories(raw_listing):
                    candidates.add(git_root / relative)
    return sorted(candidates), complete


@contextlib.contextmanager
def _local_environment(name: str, value: str) -> Iterator[None]:
    old = os.environ.get(name)
    present = name in os.environ
    os.environ[name] = value
    try:
        yield
    finally:
        if present:
            os.environ[name] = old or ""
        else:
            os.environ.pop(name, None)


def _section(
    name: str, status: str, evidence: dict[str, Any], limitations: list[str] | None = None
) -> dict[str, Any]:
    return {"name": name, "status": status, "evidence": evidence, "limitations": limitations or []}


def _load_json(path: Path) -> tuple[dict[str, Any] | None, str]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, "missing"
    except (OSError, ValueError, UnicodeError):
        return None, "unreadable_or_malformed"
    return (raw, "available") if isinstance(raw, dict) else (None, "invalid_shape")


def _plugin_manifest(root: Path | None) -> tuple[dict[str, Any], Path | None]:
    if root is None:
        return {
            "status": "UNKNOWN",
            "reason": "no known plugin root was supplied or attached",
        }, None
    for rel in (Path(".claude-plugin/plugin.json"), Path(".codex-plugin/plugin.json")):
        path = root / rel
        raw, _state = _load_json(path)
        if raw is None or raw.get("name") != "sidegraph":
            continue
        return (
            {
                "status": "available",
                "version": raw.get("version") if isinstance(raw.get("version"), str) else "UNKNOWN",
                "source": "claude_manifest" if ".claude-plugin" in rel.parts else "codex_manifest",
                "mcp_declared": bool(raw.get("mcpServers")),
                "hooks_declared": bool(raw.get("hooks")),
                "skills_declared": bool(raw.get("skills")),
            },
            path,
        )
    return {"status": "UNKNOWN", "reason": "no readable Sidegraph plugin manifest"}, None


def _wiring_declarations(root: Path | None) -> dict[str, Any]:
    if root is None:
        return {"status": "UNKNOWN", "reason": "plugin root is unknown"}
    candidates = (".mcp.json", "codex/mcp.json", "codex/hooks.json", "hooks/hooks.json")
    result: dict[str, Any] = {}
    malformed: list[str] = []
    for relative in candidates:
        path = root / relative
        raw, state = _load_json(path)
        if raw is None:
            if state != "missing":
                result[relative] = {"status": state}
                malformed.append(relative)
            continue
        if relative.endswith("mcp.json"):
            servers = raw.get("mcpServers")
            names = (
                sorted(name for name in servers if isinstance(name, str))
                if isinstance(servers, dict)
                else sorted(name for name in raw if isinstance(name, str))
            )
            result[relative] = {
                "status": "available",
                "server_names": names,
                "sidegraph_declared": "sidegraph" in names,
            }
        else:
            hooks = raw.get("hooks")
            events = (
                sorted(name for name in hooks if isinstance(name, str))
                if isinstance(hooks, dict)
                else []
            )

            def contains_refresh_guard(value: Any) -> bool:
                if isinstance(value, str):
                    return "GRAPHIFY_NO_AUTO_REFRESH=1" in value
                if isinstance(value, dict):
                    return any(contains_refresh_guard(child) for child in value.values())
                if isinstance(value, list):
                    return any(contains_refresh_guard(child) for child in value)
                return False

            result[relative] = {
                "status": "available",
                "hook_events": events,
                "graphify_refresh_guard_declared": contains_refresh_guard(raw),
            }
    return (
        {
            "status": "warning" if malformed else "available",
            "files": result,
            "malformed_files": malformed,
        }
        if result
        else {"status": "UNKNOWN", "reason": "no readable MCP or hook config was found"}
    )


def _refresh_hook_guard(wiring: dict[str, Any]) -> dict[str, str]:
    files = wiring.get("files")
    if not isinstance(files, dict):
        return {"status": "UNKNOWN", "reason": "no readable hook configuration was found"}
    hook_files = [
        item
        for name, item in files.items()
        if name.endswith("hooks.json") and isinstance(item, dict)
    ]
    if not hook_files:
        return {"status": "UNKNOWN", "reason": "no readable hook configuration was found"}
    declared = any(item.get("graphify_refresh_guard_declared") is True for item in hook_files)
    return {"status": "declared" if declared else "not_declared"}


def _path_install_metadata(hooks: Any) -> dict[str, Any]:
    found = hooks._path_install()
    if found is not None:
        return {
            "status": "available",
            "command": found.command,
            "version": found.version,
            "uv_tool": found.uv_tool,
        }
    own = hooks._own_bin_dirs()
    search = os.pathsep.join(
        item
        for item in os.environ.get("PATH", "").split(os.pathsep)
        if item and os.path.realpath(item) not in own
    )
    present = next(
        (name for name in hooks._PROBED_COMMANDS if shutil.which(name, path=search)), None
    )
    if present is None:
        return {"status": "absent", "optional": True}
    return {
        "status": "UNKNOWN",
        "reason": "a PATH command exists but its install metadata is unsupported",
    }


def _index_safety(store: Path, avoid_index_open: Any) -> tuple[bool, str]:
    index = store / "index.db"
    if not index.exists():
        return (
            True,
            "missing index: canonical-only curation is safe; index checks and stats are skipped",
        )
    journal = store / "index.db-journal"
    try:
        if journal.is_file() and journal.stat().st_size > 0:
            return False, "nonempty rollback journal: SQL-backed diagnostic paths skipped"
    except OSError:
        return False, "index journal state is unreadable"
    reason = avoid_index_open(store)
    if reason is not None:
        return False, reason
    return True, "stable rollback-mode index"


def _inventory(store: Path, doctor: Any) -> dict[str, Any]:
    counts: dict[str, int | str] = {}
    unknown_age = 0
    pending: list[datetime] = []
    unknown_reasons: list[str] = []
    pending_totals_known = True
    for directory, id_key in (("decisions", "id"), ("facts", "id"), ("domains", "domain_id")):
        try:
            records = doctor._iter_records(store, directory)
        except (OSError, ValueError):
            counts[directory] = "UNKNOWN"
            unknown_reasons.append(f"{directory}_unreadable")
            pending_totals_known = False
            continue
        counts[directory] = len(records)
        for _path, record in records:
            if record.get("status") != "proposed":
                continue
            rid = record.get(id_key)
            created = doctor._ulid_datetime(rid) if isinstance(rid, str) else None
            if created is None:
                unknown_age += 1
            else:
                pending.append(created)
    try:
        counts["entities"] = len(doctor._iter_records(store, "entities"))
    except (OSError, ValueError):
        counts["entities"] = "UNKNOWN"
        unknown_reasons.append("entities_unreadable")
    try:
        archives = doctor._check_archive_dir(store / "archive")
        counts["archived_decisions"] = len(archives.decisions)
        counts["archived_domains"] = len(archives.domains)
    except (OSError, ValueError):
        counts["archived_decisions"] = "UNKNOWN"
        counts["archived_domains"] = "UNKNOWN"
        unknown_reasons.append("archives_unreadable")
    observed_proposed_count = len(pending) + unknown_age
    age_days = (
        max(0, int((datetime.now(UTC) - min(pending)).total_seconds() // 86400))
        if pending
        else None
    )
    if unknown_age:
        unknown_reasons.append("pending_age_unparseable_id")
    return {
        "canonical_counts": counts,
        "proposed_count": observed_proposed_count if pending_totals_known else "UNKNOWN",
        "observed_proposed_count": observed_proposed_count,
        "oldest_pending_age_days": (
            age_days if pending_totals_known and not unknown_age else "UNKNOWN"
        ),
        "pending_age_unknown_count": unknown_age if pending_totals_known else "UNKNOWN",
        "unknown_reasons": unknown_reasons,
    }


def _recommendation(
    priority: str,
    action: str,
    reason: str,
    scope: str,
    argv: list[str] | None,
    cwd: Path,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Describe a manual command without embedding paths in shell text."""
    return {
        "priority": priority,
        "action": action,
        "reason": reason,
        "scope": scope,
        "argv": argv or [],
        "cwd": str(cwd),
        "env": env or {},
    }


def _stats_summary(report: Any) -> dict[str, Any]:
    activation = report.activation
    memory = report.memory
    reach = report.reach
    return {
        "status": "available",
        "window_days": report.window_days,
        "activation": {
            "sessions_total": activation.sessions_total,
            "sessions_with_retrieval": activation.sessions_with_retrieval,
            "sessions_asked_but_empty": activation.sessions_asked_but_empty,
            "sessions_touch_only": activation.sessions_touch_only,
            "showings": activation.showings,
            "mature": activation.mature,
            "render_journal": activation.render_journal,
            "budget_journal": activation.budget_journal,
        },
        "memory": None
        if memory is None
        else {
            "decisions": memory.decisions,
            "facts": memory.facts,
            "domains": memory.domains,
            "historical": memory.historical,
        },
        "reach": {
            "files_touched": reach.files_touched,
            "files_touched_with_memory": reach.files_touched_with_memory,
        },
        "index_stale": report.index_stale,
        "binding_statuses_computed": report.binding_statuses_computed,
        "coverage": "observed activity only; counts do not measure usefulness or saved effort",
    }


def collect(
    *,
    project: str | Path,
    store: str | Path | None = None,
    graph: str | Path | None = None,
    plugin_root: str | Path | None = None,
    host: str = "unknown",
) -> dict[str, Any]:
    """Collect six evidence sections and prioritized manual-only recommendations."""
    modules = _imports()
    doctor = modules["doctor"]
    verify = modules["verify"]
    launch_dir = Path(project).absolute()
    git_root = _git_root(launch_dir, modules["git_env"])
    root = git_root or launch_dir
    store_path = Path(
        modules["resolve_store_path"](
            str(store) if store else None, root=str(root), warn_on_create=False
        )
    ).absolute()
    graph_path = Path(graph).absolute() if graph else modules["default_graph_path"](store_path)
    plugin_value = (
        plugin_root or os.environ.get("CLAUDE_PLUGIN_ROOT") or os.environ.get("PLUGIN_ROOT")
    )
    plugin_path = (
        Path(plugin_value).absolute() if plugin_value else Path(__file__).resolve().parents[3]
    )
    git_index = _git_index(root, git_root, modules["git_env"])
    candidate_dirs, settings_discovery_complete = _candidate_settings_dirs(
        launch_dir, root, git_root, modules["git_env"], modules["claude_settings"]
    )
    protected = _protected_paths(
        root, store_path, graph_path, plugin_path, git_index, candidate_dirs
    )
    before, before_errors = _fingerprint(protected)
    if not settings_discovery_complete:
        before_errors.append("claude-settings-directory-listing")
    if git_index is None:
        before_errors.append("git-index")

    sections: list[dict[str, Any]] = []
    recommendations: list[dict[str, Any]] = []
    package_version = modules["__version__"]
    dist_version = "UNKNOWN"
    dist_source = "imported package; distribution metadata unavailable"
    try:
        dist = importlib.metadata.distribution("sidegraph")
        dist_version = dist.version
        dist_source = "installed distribution metadata available"
    except importlib.metadata.PackageNotFoundError:
        pass
    runtime_match = "UNKNOWN" if dist_version == "UNKNOWN" else dist_version == package_version
    if runtime_match is False:
        dist_source = "imported package version differs from distribution metadata"

    manifest, _manifest_path = _plugin_manifest(plugin_path)
    active_mcp = {
        "status": "UNKNOWN",
        "reason": "the collector cannot safely probe the active server",
    }
    try:
        if host != "claude":
            raise RuntimeError("Claude scope check does not apply to the identified host")
        with _local_environment("GIT_OPTIONAL_LOCKS", "0"):
            reach = modules["claude_settings"].plugin_reach(launch_dir)
        reach_data = (
            {
                "status": "available",
                "project_only": reach.project_only,
                "nested_off": len(reach.off),
            }
            if reach is not None
            else {"status": "UNKNOWN", "reason": "Claude project reach could not be determined"}
        )
    except Exception:
        reach_data = {
            "status": "UNKNOWN",
            "reason": "Claude scope evidence is unavailable for this host or launch context",
        }
    wiring = _wiring_declarations(plugin_path)
    section1_status = (
        "warning"
        if (
            manifest["status"] == "UNKNOWN"
            or wiring["status"] in ("UNKNOWN", "warning")
            or active_mcp["status"] == "UNKNOWN"
            or reach_data["status"] == "UNKNOWN"
        )
        else "OK"
    )
    sections.append(
        _section(
            "plugin_and_wiring",
            section1_status,
            {
                "host": host,
                "plugin_manifest": manifest,
                "static_wiring": wiring,
                "claude_reach": reach_data,
                "active_mcp": active_mcp,
            },
            ["Static configuration does not prove the active MCP server answered."],
        )
    )
    if reach_data.get("project_only") or reach_data.get("nested_off"):
        recommendations.append(
            _recommendation(
                "activation",
                "Review the reported Claude project and nested settings files manually.",
                "Plugin reach is limited by the effective settings at the reported paths.",
                f"project={root}; launch={launch_dir}; settings=.claude/settings.json",
                ["sidegraph-doctor", "--json", "--db", str(store_path), "--graph", str(graph_path)],
                root,
            )
        )
    if wiring["status"] in ("UNKNOWN", "warning"):
        recommendations.append(
            _recommendation(
                "activation",
                "Inspect the reported static MCP and hook configuration files manually.",
                "One or more wiring files are missing, malformed, or unavailable.",
                f"plugin={plugin_path}",
                None,
                root,
            )
        )

    if plugin_path:
        with _local_environment("CLAUDE_PLUGIN_ROOT", str(plugin_path)):
            path_install = _path_install_metadata(modules["hooks"])
    else:
        path_install = _path_install_metadata(modules["hooks"])
    manifest_version = manifest.get("version", "UNKNOWN")
    manifest_match = (
        "UNKNOWN" if manifest_version == "UNKNOWN" else manifest_version == package_version
    )
    path_match = (
        "UNKNOWN"
        if path_install["status"] != "available"
        else path_install["version"] == package_version
    )
    versions_status = (
        "warning"
        if (
            path_install["status"] == "UNKNOWN"
            or manifest_match in ("UNKNOWN", False)
            or (path_install["status"] == "available" and path_match is False)
            or active_mcp["status"] == "UNKNOWN"
            or runtime_match in ("UNKNOWN", False)
        )
        else "OK"
    )
    sections.append(
        _section(
            "versions",
            versions_status,
            {
                "collector_runtime": {
                    "version": package_version,
                    "distribution_version": dist_version,
                    "source": dist_source,
                    "package_file": modules["package_file"],
                    "interpreter": modules["interpreter"],
                    "source_root": modules["source_root"],
                    "distribution_match": runtime_match,
                },
                "plugin_manifest_version": manifest_version,
                "skill_version": "UNKNOWN; the bundled skill has no separate version stamp",
                "active_mcp_version": "UNKNOWN",
                "path_install": path_install,
                "collector_manifest_match": manifest_match,
                "collector_path_match": path_match,
                "comparison": "collector runtime compared with manifest and PATH metadata only",
            },
            ["No active MCP identity was inferred from the collector package or launch cache."],
        )
    )
    if (
        manifest_match is False
        or path_match is False
        or runtime_match is False
        or path_install["status"] == "UNKNOWN"
    ):
        recommendations.append(
            _recommendation(
                "integrity",
                "Inspect the identified collector, manifest, and PATH install metadata manually.",
                "Known version metadata differs or is unsupported; active MCP identity "
                "remains unknown.",
                f"project={root}; plugin={plugin_path}",
                ["sidegraph-doctor", "--json", "--db", str(store_path), "--graph", str(graph_path)],
                root,
            )
        )

    graph_status = "warning"
    graph_rebuild_scope_known = False
    try:
        reader = modules["GraphifyReader"](str(graph_path))
        with _local_environment("GIT_OPTIONAL_LOCKS", "0"):
            graph_root = reader.repo_root()
            root_match = (
                "UNKNOWN"
                if git_root is None or graph_root is None
                else git_root.resolve() == graph_root.resolve()
            )
            freshness = reader.freshness(root=root)
            try:
                subdir_mismatch = reader.detect_subdir_mismatch(git_root)
            except (OSError, ValueError):
                subdir_mismatch = "UNKNOWN"
        engine_version = reader.engine_version()
        try:
            installed_graphify = importlib.metadata.version(modules["GRAPHIFY_PACKAGE"])
        except importlib.metadata.PackageNotFoundError:
            installed_graphify = "UNKNOWN"
        graph_rebuild_scope_known = root_match is True and subdir_mismatch is None
        refresh_guard = _refresh_hook_guard(wiring)
        graph_evidence = {
            "status": "available",
            "engine_recorded_version": engine_version or "UNKNOWN",
            "installed_graphify_version": installed_graphify,
            "engine_version_tested_by_sidegraph": reader.engine_version_tested(),
            "built_at_commit": reader.built_at_commit() or "UNKNOWN",
            "project_root": str(git_root) if git_root else "UNKNOWN",
            "graph_location_root": str(graph_root) if graph_root else "UNKNOWN",
            "freshness_root": str(root),
            "location_root_match": root_match,
            "subdirectory_build_check": (
                subdir_mismatch if subdir_mismatch is not None else "no_finding_or_ambiguous"
            ),
            "freshness": freshness.model_dump(mode="json"),
            "tested_engine_versions": sorted(modules["TESTED_ENGINE_VERSIONS"]),
            "refresh_hook_guard": refresh_guard,
        }
        graph_status = (
            "OK"
            if (
                freshness.state == "fresh"
                and engine_version in modules["TESTED_ENGINE_VERSIONS"]
                and installed_graphify != "UNKNOWN"
                and reader.built_at_commit() is not None
                and root_match is True
                and subdir_mismatch is None
                and refresh_guard.get("status") != "UNKNOWN"
            )
            else "warning"
        )
    except Exception:
        graph_evidence = {
            "status": "UNKNOWN",
            "reason": "graph is missing, unreadable, or freshness could not be checked",
        }
    sections.append(_section("graphify", graph_status, graph_evidence))
    graph_freshness_state = graph_evidence.get("freshness", {}).get("state")
    graph_scope_matches = (
        graph_evidence.get("location_root_match") is True and graph_rebuild_scope_known
    )
    if (
        graph_evidence.get("status") == "available"
        and graph_freshness_state == "stale"
        and graph_scope_matches
    ):
        recommendations.append(
            _recommendation(
                "freshness",
                "After this report ends, rebuild the proven-stale graph manually.",
                "The existing graph freshness check proves the graph is stale.",
                f"project={root}; graph={graph_path}",
                ["graphify", "update", "."],
                root,
                {"GRAPHIFY_NO_AUTO_REFRESH": "1"},
            )
        )
    elif graph_status == "warning" or graph_evidence.get("status") != "available":
        recommendations.append(
            _recommendation(
                "freshness",
                "Inspect the Graphify path, location, version, and freshness manually.",
                "Graph evidence is missing, unknown, stale at an unresolved scope, or otherwise "
                "warning.",
                f"project={root}; graph={graph_path}",
                ["sidegraph-doctor", "--json", "--db", str(store_path), "--graph", str(graph_path)],
                root,
            )
        )

    if graph_evidence.get("status") == "available" and (
        graph_evidence.get("engine_recorded_version") == "UNKNOWN"
        or graph_evidence.get("installed_graphify_version") == "UNKNOWN"
        or graph_evidence.get("engine_recorded_version") not in modules["TESTED_ENGINE_VERSIONS"]
    ):
        recommendations.append(
            _recommendation(
                "freshness",
                "Inspect Graphify version metadata and Sidegraph compatibility manually.",
                "The recorded or installed Graphify version is unknown or untested.",
                f"project={root}; graph={graph_path}",
                None,
                root,
            )
        )

    violations: list[dict[str, str]] = []
    verify_error: str | None = None
    try:
        with _local_environment("GIT_OPTIONAL_LOCKS", "0"):
            result = verify.verify_snapshot(store_path)
        violations = [{"code": item.code} for item in result]
    except Exception as exc:
        verify_error = type(exc).__name__
    index_safe, index_reason = _index_safety(store_path, modules["_avoid_index_open"])
    findings: list[dict[str, str]] = []
    skipped: list[str] = []
    statistics: dict[str, Any]
    doctor_error: str | None = None
    if index_safe:
        index = None
        try:
            with _local_environment("GIT_OPTIONAL_LOCKS", "0"):
                try:
                    graph_reader = (
                        modules["GraphifyReader"](str(graph_path)) if graph_path.is_file() else None
                    )
                except Exception:
                    graph_reader = None
                index = (
                    doctor.open_curation_index(store_path)
                    if (store_path / "index.db").is_file()
                    else None
                )
                report = doctor.curate(store_path, reader=graph_reader, index=index)
                findings = [{"code": item.code} for item in report.findings]
                skipped = list(report.skipped)
                if index is None:
                    skipped.extend(["binding-status", "never-surfaced", "orphaned-records"])
                else:
                    try:
                        if not modules["binding_statuses_computed"](index):
                            skipped.append("orphaned-records")
                    except sqlite3.Error:
                        skipped.append("orphaned-records")
        except Exception as exc:
            doctor_error = type(exc).__name__
        finally:
            if index is not None:
                index.close()
        if (store_path / "index.db").is_file():
            try:
                with _local_environment("GIT_OPTIONAL_LOCKS", "0"):
                    statistics = _stats_summary(modules["build_report"](store_path, graph_path))
            except Exception:
                statistics = {"status": "UNKNOWN", "reason": "statistics collection failed"}
        else:
            statistics = {
                "status": "UNKNOWN",
                "reason": "index absent; stats skipped to avoid creating an index",
            }
    else:
        skipped = ["all doctor advisory paths", "stats"]
        statistics = {"status": "UNKNOWN", "reason": index_reason}
        recommendations.append(
            _recommendation(
                "integrity",
                "Inspect the unsafe index manually after this report; ordinary SQLite "
                "diagnostics may create sidecars.",
                index_reason,
                f"project={root}; store={store_path}; graph={graph_path}",
                ["sidegraph-doctor", "--json", "--db", str(store_path), "--graph", str(graph_path)],
                root,
            )
        )
    findings_by_code = dict(Counter(item["code"] for item in findings))
    if not index_safe:
        doctor_evidence = {
            "status": "UNKNOWN",
            "reason": index_reason,
            "counts_by_code": "UNKNOWN",
            "skipped": skipped,
        }
    elif doctor_error:
        doctor_evidence = {
            "status": "UNKNOWN",
            "reason": "doctor curation failed",
            "error_type": doctor_error,
            "counts_by_code": "UNKNOWN",
        }
    else:
        doctor_evidence = {
            "status": "available",
            "findings_by_code": findings_by_code,
            "findings": findings,
            "skipped": sorted(set(skipped)),
        }
    git_status = _git(["status", "--porcelain"], root, modules["git_env"])
    if git_status is not None:
        git_status = git_status.strip()
    store_status = (
        "broken"
        if violations
        else "warning"
        if verify_error or not index_safe or git_root is None or git_status is None
        else "OK"
    )
    if violations or verify_error:
        recommendations.append(
            _recommendation(
                "integrity",
                "Review strict store verification codes and decide any repair manually.",
                "Strict verification found violations or could not complete.",
                f"project={root}; store={store_path}",
                ["sidegraph-verify", "--json", "--db", str(store_path)],
                root,
            )
        )
    if git_root is None or git_status is None:
        recommendations.append(
            _recommendation(
                "integrity",
                "Inspect Git repository status manually for the reported project.",
                "The repository root or read-only Git status could not be established.",
                f"project={root}",
                ["git", "status", "--porcelain"],
                root,
            )
        )
    sections.append(
        _section(
            "store_integrity",
            store_status,
            {
                "verification": {
                    "status": "UNKNOWN" if verify_error else "broken" if violations else "OK",
                    "violation_codes": dict(Counter(v["code"] for v in violations)),
                    "error_type": verify_error,
                },
                "index": {
                    "present": (store_path / "index.db").exists(),
                    "diagnostic_access": "safe" if index_safe else "skipped",
                    "reason": index_reason,
                },
                "git": {
                    "root_known": git_root is not None,
                    "dirty": "UNKNOWN" if git_status is None else bool(git_status),
                },
                "records_on_other_branches": "not exhaustively checked",
            },
        )
    )
    sections.append(
        _section(
            "anchors_and_entities",
            "warning" if doctor_evidence["status"] != "available" or skipped or findings else "OK",
            {
                "doctor_advisories": doctor_evidence,
                "binding_currency": "UNKNOWN"
                if "orphaned-records" in skipped or not index_safe
                else "see doctor findings and skipped checks",
            },
        )
    )
    try:
        inventory = _inventory(store_path, doctor)
    except Exception:
        inventory = {
            "canonical_counts": "UNKNOWN",
            "proposed_count": "UNKNOWN",
            "oldest_pending_age_days": "UNKNOWN",
            "pending_age_unknown_count": "UNKNOWN",
            "unknown_reasons": ["inventory_read_failed"],
        }
    inventory["statistics"] = statistics
    usage_status = (
        "warning"
        if statistics.get("status") != "available"
        or statistics.get("index_stale")
        or not statistics.get("binding_statuses_computed", True)
        or inventory.get("canonical_counts") == "UNKNOWN"
        or inventory.get("unknown_reasons")
        or inventory.get("oldest_pending_age_days") == "UNKNOWN"
        else "OK"
    )
    sections.append(_section("inventory_and_usage", usage_status, inventory))
    if inventory.get("canonical_counts") == "UNKNOWN" or inventory.get("unknown_reasons"):
        recommendations.append(
            _recommendation(
                "curation",
                "Inspect canonical inventory and archive files manually; unknown counts "
                "were withheld.",
                "; ".join(inventory.get("unknown_reasons", [])) or "inventory evidence unavailable",
                f"project={root}; store={store_path}",
                ["sidegraph-doctor", "--json", "--db", str(store_path), "--graph", str(graph_path)],
                root,
            )
        )
    if findings:
        recommendations.append(
            _recommendation(
                "curation",
                "Review the reported doctor advisory codes and choose the matching repair "
                "skill manually.",
                "Canonical curation reported advisory codes.",
                f"project={root}; store={store_path}; graph={graph_path}",
                ["sidegraph-doctor", "--json", "--db", str(store_path), "--graph", str(graph_path)],
                root,
            )
        )
    if index_safe and (statistics.get("status") != "available" or statistics.get("index_stale")):
        recommendations.append(
            _recommendation(
                "usage",
                "After this report, run a scoped sync manually to prepare the derived index "
                "for inspection.",
                "The index is absent, stale, or statistics could not be collected.",
                f"project={root}; store={store_path}; graph={graph_path}",
                ["sidegraph-sync", "--json", "--db", str(store_path), "--graph", str(graph_path)],
                root,
            )
        )

    after, after_errors = _fingerprint(protected)
    changed = sorted(
        key for key in before.keys() | after.keys() if before.get(key) != after.get(key)
    )
    stable = not before_errors and not after_errors and not changed
    preservation = {
        "status": "verified" if stable else "unstable_or_unverified",
        "protected_input_count": len(set(before) | set(after)),
        "changed_input_count": len(changed),
        "unreadable_input_count": len(set(before_errors + after_errors)),
        "unverified_reasons": (
            [] if settings_discovery_complete else ["claude_settings_directory_listing_unavailable"]
        ),
        "scope": "collector process only; concurrent host activity can change inputs",
    }
    overall = (
        "broken"
        if any(section["status"] == "broken" for section in sections)
        else "warning"
        if not stable or any(section["status"] == "warning" for section in sections)
        else "OK"
    )
    order = ("activation", "integrity", "freshness", "curation", "usage")
    deduplicated: dict[tuple[tuple[str, ...], str], dict[str, Any]] = {}
    for item in recommendations:
        key = (tuple(item["argv"]), item["scope"])
        if key in deduplicated:
            prior = deduplicated[key]
            prior["reason"] = "; ".join(sorted({prior["reason"], item["reason"]}))
            prior["action"] = "; ".join(sorted({prior["action"], item["action"]}))
        else:
            deduplicated[key] = item
    recommendations = list(deduplicated.values())
    recommendations.sort(
        key=lambda item: order.index(item["priority"]) if item["priority"] in order else 9
    )
    return {
        "overall": overall,
        "sections": sections,
        "preservation": preservation,
        "recommendations": recommendations,
        "limitations": ["Live MCP identity is not observable through this read-only collector."],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True, type=Path)
    parser.add_argument("--store", type=Path)
    parser.add_argument("--graph", type=Path)
    parser.add_argument("--plugin-root", type=Path)
    parser.add_argument("--host", default="unknown", choices=("claude", "codex", "unknown"))
    args = parser.parse_args(argv)
    try:
        result = collect(
            project=args.project,
            store=args.store,
            graph=args.graph,
            plugin_root=args.plugin_root,
            host=args.host,
        )
    except Exception as exc:
        print(
            json.dumps(
                {"error": type(exc).__name__, "message": "health report could not be collected"}
            )
        )
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
