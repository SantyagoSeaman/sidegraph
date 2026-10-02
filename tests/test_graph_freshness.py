"""A stale graph is detectable: ``GraphifyReader.freshness()`` compares graph.json's build
commit with HEAD and answers ``fresh`` / ``stale`` / ``unknown`` (spec D1-D3).

Red target (the whole module): unfixed code has no ``freshness`` on the reader (AttributeError).
The fixture helper at the top is imported by the other stale-graph test modules (SessionStart,
get_task_context, doctor, stats) so that all four surfaces are driven by the same repository.
# see design/superpowers/specs/2026-10-01-stale-graph-visible-design.md (4.1)
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import NamedTuple

from sidegraph.engine.reader import GraphifyReader
from sidegraph.freshness import GraphFreshness, staleness_phrase

# Explicit mtimes, never the clock: a file edited before the build is older than graph.json,
# one edited after it is newer. Nanoseconds, as os.utime and st_mtime_ns want them.
T_FILE = 1_700_000_000 * 10**9
T_GRAPH = T_FILE + 100 * 10**9
T_AFTER = T_GRAPH + 100 * 10**9


class Fixture(NamedTuple):
    repo: Path
    graph: Path
    first: str  # commit A, the commit the graph is built at


def git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True)
    return result.stdout.strip()


def head(repo: Path) -> str:
    return git(repo, "rev-parse", "HEAD")


def touch(path: Path, ns: int) -> None:
    os.utime(path, ns=(ns, ns))


def node(i: int, source_file: str) -> dict:
    return {
        "id": f"n{i}",
        "label": f"fn_{i}()",
        "norm_label": f"fn_{i}()",
        "file_type": "code",
        "source_file": source_file,
        "source_location": "L1",
        "community": 1,
    }


def write_graph(
    path: Path, built_at: str | None, source_files: list[str], mtime_ns: int = T_GRAPH
) -> None:
    """A minimal graph.json (shape of ``tests/fixtures/mini_graph.json``) at ``path``."""
    data: dict = {
        "directed": True,
        "multigraph": False,
        "nodes": [node(i, p) for i, p in enumerate(source_files)],
        "links": [],
    }
    if built_at is not None:
        data["built_at_commit"] = built_at
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))
    touch(path, mtime_ns)


def commit(repo: Path, message: str, files: dict[str, str | None], mtime_ns: int = T_AFTER) -> str:
    """Write (or, for ``None``, delete) files, commit them, and return the commit's sha.
    Written files get ``mtime_ns``: the edit is later than the build unless a test says so."""
    for rel, text in files.items():
        p = repo / rel
        if text is None:
            p.unlink()
            continue
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        touch(p, mtime_ns)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", message)
    return head(repo)


def make_repo(tmp_path: Path, *, graph_dir: str = "graphify-out") -> Fixture:
    """A git repository whose commit A holds ``pkg/m.py``, and a graph.json built at A
    (ignored by git, like Graphify's real output) that holds exactly that file."""
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "commit.gpgsign", "false")
    first = commit(
        repo,
        "A",
        {".gitignore": "graphify-out/\n.cache/\n.sidegraph/\n", "pkg/m.py": "def a():\n    pass\n"},
        mtime_ns=T_FILE,
    )
    graph = repo / graph_dir / "graph.json"
    write_graph(graph, first, ["pkg/m.py"])
    return Fixture(repo, graph, first)


def stale_repo(tmp_path: Path) -> Fixture:
    """The T1 repository: commit B edited ``pkg/m.py`` after the graph was built at A."""
    fx = make_repo(tmp_path)
    commit(fx.repo, "B", {"pkg/m.py": "def a():\n    return 1\n"})
    return fx


def test_t1_a_file_the_graph_holds_changed_after_the_build_is_stale(tmp_path):
    fx = stale_repo(tmp_path)

    f = GraphifyReader(fx.graph).freshness()

    assert f.state == "stale"
    assert f.in_history is True
    assert f.commits_behind == 1
    assert f.changed == 1
    assert f.sample == ["pkg/m.py"]
    assert f.built_at == fx.first
    assert f.head == head(fx.repo)
    assert f.reason is None


