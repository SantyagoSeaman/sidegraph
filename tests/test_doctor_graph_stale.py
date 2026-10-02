"""``sidegraph-doctor`` reports a stale code graph as ``graph-stale`` (spec D6).

Red target: unfixed code has no ``GRAPH_STALE`` code (ImportError), so a graph that never
caught up with HEAD is silent in the one health check people run. The repositories come from
``tests.test_graph_freshness``.
# see design/superpowers/specs/2026-10-01-stale-graph-visible-design.md (D6, T14-T15)
"""

from __future__ import annotations

from sidegraph.doctor import GRAPH_STALE, curate
from sidegraph.engine.reader import GraphifyReader
from tests.test_graph_freshness import make_repo, stale_repo, write_graph


def _store_dir(tmp_path):
    store_dir = tmp_path / "store"
    store_dir.mkdir()
    return store_dir


def test_t14_a_stale_graph_is_one_finding_with_the_rebuild_command(tmp_path):
    fx = stale_repo(tmp_path)
    reader = GraphifyReader(fx.graph)

    report = curate(_store_dir(tmp_path), reader=reader)

    findings = [f for f in report.findings if f.code == GRAPH_STALE]
    assert len(findings) == 1
    (finding,) = findings
    assert finding.path == str(fx.graph)
    assert "the code graph is stale" in finding.detail
    assert f"built at {fx.first[:7]}, 1 commit behind HEAD, 1 file changed since" in finding.detail
    assert "(e.g. pkg/m.py)" in finding.detail
    assert "graphify update ." in finding.detail
    assert "sidegraph-sync" in finding.detail


def test_the_example_list_ends_in_an_ellipsis_when_more_files_changed(tmp_path):
    from tests.test_graph_freshness import commit

    fx = make_repo(tmp_path)
    commit(fx.repo, "B", {f"new/f{i}.py": "x = 1\n" for i in range(7)})

    report = curate(_store_dir(tmp_path), reader=GraphifyReader(fx.graph))

    (finding,) = [f for f in report.findings if f.code == GRAPH_STALE]
    assert "7 files changed since" in finding.detail
    assert "(e.g. new/f0.py, new/f1.py, new/f2.py, new/f3.py, new/f4.py, …)" in finding.detail


def test_t15_a_fresh_graph_is_not_reported(tmp_path):
    """Red against nothing: a guard against a finding on every run."""
    fx = make_repo(tmp_path)

    report = curate(_store_dir(tmp_path), reader=GraphifyReader(fx.graph))

    assert GRAPH_STALE not in [f.code for f in report.findings]


def test_an_unknown_comparison_is_not_reported(tmp_path):
    fx = stale_repo(tmp_path)
    write_graph(fx.graph, "abc123", ["pkg/m.py"])

    report = curate(_store_dir(tmp_path), reader=GraphifyReader(fx.graph))

    assert GRAPH_STALE not in [f.code for f in report.findings]


def test_no_reader_reports_nothing(tmp_path):
    report = curate(_store_dir(tmp_path), reader=None)

    assert GRAPH_STALE not in [f.code for f in report.findings]


def test_a_stale_graph_escalates_under_check(tmp_path, monkeypatch, capsys):
    """`sidegraph-doctor --check` exits 2 on any advisory finding, a stale graph included."""
    from sidegraph.cli import doctor_main
    from sidegraph.store import Store

    fx = stale_repo(tmp_path)
    store_dir = tmp_path / "store"
    Store(store_dir).close()
    monkeypatch.chdir(fx.repo)

    code = doctor_main(["--db", str(store_dir), "--graph", str(fx.graph), "--check"])

    out = capsys.readouterr().out
    assert GRAPH_STALE in out
    assert code == 2
