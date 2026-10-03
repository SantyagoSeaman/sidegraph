"""Write tools carry a commit hint: a record written into a store that lives in a git
repository is stranded until it is committed, so the result says so.

The hint goes on success results only, through each tool's testable core, and only when the
store sits inside a repository. ``ratify`` and ``sync_anchors`` carry none (their results are
keyed by id, or write only on a moved rung).
see design/superpowers/specs/2026-10-02-stranded-store-writes-design.md (D3; T15)
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from sidegraph.server import (
    _add_anchors_impl,
    _add_decision_impl,
    _add_domain_impl,
    _add_fact_impl,
    _propose_decisions_impl,
    _propose_domains_impl,
    _supersede_decision_impl,
    _supersede_domain_impl,
    _supersede_fact_impl,
)
from sidegraph.store import Store

HINT = (
    "Commit {prefix} in the same change as the work that produced this record, so it reaches "
    "other checkouts and teammates."
)

DICT_TOOLS = [
    "add_decision",
    "supersede_decision",
    "add_fact",
    "supersede_fact",
    "add_anchors",
    "add_domain",
    "supersede_domain",
]


def git_store(tmp_path: Path, nested: str = "") -> Store:
    """A store inside a real git repository, at ``<repo>/<nested>/.sidegraph``."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    parent = repo / nested if nested else repo
    parent.mkdir(parents=True, exist_ok=True)
    return Store(parent / ".sidegraph")


def plain_store(tmp_path: Path) -> Store:
    """A store with no repository above it (``tmp_path`` is outside git)."""
    parent = tmp_path / "plain"
    parent.mkdir()
    return Store(parent / ".sidegraph")


def run_dict_tools(store: Store) -> dict[str, dict]:
    anchors = [{"name": "x", "file_path": "x.py"}]
    decision = _add_decision_impl(store, None, "t", "adr", "c", "ch")
    fact = _add_fact_impl(store, None, statement="s", source="src", anchors=anchors)
    domain = _add_domain_impl(store, None, slug="pay", title="Pay", summary="s.")
    return {
        "add_decision": decision,
        "supersede_decision": _supersede_decision_impl(
            store, None, decision["id"], "t2", "adr", "c2", "ch2"
        ),
        "add_fact": fact,
        "supersede_fact": _supersede_fact_impl(
            store, None, fact["id"], statement="s2", source="src2", anchors=anchors
        ),
        "add_anchors": _add_anchors_impl(store, None, decision["id"], [{"name": "y"}]),
        "add_domain": domain,
        "supersede_domain": _supersede_domain_impl(store, None, "pay", "pay-v2", "Pay v2", "s."),
    }


def test_t15_every_dict_write_tool_has_the_hint_inside_a_git_repository(tmp_path):
    results = run_dict_tools(git_store(tmp_path))

    assert {name: r.get("commit_hint") for name, r in results.items()} == {
        name: HINT.format(prefix=".sidegraph/") for name in DICT_TOOLS
    }


def test_t15_the_hint_names_the_store_relative_to_the_repository_root(tmp_path):
    store = git_store(tmp_path, nested="apps/web")

    result = _add_decision_impl(store, None, "t", "adr", "c", "ch")

    assert result["commit_hint"] == HINT.format(prefix="apps/web/.sidegraph/")


@pytest.mark.parametrize("name", DICT_TOOLS)
def test_t15_no_dict_write_tool_has_the_hint_outside_a_git_repository(tmp_path, name):
    assert "commit_hint" not in run_dict_tools(plain_store(tmp_path))[name]


def test_t15_add_anchors_error_dict_has_no_hint(tmp_path):
    store = git_store(tmp_path)

    assert _add_anchors_impl(store, None, "01NOPE", [{"name": "x"}]) == {
        "error": "unknown record '01NOPE'"
    }