def test_t2_a_new_file_of_a_type_the_graph_holds_is_stale(tmp_path):
    fx = make_repo(tmp_path)
    commit(fx.repo, "B", {"pkg/n.py": "def b():\n    pass\n"})

    f = GraphifyReader(fx.graph).freshness()

    assert f.state == "stale"
    assert f.changed == 1
    assert f.sample == ["pkg/n.py"]


def test_t3_a_commit_that_only_touches_types_the_graph_lacks_is_fresh(tmp_path):
    """A `.txt` does not make a `.py` graph stale: the commit count never decides."""
    fx = make_repo(tmp_path)
    commit(fx.repo, "B", {"notes.txt": "hello\n"})

    f = GraphifyReader(fx.graph).freshness()

    assert f.state == "fresh"
    assert f.commits_behind == 1
    assert f.in_history is True
    assert f.changed == 0
    assert f.sample == []


def test_t4_a_graph_built_at_head_is_fresh(tmp_path):
    fx = make_repo(tmp_path)

    f = GraphifyReader(fx.graph).freshness()

    assert f.state == "fresh"
    assert f.commits_behind == 0
    assert f.built_at == f.head == fx.first


def test_t5_a_graph_without_a_build_commit_is_unknown(tmp_path):
    fx = make_repo(tmp_path)
    write_graph(fx.graph, None, ["pkg/m.py"])

    f = GraphifyReader(fx.graph).freshness()

    assert f.state == "unknown"
    assert f.reason is not None and "build commit" in f.reason


def test_t5b_a_build_commit_that_is_not_a_full_id_is_unknown(tmp_path):
    """`abc123`, `v1`, `-x`: a short id or a ref could resolve to anything, and an
    option-like value must never reach git."""
    fx = make_repo(tmp_path)
    for value in (
        "abc123",
        "v1",
        "-x",
        "HEAD",
        fx.first[:12],
        fx.first.upper(),
        fx.first + "..HEAD",  # starts like a full id: a prefix match would let it through
        fx.first + "\n",
    ):
        write_graph(fx.graph, value, ["pkg/m.py"])
        f = GraphifyReader(fx.graph).freshness()
        assert f.state == "unknown", value
        assert f.reason is not None and "full commit id" in f.reason, value


def test_t5c_a_build_commit_that_is_not_a_string_reads_as_absent(tmp_path):
    fx = make_repo(tmp_path)
    for value in ("", 42, ["x"]):
        write_graph(fx.graph, value, ["pkg/m.py"])  # type: ignore[arg-type]
        assert GraphifyReader(fx.graph).built_at_commit() is None, value
    write_graph(fx.graph, fx.first, ["pkg/m.py"])
    assert GraphifyReader(fx.graph).built_at_commit() == fx.first


def test_t6_a_full_sha_the_repository_lacks_is_unknown(tmp_path):
    fx = make_repo(tmp_path)
    write_graph(fx.graph, "0" * 39 + "1", ["pkg/m.py"])

    f = GraphifyReader(fx.graph).freshness()

    assert f.state == "unknown"
    assert f.reason is not None and "does not have" in f.reason
    assert "0000000" in f.reason


def test_t7_a_graph_outside_any_git_repository_is_unknown(tmp_path):
    graph = tmp_path / "plain" / "graph.json"
    write_graph(graph, "a" * 40, ["pkg/m.py"])

    f = GraphifyReader(graph).freshness()

    assert f.state == "unknown"
    assert f.reason is not None and "not inside a git repository" in f.reason


def test_t8_git_that_cannot_run_is_unknown_not_an_exception(tmp_path, monkeypatch):
    fx = stale_repo(tmp_path)
    reader = GraphifyReader(fx.graph)

    def boom(*a, **k):
        raise OSError("no git here")

    monkeypatch.setattr("sidegraph.engine.reader.subprocess.run", boom)

    f = reader.freshness()

    assert f.state == "unknown"


def test_t8b_a_git_call_that_outlives_the_deadline_is_unknown(tmp_path, monkeypatch):
    fx = stale_repo(tmp_path)
    reader = GraphifyReader(fx.graph)
    real = subprocess.run

    def slow(cmd, *a, **k):
        if cmd[:2] == ["git", "rev-parse"] and "HEAD" in cmd:
            raise subprocess.TimeoutExpired(cmd, k.get("timeout", 0))
        return real(cmd, *a, **k)

    monkeypatch.setattr("sidegraph.engine.reader.subprocess.run", slow)

    f = reader.freshness()

    assert f.state == "unknown"
    assert f.reason == "git timed out"


