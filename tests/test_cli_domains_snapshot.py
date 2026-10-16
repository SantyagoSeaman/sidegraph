"""`sidegraph-domains snapshot` (D5.4).

see design/superpowers/specs/2026-10-14-graphify-upgrade-design.md (D5)"""

from __future__ import annotations

import contextlib
import sqlite3
import subprocess
from pathlib import Path

import pytest

import sidegraph
from sidegraph import domain_snapshot
from sidegraph import sync as sync_mod
from sidegraph.cli import domains_main
from sidegraph.engine.reader import GraphifyReader
from sidegraph.schema import DomainStatus
from sidegraph.store import SNAPSHOT_SIGNATURE_KEY, Store
from sidegraph.store_layout import snapshot_signature
from sidegraph.sync import sync
from tests.graph_fixtures import write_graph
from tests.snapshot_support import legacy_graph, mention_graph, standard_domains


@pytest.fixture
def openable(monkeypatch):
    """A development build of an earlier release refuses to raise a store to 0.14.0 (and
    would refuse to reopen it): run as the first release that reads snapshots."""
    monkeypatch.setattr(sidegraph, "__version__", "0.14.0")


def _project(tmp_path, graph: dict, name: str):
    root = tmp_path / "proj" / ".sidegraph"
    path = write_graph(tmp_path, f"{name}.json", graph)
    with Store(root) as store:
        standard_domains(store)
        sync(store, GraphifyReader(path))
    return root, path


def _run(*argv: str) -> int:
    return domains_main(["snapshot", *argv])


def test_a_first_snapshot_of_a_mention_edge_graph_is_refused(tmp_path, capsys, openable):
    """Red against unfixed code: argparse has no `snapshot` (SystemExit 2)."""
    root, graph = _project(tmp_path, mention_graph(), "mention")
    marker = (root / "format").read_text(encoding="utf-8")
    assert _run("--db", str(root), "--graph", str(graph)) == 1
    out = capsys.readouterr().out
    assert "--build-with 0.9.6" in out and "--accept-current-engine" in out
    assert not domain_snapshot.snapshot_path(root).exists()
    assert (root / "format").read_text(encoding="utf-8") == marker


def test_accept_current_engine_takes_it_anyway(tmp_path, openable):
    root, graph = _project(tmp_path, mention_graph(), "mention")
    assert _run("--db", str(root), "--graph", str(graph), "--accept-current-engine") == 0
    assert domain_snapshot.load_snapshot(root).engine_generation == "mention-edges"


def test_a_first_snapshot_prints_counts_and_raises_the_requirement(tmp_path, capsys, openable):
    root, graph = _project(tmp_path, legacy_graph(), "legacy")
    assert _run("--db", str(root), "--graph", str(graph)) == 0
    out = capsys.readouterr().out
    assert "froze 3 domain(s) over 5 file(s)" in out
    assert (
        "  payments: 2 file(s)" in out and "  core: 1 file(s)" in out and "  docs: 1 file(s)" in out
    )
    assert "requires-sidegraph >= 0.14.0" in (root / "format").read_text(encoding="utf-8")


def test_a_refused_requirement_writes_no_snapshot(tmp_path, capsys, monkeypatch):
    """Red against a command that writes the snapshot first, or lets the ValueError escape:
    0.13.0 may not require 0.14.0, a version not installed where it is raised."""
    monkeypatch.setattr(sidegraph, "__version__", "0.13.0")
    root, graph = _project(tmp_path, legacy_graph(), "legacy")
    assert _run("--db", str(root), "--graph", str(graph)) == 1
    out = capsys.readouterr().out
    assert out.count("\n") == 1 and out.startswith("error: cannot require Sidegraph >= 0.14.0")
    assert not domain_snapshot.snapshot_path(root).exists()


def test_a_second_run_needs_refresh(tmp_path, capsys, openable):
    root, graph = _project(tmp_path, legacy_graph(), "legacy")
    assert _run("--db", str(root), "--graph", str(graph)) == 0
    capsys.readouterr()
    assert _run("--db", str(root), "--graph", str(graph)) == 1
    assert "pass --refresh" in capsys.readouterr().out
    assert _run("--db", str(root), "--graph", str(graph), "--refresh") == 0


