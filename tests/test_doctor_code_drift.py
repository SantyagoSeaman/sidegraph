"""Staleness machinery D5: doctor's ``code-drift`` advisory check — a LIVE decision's
anchored file changed since its capture-time HEAD. Stores are hand-built minimal canonical
JSON files, same convention as ``test_doctor.py``; git-backed tests use a real ``tmp_path``
repo, same convention as ``test_verify_transitions.py``'s git-backed integration group.
# see design/superpowers/specs/2026-07-30-staleness-machinery-design.md (D5)
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
from pathlib import Path

import pytest
from ulid import ULID

from sidegraph.doctor import CODE_DRIFT, curate


def _git(args: list[str], cwd: Path) -> None:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    assert result.returncode == 0, f"git {args} failed: {result.stderr}"


def _head(cwd: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=cwd, capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture
def git_repo(tmp_path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-q"], cwd=repo)
    _git(["config", "user.email", "test@example.com"], cwd=repo)
    _git(["config", "user.name", "Test"], cwd=repo)
    return repo


def _write(root: Path, rel: str, payload: object) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload), encoding="utf-8")


def _ulid() -> str:
    return str(ULID())


def _decision(root: Path, rid: str, *, commit: str | None, status: str = "accepted") -> None:
    _write(
        root,
        f"decisions/{rid}.json",
        {
            "id": rid,
            "status": status,
            "valid_to": None,
            "provenance": {"source": "agent", "commit": commit},
        },
    )


def _entity(root: Path, eid: str, name: str, file_path: str) -> None:
    _write(
        root,
        f"entities/{eid}.json",
        {
            "entity_id": eid,
            "canonical_name": name,
            "descriptor": {"name": name, "file_path": file_path},
        },
    )


def _bind(root: Path, rid: str, entity_id: str) -> None:
    _write(root, f"bindings/{rid}.json", [{"entity_id": entity_id, "tier": 2, "weight": 1.0}])


def _index_with_bindings(root: Path, rows: list[tuple[str, str, str]]) -> None:
    conn = sqlite3.connect(root / "index.db")
    conn.execute("CREATE TABLE anchor_bindings (record_id TEXT, entity_id TEXT, data TEXT)")
    for record_id, entity_id, status in rows:
        conn.execute(
            "INSERT INTO anchor_bindings VALUES (?, ?, ?)",
            (record_id, entity_id, json.dumps({"status": status})),
        )
    conn.commit()
    conn.close()


def _codes(report) -> list[str]:
    return [f.code for f in report.findings]


# -- finding on a moved/changed anchored file -----------------------------------------------


def test_code_drift_finding_on_file_changed_since_capture(git_repo):
    store_dir = git_repo / ".sidegraph"
    src = git_repo / "trader" / "exec.py"
    src.parent.mkdir(parents=True)
    src.write_text("original\n")
    _git(["add", "-A"], cwd=git_repo)
    _git(["commit", "-q", "-m", "initial"], cwd=git_repo)
    commit = _head(git_repo)

    rid, eid = _ulid(), _ulid()
    _decision(store_dir, rid, commit=commit)
    _entity(store_dir, eid, "Trader", "trader/exec.py")
    _bind(store_dir, rid, eid)

    src.write_text("changed\n")
    _git(["add", "-A"], cwd=git_repo)
    _git(["commit", "-q", "-m", "change trader"], cwd=git_repo)

    report = curate(store_dir)
    assert CODE_DRIFT in _codes(report)
    drift = next(
        f for f in report.findings if f.code == CODE_DRIFT and "trader/exec.py" in f.detail
    )
    assert commit[:8] in drift.detail
    assert drift.path == str(store_dir / "decisions" / f"{rid}.json")


def test_code_drift_no_finding_when_anchored_file_unchanged(git_repo):
    store_dir = git_repo / ".sidegraph"
    src = git_repo / "trader" / "exec.py"
    src.parent.mkdir(parents=True)
    src.write_text("original\n")
    _git(["add", "-A"], cwd=git_repo)
    _git(["commit", "-q", "-m", "initial"], cwd=git_repo)
    commit = _head(git_repo)

    rid, eid = _ulid(), _ulid()
    _decision(store_dir, rid, commit=commit)
    _entity(store_dir, eid, "Trader", "trader/exec.py")
    _bind(store_dir, rid, eid)

    # A second, unrelated commit -- trader/exec.py itself never changes.
    (git_repo / "other.py").write_text("x\n")
    _git(["add", "-A"], cwd=git_repo)
    _git(["commit", "-q", "-m", "unrelated"], cwd=git_repo)

    report = curate(store_dir)
    assert CODE_DRIFT not in _codes(report)


def test_terminal_decision_is_not_a_drift_target(git_repo):
    """Only LIVE (proposed/accepted) decisions are drift targets -- a superseded one
    predates a rewrite for a reason unrelated to this check's charter."""
    store_dir = git_repo / ".sidegraph"
    src = git_repo / "trader" / "exec.py"
    src.parent.mkdir(parents=True)
    src.write_text("original\n")
    _git(["add", "-A"], cwd=git_repo)
    _git(["commit", "-q", "-m", "initial"], cwd=git_repo)
    commit = _head(git_repo)

    rid, eid = _ulid(), _ulid()
    _decision(store_dir, rid, commit=commit, status="superseded")
    _entity(store_dir, eid, "Trader", "trader/exec.py")
    _bind(store_dir, rid, eid)

    src.write_text("changed\n")
    _git(["add", "-A"], cwd=git_repo)
    _git(["commit", "-q", "-m", "change"], cwd=git_repo)

    report = curate(store_dir)
    assert CODE_DRIFT not in _codes(report)


