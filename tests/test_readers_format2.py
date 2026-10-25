"""Every reader that walks the store asks the inventory and ``effective()`` (T-P1).

Doctor, ADR export, stats, integrity and the bootstrap catalog each read a format-2 store
through ``store_view``: a packed record, a revived archive segment and a dropped-then-compacted
decision read the same everywhere, before and after ``sidegraph-migrate``.

# see design/superpowers/specs/2026-10-20-packed-store-format-design.md (§7.2, §9 T-P1)
"""

# ruff: noqa: F811  (the imported ``git_repo`` fixture is redefined as a test argument)

from __future__ import annotations

import io
import json
import re
import sqlite3
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

import sidegraph.host.hooks as hooks
from sidegraph import adr, doctor, integrity
from sidegraph.adr_cli import main as adr_main
from sidegraph.migrate import migrate
from sidegraph.schema import DecisionKind, DecisionStatus, Fact, Provenance
from sidegraph.stats.model import build_report
from sidegraph.store import VOLATILE_STALE_KEY, Store
from sidegraph.store_rules import effective
from sidegraph.store_view import inventory_of
from tests.store_format2_support import (
    commit_all,
    decision_body,
    domain_body,
    fact_body,
    format1_store,
    git,
    git_repo,  # noqa: F401  (pytest fixture)
    make_store,
    put_archive,
    put_file,
    put_shard_line,
)
from tests.test_host_pretool_records import Project, _payload

SESSION_BEFORE = "3f2a9c1e-5b7d-4e8a-9c0b-1d2e3f4a5b6c"
SESSION_AFTER = "4a3b0d2f-6c8e-4f9b-8d1c-2e3f4a5b6c7d"  # a session is told a record once
OLD = "2026-01-01T00:00:00Z"
GITIGNORE = "index.db*\n*.tmp\n"


# -- the world: a committed format-1 store with every shape the readers care about -----------


class World:
    """A repo with a format-1 store: an anchored accepted gotcha, a rejected decision ``D`` with
    a fact that supports only it, and a stranded open fact."""

    def __init__(self, repo: Path) -> None:
        self.repo = repo
        format1_store(repo / ".sidegraph")
        self.project = Project(repo)
        self.live = self.project.record("src/a.py", "Gotcha A")
        self.adr = self.project.record(
            "src/c.py", "Adopt the cache", kind=DecisionKind.ADR, choice="Use the cache."
        )
        self.rule = self.project.record(
            "src/d.py", "Never skip the lock", kind=DecisionKind.CONSTRAINT, choice="Lock first."
        )
        self.dropped = self.project.record(
            "src/b.py", "Declined idea", kind=DecisionKind.ADR, status=DecisionStatus.REJECTED
        )
        self.fact = self.project.store.add_fact(
            Fact(
                statement="measured on D",
                source="benchmark",
                supports=[self.dropped.id],
                valid_from=datetime.now(UTC),
                provenance=Provenance(source="manual"),
            )
        )
        self.project.store.set_meta(VOLATILE_STALE_KEY, "0")
        self.project.close()
        (self.store_dir / ".gitignore").write_text(GITIGNORE, encoding="utf-8")
        commit_all(repo, "store", date=OLD)

    @property
    def store_dir(self) -> Path:
        return self.repo / ".sidegraph"

    def migrate(self) -> None:
        migrate(self.store_dir, dry_run=False, any_branch=True)
        with Store(self.store_dir) as store:
            store.set_meta(VOLATILE_STALE_KEY, "0")


@pytest.fixture
def world(git_repo: Path) -> World:
    return World(git_repo)


def _pretool(world: World, monkeypatch, capsys, rel: str, session: str) -> str:
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(world.repo))
    monkeypatch.setenv("SIDEGRAPH_DIR", str(world.store_dir))
    payload = {"cwd": str(world.repo), **_payload("Read", session, file_path=str(world.repo / rel))}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    hooks.pre_tool_use()
    captured = capsys.readouterr()
    return json.loads(captured.out)["hookSpecificOutput"]["additionalContext"]


