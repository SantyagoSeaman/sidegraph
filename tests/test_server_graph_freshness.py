"""``get_task_context`` says what happened to each seed the graph does not hold (spec D5).

Red target: unfixed code appends nothing, so a seed missing from the graph gets the bare
render. The repositories come from ``tests.test_graph_freshness``.
# see design/superpowers/specs/2026-10-01-stale-graph-visible-design.md (D5, T11-T13, T24)
"""

from __future__ import annotations

import pytest

from sidegraph.engine.reader import GraphifyReader
from sidegraph.freshness import GraphFreshness
from sidegraph.retrieval import RetrievalBudget
from sidegraph.server import _get_task_context_impl, _retrieve, _seeds_from_args
from sidegraph.store import Store
from tests.test_graph_freshness import commit, make_repo, stale_repo, write_graph

HEADING = "## Not in the code graph"


def _call(tmp_path, reader, files=None, entities=None) -> str:
    store = Store(tmp_path / "t.db")
    try:
        return _get_task_context_impl(
            store,
            reader,
            files=files,
            entities=entities,
            structure_budget=4000,
            memory_budget=2000,
        )
    finally:
        store.close()


def _bare_render(tmp_path, reader, files=None, entities=None) -> str:
    """What the tool printed before the block existed: ``ctx.render()`` and nothing else."""
    store = Store(tmp_path / "bare.db")
    try:
        ctx = _retrieve(
            _seeds_from_args(files, entities), store, reader, RetrievalBudget(4000, 2000)
        )
        return ctx.render()
    finally:
        store.close()


def _block(out: str) -> list[str]:
    """The lines of the block, heading excluded."""
    assert HEADING in out
    return out.split(HEADING + "\n", 1)[1].splitlines()


def test_t11_a_seed_the_stale_graph_lacks_gets_the_stale_sentence(tmp_path):
    fx = stale_repo(tmp_path)
    (fx.repo / "pkg" / "n.py").write_text("def b():\n    pass\n")
    reader = GraphifyReader(fx.graph)

    out = _call(tmp_path, reader, files=["pkg/n.py", "pkg/m.py"])

    lines = _block(out)
    assert len(lines) == 1
    assert "pkg/n.py" in lines[0] and "pkg/m.py" not in lines[0]
    assert lines[0].startswith("1 of 2 seed paths exists but is not in the code graph: pkg/n.py.")
    assert "The graph is stale (built at" in lines[0]
    assert "1 commit behind HEAD" in lines[0]
    assert "`graphify update .`" in lines[0]
    assert out.startswith(_bare_render(tmp_path, reader, files=["pkg/n.py", "pkg/m.py"]))
    assert out.index(HEADING) > out.index("pkg/m.py")


def test_t12_seeds_the_graph_holds_leave_the_output_and_the_cost_unchanged(tmp_path, monkeypatch):
    """Red against nothing: guards the common path's bytes and its cost (mutation M3)."""
    fx = stale_repo(tmp_path)
    reader = GraphifyReader(fx.graph)
    calls: list[str] = []
    real_freshness, real_root = reader.freshness, reader.repo_root

    def counting_freshness(*a, **k):
        calls.append("freshness")
        return real_freshness(*a, **k)

    def counting_root(*a, **k):
        calls.append("repo_root")
        return real_root(*a, **k)

    monkeypatch.setattr(reader, "freshness", counting_freshness)
    monkeypatch.setattr(reader, "repo_root", counting_root)

    out = _call(tmp_path, reader, files=["pkg/m.py"])

    assert out == _bare_render(tmp_path, reader, files=["pkg/m.py"])
    assert HEADING not in out
    assert calls == []


def test_a_call_with_no_seeds_is_unchanged(tmp_path):
    fx = stale_repo(tmp_path)
    reader = GraphifyReader(fx.graph)

    assert _call(tmp_path, reader) == _bare_render(tmp_path, reader)


def test_t13_an_existing_file_missing_from_a_fresh_graph_says_the_graph_is_current(tmp_path):
    fx = make_repo(tmp_path)
    (fx.repo / "extra.py").write_text("x = 1\n")
    reader = GraphifyReader(fx.graph)

    out = _call(tmp_path, reader, files=["extra.py"])

    (line,) = _block(out)
    assert "No committed change since the build explains it" in line
    assert "newer than the build and not committed yet (rebuild with `graphify update .`)" in line
    assert "sit under an excluded path, or be a file type Graphify skips." in line
    assert "The graph is current" not in line and "does not index" not in line
    assert "stale" not in line
    assert "extra.py" in line


