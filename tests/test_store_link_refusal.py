"""A store path that leaves its repository through a link is refused, with no opt-in.

Every reader that opens a store goes through ``store_layout.check_store_requirement`` or has its
own call to ``refuse_outside_store_link``; one test per surface keeps each call honest.
# see design/superpowers/specs/2026-11-12-trust-boundary-clone-content-design.md (D8)
"""

from __future__ import annotations

import asyncio
import io
import json
import ntpath
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import fastmcp
import mcp.types as mt
import pytest

import sidegraph.server as server_module
from sidegraph import adr_cli, gitio
from sidegraph.cli import (
    compact_main,
    doctor_main,
    domains_main,
    export_okf_main,
    import_main,
    init_main,
    migrate_main,
    require_main,
    stats_main,
    sync_main,
    verify_main,
    viz_main,
)
from sidegraph.host import hooks
from sidegraph.hot_index import HotIndex
from sidegraph.stats.model import build_report
from sidegraph.store import Store
from sidegraph.store_layout import (
    StoreLinkRefused,
    StoreRequirementError,
    outside_store_link,
    refuse_outside_store_link,
)
from sidegraph.verify import verify_against, verify_snapshot
from tests.test_host_integrity import start

REASON = "leaves its repository through a link"


@pytest.fixture(scope="session", autouse=True)
def _symlinks_work(tmp_path_factory: pytest.TempPathFactory) -> None:
    base = tmp_path_factory.mktemp("probe")
    try:
        (base / "link").symlink_to(base, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks cannot be created here")


class Linked:
    def __init__(self, tmp_path: Path) -> None:
        self.tmp = tmp_path
        self.repo = tmp_path / "repo"
        (self.repo / ".git").mkdir(parents=True)
        self.target = tmp_path / "shared"
        Store(self.target).close()
        self.link = self.repo / ".sidegraph"
        self.link.symlink_to("../shared", target_is_directory=True)
        self.before = self.listing()

    def listing(self) -> list[str]:
        return sorted(p.name for p in self.target.iterdir())

    def unchanged(self) -> bool:
        return self.listing() == self.before


@pytest.fixture
def linked(tmp_path: Path) -> Linked:
    return Linked(tmp_path)


def _link(path: Path, target: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.symlink_to(target, target_is_directory=True)


def test_outside_store_link_layouts(tmp_path, monkeypatch, linked):
    repo, target = linked.repo, linked.target
    assert outside_store_link(linked.link) == str(target.resolve())
    # a dangling link
    _link(repo / "dangling" / ".sidegraph", "../../missing")
    assert outside_store_link(repo / "dangling" / ".sidegraph") == str(
        (tmp_path / "missing").resolve()
    )
    # a link in a directory above the store
    (tmp_path / "outpkg").mkdir()
    _link(repo / "pkg", "../outpkg")
    assert outside_store_link(repo / "pkg" / ".sidegraph") == str(
        (tmp_path / "outpkg" / ".sidegraph").resolve()
    )
    # a linked-worktree-shaped checkout: a .git FILE
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / ".git").write_text("gitdir: ../repo/.git/worktrees/wt\n", encoding="utf-8")
    (repo / "main-store").mkdir()
    _link(wt / ".sidegraph", "../repo/main-store")
    assert outside_store_link(wt / ".sidegraph") == str((repo / "main-store").resolve())
    # the layout that broke the first design: the link brings a .git of its own
    external = tmp_path / "external"
    (external / ".git").mkdir(parents=True)
    (external / "deep" / ".git").mkdir(parents=True)
    _link(repo / "pkg2", "../external")
    assert outside_store_link(repo / "pkg2" / ".sidegraph") == str(
        (external / ".sidegraph").resolve()
    )
    assert outside_store_link(repo / "pkg2" / "deep" / ".sidegraph") == str(
        (external / "deep" / ".sidegraph").resolve()
    )
    # what stays inside
    (repo / "plain" / ".sidegraph").mkdir(parents=True)
    assert outside_store_link(repo / "plain" / ".sidegraph") is None
    (repo / "stores" / "real").mkdir(parents=True)
    _link(repo / "inner" / ".sidegraph", "../stores/real")
    assert outside_store_link(repo / "inner" / ".sidegraph") is None
    bare = tmp_path / "no-repo"
    (bare / "real").mkdir(parents=True)
    _link(bare / ".sidegraph", "real")
    assert outside_store_link(bare / ".sidegraph") is None
    alias = tmp_path / "alias"
    alias.symlink_to(repo, target_is_directory=True)
    (repo / "plain2" / ".sidegraph").mkdir(parents=True)
    assert outside_store_link(alias / "plain2" / ".sidegraph") is None
    monkeypatch.chdir(repo / "plain")
    assert outside_store_link(".sidegraph") is None
    monkeypatch.undo()
    assert outside_store_link(tmp_path / "somewhere" / ".sidegraph") is None
    nested = repo / "vendor" / "other"
    (nested / ".git").mkdir(parents=True)
    (nested / ".sidegraph").mkdir()
    assert outside_store_link(nested / ".sidegraph") is None
    _link(repo / "inpkg", "vendor/other")
    assert outside_store_link(repo / "inpkg" / ".sidegraph") is None
    # a nested repository whose link leaves it but stays inside the outer one is still refused
    (nested / ".sidegraph").rmdir()
    (repo / "sibling").mkdir()
    _link(nested / ".sidegraph", "../../sibling")
    assert outside_store_link(nested / ".sidegraph") == str((repo / "sibling").resolve())
    # a sibling whose name begins with the repository's name is outside, not inside
    (tmp_path / "repo-shared").mkdir()
    _link(repo / "near" / ".sidegraph", "../../repo-shared")
    assert outside_store_link(repo / "near" / ".sidegraph") == str(
        (tmp_path / "repo-shared").resolve()
    )


def test_two_drives_count_as_outside(monkeypatch, linked):
    def two_drives(paths: Any) -> str:
        return ntpath.commonpath(["C:\\a", "D:\\b"])

    monkeypatch.setattr(os.path, "commonpath", two_drives)
    assert outside_store_link(linked.link) == str(linked.target.resolve())
    with pytest.raises(StoreLinkRefused) as caught:
        refuse_outside_store_link(linked.link)
    assert str(caught.value).startswith(REASON)


def test_store_refuses_a_directory_link(linked):
    with pytest.raises(StoreLinkRefused) as caught:
        Store(linked.link)
    assert issubclass(StoreLinkRefused, StoreRequirementError)
    assert caught.value.kind == "link"
    assert str(caught.value).startswith(REASON)
    assert str(linked.target.resolve()) in str(caught.value)
    assert linked.unchanged()


def test_store_refuses_under_the_plugin_environment(linked, monkeypatch):
    monkeypatch.setenv("SIDEGRAPH_DIR", ".sidegraph")
    monkeypatch.chdir(linked.repo)
    with pytest.raises(StoreLinkRefused):
        Store(".sidegraph")
    assert linked.unchanged()


def test_store_refuses_a_dangling_link(tmp_path):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / ".sidegraph").symlink_to("../missing", target_is_directory=True)
    with pytest.raises(StoreLinkRefused):
        Store(repo / ".sidegraph")
    assert not (tmp_path / "missing").exists()