def _doctor(store_dir: Path) -> list[tuple[str, str]]:
    """The curation findings as ``(code, detail)``, the path left out: a packed record's
    finding names its shard, a file's names the file."""
    report = doctor.curate(store_dir)
    # the unmigrated store carries store-not-migrated by design
    return sorted((f.code, f.detail) for f in report.findings if f.code != "store-not-migrated")


def _adr(world: World, capsys, out: Path) -> dict[str, str]:
    """The ADR export preview as ``record_id -> diff``, link targets masked: a packed record
    links to its shard, a file to its file."""
    args = ["--db", str(world.store_dir), "--out", str(out), "--json"]
    code = adr_main(args)
    captured = capsys.readouterr()
    assert code == 0, captured.err
    changes = json.loads(captured.out)["changes"]
    return {c["record_id"]: _mask_paths(c["diff"]) for c in changes}


def _mask_paths(diff: str) -> str:
    """Only what is derived from where a record is stored: link targets, and the header hash
    that covers them. Everything else, the record text included, must be equal."""
    diff = re.sub(r"\]\([^)]*\)", "](link)", diff)
    return re.sub(r"sha256=[0-9a-f]{64}", "sha256=-", diff)


def _stats(store_dir: Path) -> dict:
    report = build_report(store_dir, None).model_dump(mode="json")
    return {k: report[k] for k in ("memory", "anchors", "index_stale")}


# -- T-P1 ----------------------------------------------------------------------------------------


def test_pretooluse_doctor_adr_stats_equal_before_and_after_migration(
    world, monkeypatch, capsys, tmp_path
):
    """T-P1. Red: doctor's ``_iter_records`` on files only (the migrated store has no
    ``decisions/`` files, so the fact reads as supporting nothing); ADR export dropping the
    packed decisions (the exported ADR and constraint vanish); any reader with its own
    precedence."""
    monkeypatch.chdir(tmp_path)
    before = (
        _pretool(world, monkeypatch, capsys, "src/a.py", SESSION_BEFORE),
        _doctor(world.store_dir),
        _adr(world, capsys, tmp_path / "docs"),
        _stats(world.store_dir),
    )
    exported = before[2]
    assert {world.adr.id, world.rule.id, "@index"} <= set(exported)  # a real export to compare
    assert "Use the cache." in exported[world.adr.id] and "Lock first." in exported[world.rule.id]
    assert world.dropped.id not in exported and world.live.id not in exported
    world.migrate()
    assert not list((world.store_dir / "decisions").glob("*.json"))  # really packed
    after = (
        _pretool(world, monkeypatch, capsys, "src/a.py", SESSION_AFTER),
        _doctor(world.store_dir),
        _adr(world, capsys, tmp_path / "docs"),
        _stats(world.store_dir),
    )
    assert after == before
    # a store this tidy is not due for maintenance, before or after the migration
    assert "maintenance-due" not in {code for code, _ in before[1] + after[1]}


def test_dropped_then_compacted_D_rejected_everywhere(git_repo, monkeypatch):
    """``D`` is a ``proposed`` shard line whose legacy archive segment holds its ``rejected``
    state (dropped, then compacted): the archive line is the successor, so retrieval's
    ``effective()`` serves it. ADR export, doctor and stats say ``rejected`` too. Red: doctor
    on files only (``supports a decision that does not exist``), a reader that serves the
    shard line, stats' staleness check blind to ``shards/``."""
    monkeypatch.chdir(git_repo)
    store = make_store(git_repo)
    put_shard_line(store, "decision", "D", decision_body("D", status="proposed"))
    put_shard_line(store, "fact", "F", fact_body("F", supports=["D"]))
    rejected = decision_body("D", status="rejected", valid_to="2026-02-01T00:00:00Z")
    put_archive(store, "seg", [{"record_type": "decision", **rejected}])
    assert effective(inventory_of(store), "decision", "D").raw["status"] == "rejected"

    assert adr.read_snapshot(store).decisions["D"].status == DecisionStatus.REJECTED
    assert any("superseded/rejected/deprecated" in d for _code, d in _doctor(store))
    with Store(store) as opened:
        assert opened.get_decision("D").status == DecisionStatus.REJECTED
    report = build_report(store, None)
    assert report.index_stale is False
    assert report.memory is not None and report.memory.historical == 1