def test_t8c_a_deadline_already_spent_is_unknown_without_a_git_call(tmp_path, monkeypatch):
    fx = stale_repo(tmp_path)
    reader = GraphifyReader(fx.graph)
    calls: list[list[str]] = []

    def counting(cmd, *a, **k):
        calls.append(cmd)
        raise AssertionError("no git call may start once the deadline is spent")

    monkeypatch.setattr("sidegraph.engine.reader.subprocess.run", counting)

    f = reader.freshness(deadline=0.0)

    assert f.state == "unknown"
    assert f.reason == "git timed out"
    assert calls == []


def test_t27_repo_root_gets_the_time_left_on_the_deadline_not_its_own_default(
    tmp_path, monkeypatch
):
    """One deadline covers every git call, `repo_root`'s included."""
    fx = stale_repo(tmp_path)
    reader = GraphifyReader(fx.graph)
    seen: list[float] = []
    real = reader.repo_root

    def recording(timeout: float = 5.0):
        seen.append(timeout)
        return real(timeout=timeout)

    monkeypatch.setattr(reader, "repo_root", recording)

    assert reader.freshness(deadline=0.5).state == "stale"

    assert len(seen) == 1 and 0 < seen[0] <= 0.5


def test_t31_a_known_root_is_used_and_repo_root_is_not_asked_again(tmp_path, monkeypatch):
    fx = stale_repo(tmp_path)
    reader = GraphifyReader(fx.graph)
    root = reader.repo_root()
    calls: list[str] = []
    monkeypatch.setattr(reader, "repo_root", lambda *a, **k: calls.append("repo_root"))

    f = reader.freshness(root=root)

    assert f.state == "stale" and f.sample == ["pkg/m.py"]
    assert calls == []


def test_t18_a_head_behind_the_build_is_reported_as_outside_history(tmp_path):
    """Built at B, then `git checkout A`: B is not an ancestor of HEAD. Rev 1 of the spec
    would have printed "0 commits behind"."""
    fx = make_repo(tmp_path)
    b = commit(fx.repo, "B", {"pkg/n.py": "def b():\n    pass\n"})
    write_graph(fx.graph, b, ["pkg/m.py", "pkg/n.py"])
    git(fx.repo, "checkout", "-q", fx.first)

    f = GraphifyReader(fx.graph).freshness()

    assert f.state == "stale"
    assert f.in_history is False
    assert f.commits_behind is None
    assert f.changed == 1
    assert f.sample == ["pkg/n.py"]
    phrase = staleness_phrase(f)
    assert "outside HEAD's history" in phrase
    assert phrase == f"built at {b[:7]}, a commit outside HEAD's history; 1 file differs from HEAD"


def test_t19_a_dirty_build_that_was_committed_afterwards_is_fresh(tmp_path):
    """Graphify indexes the working tree and stamps HEAD. Edit, build, then commit: the graph
    is one commit "behind" with current content. The file is no newer than graph.json."""
    fx = make_repo(tmp_path)
    (fx.repo / "pkg" / "m.py").write_text("def a():\n    return 2\n")
    touch(fx.repo / "pkg" / "m.py", T_GRAPH - 10**9)  # edited, then the graph was built
    commit(fx.repo, "B", {}, mtime_ns=T_GRAPH - 10**9)

    f = GraphifyReader(fx.graph).freshness()

    assert f.state == "fresh"
    assert f.commits_behind == 1
    assert f.changed == 0


def test_t19b_a_file_edited_exactly_at_the_build_time_is_reflected(tmp_path):
    fx = make_repo(tmp_path)
    commit(fx.repo, "B", {"pkg/m.py": "def a():\n    return 3\n"}, mtime_ns=T_GRAPH)

    assert GraphifyReader(fx.graph).freshness().state == "fresh"