# -- unstamped records: counted once alongside real drift, silent when the store has no --
# -- commit-stamped decision at all (STOP-and-report: see the report to team-lead) --------


def test_store_with_no_commit_stamped_decision_at_all_is_silent(tmp_path):
    """Every pre-existing store (created before this wave shipped) has ZERO commit-stamped
    decisions -- an unconditional "N record(s) predate commit stamping" note would cost
    every one of them a clean sidegraph-doctor run forever, which contradicts the
    pre-existing CLI contract (test_cli_doctor.py's healthy-store fixtures, uncommented,
    have no ``provenance.commit`` and must stay ``clean``). The check stays a complete
    no-op until at least one commit-stamped, anchored decision exists to actually check."""
    store_dir = tmp_path / ".sidegraph"
    for _ in range(3):
        _decision(store_dir, _ulid(), commit=None)
    report = curate(store_dir, repo_root=tmp_path)
    assert not any(f.code == CODE_DRIFT for f in report.findings)


def test_unstamped_and_drift_findings_coexist(git_repo):
    store_dir = git_repo / ".sidegraph"
    src = git_repo / "trader" / "exec.py"
    src.parent.mkdir(parents=True)
    src.write_text("original\n")
    _git(["add", "-A"], cwd=git_repo)
    _git(["commit", "-q", "-m", "initial"], cwd=git_repo)
    commit = _head(git_repo)

    rid, eid = _ulid(), _ulid()
    _decision(store_dir, rid, commit=commit)
    _entity(store_dir, eid, "Trader", "trader/exec.py")
    _bind(store_dir, rid, eid)
    _decision(store_dir, _ulid(), commit=None)  # pre-wave record, no commit

    src.write_text("changed\n")
    _git(["add", "-A"], cwd=git_repo)
    _git(["commit", "-q", "-m", "change trader"], cwd=git_repo)

    report = curate(store_dir)
    findings = [f for f in report.findings if f.code == CODE_DRIFT]
    assert len(findings) == 2
    note = next(f for f in findings if "1 record(s) predate commit stamping" in f.detail)
    assert note.record_id is None and note.record_type is None
    assert any("trader/exec.py" in f.detail for f in findings)


# -- git-unavailable: skipped-note finding, never a crash, never a silent zero ---------------


def test_gitless_store_yields_skipped_note_never_silent_zero(tmp_path):
    """`store_dir` sits outside any git repository -- `_find_repo_root` fails, and that
    failure must surface as a Finding (never a crash, never an empty findings list that
    reads as a clean pass -- design ruling, the C1 silent-zero shape rejected)."""
    store_dir = tmp_path / ".sidegraph"
    rid, eid = _ulid(), _ulid()
    _decision(store_dir, rid, commit="deadbeef")
    _entity(store_dir, eid, "Trader", "trader/exec.py")
    _bind(store_dir, rid, eid)

    report = curate(store_dir)  # no repo_root override -- must resolve (and fail) itself
    findings = [f for f in report.findings if f.code == CODE_DRIFT]
    assert len(findings) == 1
    assert "git unavailable" in findings[0].detail