def test_adr_export_revived_domain_with_segment_shows_live(git_repo):
    """A domain whose legacy archive segment still holds its ``dropped`` state while the live
    shard line has moved on (``superseded``): the export holds the live record. Red: the
    archive overwrite kept in ``adr.read_snapshot`` (``dropped``)."""
    store = make_store(git_repo)
    put_shard_line(store, "domain", "DM1", domain_body("DM1", status="superseded", title="Live"))
    put_shard_line(store, "domain", "DM2", domain_body("DM2", status="accepted", supersedes="DM1"))
    put_archive(
        store,
        "seg",
        [{"record_type": "domain", **domain_body("DM1", status="dropped", title="Live")}],
    )
    snap = adr.read_snapshot(store)
    assert snap.domains["DM1"].status.value == "superseded"


def test_orphaned_records_names_shard_for_packed(world):
    """``orphaned-records`` names ``shards/NN.shard`` for a packed record, the file for a
    per-record one. Red: ``<table>/<id>.json`` always."""
    world.migrate()
    store_dir = world.store_dir
    conn = sqlite3.connect(store_dir / "index.db")
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("UPDATE anchor_bindings SET data = json_set(data, '$.status', 'orphaned')")
        conn.commit()
        inputs = integrity.Inputs(store_dir=store_dir, index=conn, now=datetime.now(UTC))
        problem = integrity._detect_orphaned_records(inputs)
    finally:
        conn.close()
    assert problem is not None
    paths = [path for path, _ in problem.findings]
    assert paths and all("/shards/" in p and p.endswith(".shard") for p in paths)


def test_orphaned_records_names_the_file_for_a_per_record_file(world):
    store_dir = world.store_dir
    conn = sqlite3.connect(store_dir / "index.db")
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("UPDATE anchor_bindings SET data = json_set(data, '$.status', 'orphaned')")
        conn.commit()
        inputs = integrity.Inputs(store_dir=store_dir, index=conn, now=datetime.now(UTC))
        problem = integrity._detect_orphaned_records(inputs)
    finally:
        conn.close()
    assert problem is not None
    assert all(p.endswith(".json") and "/decisions/" in p for p, _ in problem.findings)


def test_catalog_reads_u2028_in_archive_line(tmp_path):
    """A U+2028 inside an archive line's string is data, not a line break (the inventory splits
    on ``\\n`` only). Red: ``str.splitlines()`` cut the line there and the record was lost."""
    from sidegraph.bootstrap.catalog import load_canonical_catalog

    store = make_store(tmp_path)
    body = decision_body("DA", title="line separator", status="rejected")
    put_archive(store, "seg", [{"record_type": "decision", **body}])
    catalog = load_canonical_catalog(store)
    assert [d.title for d in catalog.decisions] == ["line separator"]


def test_catalog_reads_u2028_in_archive_line_of_a_per_record_store(tmp_path):
    """The same line in a format-1 archive segment. Red: ``splitlines()`` on the segment."""
    from sidegraph.bootstrap.catalog import load_canonical_catalog

    store = format1_store(tmp_path / ".sidegraph")
    body = decision_body("DA", title="line\u2028separator", status="rejected")
    put_archive(store, "seg", [{"record_type": "decision", **body}])
    assert [d.title for d in load_canonical_catalog(store).decisions] == ["line\u2028separator"]


