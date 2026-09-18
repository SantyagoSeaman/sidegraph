"""Defect 2: `graphify update` run from a subdirectory instead of the repo root leaves
every recorded `source_file` relative to THAT subdirectory. Every descriptor this reader
resolves then stops matching real repo-relative paths, and a person sees mass, unexplained
`orphaned` findings with nothing pointing at the real cause.

`GraphifyReader.detect_subdir_mismatch` is the engine-seam diagnostic: it samples
anchorable `source_file` values, checks their existence relative to a repo root, and only
reports a mismatch when it can also point at the single subdirectory that explains ALL of
the missing sample -- see the method's own docstring for why that second condition is
what keeps a legitimately partial graph (doc-only corpus, an `--exclude`d build) from ever
tripping this.
"""

import json

from sidegraph.engine.reader import GraphifyReader

ANCHORABLE_PATHS = [f"pkg/mod_{i}.py" for i in range(10)]


def _node(i: int, path: str) -> dict:
    return {
        "id": f"n{i}",
        "label": f"fn_{i}()",
        "norm_label": f"fn_{i}()",
        "file_type": "code",
        "source_file": path,
        "community": i % 3,
    }


def _write_graph(tmp_path, name, paths):
    data = {
        "built_at_commit": "v1",
        "nodes": [_node(i, p) for i, p in enumerate(paths)],
        "links": [],
    }
    p = tmp_path / name
    p.write_text(json.dumps(data))
    return p


def test_flags_when_every_missing_path_resolves_under_one_subdir(tmp_path):
    """The live bug: graphify was run from `src/`, so every source_file in the graph is
    relative to `src/` instead of the repo root. None of the sampled paths exist at the
    repo root, but ALL of them exist under `src/` -- the positive signal."""
    repo_root = tmp_path
    for p in ANCHORABLE_PATHS:
        real = repo_root / "src" / p
        real.parent.mkdir(parents=True, exist_ok=True)
        real.write_text("pass\n")
    reader = GraphifyReader(_write_graph(tmp_path, "graph.json", ANCHORABLE_PATHS))

    mismatch = reader.detect_subdir_mismatch(repo_root=repo_root)

    assert mismatch is not None
    assert mismatch["likely_run_from"] == "src"
    assert mismatch["sampled"] == len(ANCHORABLE_PATHS)
    assert mismatch["missing"] == len(ANCHORABLE_PATHS)


def test_no_mismatch_when_paths_resolve_at_repo_root(tmp_path):
    """The ordinary, healthy case: graphify ran from the repo root, so every source_file
    already resolves there. Must never fire -- this is the overwhelming common case and a
    false positive here would be worse noise than the bug itself."""
    repo_root = tmp_path
    for p in ANCHORABLE_PATHS:
        real = repo_root / p
        real.parent.mkdir(parents=True, exist_ok=True)
        real.write_text("pass\n")
    reader = GraphifyReader(_write_graph(tmp_path, "graph.json", ANCHORABLE_PATHS))

    assert reader.detect_subdir_mismatch(repo_root=repo_root) is None


def test_no_mismatch_for_legitimately_partial_graph(tmp_path):
    """A doc-only/`--exclude`d build: only SOME of the anchorable nodes are indexed, but
    the ones that are still sit at their real repo-relative paths. Missing ratio never
    crosses the threshold, so this must not be confused with a subdir-run graph."""
    repo_root = tmp_path
    included = ANCHORABLE_PATHS[:9]  # 9/10 resolve fine; 1 legitimately doesn't exist yet
    for p in included:
        real = repo_root / p
        real.parent.mkdir(parents=True, exist_ok=True)
        real.write_text("pass\n")
    reader = GraphifyReader(_write_graph(tmp_path, "graph.json", ANCHORABLE_PATHS))

    assert reader.detect_subdir_mismatch(repo_root=repo_root) is None


def test_no_mismatch_when_sample_too_small(tmp_path):
    """A tiny/fixture-sized graph abstains rather than risk a false positive: 3 unique
    anchorable paths is below the minimum sample size, even though all 3 are "missing" at
    the repo root and would resolve under a subdirectory."""
    repo_root = tmp_path
    small = ["a.py", "b.py", "c.py"]
    for p in small:
        real = repo_root / "sub" / p
        real.parent.mkdir(parents=True, exist_ok=True)
        real.write_text("pass\n")
    reader = GraphifyReader(_write_graph(tmp_path, "graph.json", small))

    assert reader.detect_subdir_mismatch(repo_root=repo_root) is None


def test_no_mismatch_when_no_subdir_explains_all_missing_paths(tmp_path):
    """A genuinely stale graph (files deleted/moved for an unrelated reason) can also
    leave most of a sample missing at the repo root -- but it is vanishingly unlikely for
    ALL of them to coincidentally resolve under one specific subdirectory. Without that
    positive confirmation, this must stay silent (routed to the ordinary orphaned-anchor
    path instead, not a false subdir-run diagnostic)."""
    repo_root = tmp_path
    (repo_root / "unrelated").mkdir()
    # Nothing in ANCHORABLE_PATHS exists anywhere on disk.
    reader = GraphifyReader(_write_graph(tmp_path, "graph.json", ANCHORABLE_PATHS))

    assert reader.detect_subdir_mismatch(repo_root=repo_root) is None


def test_repo_root_none_when_not_a_git_repo(tmp_path):
    """GraphifyReader.repo_root() -- the shared, never-raising git lookup both the
    subdir-mismatch diagnostic and sync.py's moved-rung guard key off of. None outside
    any git working tree."""
    reader = GraphifyReader(_write_graph(tmp_path, "graph.json", ANCHORABLE_PATHS))
    assert reader.repo_root() is None


def test_detect_subdir_mismatch_defaults_to_reader_repo_root(tmp_path):
    """Without an explicit repo_root, the method resolves its own via repo_root() (git
    rev-parse from the graph's own location) rather than requiring every caller to
    resolve and pass one."""
    import subprocess

    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    for p in ANCHORABLE_PATHS:
        real = tmp_path / "src" / p
        real.parent.mkdir(parents=True, exist_ok=True)
        real.write_text("pass\n")
    reader = GraphifyReader(_write_graph(tmp_path, "graph.json", ANCHORABLE_PATHS))

    mismatch = reader.detect_subdir_mismatch()

    assert mismatch is not None
    assert mismatch["likely_run_from"] == "src"