def test_gitless_store_with_only_unstamped_records_stays_silent(tmp_path):
    """No commit-stamped, anchored decision at all -> `by_commit` stays empty -> the whole
    check is a no-op (see the "silent when nothing stamped" test above), never touching
    git at all -- so a store outside any git repo doesn't even hit the git-unavailable
    path here, let alone raise."""
    store_dir = tmp_path / ".sidegraph"
    _decision(store_dir, _ulid(), commit=None)
    report = curate(store_dir)
    assert not any(f.code == CODE_DRIFT for f in report.findings)


# -- orphaned-binding exclusion ---------------------------------------------------------------


def test_orphaned_binding_excluded_from_code_drift(git_repo):
    store_dir = git_repo / ".sidegraph"
    src = git_repo / "trader" / "exec.py"
    src.parent.mkdir(parents=True)
    src.write_text("original\n")
    _git(["add", "-A"], cwd=git_repo)
    _git(["commit", "-q", "-m", "initial"], cwd=git_repo)
    commit = _head(git_repo)

    rid, eid = _ulid(), _ulid()
    _decision(store_dir, rid, commit=commit)
    _entity(store_dir, eid, "Trader", "trader/exec.py")
    _bind(store_dir, rid, eid)
    _index_with_bindings(store_dir, [(rid, eid, "orphaned")])

    src.write_text("changed\n")
    _git(["add", "-A"], cwd=git_repo)
    _git(["commit", "-q", "-m", "change trader"], cwd=git_repo)

    report = curate(store_dir)
    assert not any(f.code == CODE_DRIFT and "trader/exec.py" in f.detail for f in report.findings)


def test_degraded_binding_still_included_in_code_drift(git_repo):
    """Only ORPHANED is excluded -- a degraded binding is still live enough to check (same
    live/degraded-vs-orphaned split ``valid_decisions_for_entity`` itself draws)."""
    store_dir = git_repo / ".sidegraph"
    src = git_repo / "trader" / "exec.py"
    src.parent.mkdir(parents=True)
    src.write_text("original\n")
    _git(["add", "-A"], cwd=git_repo)
    _git(["commit", "-q", "-m", "initial"], cwd=git_repo)
    commit = _head(git_repo)

    rid, eid = _ulid(), _ulid()
    _decision(store_dir, rid, commit=commit)
    _entity(store_dir, eid, "Trader", "trader/exec.py")
    _bind(store_dir, rid, eid)
    _index_with_bindings(store_dir, [(rid, eid, "degraded")])

    src.write_text("changed\n")
    _git(["add", "-A"], cwd=git_repo)
    _git(["commit", "-q", "-m", "change trader"], cwd=git_repo)

    report = curate(store_dir)
    assert any(f.code == CODE_DRIFT and "trader/exec.py" in f.detail for f in report.findings)


# -- appended at the END of curate()'s listing order -----------------------------------------


def test_code_drift_finding_appended_after_every_other_check(git_repo):
    store_dir = git_repo / ".sidegraph"
    src = git_repo / "trader" / "exec.py"
    src.parent.mkdir(parents=True)
    src.write_text("original\n")
    _git(["add", "-A"], cwd=git_repo)
    _git(["commit", "-q", "-m", "initial"], cwd=git_repo)
    commit = _head(git_repo)

    rid, eid = _ulid(), _ulid()
    _decision(store_dir, rid, commit=commit)
    _entity(store_dir, eid, "Trader", "trader/exec.py")
    _bind(store_dir, rid, eid)
    # Also an unrelated dangling decision, to guarantee at least one earlier-listed finding.
    _decision(store_dir, _ulid(), commit=None)

    src.write_text("changed\n")
    _git(["add", "-A"], cwd=git_repo)
    _git(["commit", "-q", "-m", "change trader"], cwd=git_repo)

    report = curate(store_dir)
    assert report.findings[-1].code == CODE_DRIFT


# -- uncomputed statuses: deleted anchors are not drift ------------------------------------


def _set_status_flag(root: Path, value: str) -> None:
    with sqlite3.connect(root / "index.db") as conn:
        conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute("INSERT INTO meta VALUES ('volatile_stale', ?)", (value,))