def test_catalog_reads_packed_records(tmp_path):
    from sidegraph.bootstrap.catalog import load_canonical_catalog

    store = make_store(tmp_path)
    put_shard_line(store, "decision", "DS", decision_body("DS"))
    put_shard_line(store, "fact", "FS", fact_body("FS"))
    put_shard_line(store, "domain", "DMS", domain_body("DMS"))
    put_file(store, "decision", decision_body("DF"))
    catalog = load_canonical_catalog(store)
    assert [d.id for d in catalog.decisions] == ["DF", "DS"]
    assert [f.id for f in catalog.facts] == ["FS"]
    assert [d.domain_id for d in catalog.domains] == ["DMS"]


def test_canonical_manifest_hashes_shards(tmp_path):
    """The apply-time manifest covers ``shards/``: a change to a shard moves it."""
    from sidegraph.bootstrap.apply import canonical_manifest

    store = make_store(tmp_path)
    path = put_shard_line(store, "decision", "DS", decision_body("DS"))
    manifest = canonical_manifest(store)
    assert any("/shards/" in key for key in manifest)
    put_shard_line(store, "decision", "DT", decision_body("DT"), bucket=path.name)
    assert canonical_manifest(store) != manifest


def test_stranded_kinds_reads_maintenance_run():
    """Deleted record files beside a changed shard are one maintenance run, not N deleted
    files; deleted files beside a new archive segment stay a compaction. Red: the old reading
    ('3 deleted file(s), 1 other')."""
    S = integrity._Stranded
    run = [S("decisions", True, None), S("facts", True, None), S("shards", False, None)]
    assert integrity._stranded_kinds(run) == "a maintenance run"
    assert integrity._stranded_kinds([S("shards", False, None)]) == "a maintenance run"
    old = [S("decisions", True, None), S("archive", False, None)]
    assert integrity._stranded_kinds(old) == "a compaction"
    assert integrity._stranded_kinds([S("decisions", True, None)]) == "1 deleted file(s)"
    assert (
        integrity._stranded_kinds([S("decisions", False, None), S("shards", False, None)])
        == "a maintenance run, 1 decision file(s)"
    )


def test_store_uncommitted_counts_a_changed_shard(world, monkeypatch):
    """A shard nobody committed is a stranded store write like any record file."""
    world.migrate()
    commit_all(world.repo, "migrated", date=OLD)
    shard = next((world.store_dir / "shards").glob("*.shard"))
    shard.write_bytes(shard.read_bytes() + b"\n")
    inputs = integrity.Inputs(
        store_dir=world.store_dir, index=None, now=datetime(2030, 1, 1, tzinfo=UTC)
    )
    problem = integrity._detect_store_uncommitted(inputs)
    assert problem is not None and "maintenance run" in problem.line


def test_stats_counts_packed_records(world, tmp_path, monkeypatch):
    """The index is not stale against a packed store, and the counts include packed records.
    Red: ``_canonical_files`` without ``shards/`` (the index rows for shards have no file)."""
    monkeypatch.chdir(tmp_path)
    world.migrate()
    report = build_report(world.store_dir, None)
    assert report.index_stale is False
    assert report.memory is not None
    assert (report.memory.decisions, report.memory.historical) == (3, 1)


def test_stats_stale_index_sees_a_changed_shard(world, tmp_path, monkeypatch):
    from sidegraph.stats import model

    monkeypatch.chdir(tmp_path)
    world.migrate()
    shard = next((world.store_dir / "shards").glob("*.shard"))
    conn = sqlite3.connect(world.store_dir / "index.db")
    conn.row_factory = sqlite3.Row
    try:
        assert model._index_is_stale(conn, world.store_dir) is False
        shard.write_bytes(shard.read_bytes() + b"\n")
        assert model._index_is_stale(conn, world.store_dir) is True
    finally:
        conn.close()


