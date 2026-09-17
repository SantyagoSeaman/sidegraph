"""П0 (git-bindings design, design/superpowers/specs/2026-08-07-git-bindings-design.md):
``_capture_commit`` closes the fact/decision mirror at the four sites the design's
Blocker-1 measured as missing it -- ``_propose_fact_one`` (standalone propose_facts),
``_add_decision_impl``, ``_add_fact_impl``, ``_supersede_fact_impl``. Mechanical twin of
the I1 session-id wave (commit 4b1c927) -- same git-repo-vs-cwd fixture convention as
``test_capture_propose.py``'s D1 group and ``test_server_supersede.py``'s D6 group.

G0 in the git-bindings test ledger: red against unfixed code (today ``None`` at all four
sites) + the outside-a-repo twin (declared exception: still ``None``).
"""

from __future__ import annotations

import subprocess

from sidegraph.capture import propose_facts
from sidegraph.server import _add_decision_impl, _add_fact_impl, _supersede_fact_impl
from sidegraph.store import Store


def _git_repo(root):
    repo = root / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True)
    (repo / "f.txt").write_text("x")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=repo, check=True)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()
    return repo, head


# -- _propose_fact_one (standalone propose_facts) ------------------------------------------


def test_propose_facts_standalone_stamps_commit_inside_a_repo(tmp_path):
    repo, head = _git_repo(tmp_path)
    store = Store(repo / ".sidegraph")
    # A standalone fact needs an anchor or a live supports id (reachability gate, D8) --
    # give it an anchor so the write actually lands, mirroring test_capture_facts.py's
    # convention.
    [res] = propose_facts(
        [
            {
                "statement": "httpx has no built-in retry",
                "source": "httpx docs",
                "anchors": [{"name": "client.py", "file_path": "src/client.py"}],
            }
        ],
        store,
        None,
    )
    assert res.status == "written"
    fact = store.get_fact(res.fact_id)
    assert fact.provenance.commit == head


def test_propose_facts_standalone_commit_none_outside_a_repo(tmp_path):
    """Declared exception (round 2 scope clause / D1 precedent): best-effort, never raises."""
    store = Store(tmp_path / "t.db")
    [res] = propose_facts(
        [
            {
                "statement": "httpx has no built-in retry",
                "source": "httpx docs",
                "anchors": [{"name": "client.py", "file_path": "src/client.py"}],
            }
        ],
        store,
        None,
    )
    assert res.status == "written"
    fact = store.get_fact(res.fact_id)
    assert fact.provenance.commit is None


# -- _add_decision_impl ----------------------------------------------------------------------


def test_add_decision_stamps_commit_inside_a_repo(tmp_path):
    repo, head = _git_repo(tmp_path)
    store = Store(repo / ".sidegraph")
    out = _add_decision_impl(store, None, title="t", kind="adr", context="c", choice="ch")
    d = store.get_decision(out["id"])
    assert d.provenance.commit == head


def test_add_decision_commit_none_outside_a_repo(tmp_path):
    store = Store(tmp_path / "t.db")
    out = _add_decision_impl(store, None, title="t", kind="adr", context="c", choice="ch")
    d = store.get_decision(out["id"])
    assert d.provenance.commit is None


# -- _add_fact_impl ---------------------------------------------------------------------------


def test_add_fact_stamps_commit_inside_a_repo(tmp_path):
    repo, head = _git_repo(tmp_path)
    store = Store(repo / ".sidegraph")
    out = _add_fact_impl(
        store,
        None,
        statement="httpx has no built-in retry",
        source="httpx docs",
        anchors=[{"name": "client.py", "file_path": "src/client.py"}],
    )
    fact = store.get_fact(out["id"])
    assert fact.provenance.commit == head


def test_add_fact_commit_none_outside_a_repo(tmp_path):
    store = Store(tmp_path / "t.db")
    out = _add_fact_impl(
        store,
        None,
        statement="httpx has no built-in retry",
        source="httpx docs",
        anchors=[{"name": "client.py", "file_path": "src/client.py"}],
    )
    fact = store.get_fact(out["id"])
    assert fact.provenance.commit is None


# -- _supersede_fact_impl ----------------------------------------------------------------------


def test_supersede_fact_stamps_commit_inside_a_repo(tmp_path):
    repo, head = _git_repo(tmp_path)
    store = Store(repo / ".sidegraph")
    old = _add_fact_impl(
        store,
        None,
        statement="s1",
        source="src1",
        anchors=[{"name": "client.py", "file_path": "src/client.py"}],
    )
    out = _supersede_fact_impl(store, None, old["id"], statement="s2", source="src2")
    fact = store.get_fact(out["id"])
    assert fact.provenance.commit == head


def test_supersede_fact_commit_none_outside_a_repo(tmp_path):
    store = Store(tmp_path / "t.db")
    old = _add_fact_impl(
        store,
        None,
        statement="s1",
        source="src1",
        anchors=[{"name": "client.py", "file_path": "src/client.py"}],
    )
    out = _supersede_fact_impl(store, None, old["id"], statement="s2", source="src2")
    fact = store.get_fact(out["id"])
    assert fact.provenance.commit is None