def test_refresh_domain_rederives_one_domain_by_its_full_rules(tmp_path, capsys, openable):
    """Red against a refresh that keeps every domain's effective membership: payments would
    stay at 2 files."""
    root, legacy = _project(tmp_path, legacy_graph(), "legacy")
    assert _run("--db", str(root), "--graph", str(legacy)) == 0
    mention = write_graph(tmp_path, "mention.json", mention_graph())
    with Store(root) as store:
        sync(store, GraphifyReader(mention))
    capsys.readouterr()
    assert (
        _run("--db", str(root), "--graph", str(mention), "--refresh", "--domain", "payments") == 0
    )
    out = capsys.readouterr().out
    assert "  payments: 4 file(s)" in out and "  core: 2 file(s)" in out


def test_flags_are_checked_before_anything_is_written(tmp_path, capsys, openable):
    root, graph = _project(tmp_path, legacy_graph(), "legacy")
    assert _run("--db", str(root), "--graph", str(graph), "--domain", "payments") == 1
    assert "--domain needs --refresh" in capsys.readouterr().out
    assert _run("--db", str(root), "--graph", str(graph)) == 0
    capsys.readouterr()
    assert _run("--db", str(root), "--graph", str(graph), "--refresh", "--domain", "nope") == 1
    assert "no accepted domain: nope" in capsys.readouterr().out
    with pytest.raises(SystemExit) as e:
        _run("--graph", str(graph), "--build-with", "0.9.6")
    assert e.value.code == 2


@pytest.mark.parametrize("nested", [False, True])
def test_build_with_takes_the_snapshot_from_head_rebuilt_by_the_old_engine(
    tmp_path, capsys, monkeypatch, openable, nested
):
    root, _mention = _project(tmp_path, mention_graph(), "mention")
    proj = root.parent
    repo = proj.parent if nested else proj
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.com",
            "commit",
            "-q",
            "--allow-empty",
            "--no-verify",
            "-m",
            "c",
        ],
        cwd=repo,
        check=True,
    )
    legacy = write_graph(tmp_path, "legacy.json", legacy_graph())
    calls = []

    def fake_build(repo, version, workdir):
        calls.append((Path(repo).resolve(), version))
        return legacy

    monkeypatch.setattr("sidegraph.cli.build_graph_at_head", fake_build)
    assert _run("--db", str(root), "--build-with", "0.9.6") == 0
    assert calls == [(proj.resolve(), "0.9.6")]
    assert "not committed at HEAD" in capsys.readouterr().out
    assert domain_snapshot.load_snapshot(root).engine_generation == "legacy"


def test_after_a_local_refresh_the_next_sync_runs(tmp_path, openable):
    """The command invalidates membership itself (guard; a later open would as well)."""
    root, graph = _project(tmp_path, legacy_graph(), "legacy")
    assert _run("--db", str(root), "--graph", str(graph)) == 0
    with Store(root) as store:
        sync(store, GraphifyReader(graph))
    assert _run("--db", str(root), "--graph", str(graph), "--refresh") == 0
    with Store(root) as store:
        assert sync(store, GraphifyReader(graph)).skipped is False


def test_the_command_records_the_snapshot_it_wrote(tmp_path, openable):
    """Red against a command that leaves the invalidation to the next open: the index would
    still record the store's earlier signature. Read with a raw connection, because opening a
    Store runs the same check itself."""
    root, graph = _project(tmp_path, legacy_graph(), "legacy")
    assert _run("--db", str(root), "--graph", str(graph)) == 0
    with contextlib.closing(sqlite3.connect(str(root / "index.db"))) as conn:
        row = conn.execute(
            "SELECT value FROM meta WHERE key = ?", (SNAPSHOT_SIGNATURE_KEY,)
        ).fetchone()
    assert row is not None and row[0] == snapshot_signature(root)
    assert row[0].startswith("sha256:")


def test_the_snapshot_raises_the_requirement_to_the_first_release_that_reads_it(
    tmp_path, monkeypatch
):
    """Red against a requirement taken from __version__."""
    monkeypatch.setattr(sidegraph, "__version__", "0.15.2")
    assert domain_snapshot.SNAPSHOT_MIN_SIDEGRAPH == "0.14.0"
    root, graph = _project(tmp_path, legacy_graph(), "legacy")
    assert _run("--db", str(root), "--graph", str(graph)) == 0
    assert "requires-sidegraph >= 0.14.0" in (root / "format").read_text(encoding="utf-8")