def test_branch_only_records_sees_packed_records(git_repo):
    """A branch that adds a per-record file for an identity ``main`` holds only in a shard is
    an edit, not a new record; a branch that adds a new identity is branch-only. Red: the
    per-file diff (the edit counted as a new record, since ``main`` has no such file)."""
    store = make_store(git_repo)
    put_shard_line(store, "decision", "DP", decision_body("DP", status="accepted"))
    (store / ".gitignore").write_text(GITIGNORE, encoding="utf-8")
    commit_all(git_repo, "base", date=OLD)
    git(git_repo, "checkout", "-q", "-b", "feature")
    put_file(store, "decision", decision_body("DP", status="rejected"))  # edit of a packed one
    put_file(store, "decision", decision_body("DN", status="proposed"))  # new
    commit_all(git_repo, "branch", date=OLD)
    git(git_repo, "checkout", "-q", "main")
    records = integrity._branch_records(
        "refs/heads/feature", "refs/heads/main", store, time.monotonic() + 60
    )
    assert [status for _path, status in sorted(records.items())] == ["proposed"]


def test_branch_only_records_on_a_per_file_store_is_unchanged(git_repo):
    store = format1_store(git_repo / ".sidegraph")
    (store / ".gitignore").write_text(GITIGNORE, encoding="utf-8")
    commit_all(git_repo, "base", date=OLD)
    git(git_repo, "checkout", "-q", "-b", "feature")
    put_file(store, "decision", decision_body("DN", status="proposed"))
    commit_all(git_repo, "branch", date=OLD)
    git(git_repo, "checkout", "-q", "main")
    records = integrity._branch_records(
        "refs/heads/feature", "refs/heads/main", store, time.monotonic() + 60
    )
    assert list(records.values()) == ["proposed"]


def test_record_dir_list_of_init_counts_shards(tmp_path):
    """``sidegraph-init`` calls a store with a shard file non-empty."""
    from sidegraph.cli import _is_empty_store_for_init

    store = make_store(tmp_path)
    assert _is_empty_store_for_init(store)
    put_shard_line(store, "decision", "DS", decision_body("DS"))
    assert not _is_empty_store_for_init(store)


# -- review round 1 ------------------------------------------------------------------------------


def _auto(rid: str, **over):
    return decision_body(
        rid,
        status="accepted",
        ratified_at="2026-01-02T00:00:00Z",
        ratified_by="auto:policy",
        **over,
    )


def test_ratification_lines_read_the_effective_records_of_a_packed_store(tmp_path):
    """The auto-share lines count a decision that lives only in a shard. Red: a format-2 store
    sent down the format-1 walk (no per-record files: no auto stamp, no lines)."""
    store = make_store(tmp_path)
    put_shard_line(store, "decision", "DA", _auto("DA"))
    lines = doctor.ratification_info_lines(store)
    assert any(line.startswith("auto share") for line in lines), lines


@pytest.mark.parametrize("where", ["file", "shard"])
def test_ratification_lines_survive_a_record_without_its_id(tmp_path, where):
    """A decision whose body lacks ``id`` (a file, or a shard line) is skipped, not a
    ``KeyError``. Red: ``r["id"]`` unguarded in the format-2 branch."""
    store = make_store(tmp_path)
    put_shard_line(store, "decision", "DA", _auto("DA"))
    body = _auto("DX")
    del body["id"]
    if where == "file":
        put_file(store, "decision", body, record_id="DX")
    else:
        put_shard_line(store, "decision", "DX", body)
    lines = doctor.ratification_info_lines(store)
    assert any(line.startswith("auto share") for line in lines)
    assert "DX" not in doctor._hot_plus_archive_records(store)["decisions"]


def test_a_quarantined_open_decision_is_not_judged_by_doctor(tmp_path):
    """A model-invalid shard decision is no effective record: doctor does not call it open or
    dangling (verify reports it). An invalid *entity* is still read, so it is not 'unreferenced'
    twice over."""
    store = make_store(tmp_path)
    bad = decision_body("DQ", status="accepted")
    bad["kind"] = "not-a-kind"
    put_shard_line(store, "decision", "DQ", bad)
    assert not [f for f in doctor.curate(store).findings if "DQ" in f.path or "DQ" in f.detail]


