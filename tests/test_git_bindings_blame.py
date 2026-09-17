"""П2 -- ``sidegraph-blame`` (design/superpowers/specs/2026-08-07-git-bindings-design.md
§2). Ledger rows G5-G8 + the blame side of G9.

Fixtures are REAL temp git repos built by the tests -- never a mocked git.
"""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime

from sidegraph.cli import blame_main
from sidegraph.schema import Decision, DecisionKind, DecisionStatus, Fact, Provenance
from sidegraph.store import Store


def _git(args: list[str], cwd) -> subprocess.CompletedProcess:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    assert result.returncode == 0, f"git {args} failed: {result.stderr}"
    return result


def _init_repo(root):
    repo = root / "repo"
    repo.mkdir()
    _git(["init", "-q"], repo)
    _git(["config", "user.email", "t@example.com"], repo)
    _git(["config", "user.name", "T"], repo)
    return repo


def _head(repo) -> str:
    return _git(["rev-parse", "HEAD"], repo).stdout.strip()


def _commit_with_message(repo, name: str, content: str, message: str) -> str:
    (repo / name).write_text(content)
    _git(["add", "-A"], repo)
    result = subprocess.run(
        ["git", "commit", "-q", "-F", "-"], cwd=repo, input=message, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    return _head(repo)


def _decision(store: Store, title: str, *, commit: str | None = None, **kw) -> Decision:
    d = Decision(
        title=title,
        kind=DecisionKind.ADR,
        status=DecisionStatus.ACCEPTED,
        context="c",
        choice="ch",
        valid_from=datetime.now(UTC),
        provenance=Provenance(source="human", commit=commit),
        **kw,
    )
    store.add_decision(d)
    return d


def _fact(store: Store, statement: str, *, commit: str | None = None) -> Fact:
    f = Fact(
        statement=statement,
        source="src",
        valid_from=datetime.now(UTC),
        provenance=Provenance(source="human", commit=commit),
    )
    store.add_fact(f)
    return f


def _run(args, monkeypatch, repo) -> tuple[int, str]:
    import io
    import sys

    monkeypatch.chdir(repo)
    buf = io.StringIO()
    old_stdout = sys.stdout
    sys.stdout = buf
    try:
        rc = blame_main(args)
    finally:
        sys.stdout = old_stdout
    return rc, buf.getvalue()


# -- G5: blame trailer join ---------------------------------------------------------------


def test_trailer_join_resolves_real_ulid_not_placeholder(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path)
    store = Store(repo / ".sidegraph")
    d = _decision(store, "implemented by this commit")
    store.close()

    message = f"Implement thing\n\nSidegraph-Decision: {d.id}\n"
    _commit_with_message(repo, "f.txt", "line1\n", message)

    rc, out = _run(["f.txt"], monkeypatch, repo)
    assert rc == 0
    assert d.id in out
    assert "implemented by this commit" in out
    # the valuesonly-typo bug echoes the format string itself back verbatim -- must
    # never appear anywhere in the output.
    assert "valuesonly" not in out
    assert "%(trailers" not in out


def test_trailer_join_tolerates_the_rendered_label_left_in_after_uncommenting(
    tmp_path, monkeypatch
):
    """End-to-end smoke regression: the hook's own rendered candidate line is
    ``Sidegraph-Decision: <id> — <label>`` (§1 step 2) -- a straight "uncomment the
    line" (the documented gesture) leaves the label in as part of the trailer's literal
    value. The join must still resolve, not treat "<id> — <label>" as one opaque
    unresolved string."""
    repo = _init_repo(tmp_path)
    store = Store(repo / ".sidegraph")
    d = _decision(store, "retry idempotency keys via httpx event hooks")
    store.close()

    message = f"Add new_fn\n\nSidegraph-Decision: {d.id} — {d.title}\n"
    _commit_with_message(repo, "f.txt", "line1\n", message)

    rc, out = _run(["f.txt"], monkeypatch, repo)
    assert rc == 0
    assert d.id in out
    assert "unresolved" not in out


def test_trailer_join_unknown_ulid_listed_as_unresolved(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path)
    store = Store(repo / ".sidegraph")
    store.close()

    fake_id = "01FAKEDOESNOTEXIST00000000"
    message = f"Implement thing\n\nSidegraph-Decision: {fake_id}\n"
    _commit_with_message(repo, "f.txt", "line1\n", message)

    rc, out = _run(["f.txt"], monkeypatch, repo)
    assert rc == 0
    assert fake_id in out
    assert "unresolved" in out


# -- G6: blame provenance join (decisions AND facts) ---------------------------------------


def test_provenance_join_resolves_decision_and_fact_with_no_trailer(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path)
    head = _commit_with_message(repo, "f.txt", "line1\n", "plain commit, no trailer\n")
    store = Store(repo / ".sidegraph")
    d = _decision(store, "provenance-joined decision", commit=head)
    f = _fact(store, "provenance-joined fact", commit=head)
    store.close()

    rc, out = _run(["f.txt"], monkeypatch, repo)
    assert rc == 0
    assert d.id in out
    assert "provenance-joined decision" in out
    assert f.id in out
    assert "provenance-joined fact" in out


# -- G7: superseded record via old sha prints superseded_by --------------------------------


def test_superseded_record_prints_superseded_by(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path)
    head = _commit_with_message(repo, "f.txt", "line1\n", "original\n")
    store = Store(repo / ".sidegraph")
    old = _decision(store, "old decision", commit=head)
    new = _decision(store, "new decision", supersedes=old.id)
    store.close()

    rc, out = _run(["f.txt"], monkeypatch, repo)
    assert rc == 0
    assert old.id in out
    assert new.id in out  # superseded_by pointer


# -- G8: --json golden + the 50-row/6000-char cap -------------------------------------------


def test_json_output_shape(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path)
    head = _commit_with_message(repo, "f.txt", "line1\n", "plain\n")
    store = Store(repo / ".sidegraph")
    d = _decision(store, "json decision", commit=head)
    store.close()

    rc, out = _run(["f.txt", "--json"], monkeypatch, repo)
    assert rc == 0
    payload = json.loads(out)
    assert payload["file"] == "f.txt"
    assert payload["omitted"] == 0
    assert payload["cap"] is None
    [hunk] = payload["hunks"]
    assert hunk["sha"]
    [rec] = hunk["records"]
    assert rec["id"] == d.id
    assert rec["resolved"] is True


def test_cap_declared_when_row_cap_hit(tmp_path, monkeypatch):
    """55 distinct single-line commits -> 55 hunk rows, over the 50-row cap -- the last
    line (human output) / the `cap` field (--json) must declare it, never silently
    truncate."""
    repo = _init_repo(tmp_path)
    lines = []
    for i in range(55):
        lines.append(f"line{i}")
        (repo / "f.txt").write_text("\n".join(lines) + "\n")
        _git(["add", "-A"], repo)
        _git(["commit", "-q", "-m", f"line {i}"], repo)

    rc, out = _run(["f.txt", "--json"], monkeypatch, repo)
    assert rc == 0
    payload = json.loads(out)
    assert len(payload["hunks"]) <= 50
    assert payload["omitted"] > 0
    assert payload["cap"] == {"rows": 50, "chars": 6000}

    rc2, out2 = _run(["f.txt"], monkeypatch, repo)
    assert rc2 == 0
    assert "omitted" in out2
    assert "50" in out2


# -- G9: gitio failures -> clean CLI error, exit 1 -------------------------------------------


def test_not_a_repo_is_a_clean_cli_error(tmp_path, monkeypatch):
    not_a_repo = tmp_path / "not_a_repo"
    not_a_repo.mkdir()
    (not_a_repo / "f.txt").write_text("x")
    rc, _ = _run(["f.txt"], monkeypatch, not_a_repo)
    assert rc == 1


def test_unknown_file_is_a_clean_cli_error(tmp_path, monkeypatch):
    repo = _init_repo(tmp_path)
    _commit_with_message(repo, "f.txt", "line1\n", "initial\n")
    rc, _ = _run(["does_not_exist.txt"], monkeypatch, repo)
    assert rc == 1
