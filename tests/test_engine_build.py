"""Building the graph of HEAD with a chosen Graphify release (snapshot --build-with).

see design/superpowers/specs/2026-10-14-graphify-upgrade-design.md (D5.4)"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from sidegraph.engine.reader import GraphBuildError, build_graph_at_head, graphify_build_command


def _repo(tmp_path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / "tracked.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.py"], cwd=repo, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.com",
            "commit",
            "-q",
            "--no-verify",
            "-m",
            "c",
        ],
        cwd=repo,
        check=True,
    )
    (repo / "untracked.py").write_text("y = 2\n", encoding="utf-8")
    return repo


def _engine(seen, code=0, err=""):
    def run(argv, **kw):
        if argv[0] == "git":
            return subprocess.run(argv, **kw)
        cwd = Path(kw["cwd"])
        seen.update(argv=argv, env=kw["env"], files=sorted(p.name for p in cwd.iterdir()))
        if code == 0:
            (cwd / "graphify-out").mkdir(exist_ok=True)
            (cwd / "graphify-out" / "graph.json").write_text(
                '{"nodes":[],"links":[]}', encoding="utf-8"
            )
        return subprocess.CompletedProcess(argv, code, "", err)

    return run


def test_the_command_names_the_release():
    """Red against nothing existing (ImportError)."""
    assert graphify_build_command("0.9.6") == [
        "uvx",
        "--from",
        "graphifyy==0.9.6",
        "graphify",
        "update",
        ".",
    ]


@pytest.mark.parametrize("bad", ["latest", "0.9", ""])
def test_anything_but_a_release_is_refused(bad):
    with pytest.raises(GraphBuildError, match="release"):
        graphify_build_command(bad)


@pytest.mark.parametrize("nested", [False, True])
def test_the_build_runs_on_head_s_tree_with_auto_refresh_off(tmp_path, nested):
    repo, seen = _repo(tmp_path), {}
    project = repo
    if nested:
        project = repo / "pkg"
        project.mkdir()
        (project / "tracked.py").write_text("x = 2\n", encoding="utf-8")
        _commit(repo, "pkg")
        (project / "untracked.py").write_text("y = 3\n", encoding="utf-8")
    work = tmp_path / "work"
    work.mkdir()
    graph = build_graph_at_head(project, "0.9.6", work, run=_engine(seen))
    assert graph.is_file() and work in graph.parents
    assert seen["argv"] == graphify_build_command("0.9.6")
    assert seen["env"]["GRAPHIFY_NO_AUTO_REFRESH"] == "1" and seen["env"]["PYTHONHASHSEED"] == "0"
    assert seen["files"] == ["tracked.py"]  # git archive HEAD: an untracked file is not there
    assert not (project / "graphify-out").exists()


@pytest.fixture(scope="module")
def read_only_repo(tmp_path_factory) -> Path:
    """One committed repo for the tests that only read it (``git archive`` never writes)."""
    return _repo(tmp_path_factory.mktemp("shared"))


def test_a_failed_build_names_the_engine_s_last_words(tmp_path, read_only_repo):
    work = tmp_path / "w"
    work.mkdir()
    with pytest.raises(GraphBuildError, match="boom: no such version"):
        build_graph_at_head(
            read_only_repo, "0.9.6", work, run=_engine({}, 1, "boom: no such version")
        )


def test_a_missing_uvx_says_how_to_get_it(tmp_path, read_only_repo):
    def run(argv, **kw):
        if argv[0] == "git":
            return subprocess.run(argv, **kw)
        raise FileNotFoundError(argv[0])

    work = tmp_path / "w"
    work.mkdir()
    with pytest.raises(GraphBuildError, match="uvx was not found"):
        build_graph_at_head(read_only_repo, "0.9.6", work, run=run)


def _commit(repo: Path, path: str) -> None:
    subprocess.run(["git", "add", path], cwd=repo, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.com",
            "commit",
            "-q",
            "--no-verify",
            "-m",
            "fixture",
        ],
        cwd=repo,
        check=True,
    )


def test_archived_graph_artifacts_do_not_seed_the_requested_engine(tmp_path):
    repo, seen = _repo(tmp_path), {}
    cached = repo / "graphify-out"
    cached.mkdir()
    old = b'{"nodes":[{"id":"old"}],"links":[]}\n'
    (cached / "graph.json").write_bytes(old)
    (cached / "manifest.json").write_text("{}\n", encoding="utf-8")
    _commit(repo, "graphify-out")
    work = tmp_path / "work"
    work.mkdir()
    graph = build_graph_at_head(repo, "0.9.6", work, run=_engine(seen))
    assert seen["files"] == ["tracked.py"]
    assert graph.read_bytes() != old
    assert (cached / "graph.json").read_bytes() == old
    assert (cached / "manifest.json").read_text(encoding="utf-8") == "{}\n"


def test_an_unsafe_archived_symlink_reports_a_build_error(tmp_path):
    repo = _repo(tmp_path)
    try:
        (repo / "unsafe-link").symlink_to("/outside-not-read")
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")
    _commit(repo, "unsafe-link")
    work = tmp_path / "work"
    work.mkdir()
    with pytest.raises(GraphBuildError, match="could not prepare HEAD's build tree"):
        build_graph_at_head(repo, "0.9.6", work, run=_engine({}))
    assert (repo / "unsafe-link").is_symlink()