def test_time_to_ratify_counts_packed_records(tmp_path):
    """A format-2 store counts the stamped records that live in shards."""
    store = make_store(tmp_path)
    put_shard_line(
        store,
        "decision",
        "DR",
        decision_body("DR", status="accepted", ratified_at="2026-01-03T00:00:00Z"),
    )
    assert doctor.ratification_latency_lines(store) == [
        "time-to-ratify: median 2 days, max 2 days (1 stamped record(s))"
    ]


def _packed_repo_with_branches(repo: Path, n: int) -> Path:
    store = make_store(repo)
    put_shard_line(store, "decision", "DP", decision_body("DP", status="accepted"))
    (store / ".gitignore").write_text(GITIGNORE, encoding="utf-8")
    commit_all(repo, "base", date=OLD)
    for i in range(n):
        git(repo, "checkout", "-q", "-b", f"feature{i}", "main")
        put_file(store, "decision", decision_body(f"DN{i}", status="proposed"))
        commit_all(repo, f"branch {i}", date=OLD)
    git(repo, "checkout", "-q", "main")
    return store


def _count_git(monkeypatch) -> list[list[str]]:
    calls: list[list[str]] = []
    real = subprocess.run

    def run(args, *a, **kw):
        calls.append(list(args))
        return real(args, *a, **kw)

    monkeypatch.setattr(subprocess, "run", run)
    return calls


def _scan(store: Path, n: int) -> dict[str, str]:
    scan: dict = {}
    out: dict[str, str] = {}
    for i in range(n):
        out.update(
            integrity._branch_records(
                f"refs/heads/feature{i}", "refs/heads/main", store, time.monotonic() + 60, scan
            )
        )
    return out


def test_packed_branch_scan_builds_the_default_inventory_once(git_repo, monkeypatch):
    """Two branches cost one ``ls-tree`` per branch plus the default branch's single one. Red:
    the default branch's inventory (and the marker probes) rebuilt for every branch."""
    store = _packed_repo_with_branches(git_repo, 2)
    calls = _count_git(monkeypatch)
    found = _scan(store, 2)
    assert sorted(found.values()) == ["proposed"] * 2
    trees = [c for c in calls if "ls-tree" in c]
    assert len(trees) == 2 + 1
    markers = [c for c in calls if "cat-file" in c and "-p" in c]
    assert len(markers) == 1, markers  # the default branch's; a branch with its blob reads none


def test_packed_branch_scan_runs_every_git_call_under_the_budget(git_repo, monkeypatch):
    """A stalled git is ``_BudgetExhausted``, like the per-file scan: every call carries the
    detector's remaining time and a timeout is translated. Red: ``inventory_from_git_tree``'s
    calls had no timeout."""
    store = _packed_repo_with_branches(git_repo, 1)
    real = subprocess.run
    timeouts: list[float | None] = []

    def stalled(args, *a, **kw):
        if "ls-tree" in args:
            timeouts.append(kw.get("timeout"))
            raise subprocess.TimeoutExpired(args, kw.get("timeout") or 0)
        return real(args, *a, **kw)

    monkeypatch.setattr(subprocess, "run", stalled)
    with pytest.raises(integrity._BudgetExhausted):
        integrity._branch_records(
            "refs/heads/feature0", "refs/heads/main", store, time.monotonic() + 30, {}
        )
    assert timeouts and all(t is not None and 0 < t <= 30 for t in timeouts)


def test_packed_branch_scan_with_the_budget_spent_is_exhausted(git_repo):
    store = _packed_repo_with_branches(git_repo, 1)
    with pytest.raises(integrity._BudgetExhausted):
        integrity._branch_records(
            "refs/heads/feature0", "refs/heads/main", store, time.monotonic() - 1, {}
        )


# -- catalog refusals, both layouts --------------------------------------------------------------


def _layout_store(tmp_path: Path, layout: str) -> Path:
    return (
        format1_store(tmp_path / ".sidegraph") if layout == "per-record" else make_store(tmp_path)
    )


