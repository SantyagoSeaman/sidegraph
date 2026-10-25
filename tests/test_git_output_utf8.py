"""Git's UTF-8 output survives a non-UTF-8 platform default.

``subprocess.run(..., text=True)`` decodes with the ANSI code page on Windows, so a Cyrillic
author name, repository path or record body came back as mojibake (or raised for bytes such as
0x90). On POSIX the fixture below makes the locale encoding cp1252, the narrowest hook that
``subprocess`` consults (``locale.getencoding``); on Windows the real default applies.
"""

from __future__ import annotations

import locale
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from sidegraph import doctor, githooks, gitio, verify
from sidegraph.capture import _derive_initiative
from sidegraph.engine.reader import GraphifyReader
from sidegraph.store import _ratifier_identity

NAME = "Алексей Сидоров"


@pytest.fixture(autouse=True)
def ansi_default(monkeypatch: pytest.MonkeyPatch) -> None:
    if sys.platform != "win32":
        monkeypatch.setattr(locale, "getencoding", lambda: "cp1252")


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "commit.gpgsign=false", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        env={**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull},
    )


posix_only = pytest.mark.skipif(sys.platform == "win32", reason="the fake git is a sh script")


def _fake_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stdout: bytes) -> None:
    """Put a ``git`` first on PATH that prints ``stdout`` verbatim and exits 0."""
    bindir = tmp_path / "fakebin"
    bindir.mkdir(exist_ok=True)
    (bindir / "out").write_bytes(stdout)
    script = bindir / "git"
    script.write_text('#!/bin/sh\nexec cat "$(dirname "$0")/out"\n')
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")


def _repo(tmp_path: Path, name: str = "repo") -> Path:
    repo = tmp_path / name
    repo.mkdir()
    _git(repo, "init", "-q")
    return repo


def _commit(repo: Path, *files: str, body: str = "x\n") -> str:
    for f in files:
        (repo / f).write_text(body, encoding="utf-8")
    _git(repo, "add", "--", *files)
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "c")
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()


def test_the_ratifier_identity_is_the_git_user_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)  # no repo-local user.name from the checkout can win
    cfg = tmp_path / "gitconfig"
    cfg.write_bytes(f"[user]\n\tname = {NAME}\n".encode())
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(cfg))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
    assert _ratifier_identity(None) == NAME


def test_the_reader_repo_root_keeps_a_non_ascii_path(tmp_path: Path) -> None:
    root = tmp_path / "проект é"
    (root / "graphify-out").mkdir(parents=True)
    _git(root, "init", "-q")
    graph = root / "graphify-out" / "graph.json"
    graph.write_text('{"nodes": [], "links": []}', encoding="utf-8")
    assert GraphifyReader(graph).repo_root() == root.resolve()


def test_git_show_returns_committed_json_exactly(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    oid = _commit(repo, "r.json", body='{"choice": "Выбрали Н и А — не иначе"}\n')
    assert verify._git_show_json(repo, oid, "r.json") == {"choice": "Выбрали Н и А — не иначе"}


def test_git_show_rejects_bytes_that_are_not_utf8(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "r.json").write_bytes(b'{"choice": "caf\xe9"}\n')
    _git(repo, "add", "r.json")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "c")
    oid = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()
    with pytest.raises(verify._UnparsableContent):
        verify._git_show_json(repo, oid, "r.json")


# -- path output stays lossless ------------------------------------------------------------


@posix_only
def test_a_repo_path_with_non_utf8_bytes_round_trips(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = os.fsencode(tmp_path) + b"/caf\xe9"
    _fake_git(tmp_path, monkeypatch, raw + b"\n")
    assert os.fsencode(verify._run_git(["x"], tmp_path).stdout.strip()) == raw
    assert os.fsencode(verify._find_repo_root(tmp_path)) == raw
    # repo_info: top, git-dir, common dir, hooks dir, then the empty tail
    _fake_git(tmp_path, monkeypatch, b"\n".join([raw, raw, raw, raw + b"/hooks", b""]))
    info = githooks.repo_info(tmp_path)
    assert info is not None
    assert os.fsencode(info.top) == raw
    assert os.fsencode(info.hooks_dir) == raw + b"/hooks"


# -- strict decoders: a garbled value is None, never stored ----------------------------------


@posix_only
def test_a_garbled_ratifier_name_is_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    _fake_git(tmp_path, monkeypatch, b"caf\xe9\n")
    assert _ratifier_identity(None) is None


@posix_only
def test_a_garbled_reader_repo_root_is_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_git(tmp_path, monkeypatch, b"/tmp/caf\xe9\n")
    graph = tmp_path / "graphify-out" / "graph.json"
    graph.parent.mkdir()
    graph.write_text('{"nodes": [], "links": []}', encoding="utf-8")
    assert GraphifyReader(graph).repo_root() is None


@posix_only
def test_a_garbled_branch_name_binds_no_initiative(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_git(tmp_path, monkeypatch, b"feature/caf\xe9\n")
    store = SimpleNamespace(path=tmp_path)
    assert _derive_initiative(store) is None  # type: ignore[arg-type]


@posix_only
def test_a_clean_branch_name_still_binds_the_initiative(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_git(tmp_path, monkeypatch, "feature/кафе\n".encode())
    assert _derive_initiative(SimpleNamespace(path=tmp_path)) == "feature-кафе"  # type: ignore[arg-type]


# -- changed file names come back exactly (git would C-quote them) ---------------------------

NAMES = ["файл с пробелом.py", " lead.py", 'q"uote.py', "tab\there.py"]


@pytest.mark.skipif(sys.platform == "win32", reason="tab and quote are not valid file names")
def test_doctor_reads_changed_names_exactly(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    base = _commit(repo, "base.py")
    _commit(repo, *NAMES)
    assert sorted(doctor._git_diff_name_only(repo, base, NAMES)) == sorted(NAMES)


def test_doctor_reads_a_cyrillic_name_with_a_space(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    base = _commit(repo, "base.py")
    _commit(repo, "файл с пробелом.py")
    assert doctor._git_diff_name_only(repo, base, ["файл с пробелом.py"]) == ["файл с пробелом.py"]


@pytest.mark.skipif(sys.platform == "win32", reason="tab and quote are not valid file names")
def test_staged_files_come_back_exactly_and_deduped_in_order(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    for f in NAMES:
        (repo / f).write_text("x", encoding="utf-8")
    _git(repo, "add", "--", *NAMES)
    assert gitio.staged_files(repo) == sorted(NAMES, key=lambda n: n.encode())


def test_staged_files_keep_a_cyrillic_name_with_a_space(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "файл с пробелом.py").write_text("x", encoding="utf-8")
    _git(repo, "add", "--", "файл с пробелом.py")
    assert gitio.staged_files(repo) == ["файл с пробелом.py"]


def test_reader_changed_files_keep_a_cyrillic_name_with_a_space(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _commit(repo, "base.py")
    _commit(repo, "файл с пробелом.py", "ещё один.py")
    graph = repo / "graphify-out" / "graph.json"
    graph.parent.mkdir()
    graph.write_text('{"nodes": [], "links": []}', encoding="utf-8")
    reader = GraphifyReader(graph)
    assert sorted(reader.changed_files()) == sorted(["файл с пробелом.py", "ещё один.py"])