def test_real_sync_orphan_deleted_then_reloaded_is_not_code_drift(git_repo, tmp_path):
    """A canonical reload must not turn a synced orphan's deleted file into drift."""
    from datetime import UTC, datetime

    from sidegraph.doctor import scan_code_drift
    from sidegraph.schema import (
        AnchorBinding,
        Decision,
        DecisionKind,
        DecisionStatus,
        Descriptor,
        Entity,
        Provenance,
    )
    from sidegraph.store import Store
    from sidegraph.sync import sync
    from tests.graph_fixtures import node, reader_of

    source = git_repo / "a.py"
    source.write_text("def gone():\n    return 1\n")
    _git(["add", "a.py"], cwd=git_repo)
    _git(["commit", "-q", "-m", "capture"], cwd=git_repo)
    commit = _head(git_repo)
    reader = reader_of(tmp_path, node("other", "Other", "other.py"))
    graph_before = (tmp_path / "graph.json").read_bytes()
    store_dir = git_repo / ".sidegraph"
    with Store(store_dir) as store:
        decision = store.add_decision(
            Decision(
                title="Deleted anchor",
                kind=DecisionKind.GOTCHA,
                status=DecisionStatus.ACCEPTED,
                context="The captured symbol was removed",
                choice="Keep decision history",
                valid_from=datetime.now(UTC),
                provenance=Provenance(source="manual", commit=commit),
            )
        )
        entity = store.upsert_entity(
            Entity(canonical_name="gone", descriptor=Descriptor(name="gone", file_path="a.py"))
        )
        store.add_binding(AnchorBinding(record_id=decision.id, entity_id=entity.entity_id, tier=2))
        before_sync = {p: p.read_bytes() for p in store_dir.rglob("*.json")}
        sync(store, reader, force=True)
        assert {p: p.read_bytes() for p in before_sync} == before_sync
        assert store.bindings_for_record(decision.id)[0].status == "orphaned"
        assert store.get_meta("volatile_stale") == "0"

    source.unlink()
    _git(["add", "-u", "--", "a.py"], cwd=git_repo)
    _git(["commit", "-q", "-m", "delete anchor"], cwd=git_repo)
    assert [e for b in scan_code_drift(store_dir).batches for e in b.entries] == []

    path = store_dir / "decisions" / f"{decision.id}.json"
    payload = json.loads(path.read_text())
    payload["context"] = "External canonical update triggers index reload"
    path.write_text(json.dumps(payload) + "\n")
    with Store(store_dir) as store:
        assert store.get_meta("volatile_stale") == "1"
        assert store.bindings_for_record(decision.id)[0].status == "live"

    before_scan = {p: p.read_bytes() for p in store_dir.rglob("*") if p.is_file()}
    scan = scan_code_drift(store_dir)
    assert {p: p.read_bytes() for p in store_dir.rglob("*") if p.is_file()} == before_scan
    assert (tmp_path / "graph.json").read_bytes() == graph_before
    assert [e for b in scan.batches for e in b.entries] == []


def test_uncomputed_modified_file_is_reported_with_unknown_binding_status(git_repo):
    """Graphless stores must retain real drift without implying computed-live bindings."""
    from sidegraph.doctor import BINDING_STATUS_CHECK, scan_code_drift

    source = git_repo / "a.py"
    source.write_text("before\n")
    _git(["add", "a.py"], cwd=git_repo)
    _git(["commit", "-q", "-m", "capture"], cwd=git_repo)
    store_dir = git_repo / ".sidegraph"
    rid, eid = _ulid(), _ulid()
    _decision(store_dir, rid, commit=_head(git_repo))
    _entity(store_dir, eid, "A", "a.py")
    _bind(store_dir, rid, eid)
    _index_with_bindings(store_dir, [(rid, eid, "live")])
    _set_status_flag(store_dir, "1")
    source.write_text("after\n")
    _git(["add", "a.py"], cwd=git_repo)
    _git(["commit", "-q", "-m", "modify anchor"], cwd=git_repo)

    report = curate(store_dir)
    drift = [f for f in report.findings if f.code == CODE_DRIFT]
    assert len(drift) == 1
    assert drift[0].path == str(store_dir / "decisions" / f"{rid}.json")
    assert "binding status unknown" in drift[0].detail
    assert BINDING_STATUS_CHECK in report.skipped
    scan = scan_code_drift(store_dir)
    assert scan.binding_statuses_uncomputed
    assert [e.files for b in scan.batches for e in b.entries] == [["a.py"]]