@pytest.mark.parametrize("layout", ["per-record", "packed"])
@pytest.mark.parametrize(
    ("relative_path", "content"),
    [("decisions/bad.json", "{"), ("archive/bad.jsonl", "{\n")],
)
def test_malformed_canonical_data_refuses_the_catalog_in_both_layouts(
    tmp_path, layout, relative_path, content
):
    """Red on format 2: the archive line was left out of the error report and the catalog came
    back empty and silent."""
    from sidegraph.bootstrap.catalog import load_canonical_catalog

    store = _layout_store(tmp_path, layout)
    path = store / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    with pytest.raises(ValueError) as caught:
        load_canonical_catalog(store)
    assert str(path) in str(caught.value) and "invalid canonical" in str(caught.value)
    if relative_path.startswith("archive"):
        assert f"{path}:1" in str(caught.value)  # the line


@pytest.mark.parametrize("layout", ["per-record", "packed"])
def test_a_model_invalid_record_refuses_the_catalog_in_both_layouts(tmp_path, layout):
    """Red on format 2: a model-invalid file was warned about and dropped; format 1 refuses."""
    from sidegraph.bootstrap.catalog import load_canonical_catalog

    store = _layout_store(tmp_path, layout)
    bad = decision_body("DQ")
    bad["kind"] = "not-a-kind"
    path = put_file(store, "decision", bad)
    with pytest.raises(ValueError) as caught:
        load_canonical_catalog(store)
    assert str(path) in str(caught.value)


def test_a_model_invalid_shard_line_refuses_the_catalog_naming_the_line(tmp_path):
    from sidegraph.bootstrap.catalog import load_canonical_catalog

    store = make_store(tmp_path)
    bad = decision_body("DQ")
    bad["kind"] = "not-a-kind"
    shard = put_shard_line(store, "decision", "DQ", bad)
    with pytest.raises(ValueError) as caught:
        load_canonical_catalog(store)
    assert f"{shard}:1" in str(caught.value)


@pytest.mark.parametrize("layout", ["per-record", "packed"])
def test_an_archive_line_of_an_unknown_type_is_ignored_in_both_layouts(tmp_path, layout, capsys):
    from sidegraph.bootstrap.catalog import load_canonical_catalog

    store = _layout_store(tmp_path, layout)
    put_archive(store, "seg", [{"record_type": "future-kind", "x": 1}])
    assert load_canonical_catalog(store).decisions == ()
    assert capsys.readouterr().err == ""


# -- a quarantined identity is not served by the strict readers ---------------------------------


def _quarantined_store(tmp_path: Path) -> Path:
    store = make_store(tmp_path)
    put_shard_line(store, "decision", "DOK", decision_body("DOK", status="accepted"))
    bad = decision_body("DQ", status="accepted")
    bad["kind"] = "not-a-kind"
    put_shard_line(store, "decision", "DQ", bad)
    return store


def test_the_strict_view_does_not_serve_a_quarantined_identity(tmp_path):
    from sidegraph.store_view import effective_records

    store = _quarantined_store(tmp_path)
    strict = effective_records(store)
    assert strict is not None and ("decision", "DOK") in strict
    assert ("decision", "DQ") not in strict
    lenient = effective_records(store, lenient=True)
    assert lenient is not None and ("decision", "DQ") in lenient  # the diagnostic read only


def test_adr_export_does_not_serve_a_quarantined_identity(tmp_path):
    """Verify refuses the store before any record is read; the snapshot builder, handed the
    strict view, has no ``DQ`` either."""
    from sidegraph.store_view import effective_records

    store = _quarantined_store(tmp_path)
    with pytest.raises(ValueError, match="store verification failed"):
        adr.read_snapshot(store)
    packed = effective_records(store, any_format=True)
    assert packed is not None
    assert set(adr._snapshot_of(store, packed).decisions) == {"DOK"}


