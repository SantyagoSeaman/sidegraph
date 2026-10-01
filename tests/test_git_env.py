import shutil
import subprocess
from pathlib import Path

from sidegraph import doctor, gitio, verify
from sidegraph.gitenv import GIT_LOCAL_ENV_VARS, git_env

_CONFIG_INJECTION = {"GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT"}


def _git(args, cwd):
    env = {**git_env(), "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}
    subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True)


def _repo(path: Path) -> Path:
    path.mkdir()
    _git(["init", "-q"], path)
    _git(["config", "user.email", "t@example.com"], path)
    _git(["config", "user.name", "T"], path)
    return path


def _commit(repo: Path, name: str, text: str) -> None:
    (repo / name).write_text(text)
    _git(["add", name], repo)
    _git(["commit", "-q", "-m", name], repo)


def _sha(repo: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        env=git_env(),
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def test_the_scrubbed_set_matches_what_git_itself_lists():
    out = subprocess.run(
        ["git", "rev-parse", "--local-env-vars"], capture_output=True, text=True, check=True
    ).stdout.split()
    assert set(out) - _CONFIG_INJECTION == GIT_LOCAL_ENV_VARS


def test_git_env_drops_local_vars_and_keeps_the_rest(monkeypatch):
    keep = ("GIT_CEILING_DIRECTORIES", "GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT", "PATH")
    for var in GIT_LOCAL_ENV_VARS:
        monkeypatch.setenv(var, "x")
    for var in keep:
        monkeypatch.setenv(var, "keep")
    env = git_env()
    assert not set(env) & GIT_LOCAL_ENV_VARS
    assert {k: env[k] for k in keep} == dict.fromkeys(keep, "keep")


def test_find_repo_root_ignores_git_dir(tmp_path, monkeypatch):
    a, b = _repo(tmp_path / "a"), _repo(tmp_path / "b")
    monkeypatch.setenv("GIT_DIR", str(b / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(b))
    assert verify._find_repo_root(a) == a.resolve()


def test_the_code_drift_diff_ignores_git_dir(tmp_path, monkeypatch):
    a, b = _repo(tmp_path / "a"), _repo(tmp_path / "b")
    _commit(a, "f", "1")
    first = _sha(a)
    _commit(a, "f", "2")
    _commit(b, "g", "1")
    monkeypatch.setenv("GIT_DIR", str(b / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(b))
    assert doctor._git_diff_name_only(a, first, ["f"]) == ["f"]


def test_gitio_keeps_honouring_git_index_file(tmp_path, monkeypatch):
    """Guard for D6: ``prepare-commit-msg`` runs with ``GIT_INDEX_FILE`` naming a partial
    commit's temporary index, and ``staged_files`` must read that one."""
    a = _repo(tmp_path / "a")
    _commit(a, "x", "x")
    (a / "y").write_text("y")
    alt = tmp_path / "alt-index"
    shutil.copy(a / ".git" / "index", alt)  # an index with nothing staged
    _git(["add", "y"], a)  # the real index stages y
    assert gitio.staged_files(a) == ["y"]
    monkeypatch.setenv("GIT_INDEX_FILE", str(alt))
    assert gitio.staged_files(a) == []


def _graph(repo: Path, paths: list[str]) -> Path:
    import json

    nodes = [
        {
            "id": f"n{i}",
            "label": f"fn_{i}()",
            "norm_label": f"fn_{i}()",
            "file_type": "code",
            "source_file": p,
            "community": 0,
        }
        for i, p in enumerate(paths)
    ]
    graph = repo / "graph.json"
    graph.write_text(json.dumps({"built_at_commit": "v1", "nodes": nodes, "links": []}))
    return graph


def test_reader_repo_root_ignores_an_inherited_git_dir(tmp_path, monkeypatch):
    from sidegraph.engine.reader import GraphifyReader

    repo_a = _repo(tmp_path / "a")
    repo_b = _repo(tmp_path / "b")
    reader = GraphifyReader(_graph(repo_a, ["a0.py"]))
    monkeypatch.setenv("GIT_DIR", str(repo_b / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(repo_b))
    assert reader.repo_root() == repo_a.resolve()


def test_doctor_graph_root_check_ignores_an_inherited_git_dir(tmp_path, monkeypatch):
    from sidegraph.doctor import GRAPH_ROOT_MISMATCH, curate
    from sidegraph.engine.reader import GraphifyReader

    names = [f"a{i}.py" for i in range(8)]
    repo_a = _repo(tmp_path / "a")
    repo_b = _repo(tmp_path / "b")
    (repo_b / "sub").mkdir()
    for n in names:
        (repo_a / n).write_text("pass\n")
        (repo_b / "sub" / n).write_text("pass\n")
    store = repo_a / ".sidegraph"
    store.mkdir()
    reader = GraphifyReader(_graph(repo_a, names))
    monkeypatch.setenv("GIT_DIR", str(repo_b / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(repo_b))
    report = curate(store, reader=reader)
    assert GRAPH_ROOT_MISMATCH not in [f.code for f in report.findings]
