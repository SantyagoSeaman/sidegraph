"""The derived initiative names the store's repository branch, not the process cwd's.

Spec: design/superpowers/specs/2026-09-30-initiative-from-store-repo-design.md (D1, T1-T3).

This file deliberately has NO autouse patch of ``_derive_initiative``: the tests build two
hermetic git repos and check which one the Tier-0 ``initiative:`` anchor follows.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from sidegraph.capture import propose
from sidegraph.store import Store


@pytest.fixture(autouse=True)
def _hermetic_git(monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", "/dev/null")


def _repo(path: Path, branch: str) -> Path:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", branch], cwd=path, check=True)
    return path


def _draft() -> dict:
    return dict(title="A lesson", kind="gotcha", context="ctx", choice="do x", anchors=[])


def _initiatives(store: Store) -> list[str]:
    rows = store._conn.execute(
        "SELECT canonical_name FROM entities WHERE canonical_name LIKE 'initiative:%'"
    ).fetchall()
    return sorted(r[0] for r in rows)


@pytest.fixture
def cwd_repo(tmp_path, monkeypatch) -> Path:
    """Repo B on a feature branch, made the process cwd."""
    b = _repo(tmp_path / "b", "feature/cwd-side")
    monkeypatch.chdir(b)
    return b


def test_initiative_follows_the_store_repo_not_the_cwd(tmp_path, cwd_repo):
    a = _repo(tmp_path / "a", "feature/store-side")
    store = Store(a / ".sidegraph")
    propose([_draft()], store, None)
    assert _initiatives(store) == ["initiative:feature-store-side"]


def test_store_outside_any_repo_binds_no_initiative(tmp_path, cwd_repo):
    bare = tmp_path / "bare"
    bare.mkdir()
    store = Store(bare / ".sidegraph")
    propose([_draft()], store, None)
    assert _initiatives(store) == []


def test_store_repo_on_main_binds_no_initiative(tmp_path, cwd_repo):
    a = _repo(tmp_path / "a", "main")
    store = Store(a / ".sidegraph")
    propose([_draft()], store, None)
    assert _initiatives(store) == []


# --- rev 3 (spec §6): logical store repo (D4) and a clean git environment (D5) ---


def _link(project: Path, target: Path) -> Store:
    """``project/.sidegraph`` -> ``target``, the supported symlinked-store-root shape."""
    (project / ".sidegraph").symlink_to(target, target_is_directory=True)
    return Store(project / ".sidegraph")


def test_symlinked_store_follows_the_project_holding_the_link(tmp_path, monkeypatch):
    p = _repo(tmp_path / "p", "feature/billing")
    v = _repo(tmp_path / "v", "feature/vault-only")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    store = _link(p, v)
    propose([_draft()], store, None)
    assert _initiatives(store) == ["initiative:feature-billing"]


def test_symlinked_store_into_a_plain_directory_still_follows_the_project(tmp_path, cwd_repo):
    p = _repo(tmp_path / "p", "feature/billing")
    plain = tmp_path / "plain"
    plain.mkdir()
    store = _link(p, plain)
    propose([_draft()], store, None)
    assert _initiatives(store) == ["initiative:feature-billing"]


def test_symlinked_store_outside_git_falls_back_to_the_target_repo(tmp_path, cwd_repo):
    p = tmp_path / "p"
    p.mkdir()
    v = _repo(tmp_path / "v", "feature/vault-only")
    store = _link(p, v)
    propose([_draft()], store, None)
    assert _initiatives(store) == ["initiative:feature-vault-only"]


def test_inherited_git_dir_does_not_select_the_repository(tmp_path, cwd_repo, monkeypatch):
    a = _repo(tmp_path / "a", "feature/store-side")
    monkeypatch.setenv("GIT_DIR", str(cwd_repo / ".git"))
    store = Store(a / ".sidegraph")
    propose([_draft()], store, None)
    assert _initiatives(store) == ["initiative:feature-store-side"]


def test_the_ceiling_survives_the_env_scrub(tmp_path, monkeypatch):
    """A store below an enclosing feature-branch repo derives nothing when the ceiling is
    the enclosing repo's root: scrubbing repository variables must not drop it."""
    enclosing = _repo(tmp_path / "enclosing", "feature/enclosing")
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(enclosing))
    sub = enclosing / "sub"
    sub.mkdir()
    store = Store(sub / ".sidegraph")
    propose([_draft()], store, None)
    assert _initiatives(store) == []


def test_detached_parent_head_is_final_no_fallback(tmp_path, cwd_repo):
    """D4.3: a zero exit with empty output (detached HEAD) gives None, never a retry that
    would read the inner repository the store directory happens to be."""
    h = _repo(tmp_path / "h", "main")
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com",
         "commit", "-q", "--allow-empty", "-m", "c"],
        cwd=h, check=True,
    )  # fmt: skip
    subprocess.run(["git", "checkout", "-q", "--detach"], cwd=h, check=True)
    inner = _repo(h / ".sidegraph", "feature/inner")
    store = Store(inner)
    propose([_draft()], store, None)
    assert _initiatives(store) == []


def test_config_injected_safe_directory_survives_the_env_scrub(tmp_path, cwd_repo, monkeypatch):
    """Containers and CI pass ``safe.directory`` through GIT_CONFIG_COUNT/KEY/VALUE; that
    selects no repository, so the scrub must keep it."""
    a = _repo(tmp_path / "a", "feature/store-side")
    monkeypatch.setenv("GIT_TEST_ASSUME_DIFFERENT_OWNER", "1")
    raw = subprocess.run(["git", "branch", "--show-current"], cwd=a, capture_output=True, text=True)
    if raw.returncode == 0:
        pytest.skip("this git ignores GIT_TEST_ASSUME_DIFFERENT_OWNER")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "safe.directory")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "*")
    store = Store(a / ".sidegraph")
    propose([_draft()], store, None)
    assert _initiatives(store) == ["initiative:feature-store-side"]


def _commit(repo: Path, message: str) -> str:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com",
         "commit", "-q", "--allow-empty", "-m", message],
        cwd=repo, check=True,
    )  # fmt: skip
    out = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    )
    return out.stdout.strip()


def test_captured_commit_ignores_an_inherited_git_dir(tmp_path, cwd_repo, monkeypatch):
    """Spec §6 (D5): the commit stamp names the store's repository, not the one an inherited
    ``GIT_DIR`` selects."""
    a = _repo(tmp_path / "a", "feature/store-side")
    head_a = _commit(a, "in A")
    head_b = _commit(cwd_repo, "in B")
    assert head_a != head_b
    monkeypatch.setenv("GIT_DIR", str(cwd_repo / ".git"))
    store = Store(a / ".sidegraph")
    [result] = propose([_draft()], store, None)
    assert store.get_decision(result.decision_id).provenance.commit == head_a