def test_uncomputed_mixed_anchors_keep_modified_path_and_exclude_deleted_path(git_repo):
    """Deletion filtering is per path, not a blanket skip of a partly orphaned record."""
    from sidegraph.doctor import scan_code_drift

    for name in ("deleted.py", "modified.py"):
        (git_repo / name).write_text("before\n")
    _git(["add", "deleted.py", "modified.py"], cwd=git_repo)
    _git(["commit", "-q", "-m", "capture"], cwd=git_repo)
    store_dir = git_repo / ".sidegraph"
    rid, deleted, modified = _ulid(), _ulid(), _ulid()
    _decision(store_dir, rid, commit=_head(git_repo))
    _entity(store_dir, deleted, "Deleted", "deleted.py")
    _entity(store_dir, modified, "Modified", "modified.py")
    _write(store_dir, f"bindings/{rid}.json", [{"entity_id": deleted}, {"entity_id": modified}])
    _index_with_bindings(store_dir, [(rid, deleted, "live"), (rid, modified, "live")])
    _set_status_flag(store_dir, "1")
    (git_repo / "deleted.py").unlink()
    (git_repo / "modified.py").write_text("after\n")
    _git(["add", "-u"], cwd=git_repo)
    _git(["commit", "-q", "-m", "delete and modify anchors"], cwd=git_repo)

    scan = scan_code_drift(store_dir)
    entries = [e for b in scan.batches for e in b.entries]
    assert [(e.record_id, e.record_path, e.files) for e in entries] == [
        (rid, str(store_dir / "decisions" / f"{rid}.json"), ["modified.py"])
    ]


def test_binding_snapshot_blocks_writer_until_rows_fetched_but_releases_before_json(
    tmp_path, monkeypatch
):
    """An autocommit read would allow a reload to commit between certification and rows."""
    from sidegraph import doctor

    _index_with_bindings(tmp_path, [("R", "E", "orphaned")])
    _set_status_flag(tmp_path, "0")
    connect, loads = sqlite3.connect, json.loads
    events = []
    with connect(tmp_path / "index.db", timeout=0) as writer:

        def assert_writer_blocked(phase):
            writer.execute("UPDATE meta SET value = '1'")
            writer.execute('UPDATE anchor_bindings SET data = \'{"status":"live"}\'')
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                writer.commit()
            events.append(phase)

        class ObservedFlagCursor(sqlite3.Cursor):
            def fetchone(self):
                row = super().fetchone()
                assert_writer_blocked("writer blocked after certification")
                return row

        class ObservedRowsCursor(sqlite3.Cursor):
            def fetchall(self):
                rows = super().fetchall()
                assert_writer_blocked("writer blocked after rows")
                return rows

        class ObservedConnection(sqlite3.Connection):
            def execute(self, sql, parameters=()):
                if sql.startswith("SELECT record_id, entity_id, data FROM anchor_bindings"):
                    return self.cursor(factory=ObservedRowsCursor).execute(sql, parameters)
                if sql.startswith("SELECT value FROM meta WHERE key"):
                    return self.cursor(factory=ObservedFlagCursor).execute(sql, parameters)
                return super().execute(sql, parameters)

        def reading_connect(*args, **kwargs):
            return connect(*args, factory=ObservedConnection, **kwargs)

        def parse_after_close(data, *args, **kwargs):
            writer.commit()  # succeeds only after doctor's reader has released its snapshot
            events.append("writer committed before parsing")
            return loads(data, *args, **kwargs)

        monkeypatch.setattr(doctor.sqlite3, "connect", reading_connect)
        monkeypatch.setattr(doctor.json, "loads", parse_after_close)
        assert doctor._binding_status_by_key(tmp_path) == {("R", "E"): "orphaned"}
        assert events == [
            "writer blocked after certification",
            "writer blocked after rows",
            "writer committed before parsing",
        ]
        assert writer.execute("SELECT value FROM meta").fetchone() == ("1",)