def test_t24_each_seed_gets_the_advice_for_what_happened_to_it(tmp_path):
    fx = stale_repo(tmp_path)
    (fx.repo / "pkg" / "n.py").write_text("def b():\n    pass\n")
    reader = GraphifyReader(fx.graph)

    out = _call(tmp_path, reader, files=["pkg/", "Nope.py", "pkg/n.py"])

    existing, other = _block(out)
    assert existing.startswith("1 of 3 seed paths exists but is not in the code graph: pkg/n.py.")
    assert "Nope.py" not in existing and "pkg/," not in existing
    assert other.startswith(
        "2 of 3 seed paths are not repo-relative paths to files in this repository: pkg/, Nope.py."
    )
    assert "pkg/n.py" not in other
    assert "repo-relative" in other and "not a directory" in other
    assert "stale" not in other


def test_t26_a_seed_that_is_not_a_normalized_repo_relative_path_is_a_path_problem(
    tmp_path, monkeypatch
):
    """`root / p` accepts an absolute path and `./x`, so they used to read as "exists but not
    in the graph" and send the caller to rebuild a graph that was never the cause."""
    fx = stale_repo(tmp_path)
    (fx.repo / "pkg" / "n.py").write_text("def b():\n    pass\n")
    reader = GraphifyReader(fx.graph)
    calls: list[str] = []
    monkeypatch.setattr(reader, "freshness", lambda *a, **k: calls.append("freshness"))
    absolute = str(fx.repo / "pkg" / "n.py")
    seeds = [absolute, "./pkg/n.py", "pkg/../pkg/n.py", "pkg//n.py"]

    out = _call(tmp_path, reader, files=seeds)

    (line,) = _block(out)
    assert line.startswith(
        "4 of 4 seed paths are not repo-relative paths to files in this repository: "
        + ", ".join(seeds)
        + "."
    )
    assert "Check the path" in line and "stale" not in line
    assert calls == []


@pytest.mark.parametrize("seed", ["../x.py", "a/../../x.py"])
def test_t26c_a_path_that_climbs_out_of_the_repository_is_a_path_problem(
    tmp_path, monkeypatch, seed
):
    """`posixpath.normpath` leaves a leading `..` alone, so `../x.py` passed the normalized
    check and the block stat-ed a real file outside the repository, then sent the caller to
    rebuild a graph. `a/../../x.py` normalizes to `../x.py`, so the normalized check already
    rejected it: it stays here as the guard that the `..` rule does not loosen that."""
    fx = stale_repo(tmp_path)
    (fx.repo / "a").mkdir()
    (tmp_path / "x.py").write_text("x = 1\n")  # one level above the repository root
    assert not (fx.repo / "x.py").exists()
    reader = GraphifyReader(fx.graph)
    calls: list[str] = []
    real = reader.freshness

    def counting(*a, **k):
        calls.append("freshness")
        return real(*a, **k)

    monkeypatch.setattr(reader, "freshness", counting)

    out = _call(tmp_path, reader, files=[seed])

    (line,) = _block(out)
    assert line.startswith(
        f"1 of 1 seed path is not a repo-relative path to a file in this repository: {seed}."
    )
    assert "Check the path" in line
    assert "exists but is not in the code graph" not in line and "stale" not in line
    assert calls == []


def test_t26d_a_missing_or_dotted_repo_relative_path_keeps_its_wording(tmp_path):
    """Guard for the `..` rule: an ordinary missing path still gets "check the path", and a
    file whose name merely contains two dots is a part, not a climb, so it still gets the
    graph-state advice."""
    fx = stale_repo(tmp_path)
    (fx.repo / "pkg" / "v1..2.py").write_text("x = 1\n")
    reader = GraphifyReader(fx.graph)

    out = _call(tmp_path, reader, files=["pkg/v1..2.py", "pkg/missing.py"])

    existing, other = _block(out)
    assert existing.startswith(
        "1 of 2 seed paths exists but is not in the code graph: pkg/v1..2.py."
    )
    assert other.startswith(
        "1 of 2 seed paths is not a repo-relative path to a file in this repository: "
        "pkg/missing.py."
    )
    assert "Check the path" in other


def test_t26b_the_same_file_written_repo_relative_gets_the_graph_state_advice(tmp_path):
    fx = stale_repo(tmp_path)
    (fx.repo / "pkg" / "n.py").write_text("def b():\n    pass\n")
    reader = GraphifyReader(fx.graph)

    out = _call(tmp_path, reader, files=[str(fx.repo / "pkg" / "n.py"), "pkg/n.py"])

    existing, other = _block(out)
    assert existing.startswith("1 of 2 seed paths exists but is not in the code graph: pkg/n.py.")
    assert other.startswith("1 of 2 seed paths is not a repo-relative path to a file")
    assert str(fx.repo) in other


def test_t31_the_repository_root_is_looked_up_once_per_call(tmp_path, monkeypatch):
    fx = stale_repo(tmp_path)
    (fx.repo / "pkg" / "n.py").write_text("def b():\n    pass\n")
    reader = GraphifyReader(fx.graph)
    calls: list[str] = []
    real = reader.repo_root

    def counting(*a, **k):
        calls.append("repo_root")
        return real(*a, **k)

    monkeypatch.setattr(reader, "repo_root", counting)

    out = _call(tmp_path, reader, files=["pkg/n.py"])

    assert "The graph is stale" in out
    assert calls == ["repo_root"]