def test_orphaned_records_does_not_locate_a_quarantined_identity(tmp_path):
    """A quarantined record has no source to name; the finding falls back to the per-record
    path rather than a shard line it did not load."""
    store = _quarantined_store(tmp_path)
    where = integrity._record_locations(store)
    assert where("decisions", "DOK").endswith(".shard")
    assert where("decisions", "DQ") == str(store / "decisions" / "DQ.json")


def test_auto_share_does_not_fall_through_to_an_archive_line_of_a_quarantined_identity(tmp_path):
    """``D``'s shard line is unreadable, so ``D`` has no effective record: its legacy archive
    line must not be counted in its place. Red: a format-2 store read as format 1 (hot wins,
    else the archive pool)."""
    store = make_store(tmp_path)
    put_shard_line(store, "decision", "DA", _auto("DA"))
    bad = decision_body("DQ", status="accepted")
    bad["kind"] = "not-a-kind"
    put_shard_line(store, "decision", "DQ", bad)
    gone = decision_body(
        "DQ",
        status="rejected",
        valid_to="2026-02-01T00:00:00Z",
        ratified_at="2026-01-02T00:00:00Z",
        ratified_by="alice",
    )
    put_archive(store, "seg", [{"record_type": "decision", **gone}])
    assert set(doctor._hot_plus_archive_records(store)["decisions"]) == {"DA"}
    lines = doctor.ratification_info_lines(store)
    assert any("decisions 1/1 (100%)" in line for line in lines), lines


# -- round 2 -------------------------------------------------------------------------------------


def test_packed_branch_scan_skips_a_record_of_an_unknown_type(git_repo):
    """A tree holding a shard line of a type this version does not know is not a crash: the
    registry would swallow it and the detector would silently say nothing. Red: ``KeyError``
    on the type."""
    store = make_store(git_repo)
    put_shard_line(store, "decision", "DP", decision_body("DP", status="accepted"))
    (store / ".gitignore").write_text(GITIGNORE, encoding="utf-8")
    commit_all(git_repo, "base", date=OLD)
    git(git_repo, "checkout", "-q", "-b", "feature0")
    shard = next((store / "shards").glob("*.shard"))
    line = b'{"type":"future-kind","id":"FK1","body":{"x":1}}\n'
    shard.write_bytes(shard.read_bytes() + line)
    put_file(store, "decision", decision_body("DN", status="proposed"))
    commit_all(git_repo, "branch", date=OLD)
    git(git_repo, "checkout", "-q", "main")
    records = integrity._branch_records(
        "refs/heads/feature0", "refs/heads/main", store, time.monotonic() + 60, {}
    )
    assert list(records.values()) == ["proposed"]


def _fail_reads(monkeypatch, suffix: str) -> None:
    real = Path.read_bytes

    def read_bytes(self):
        if self.name.endswith(suffix):
            raise PermissionError(13, "Permission denied", str(self))
        return real(self)

    monkeypatch.setattr(Path, "read_bytes", read_bytes)


@pytest.mark.parametrize(
    ("kind", "suffix"), [("file", "DF.json"), ("shard", ".shard"), ("archive", ".jsonl")]
)
def test_catalog_refuses_a_store_file_it_cannot_read(tmp_path, monkeypatch, kind, suffix):
    """A failed read of a per-record file, a shard or an archive segment refuses the catalog
    naming the path, as format 1 does. Red: the inventory's default callback dropped the file
    and the catalog came back empty."""
    from sidegraph.bootstrap.catalog import load_canonical_catalog

    store = make_store(tmp_path)
    put_file(store, "decision", decision_body("DF"))
    put_shard_line(store, "decision", "DS", decision_body("DS"))
    put_archive(
        store, "seg", [{"record_type": "decision", **decision_body("DA", status="rejected")}]
    )
    _fail_reads(monkeypatch, suffix)
    with pytest.raises(ValueError) as caught:
        load_canonical_catalog(store)
    assert f"invalid canonical {kind}" in str(caught.value)
    assert suffix in str(caught.value) and "Permission denied" in str(caught.value)