@pytest.mark.parametrize(
    ("index_state", "status", "want_files", "unknown"),
    [
        ("0", "live", ["deleted.py", "modified.py"], False),
        ("0", "degraded", ["deleted.py", "modified.py"], False),
        ("0", "orphaned", [], False),
        ("1", "live", ["modified.py"], True),
        ("1", "orphaned", [], True),
        ("no-meta", "live", ["deleted.py", "modified.py"], False),
        ("missing", "live", ["modified.py"], True),
        ("corrupt", "live", ["modified.py"], True),
        pytest.param("bad-meta", "orphaned", [], True, id="unreadable-meta-keeps-readable-orphan"),
    ],
)
def test_drift_certification_preserves_legacy_and_handles_unavailable_index(
    git_repo, index_state, status, want_files, unknown
):
    """Compatibility guards plus unavailable-index fallback: no blanket graphless skip."""
    from sidegraph.doctor import scan_code_drift

    for name in ("deleted.py", "modified.py"):
        (git_repo / name).write_text("before\n")
    _git(["add", "deleted.py", "modified.py"], cwd=git_repo)
    _git(["commit", "-q", "-m", "capture"], cwd=git_repo)
    store_dir = git_repo / ".sidegraph"
    rid = _ulid()
    _decision(store_dir, rid, commit=_head(git_repo))
    eids = []
    for name in ("deleted.py", "modified.py"):
        eid = _ulid()
        eids.append(eid)
        _entity(store_dir, eid, name, name)
    _write(store_dir, f"bindings/{rid}.json", [{"entity_id": eid} for eid in eids])
    if index_state == "corrupt":
        (store_dir / "index.db").write_bytes(b"unreadable SQLite index")
    elif index_state != "missing":
        _index_with_bindings(store_dir, [(rid, eid, status) for eid in eids])
        if index_state == "bad-meta":
            with sqlite3.connect(store_dir / "index.db") as conn:
                conn.execute("CREATE TABLE meta (key TEXT)")
        elif index_state == "no-bindings":
            _set_status_flag(store_dir, "0")
            with sqlite3.connect(store_dir / "index.db") as conn:
                conn.execute("DROP TABLE anchor_bindings")
        elif index_state != "no-meta":
            _set_status_flag(store_dir, index_state)
            if index_state == "no-key":
                with sqlite3.connect(store_dir / "index.db") as conn:
                    conn.execute("DELETE FROM meta")
    (git_repo / "deleted.py").unlink()
    (git_repo / "modified.py").write_text("after\n")
    _git(["add", "-u"], cwd=git_repo)
    _git(["commit", "-q", "-m", "change anchors"], cwd=git_repo)

    scan = scan_code_drift(store_dir)
    assert [e.files for b in scan.batches for e in b.entries] == (
        [want_files] if want_files else []
    )
    assert scan.binding_statuses_uncomputed is unknown
    findings = [f for f in curate(store_dir).findings if f.code == CODE_DRIFT]
    assert len(findings) == bool(want_files)
    for finding in findings:
        assert ("binding status unknown" in finding.detail) is unknown


def test_uncomputed_rename_keeps_new_anchor_but_not_deleted_old_path(git_repo):
    """Git rename detection must not transfer an old binding onto the renamed file."""
    from sidegraph.doctor import scan_code_drift

    (git_repo / "old.py").write_text("unchanged contents\n")
    _git(["add", "old.py"], cwd=git_repo)
    _git(["commit", "-q", "-m", "capture"], cwd=git_repo)
    store_dir = git_repo / ".sidegraph"
    rid, old, new = _ulid(), _ulid(), _ulid()
    _decision(store_dir, rid, commit=_head(git_repo))
    _entity(store_dir, old, "Old", "old.py")
    _entity(store_dir, new, "New", "new.py")
    _write(store_dir, f"bindings/{rid}.json", [{"entity_id": old}, {"entity_id": new}])
    _git(["mv", "old.py", "new.py"], cwd=git_repo)
    _git(["commit", "-q", "-m", "rename anchor"], cwd=git_repo)

    scan = scan_code_drift(store_dir)
    assert [e.files for b in scan.batches for e in b.entries] == [["new.py"]]


@pytest.mark.parametrize(
    "name",
    [
        "modified.py",
        pytest.param(
            "line\nbreak.py", marks=pytest.mark.skipif(os.name == "nt", reason="POSIX filename")
        ),
    ],
)
def test_uncommitted_worktree_deletion_does_not_hide_committed_drift(git_repo, name):
    """Only HEAD deletions are excluded; NUL parsing must preserve a newline filename."""
    from sidegraph.doctor import scan_code_drift

    source = git_repo / name
    source.write_text("before\n")
    _git(["add", "--", name], cwd=git_repo)
    _git(["commit", "-q", "-m", "capture"], cwd=git_repo)
    store_dir = git_repo / ".sidegraph"
    rid, eid = _ulid(), _ulid()
    _decision(store_dir, rid, commit=_head(git_repo))
    _entity(store_dir, eid, name, name)
    _bind(store_dir, rid, eid)
    source.write_text("after\n")
    _git(["add", "--", name], cwd=git_repo)
    _git(["commit", "-q", "-m", "modify anchor"], cwd=git_repo)
    source.unlink()

    scan = scan_code_drift(store_dir)
    assert [e.files for b in scan.batches for e in b.entries] == [[name]]