def test_t24_a_directory_or_missing_path_alone_does_not_ask_for_the_graph_state(
    tmp_path, monkeypatch
):
    fx = stale_repo(tmp_path)
    reader = GraphifyReader(fx.graph)
    calls: list[str] = []
    monkeypatch.setattr(reader, "freshness", lambda *a, **k: calls.append("freshness"))

    out = _call(tmp_path, reader, files=["pkg/", "Nope.py"])

    (line,) = _block(out)
    assert line.startswith(
        "2 of 2 seed paths are not repo-relative paths to files in this repository: pkg/, Nope.py."
    )
    assert calls == []


def test_t24_at_most_ten_paths_are_listed_per_group(tmp_path):
    fx = stale_repo(tmp_path)
    names = [f"extra/f{i:02d}.py" for i in range(1, 13)]
    for name in names:
        p = fx.repo / name
        p.parent.mkdir(exist_ok=True)
        p.write_text("x = 1\n")
    reader = GraphifyReader(fx.graph)

    out = _call(tmp_path, reader, files=names)

    (line,) = _block(out)
    assert "12 of 12 seed paths exist but are not in the code graph:" in line
    assert "extra/f10.py, and 2 more." in line
    assert "extra/f11.py" not in line and "extra/f12.py" not in line


def test_t24_an_entity_seed_with_a_file_path_alone_produces_the_block(tmp_path):
    fx = stale_repo(tmp_path)
    (fx.repo / "pkg" / "n.py").write_text("def b():\n    pass\n")
    reader = GraphifyReader(fx.graph)

    out = _call(tmp_path, reader, entities=[{"name": "x", "file_path": "pkg/n.py"}])

    (line,) = _block(out)
    assert line.startswith("1 of 1 seed path exists but is not in the code graph: pkg/n.py.")
    assert "The graph is stale" in line


def test_t24_seed_paths_are_deduplicated_across_files_and_entities(tmp_path):
    fx = stale_repo(tmp_path)
    (fx.repo / "pkg" / "n.py").write_text("def b():\n    pass\n")
    reader = GraphifyReader(fx.graph)

    out = _call(
        tmp_path,
        reader,
        files=["pkg/n.py"],
        entities=[{"name": "x", "file_path": "pkg/n.py"}, {"name": "y", "file_path": None}],
    )

    (line,) = _block(out)
    assert line.startswith("1 of 1 seed path exists but is not in the code graph: pkg/n.py.")


def test_t24_an_unknown_comparison_is_said_as_such(tmp_path, monkeypatch):
    fx = stale_repo(tmp_path)
    (fx.repo / "pkg" / "n.py").write_text("def b():\n    pass\n")
    reader = GraphifyReader(fx.graph)
    monkeypatch.setattr(
        reader,
        "freshness",
        lambda *a, **k: GraphFreshness(state="unknown", reason="git timed out"),
    )

    out = _call(tmp_path, reader, files=["pkg/n.py"])

    (line,) = _block(out)
    assert (
        "Could not tell whether the graph is current (git timed out): "
        "rebuild it with `graphify update .` if these files are new."
    ) in line


def test_a_graph_outside_git_reports_every_missing_path_as_a_path_problem(tmp_path):
    graph = tmp_path / "plain" / "graph.json"
    write_graph(graph, "a" * 40, ["pkg/m.py"])
    reader = GraphifyReader(graph)

    out = _call(tmp_path, reader, files=["pkg/n.py"])

    (line,) = _block(out)
    assert line.startswith(
        "1 of 1 seed path is not a repo-relative path to a file in this repository: pkg/n.py."
    )


def test_a_failure_in_the_block_leaves_the_rendered_text_untouched(tmp_path, monkeypatch):
    fx = stale_repo(tmp_path)
    reader = GraphifyReader(fx.graph)

    def boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(reader, "repo_root", boom)

    out = _call(tmp_path, reader, files=["pkg/n.py"])

    assert out == _bare_render(tmp_path, reader, files=["pkg/n.py"])


def test_a_missing_reader_adds_nothing(tmp_path):
    out = _call(tmp_path, None, files=["pkg/n.py"])

    assert HEADING not in out


def test_the_block_is_a_blank_line_apart_and_closes_the_output(tmp_path):
    """A hit and a miss in one call: the render comes first, the block last."""
    fx = make_repo(tmp_path)
    commit(fx.repo, "B", {"pkg/n.py": "def b():\n    pass\n"})
    reader = GraphifyReader(fx.graph)

    out = _call(tmp_path, reader, files=["pkg/m.py", "pkg/n.py"])

    assert "\n\n" + HEADING + "\n" in out
    assert out.endswith("\n".join(_block(out)))