def test_dry_run_prints_the_counts_and_writes_nothing(tmp_path, capsys):
    """Red against unfixed code: argparse has no --dry-run (SystemExit 2). Runs as the
    development version on purpose: a dry run raises nothing, so nothing refuses it."""
    root, graph = _project(tmp_path, legacy_graph(), "legacy")
    marker = (root / "format").read_text(encoding="utf-8")
    assert _run("--db", str(root), "--graph", str(graph), "--dry-run") == 0
    out = capsys.readouterr().out
    assert "would freeze 3 domain(s) over 5 file(s)" in out
    assert (
        "  payments: 2 file(s)" in out and "  core: 1 file(s)" in out and "  docs: 1 file(s)" in out
    )
    assert not domain_snapshot.snapshot_path(root).exists()
    assert (root / "format").read_text(encoding="utf-8") == marker


def test_dry_run_never_creates_or_backfills_the_store(tmp_path, capsys):
    """Red against a dry run that opens a normal Store first: it would create the missing store
    (format marker, .gitignore, canonical directories) and backfill the marker-less one."""
    graph = write_graph(tmp_path, "legacy.json", legacy_graph())
    missing = tmp_path / "none" / ".sidegraph"
    assert _run("--db", str(missing), "--graph", str(graph), "--dry-run") == 1
    assert "there is no store at" in capsys.readouterr().out
    assert not missing.exists()
    root, _legacy = _project(tmp_path, legacy_graph(), "legacy")
    (root / "format").unlink()
    assert _run("--db", str(root), "--graph", str(graph), "--dry-run") == 1
    assert "format" in capsys.readouterr().out
    assert not (root / "format").exists()


def test_dry_run_never_stamps_an_empty_store(tmp_path, capsys):
    """Red against a guard that checks only the layout: an existing store with no record and no
    creation marker gets stamping_live_since written by the open."""
    graph = write_graph(tmp_path, "legacy.json", legacy_graph())
    root = tmp_path / "empty" / ".sidegraph"
    Store(root).close()
    (root / "stamping_live_since").unlink()
    assert _run("--db", str(root), "--graph", str(graph), "--dry-run") == 1
    assert "stamping_live_since" in capsys.readouterr().out
    assert not (root / "stamping_live_since").exists()


def test_dry_run_skips_the_engine_guard(tmp_path, capsys):
    root, graph = _project(tmp_path, mention_graph(), "mention")
    assert _run("--db", str(root), "--graph", str(graph), "--dry-run") == 0
    assert "would freeze 3 domain(s)" in capsys.readouterr().out
    assert not domain_snapshot.snapshot_path(root).exists()


def test_dry_run_over_a_snapshot_prints_what_a_refresh_would_freeze(tmp_path, capsys, openable):
    """Red against a dry run that refuses a second run, or that counts by the rules: core gains
    new.py by vote, and payments keeps its two files although its rule claims merged community
    1."""
    root, legacy = _project(tmp_path, legacy_graph(), "legacy")
    assert _run("--db", str(root), "--graph", str(legacy)) == 0
    mention = write_graph(tmp_path, "mention.json", mention_graph())
    with Store(root) as store:
        sync(store, GraphifyReader(mention))
    before = domain_snapshot.snapshot_path(root).read_bytes()
    capsys.readouterr()
    assert _run("--db", str(root), "--graph", str(mention), "--dry-run") == 0
    out = capsys.readouterr().out
    assert "  payments: 2 file(s)" in out and "  core: 2 file(s)" in out
    assert domain_snapshot.snapshot_path(root).read_bytes() == before


def test_a_pull_while_the_command_computes_is_never_overwritten(
    tmp_path, capsys, monkeypatch, openable
):
    """Red against a command that checks the file only before it computes: git takes no lock,
    and the pulled snapshot would be overwritten by one refreshed from the file it replaced."""
    root, legacy = _project(tmp_path, legacy_graph(), "legacy")
    assert _run("--db", str(root), "--graph", str(legacy)) == 0
    pulled = domain_snapshot.load_snapshot(root).model_copy(update={"store_commit": "f" * 40})
    real = sync_mod._MembershipView.membership
    pulls: list[int] = []

    def membership(self, domain):
        if not pulls:  # the pull lands while the membership is computed
            pulls.append(domain_snapshot.write_snapshot(root, pulled))
        return real(self, domain)

    monkeypatch.setattr(sync_mod._MembershipView, "membership", membership)
    capsys.readouterr()
    assert _run("--db", str(root), "--graph", str(legacy), "--refresh") == 1
    assert "changed while this command ran" in capsys.readouterr().out
    assert domain_snapshot.load_snapshot(root).store_commit == "f" * 40