@pytest.mark.parametrize("data", ["{", "null", "[]", '{"status": []}', "{}", None])
def test_malformed_binding_rows_do_not_abort_drift_status_read(tmp_path, data):
    """A malformed raw row must not hide the usable orphaned row beside it."""
    from sidegraph.doctor import _binding_status_by_key

    _index_with_bindings(tmp_path, [("R", "E", "orphaned")])
    with sqlite3.connect(tmp_path / "index.db") as conn:
        conn.execute("INSERT INTO anchor_bindings VALUES ('bad', 'bad', ?)", (data,))
    assert _binding_status_by_key(tmp_path) == {("R", "E"): "orphaned"}


# --- scan fields for grouped triage (basis, check blobs, root, statuses, compared HEAD) ---
# see design/superpowers/specs/2026-10-27-grouped-drift-triage-design.md §4.1

from sidegraph import doctor  # noqa: E402
from sidegraph.doctor import _binding_entries, _iter_records  # noqa: E402
from sidegraph.schema import AnchorBinding, Descriptor, Entity  # noqa: E402
from tests.drift_checks_support import git, project, require_check  # noqa: E402, F401

# ruff: noqa: F811  (the imported ``project`` fixture is redefined as a test argument)


def _plain_scan(store_dir: Path, **kwargs):
    """``scan_code_drift`` without ``resolve_head``: the ``curate()`` path."""
    return doctor._scan_code_drift(
        _iter_records(store_dir, "decisions"),
        _binding_entries(store_dir),
        _iter_records(store_dir, "entities"),
        store_dir,
        None,
        **kwargs,
    )


def _entry(scan):
    entries = [e for b in scan.batches for e in b.entries]
    assert len(entries) == 1
    return entries[0]


def _commit(root: Path, name: str, content: str) -> None:
    (root / name).write_text(content, encoding="utf-8")
    git(root, "add", "--", name)
    git(root, "commit", "-q", "-m", f"change {name}")


def _anchor(store, decision, file_path: str):
    entity = store.upsert_entity(
        Entity(canonical_name=file_path, descriptor=Descriptor(name=file_path, file_path=file_path))
    )
    store.add_binding(AnchorBinding(record_id=decision.id, entity_id=entity.entity_id, tier=2))
    return entity


def test_scan_exposes_repo_root_statuses_and_compared_head(project):
    root, store, decision = project
    scan = _plain_scan(store.path)
    assert scan.repo_root is not None and scan.repo_root.resolve() == root.resolve()
    assert scan.binding_statuses is not None
    assert scan.compared_head is None


def test_basis_capture_without_check(project):
    _root, store, _decision = project
    entry = _entry(_plain_scan(store.path))
    assert (entry.basis, entry.check_reason, entry.check_blobs) == ("capture", None, None)


def test_basis_check_on_checked_files_changed(project):
    root, store, decision = project
    require_check(root, decision.id)
    checked = git(root, "rev-parse", "HEAD:code.py")
    _commit(root, "code.py", "def policy():\n    return 2\n")
    entry = _entry(_plain_scan(store.path))
    assert entry.check_reason == "checked files changed"
    assert entry.basis == "check"
    assert entry.check_blobs == {"code.py": (checked, "100644")}


def test_basis_scope_for_both_scope_reasons(project):
    root, store, decision = project
    # Reason 1: a new anchored file the check never listed; the listed file keeps its blob.
    require_check(root, decision.id)
    stored = git(root, "rev-parse", "HEAD:code.py")
    _commit(root, "dependency.py", "dependency = 1\n")
    _anchor(store, decision, "dependency.py")
    entry = _entry(_plain_scan(store.path))
    assert entry.check_reason == "checked subject or anchor scope changed"
    assert entry.basis == "scope"
    assert entry.check_blobs == {"code.py": (stored, "100644")}
    assert "dependency.py" in entry.files and "dependency.py" not in entry.check_blobs