def test_t25_a_no_op_rebuild_that_only_rewrote_the_manifest_is_fresh(tmp_path):
    """Shaped like the producer: when topology is unchanged `graphify update .` leaves
    graph.json (and its built_at_commit and mtime) alone but rewrites the sibling
    manifest.json. The file edited since the last real build is older than that rewrite."""
    fx = make_repo(tmp_path)
    commit(fx.repo, "B", {"pkg/m.py": "def a():\n    return 1  # a comment\n"}, mtime_ns=T_AFTER)
    assert GraphifyReader(fx.graph).freshness().state == "stale"  # before the no-op rebuild

    manifest = fx.graph.parent / "manifest.json"
    manifest.write_text("{}")
    touch(manifest, T_AFTER + 10**9)

    f = GraphifyReader(fx.graph).freshness()

    assert f.state == "fresh"
    assert f.commits_behind == 1
    assert f.changed == 0


def test_t25b_a_manifest_older_than_the_edit_does_not_hide_it(tmp_path):
    fx = make_repo(tmp_path)
    commit(fx.repo, "B", {"pkg/m.py": "def a():\n    return 1\n"}, mtime_ns=T_AFTER)
    manifest = fx.graph.parent / "manifest.json"
    manifest.write_text("{}")
    touch(manifest, T_GRAPH)

    assert GraphifyReader(fx.graph).freshness().state == "stale"


def test_t30_a_changed_file_that_cannot_be_statted_cannot_be_called_reflected(
    tmp_path, monkeypatch
):
    """A dirty build committed afterwards is `fresh` by the mtime rule (T19). When the file's
    mtime cannot be read the rule cannot clear it, so it counts."""
    fx = make_repo(tmp_path)
    (fx.repo / "pkg" / "m.py").write_text("def a():\n    return 2\n")
    touch(fx.repo / "pkg" / "m.py", T_GRAPH - 10**9)
    commit(fx.repo, "B", {}, mtime_ns=T_GRAPH - 10**9)
    reader = GraphifyReader(fx.graph)
    assert reader.freshness().state == "fresh"
    real_stat = Path.stat

    def flaky(self, *a, **k):
        if self.name == "m.py":
            raise PermissionError("no stat for you")
        return real_stat(self, *a, **k)

    monkeypatch.setattr(Path, "stat", flaky)

    f = reader.freshness()

    assert f.state == "stale"
    assert f.sample == ["pkg/m.py"]


def test_t20_a_graph_two_levels_below_the_root_is_still_compared(tmp_path):
    """`git diff --relative` from `.cache/graphify` would print nothing (rev 1, R6)."""
    fx = make_repo(tmp_path, graph_dir=".cache/graphify")
    commit(fx.repo, "B", {"pkg/m.py": "def a():\n    return 1\n"})

    f = GraphifyReader(fx.graph).freshness()

    assert f.state == "stale"
    assert f.sample == ["pkg/m.py"]


def test_t21_a_rename_out_of_a_graph_type_is_stale(tmp_path):
    """`pkg/m.py` -> `pkg/m.txt`: the graph still holds `pkg/m.py`. Without
    `--no-renames` git lists only the new name, which is not a graph type."""
    fx = make_repo(tmp_path)
    git(fx.repo, "mv", "pkg/m.py", "pkg/m.txt")
    git(fx.repo, "commit", "-q", "-m", "B")

    f = GraphifyReader(fx.graph).freshness()

    assert f.state == "stale"
    assert f.changed == 1
    assert f.sample == ["pkg/m.py"]


def test_t22_a_non_ascii_file_name_is_listed_unquoted(tmp_path):
    """Plain `git diff --name-only` prints `"pkg/\\303\\274.py"`; `-z` does not quote."""
    fx = make_repo(tmp_path)
    commit(fx.repo, "B", {"pkg/ü.py": "def u():\n    pass\n"})

    f = GraphifyReader(fx.graph).freshness()

    assert f.state == "stale"
    assert "pkg/ü.py" in f.sample


def test_t23_a_new_file_in_a_dot_directory_is_not_indexable(tmp_path):
    """Graphify skips dot-directories, so a file there is not a file the graph should hold."""
    fx = make_repo(tmp_path)
    commit(fx.repo, "B", {".hidden/x.py": "x = 1\n"})

    f = GraphifyReader(fx.graph).freshness()

    assert f.state == "fresh"
    assert f.commits_behind == 1