def test_store_refuses_a_committed_link_to_another_repository(tmp_path):
    """``clone/pkg -> ../external`` brings the target's own ``.git``; it is no boundary."""
    clone, external = tmp_path / "clone", tmp_path / "external"
    (clone / ".git").mkdir(parents=True)
    (external / ".git").mkdir(parents=True)
    (clone / "pkg").symlink_to("../external", target_is_directory=True)
    with pytest.raises(StoreLinkRefused):
        Store(clone / "pkg" / ".sidegraph")
    assert not (external / ".sidegraph").exists()
    assert HotIndex.open(clone / "pkg" / ".sidegraph") is None


def test_light_readers_return_nothing(linked):
    assert HotIndex.open(linked.link) is None
    assert gitio.open_index_ro(linked.link) is None
    assert linked.unchanged()
    index = HotIndex.open(linked.target)
    assert index is not None
    index.close()
    conn = gitio.open_index_ro(linked.target)
    assert conn is not None
    conn.close()


def test_stats_refuses_the_link(linked, capsys):
    with pytest.raises(StoreLinkRefused):
        build_report(linked.link, None, window_days=30, now=datetime.now(UTC))
    assert stats_main(["--db", str(linked.link)]) == 1
    err = capsys.readouterr().err
    assert "cannot read the store" in err
    assert REASON in err
    assert linked.unchanged()


def test_verify_snapshot_and_its_commands_refuse_the_link(linked, capsys):
    with pytest.raises(StoreLinkRefused):
        verify_snapshot(linked.link)
    for command in (verify_main, doctor_main):
        assert command(["--db", str(linked.link)]) == 1
        err = capsys.readouterr().err
        assert "store not readable" in err
        assert REASON in err
    assert linked.unchanged()