def test_a_dry_run_refuses_a_snapshot_pulled_after_its_checks(
    tmp_path, capsys, monkeypatch, openable
):
    """Red against a take_snapshot that reads whatever is on disk: the dry run chose a first run
    (no snapshot), a pull then brought one, and it would print the rules' counts as what a first
    snapshot would freeze. A dry run writes nothing and never hashes the file again;
    take_snapshot's own check (based_on) is what refuses it."""
    root, legacy = _project(tmp_path, legacy_graph(), "legacy")
    assert _run("--db", str(root), "--graph", str(legacy)) == 0
    path = domain_snapshot.snapshot_path(root)
    pulled = path.read_bytes()
    path.unlink()

    def reader_after_a_pull(graph):
        path.write_bytes(pulled)
        return GraphifyReader(graph)

    monkeypatch.setattr("sidegraph.cli.GraphifyReader", reader_after_a_pull)
    capsys.readouterr()
    assert _run("--db", str(root), "--graph", str(legacy), "--dry-run") == 1
    assert "changed while this command ran" in capsys.readouterr().out


def test_a_domain_dropped_while_the_graph_is_read_is_refused(
    tmp_path, capsys, monkeypatch, openable
):
    """Red against a --domain check made only before the graph is built or read: the drop lands
    meanwhile, and the refresh would freeze a snapshot without the re-derivation it was asked
    for, and say nothing."""
    root, legacy = _project(tmp_path, legacy_graph(), "legacy")
    assert _run("--db", str(root), "--graph", str(legacy)) == 0
    before = domain_snapshot.snapshot_path(root).read_bytes()

    def reader_after_a_drop(path):
        with Store(root) as other:
            payments = next(
                d for d in other.iter_domains(status=DomainStatus.ACCEPTED) if d.slug == "payments"
            )
            other.ratify_domains(drop=[payments.domain_id])
        return GraphifyReader(path)

    monkeypatch.setattr("sidegraph.cli.GraphifyReader", reader_after_a_drop)
    capsys.readouterr()
    assert _run("--db", str(root), "--graph", str(legacy), "--refresh", "--domain", "payments") == 1
    assert "no accepted domain: payments" in capsys.readouterr().out
    assert domain_snapshot.snapshot_path(root).read_bytes() == before


@pytest.mark.parametrize("part_flags", [[], ["--domain", "billing-a"]])
def test_refresh_requires_every_part_of_a_split(tmp_path, capsys, openable, part_flags):
    """A plain/partial refresh must not silently discard a split's frozen entries."""
    from tests.snapshot_support import PAYMENTS_IDS, accept

    root, graph = _project(tmp_path, legacy_graph(), "legacy")
    assert _run("--db", str(root), "--graph", str(graph)) == 0
    with Store(root) as store:
        old = store.find_domain_by_slug("payments")
        accept(store, "billing-a", supersedes=old, seeds=[("Invoice", "src/pay/model.py")])
        accept(store, "billing-b", supersedes=old, seeds=[("Invoice", "src/pay/model.py")])
    path = domain_snapshot.snapshot_path(root)
    before = path.read_bytes(), (root / "format").read_bytes()
    capsys.readouterr()

    assert _run("--db", str(root), "--graph", str(graph), "--refresh", *part_flags) == 1
    assert "--domain billing-a --domain billing-b" in capsys.readouterr().out
    assert (path.read_bytes(), (root / "format").read_bytes()) == before
    with Store(root) as store:
        report = sync(store, GraphifyReader(graph), force=True)
        assert report.snapshot_splits == [
            {"slug": "payments", "domains": ["billing-a", "billing-b"]}
        ]

    assert (
        _run(
            "--db",
            str(root),
            "--graph",
            str(graph),
            "--refresh",
            "--domain",
            "billing-a",
            "--domain",
            "billing-b",
        )
        == 0
    )
    with Store(root) as store:
        report = sync(store, GraphifyReader(graph), force=True)
        assert report.snapshot_splits == []
        for slug in ("billing-a", "billing-b"):
            domain = store.find_domain_by_slug(slug)
            assert store.domain_member_ids(domain.domain_id) == PAYMENTS_IDS