def test_a_deleted_file_the_graph_held_is_stale_and_one_it_never_held_is_not(tmp_path):
    fx = make_repo(tmp_path)
    b = commit(fx.repo, "B", {"pkg/gone.py": "x = 1\n"})
    write_graph(fx.graph, b, ["pkg/m.py"])  # the graph never held pkg/gone.py
    commit(fx.repo, "C", {"pkg/gone.py": None})

    assert GraphifyReader(fx.graph).freshness().state == "fresh"

    write_graph(fx.graph, b, ["pkg/m.py", "pkg/gone.py"])  # now it did
    f = GraphifyReader(fx.graph).freshness()
    assert f.state == "stale"
    assert f.sample == ["pkg/gone.py"]


def test_a_changed_file_with_no_extension_counts_only_when_the_graph_holds_it(tmp_path):
    """An extensionless graph file must not make every extensionless change count."""
    fx = make_repo(tmp_path)
    write_graph(fx.graph, fx.first, ["pkg/m.py", "Makefile"])
    commit(fx.repo, "B", {"LICENSE": "MIT\n"})

    assert GraphifyReader(fx.graph).freshness().state == "fresh"

    commit(fx.repo, "C", {"Makefile": "all:\n"})
    f = GraphifyReader(fx.graph).freshness()
    assert f.state == "stale"
    assert f.sample == ["Makefile"]


def test_the_sample_holds_five_paths_graph_files_first_each_group_sorted(tmp_path):
    fx = make_repo(tmp_path)
    write_graph(fx.graph, fx.first, ["pkg/m.py", "pkg/z.py"])
    commit(fx.repo, "B", {"pkg/z.py": "z = 1\n"})
    commit(fx.repo, "C", {**{f"new/{c}.py": "x = 1\n" for c in "edcba"}, "pkg/m.py": "m = 2\n"})

    f = GraphifyReader(fx.graph).freshness()

    assert f.state == "stale"
    assert f.changed == 7
    assert f.sample == ["pkg/m.py", "pkg/z.py", "new/a.py", "new/b.py", "new/c.py"]


def test_source_files_holds_every_file_type_and_is_memoized(tmp_path):
    graph = tmp_path / "graph.json"
    data = {
        "nodes": [
            {**node(0, "a.py")},
            {**node(1, "docs/x.md"), "file_type": "document"},
            {**node(2, "img.png"), "file_type": "image"},
            {"id": "n3", "label": "orphan", "file_type": "code"},
        ],
        "links": [],
    }
    graph.write_text(json.dumps(data))
    reader = GraphifyReader(graph)

    assert reader.source_files() == frozenset({"a.py", "docs/x.md", "img.png"})
    assert reader.source_files() is reader.source_files()


def test_repo_root_takes_a_timeout_and_keeps_its_default(tmp_path, monkeypatch):
    fx = make_repo(tmp_path)
    reader = GraphifyReader(fx.graph)
    seen: list[float | None] = []
    real = subprocess.run

    def spy(cmd, *a, **k):
        seen.append(k.get("timeout"))
        return real(cmd, *a, **k)

    monkeypatch.setattr("sidegraph.engine.reader.subprocess.run", spy)

    reader.repo_root()
    reader.repo_root(timeout=1.5)

    assert seen == [5.0, 1.5]


def test_staleness_phrase_follows_the_counts():
    one = GraphFreshness(
        state="stale", built_at="a" * 40, in_history=True, commits_behind=1, changed=1
    )
    many = GraphFreshness(
        state="stale",
        built_at="314f1ac" + "0" * 33,
        in_history=True,
        commits_behind=314,
        changed=258,
    )
    outside_many = GraphFreshness(state="stale", built_at="b" * 40, in_history=False, changed=2)

    assert staleness_phrase(one) == "built at aaaaaaa, 1 commit behind HEAD, 1 file changed since"
    assert staleness_phrase(many) == (
        "built at 314f1ac, 314 commits behind HEAD, 258 files changed since"
    )
    assert staleness_phrase(outside_many) == (
        "built at bbbbbbb, a commit outside HEAD's history; 2 files differ from HEAD"
    )