@pytest.mark.parametrize(
    ("command", "arguments"),
    [
        (sync_main, []),
        (import_main, []),
        (import_main, ["--docs", "{doc}"]),
        (domains_main, ["bootstrap"]),
    ],
    ids=["sync", "import-rationale", "import-docs", "domains-bootstrap"],
)
def test_graph_commands_name_the_link_when_there_is_no_graph(
    linked, capsys, monkeypatch, command, arguments
):
    """No graph file in the repository: the refusal still comes first, not "graph not readable",
    and no document is handed to the importer."""
    doc = linked.tmp / "decision.md"
    doc.write_text("# A decision\n", encoding="utf-8")
    arguments = [a.format(doc=doc) for a in arguments]

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("the importer ran behind a refused store")

    monkeypatch.setattr("sidegraph.cli.import_docs", forbidden)
    assert command([*arguments, "--db", str(linked.link)]) == 1
    err = capsys.readouterr().err
    assert "store not readable" in err
    assert REASON in err
    assert "graph not readable" not in err
    assert linked.unchanged()


def test_verify_against_refuses_the_link(linked):
    with pytest.raises(StoreLinkRefused):
        verify_against(linked.link, "HEAD")


def test_session_start_says_why(linked, monkeypatch, capsys):
    out = start(monkeypatch, capsys, linked.link, project=linked.repo)
    assert "leaves its repository" in out["systemMessage"]
    assert "SIDEGRAPH_DIR" in out["systemMessage"]
    assert linked.unchanged()


def test_stop_refuses_before_it_reads(linked, monkeypatch, capsys):
    real = gitio.open_index_ro
    calls: list[Path] = []

    def spy(store_dir: Path, **kwargs: Any) -> Any:
        calls.append(store_dir)
        return real(store_dir, **kwargs)

    monkeypatch.setattr(gitio, "open_index_ro", spy)
    monkeypatch.setenv("SIDEGRAPH_DIR", str(linked.link))
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(linked.repo))
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"session_id": "s1"})))
    hooks.stop()
    assert json.loads(capsys.readouterr().out) == {}
    assert calls == []
    assert linked.unchanged()


def test_mcp_names_the_link(linked, monkeypatch):
    assert REASON in (server_module._missing_store_setup(str(linked.link)) or "")
    monkeypatch.setenv("SIDEGRAPH_DIR", ".sidegraph")
    monkeypatch.chdir(linked.repo)
    monkeypatch.setattr(server_module, "_store", None)

    async def run() -> Any:
        async with fastmcp.Client(server_module.mcp) as client:
            return await client.call_tool(
                "add_decision",
                {"title": "Example", "kind": "adr", "context": "Example", "choice": "Example"},
                raise_on_error=False,
            )

    result = asyncio.run(run())
    text = "\n".join(b.text for b in result.content if isinstance(b, mt.TextContent))
    assert result.is_error
    assert REASON in text
    assert server_module._store is None
    assert linked.unchanged()


def test_bootstrap_refuses_the_link(linked, monkeypatch, capsys):
    from sidegraph.bootstrap.cli import main
    from tests.test_bootstrap_cli import forbidden_review_input, write_bootstrap_repo

    write_bootstrap_repo(linked.repo, host="claude-code")
    monkeypatch.setattr("builtins.input", forbidden_review_input)  # refused before any prompt
    rc = main(["--root", str(linked.repo), "--host", "claude-code"])
    err = capsys.readouterr().err
    assert rc == 1
    assert REASON in err
    assert linked.unchanged()


def test_what_still_opens(tmp_path, linked, monkeypatch, capsys):
    repo = tmp_path / "inside"
    (repo / ".git").mkdir(parents=True)
    (repo / "stores" / "real").mkdir(parents=True)
    (repo / ".sidegraph").symlink_to("stores/real", target_is_directory=True)
    store = Store(repo / ".sidegraph")
    store.close()
    assert (repo / "stores" / "real" / "format").is_file()
    Store(linked.target).close()  # by its own absolute path
    monkeypatch.setenv("SIDEGRAPH_DIR", str(linked.target))
    out = start(monkeypatch, capsys, linked.target, project=linked.repo)
    assert REASON not in out.get("systemMessage", "")