def propose_decisions(store: Store) -> list[dict]:
    """``[rejected, written]`` decisions, then ``[deduped, written]`` standalone facts."""
    limit = {"statement": "limit is 100", "source": "docs", "anchors": [{"name": "Bar"}]}
    _propose_decisions_impl(store, None, [], facts=[limit])
    return _propose_decisions_impl(
        store,
        None,
        [
            {"title": "no kind"},
            {
                "title": "Retries live in the adapter",
                "kind": "lesson",
                "context": "c",
                "choice": "ch",
            },
        ],
        facts=[limit, dict(limit, statement="another limit", anchors=[{"name": "Baz"}])],
    )


def test_t15_propose_decisions_hints_only_the_written_elements(tmp_path):
    results = propose_decisions(git_store(tmp_path))

    assert [(r["status"], r.get("commit_hint")) for r in results] == [
        ("rejected", None),
        ("written", HINT.format(prefix=".sidegraph/")),
        ("deduped", None),
        ("written", HINT.format(prefix=".sidegraph/")),
    ]


def test_t15_propose_decisions_has_no_hint_outside_a_git_repository(tmp_path):
    results = propose_decisions(plain_store(tmp_path))

    assert [r["status"] for r in results] == ["rejected", "written", "deduped", "written"]
    assert all("commit_hint" not in r for r in results)


def test_t15_propose_domains_hints_only_the_proposed_elements(tmp_path):
    store = git_store(tmp_path)
    drafts = [
        {"slug": "one", "title": "One", "summary": "First area."},
        {"slug": "one", "title": "One again", "summary": "A taken slug."},
        {"title": "no slug"},
    ]

    results = _propose_domains_impl(store, None, drafts)

    assert [(r["status"], r.get("commit_hint")) for r in results] == [
        ("proposed", HINT.format(prefix=".sidegraph/")),
        ("skipped", None),
        ("rejected", None),
    ]


def test_t15_propose_domains_has_no_hint_outside_a_git_repository(tmp_path):
    results = _propose_domains_impl(
        plain_store(tmp_path), None, [{"slug": "one", "title": "One", "summary": "First area."}]
    )

    assert results[0]["status"] == "proposed" and "commit_hint" not in results[0]


# -- a store that is a symlink: the hint only when git would commit the records -------------------


def linked_store(tmp_path: Path, target: Path) -> Store:
    """A store at ``<repo>/.sidegraph`` that is a symlink to ``target`` (created if absent)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    target.mkdir(parents=True, exist_ok=True)
    (repo / ".sidegraph").symlink_to(target, target_is_directory=True)
    return Store(repo / ".sidegraph")


def test_a_store_symlinked_outside_the_repository_has_no_hint(tmp_path):
    """Git commits the link, not the records in its target, so "commit .sidegraph/" is false."""
    store = linked_store(tmp_path, tmp_path / "shared" / "store")

    results = run_dict_tools(store)

    assert all("commit_hint" not in result for result in results.values())
    assert all("commit_hint" not in r for r in propose_decisions(store))


def test_a_store_symlinked_into_the_repository_keeps_the_hint(tmp_path):
    """The target is tracked by the same repository, so committing it commits the records."""
    store = linked_store(tmp_path, tmp_path / "repo" / "shared" / "store")

    results = run_dict_tools(store)

    assert {name: r.get("commit_hint") for name, r in results.items()} == {
        name: HINT.format(prefix=".sidegraph/") for name in DICT_TOOLS
    }


def test_a_plain_store_beside_a_symlinked_one_keeps_the_hint(tmp_path):
    """The check is about this store's own target, not about symlinks anywhere in the tree."""
    store = git_store(tmp_path, nested="apps/web")
    (tmp_path / "elsewhere").mkdir()
    (tmp_path / "repo" / "apps" / "link").symlink_to(tmp_path / "elsewhere")

    assert _add_decision_impl(store, None, "t", "adr", "c", "ch")["commit_hint"] == HINT.format(
        prefix="apps/web/.sidegraph/"
    )
