"""Shared setup for the moved-rung adoption tests: real git repositories, a graph on disk, a
store with one entity, and ``rebind_entity`` run over them.
# see design/superpowers/specs/2026-10-01-moved-rung-false-adoption-design.md
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from sidegraph.engine.reader import GraphifyReader
from sidegraph.gitenv import git_env
from sidegraph.schema import Descriptor, Entity
from sidegraph.store import Store
from sidegraph.sync import rebind_entity

_GIT_CONFIG = {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}


def _git(cwd: Path, *args: str, when: str | None = None) -> str:
    """``when`` pins author and committer dates, for tests that depend on commit order."""
    env = {**git_env(), **_GIT_CONFIG}
    if when is not None:
        env |= {"GIT_AUTHOR_DATE": when, "GIT_COMMITTER_DATE": when}
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, env=env)
    assert result.returncode == 0, f"git {args} failed: {result.stderr}"
    return result.stdout


def _configure(repo: Path) -> None:
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")


def _repo(root: Path, name: str = "repo") -> Path:
    """A fresh repository whose scratch directory ``.sg/`` (graph, store) is never tracked."""
    repo = root / name
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _configure(repo)
    (repo / ".git" / "info" / "exclude").write_text(".sg/\n")
    return repo


def _write(repo: Path, rel: str, text: str = "x\n") -> None:
    target = repo / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)


def _commit(repo: Path, message: str, when: str | None = None) -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message, when=when)


def _graph(repo: Path, nodes: list[tuple[str, str, str]]) -> GraphifyReader:
    """A graph of ``(node id, label, source file)`` rows, written into the repo's ``.sg/``."""
    rows = [
        {
            "id": node_id,
            "label": label,
            "norm_label": label,
            "file_type": "document" if file.endswith(".md") else "code",
            "source_file": file,
            "community": 1,
        }
        for node_id, label, file in nodes
    ]
    path = repo / ".sg" / "graph.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps({"built_at_commit": "vB", "nodes": rows, "links": []}))
    return GraphifyReader(path)


def _store(repo: Path, name: str, old_path: str) -> tuple[Store, Entity]:
    store = Store(repo / ".sg" / "t.db")
    entity = store.upsert_entity(
        Entity(
            canonical_name=name,
            descriptor=Descriptor(name=name, file_path=old_path),
            last_seen_node_id="old",
            last_seen_graph_version="vA",
        )
    )
    return store, entity


def _rebind(repo: Path, nodes, name: str, old_path: str, *, canonical_writes: bool = True):
    reader = _graph(repo, nodes)
    store, entity = _store(repo, name, old_path)
    outcome = rebind_entity(
        entity, store, reader, repo_root=repo, canonical_writes=canonical_writes
    )
    return outcome, store.get_entity(entity.entity_id)


def _assert_refused(outcome, kept, old_path: str) -> None:
    assert outcome.status == "orphaned"
    assert kept.descriptor.file_path == old_path  # the canonical descriptor is untouched
    assert kept.last_seen_node_id == "old"


def _sign_commits_with_ssh(repo: Path) -> None:
    """Sign every commit with a throwaway ssh key and make ``git log`` print the signature
    check. Skips the test when this machine cannot sign."""
    key = repo.parent / "signing_key"
    made = subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "test", "-f", str(key)],
        capture_output=True,
    )
    if made.returncode != 0:
        pytest.skip("ssh-keygen cannot make a signing key here")
    signers = repo.parent / "allowed_signers"
    signers.write_text(f"test@example.com {key.with_suffix('.pub').read_text()}")
    for name, value in [
        ("gpg.format", "ssh"),
        ("user.signingkey", str(key)),
        ("gpg.ssh.allowedSignersFile", str(signers)),
        ("commit.gpgsign", "true"),
        ("log.showSignature", "true"),
    ]:
        _git(repo, "config", name, value)
    probe = subprocess.run(
        ["git", "commit", "-q", "--allow-empty", "-m", "probe"],
        cwd=repo,
        capture_output=True,
        env={**git_env(), **_GIT_CONFIG},
    )
    if probe.returncode != 0:
        pytest.skip("git cannot sign a commit with ssh here")