def test_basis_scope_required_anchor_file_scope_changed(project):
    root, store, decision = project
    _commit(root, "dependency.py", "dependency = 1\n")
    _anchor(store, decision, "dependency.py")
    require_check(root, decision.id, files=["code.py", "dependency.py"])
    stored = git(root, "rev-parse", "HEAD:dependency.py")
    git(root, "rm", "-q", "dependency.py")
    git(root, "commit", "-q", "-m", "remove dependency")
    entry = _entry(_plain_scan(store.path))
    assert entry.check_reason == "required anchor file scope changed"
    assert entry.basis == "scope"
    assert entry.check_blobs is not None
    assert entry.check_blobs["dependency.py"] == (stored, "100644")


def _apply(scan, store, root, batches=None):
    """Run the overlay directly, optionally over hand-built capture batches."""
    batches = scan.batches if batches is None else batches
    by_commit = {
        batch.commit: [(Path(e.record_path), e.record_id, set(e.files)) for e in batch.entries]
        for batch in batches
    }
    return doctor._apply_drift_checks(
        batches,
        by_commit,
        _iter_records(store.path, "decisions"),
        _binding_entries(store.path),
        _iter_records(store.path, "entities"),
        scan.binding_statuses or {},
        store.path,
        root,
        None,
        lambda: None,
    )


def test_basis_capture_for_outside_scope_reason(project):
    """Test 8: a drifted anchored file the check does not list keeps the capture baseline."""
    root, store, decision = project
    _commit(root, "code.py", "def policy():\n    return 1\n")  # back to capture bytes
    require_check(root, decision.id)
    scan = _plain_scan(store.path)
    assert not [e for b in scan.batches for e in b.entries]
    # Hand-built capture batch: the record also drifted on extra.py, outside the check.
    commit = decision.provenance.commit
    batch = doctor.DriftBatch(
        commit,
        False,
        [
            doctor.DriftEntry(
                str(store.path / "decisions" / "x.json"), decision.id, ["code.py", "extra.py"]
            )
        ],
    )
    batches, _skipped, _unreadable, _head = _apply(scan, store, root, [batch])
    entry = batches[0].entries[0]
    assert entry.check_reason == "changed anchored file outside checked scope"
    assert (entry.basis, entry.check_blobs, entry.files) == ("capture", None, ["extra.py"])


def test_basis_capture_for_invalid_check_advisory_retained(project, monkeypatch):
    root, store, decision = project
    require_check(root, decision.id)
    _commit(root, "code.py", "def policy():\n    return 2\n")  # drifts the capture entry

    def invalid(*_a, **_k):
        raise ValueError("invalid")

    monkeypatch.setattr(doctor.drift_checks, "scope_leaves", invalid)
    entry = _entry(_plain_scan(store.path))
    assert entry.check_reason == "latest scoped check is invalid; capture advisory retained"
    assert (entry.basis, entry.check_blobs) == ("capture", None)


def test_basis_capture_for_scoped_comparison_unavailable(project, monkeypatch):
    root, store, decision = project
    require_check(root, decision.id)
    _commit(root, "code.py", "def policy():\n    return 2\n")

    def unavailable(*_a, **_k):
        raise ValueError("boom")

    monkeypatch.setattr(doctor.drift_checks, "tree_entries", unavailable)
    entry = _entry(_plain_scan(store.path))
    assert entry.check_reason == "scoped comparison unavailable; capture advisory retained"
    assert (entry.basis, entry.check_blobs) == ("capture", None)


def test_compared_head_is_current_when_overlay_resolved_head(project):
    root, store, decision = project
    require_check(root, decision.id)
    _commit(root, "code.py", "def policy():\n    return 2\n")
    scan = _plain_scan(store.path)
    assert scan.compared_head == git(root, "rev-parse", "HEAD")


def test_compared_head_falls_back_to_head_argument(project):
    root, store, decision = project
    require_check(root, decision.id)
    scan = _plain_scan(store.path)
    explicit = "0" * 40
    by_commit: dict = {}
    result = doctor._apply_drift_checks(
        [],
        by_commit,
        _iter_records(store.path, "decisions"),
        _binding_entries(store.path),
        _iter_records(store.path, "entities"),
        scan.binding_statuses or {},
        store.path,
        root,
        explicit,
        lambda: None,
    )
    assert result[3] == explicit
    assert (
        doctor._apply_drift_checks(
            [],
            by_commit,
            [],
            {},
            [],
            {},
            store.path,
            root,
            None,
            lambda: None,
        )[3]
        is None
    )
