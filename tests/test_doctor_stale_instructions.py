"""stale-instructions check: an auto-injected agent-instructions file still carrying the
distinctive wording of a SUPERSEDED decision.

Why it exists: the whitepaper's §12.6 measurement — the injected file reaches the agent
at turn zero, before retrieval — so an abandoned rule surviving there outranks the store's
own correction by default.

Red targets: test_flags_superseded_wording is red against unfixed code (no such check).
The negative tests are declared over-reach guards: they pass before and after, and exist
because a noisy advisory check is worse than none.
"""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path

from sidegraph.doctor import STALE_INSTRUCTIONS, curate
from sidegraph.schema import Decision, DecisionKind, DecisionStatus, Provenance
from sidegraph.store import Store

PHRASE = "Retry inside charge() using the vendor backoff helper"


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    return repo


def _decision(store: Store, title: str, status: DecisionStatus) -> Decision:
    d = Decision(
        title=title,
        kind=DecisionKind.ADR,
        status=status,
        context="c",
        choice="ch",
        valid_from=datetime.now(UTC),
        valid_to=datetime.now(UTC) if status == DecisionStatus.SUPERSEDED else None,
        provenance=Provenance(source="manual"),
    )
    store._write_decision(d)
    store._conn.commit()
    return d


def test_flags_superseded_wording_in_instructions_file(tmp_path):
    repo = _repo(tmp_path)
    store = Store(repo / ".sidegraph")
    d = _decision(store, PHRASE, DecisionStatus.SUPERSEDED)
    (repo / "CLAUDE.md").write_text(f"# Conventions\n\n- {PHRASE} for all payment calls.\n")
    findings = curate(repo / ".sidegraph", repo_root=repo).findings
    hits = [f for f in findings if f.code == STALE_INSTRUCTIONS]
    assert len(hits) == 1
    assert d.id in hits[0].detail
    assert "CLAUDE.md" in hits[0].path


def test_accepted_record_wording_is_not_flagged(tmp_path):
    """Over-reach guard (declared): an ACCEPTED record's wording SHOULD live there."""
    repo = _repo(tmp_path)
    store = Store(repo / ".sidegraph")
    _decision(store, PHRASE, DecisionStatus.ACCEPTED)
    (repo / "CLAUDE.md").write_text(f"- {PHRASE}\n")
    assert not [
        f
        for f in curate(repo / ".sidegraph", repo_root=repo).findings
        if f.code == STALE_INSTRUCTIONS
    ]


def test_short_titles_never_match(tmp_path):
    """Over-reach guard (declared): short/common titles would make this check noise."""
    repo = _repo(tmp_path)
    store = Store(repo / ".sidegraph")
    _decision(store, "Use SQLite", DecisionStatus.SUPERSEDED)
    (repo / "CLAUDE.md").write_text("We use SQLite for the index.\n")
    assert not [
        f
        for f in curate(repo / ".sidegraph", repo_root=repo).findings
        if f.code == STALE_INSTRUCTIONS
    ]


def test_no_instructions_file_is_clean(tmp_path):
    repo = _repo(tmp_path)
    store = Store(repo / ".sidegraph")
    _decision(store, PHRASE, DecisionStatus.SUPERSEDED)
    assert not [
        f
        for f in curate(repo / ".sidegraph", repo_root=repo).findings
        if f.code == STALE_INSTRUCTIONS
    ]


def test_matching_ignores_markdown_emphasis_and_spacing(tmp_path):
    """The file rarely quotes a title byte-for-byte — normalization is the point."""
    repo = _repo(tmp_path)
    store = Store(repo / ".sidegraph")
    _decision(store, PHRASE, DecisionStatus.SUPERSEDED)
    (repo / "AGENTS.md").write_text(
        "- **Retry   inside `charge()` using the vendor    backoff helper**\n"
    )
    hits = [
        f
        for f in curate(repo / ".sidegraph", repo_root=repo).findings
        if f.code == STALE_INSTRUCTIONS
    ]
    assert len(hits) == 1
    assert "AGENTS.md" in hits[0].path