def test_a_dotdot_after_a_link_is_resolved_as_the_system_does(tmp_path):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (tmp_path / "outside" / "sub").mkdir(parents=True)
    _link(repo / "pkg", "../outside/sub")
    path = repo / "pkg" / ".." / ".sidegraph"  # the system reads it as outside/.sidegraph
    assert outside_store_link(path) == str((tmp_path / "outside" / ".sidegraph").resolve())
    with pytest.raises(StoreLinkRefused):
        Store(path)
    assert not (tmp_path / "outside" / ".sidegraph").exists()
    (repo / "plain" / "sub").mkdir(parents=True)  # a `..` with no link stays inside
    assert outside_store_link(repo / "plain" / "sub" / ".." / ".sidegraph") is None
    Store(repo / "plain" / "sub" / ".." / ".sidegraph").close()
    assert (repo / "plain" / ".sidegraph" / "format").is_file()


def test_the_notice_keeps_the_target_with_a_long_store_path(tmp_path, monkeypatch, capsys):
    repo = tmp_path / ("d" * 40) / ("e" * 40) / "repo"
    (repo / ".git").mkdir(parents=True)
    target = tmp_path / "shared"
    Store(target).close()
    link = repo / ".sidegraph"
    link.symlink_to(os.path.relpath(target, repo), target_is_directory=True)
    assert len(str(link)) >= 127
    out = start(monkeypatch, capsys, link, project=repo)
    assert str(target.resolve()) in out["systemMessage"]


def test_the_remedy_does_not_tell_a_plugin_user_to_set_the_variable(linked, monkeypatch, capsys):
    out = start(monkeypatch, capsys, linked.link, project=linked.repo)
    assert "real directory inside the repository" in out["systemMessage"]
    assert "set SIDEGRAPH_DIR to the shared directory" not in out["systemMessage"]


def test_mcp_names_a_dangling_link(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / ".sidegraph").symlink_to("../missing", target_is_directory=True)
    monkeypatch.setenv("SIDEGRAPH_DIR", ".sidegraph")
    monkeypatch.chdir(repo)
    monkeypatch.setattr(server_module, "_store", None)

    async def run() -> Any:
        async with fastmcp.Client(server_module.mcp) as client:
            return await client.call_tool(
                "add_decision",
                {"title": "Example", "kind": "adr", "context": "Example", "choice": "Example"},
                raise_on_error=False,
            )

    result = asyncio.run(run())
    text = "\n".join(b.text for b in result.content if isinstance(b, mt.TextContent))
    assert result.is_error
    assert REASON in text
    assert not (tmp_path / "missing").exists()


# -- every command that takes a store path names the link and reads nothing behind it ----------

_WATCHED: list[str] = []
_SEEN: list[str] = []


def _audit(event: str, args: tuple[Any, ...]) -> None:
    if not _WATCHED or event not in ("open", "os.listdir", "os.scandir", "sqlite3.connect"):
        return
    raw = args[0] if args else None
    if not isinstance(raw, (str, bytes, os.PathLike)):
        return
    try:
        real = os.path.realpath(raw)
    except (OSError, ValueError):
        return
    if any(real == w or real.startswith(w + os.sep) for w in _WATCHED):
        _SEEN.append(f"{event} {real}")


@pytest.fixture(scope="module", autouse=True)
def _install_audit_hook() -> None:
    sys.addaudithook(_audit)  # cannot be removed; it records only while a test watches


COMMANDS = {
    "stats": lambda db, out: stats_main(["--db", db]),
    "compact": lambda db, out: compact_main(["--db", db]),
    "migrate": lambda db, out: migrate_main(["--db", db]),
    "require": lambda db, out: require_main(["--sidegraph", "0.1.0", "--db", db]),
    "viz": lambda db, out: viz_main(["--db", db]),
    "export-okf": lambda db, out: export_okf_main(["--db", db, "--out", out]),
    "export-adr": lambda db, out: adr_cli.main(["--db", db, "--out", out]),
    "init": lambda db, out: init_main(["--db", db]),
}


@pytest.mark.parametrize("layout", ["dangling", "empty", "store"])
@pytest.mark.parametrize("command", list(COMMANDS))
def test_every_command_names_the_link_and_reads_nothing(tmp_path, capsys, command, layout):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    target = tmp_path / "shared"
    if layout == "empty":
        target.mkdir()
    elif layout == "store":
        Store(target).close()
    link = repo / ".sidegraph"
    link.symlink_to("../shared", target_is_directory=True)
    before = sorted(p.name for p in target.iterdir()) if target.exists() else None
    capsys.readouterr()
    _SEEN.clear()
    _WATCHED[:] = [str(target.resolve())]
    try:
        rc = COMMANDS[command](str(link), str(tmp_path / "out"))
    finally:
        _WATCHED.clear()
    err = capsys.readouterr().err
    assert rc == 1, err
    assert REASON in err
    assert _SEEN == []
    assert (sorted(p.name for p in target.iterdir()) if target.exists() else None) == before
