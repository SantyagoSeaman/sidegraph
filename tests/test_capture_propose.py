from datetime import UTC, datetime
from pathlib import Path

import pytest

from sidegraph.capture import format_proposal, propose
from sidegraph.engine.reader import GraphifyReader
from sidegraph.schema import Decision, DecisionKind, DecisionStatus, Provenance
from sidegraph.store import Store

FIXTURE = Path(__file__).parent / "fixtures" / "mini_graph.json"


@pytest.fixture(autouse=True)
def _no_ambient_initiative(monkeypatch):
    """Tests must not depend on the ambient git branch (see test 5's explicit initiative=).

    Without this, `_derive_initiative()` picks up whatever branch the repo happens to be on
    and silently adds a Tier-0 binding, which the other tests don't account for.
    """
    monkeypatch.setattr("sidegraph.capture._derive_initiative", lambda: None)


def _draft(**over):
    base = dict(
        title="Use locks in Trader",
        kind="gotcha",
        context="races seen",
        choice="lock around order placement",
        anchors=[{"name": "Trader", "file_path": "trader/exec.py"}],
    )
    base.update(over)
    return base


def test_propose_writes_proposed_with_provenance_and_anchors(tmp_path):
    store = Store(tmp_path / "t.db")
    reader = GraphifyReader(FIXTURE)
    results = propose([_draft()], store, reader, session_id="s1", author="alex")
    assert results[0].status == "written"
    d = store.get_decision(results[0].decision_id)
    assert d.status == DecisionStatus.PROPOSED
    assert d.provenance.session_id == "s1"
    assert d.provenance.graph_version == reader.graph_version()  # from the fixture
    assert d.provenance.graph_version.startswith("abc123:")
    binds = store.bindings_for_record(d.id)
    assert {b.tier for b in binds} == {2, 1}  # leaf + community


def test_propose_redacts_before_store(tmp_path):
    store = Store(tmp_path / "t.db")
    results = propose([_draft(context="key AKIAIOSFODNN7EXAMPLE leaked", anchors=[])], store, None)
    d = store.get_decision(results[0].decision_id)
    assert "AKIAIOSFODNN7EXAMPLE" not in d.context
    assert "[REDACTED]" in d.context
    assert results[0].redactions == 1


def test_propose_dedups_same_kind_and_title_on_shared_entity(tmp_path):
    store = Store(tmp_path / "t.db")
    reader = GraphifyReader(FIXTURE)
    first = propose([_draft()], store, reader)
    store.ratify(first[0].decision_id)  # currently-valid now
    second = propose([_draft(context="different words")], store, reader)
    assert second[0].status == "deduped"
    third = propose([_draft(title="A different lesson entirely")], store, reader)
    assert third[0].status == "written"  # different title -> writes


def test_propose_malformed_draft_rejected_batch_continues(tmp_path):
    store = Store(tmp_path / "t.db")
    results = propose([{"title": "no kind"}, _draft(anchors=[])], store, None)
    assert results[0].status == "rejected" and results[0].reason
    assert results[1].status == "written"


def test_propose_no_reader_orphaned_anchor_and_initiative(tmp_path):
    store = Store(tmp_path / "t.db")
    results = propose([_draft(initiative="metadata-platform")], store, None)
    d_id = results[0].decision_id
    binds = store.bindings_for_record(d_id)
    tiers = {b.tier: b for b in binds}
    assert tiers[2].status == "orphaned"  # leaf recorded, degraded
    assert tiers[0].status == "live"  # initiative Tier-0
    init = store.get_entity(tiers[0].entity_id)
    assert init.canonical_name == "initiative:metadata-platform"


def test_propose_supersedes_closes_predecessor(tmp_path):
    store = Store(tmp_path / "t.db")
    old = store.add_decision(
        Decision(
            title="old way",
            kind=DecisionKind.ADR,
            context="c",
            choice="x",
            valid_from=datetime.now(UTC),
            provenance=Provenance(source="manual"),
        )
    )
    results = propose([_draft(title="new way", supersedes=old.id, anchors=[])], store, None)
    assert results[0].status == "written"
    assert store.get_decision(old.id).status == DecisionStatus.SUPERSEDED


def test_propose_skips_tag_that_redacts_to_exactly_redacted(tmp_path):
    """A tag whose entire text WAS the secret (redact() replaces the whole string with
    "[REDACTED]") must not mint a nameless `tag:redacted` entity (M5, M2 review fold-in)."""
    store = Store(tmp_path / "t.db")
    results = propose(
        [_draft(tags=["api_key=sk-live-abc123def456"], anchors=[])],
        store,
        None,
    )
    assert results[0].status == "written"
    assert store.find_abstract_entity("tag:redacted") is None
    did = results[0].decision_id
    bindings = store.bindings_for_record(did)
    assert not any(b.tier == 0 for b in bindings)  # no tag-0 binding created at all


def test_propose_keeps_tag_containing_but_not_equal_to_redacted(tmp_path):
    """A tag that merely CONTAINS "redacted" (no secret matched, nothing scrubbed) still
    slugifies to something else and is kept."""
    store = Store(tmp_path / "t.db")
    results = propose(
        [_draft(tags=["redacted-config"], anchors=[])],
        store,
        None,
    )
    assert results[0].status == "written"
    assert store.find_abstract_entity("tag:redacted-config") is not None


def test_propose_mixed_tags_only_redacted_one_skipped(tmp_path):
    store = Store(tmp_path / "t.db")
    results = propose(
        [_draft(tags=["api_key=sk-live-abc123def456", "Security"], anchors=[])],
        store,
        None,
    )
    assert results[0].status == "written"
    assert store.find_abstract_entity("tag:redacted") is None
    assert store.find_abstract_entity("tag:security") is not None


# -- propose: per-anchor ambiguous feedback (Gate-5 finding S3) ---------------------------


def test_propose_reports_ambiguous_anchor_in_anchors_skipped(tmp_path):
    import json

    nodes = [
        {
            "id": "a",
            "label": "run()",
            "norm_label": "run()",
            "file_type": "code",
            "source_file": "m.py",
            "community": "7",
        },
        {
            "id": "b",
            "label": "run()",
            "norm_label": "run()",
            "file_type": "code",
            "source_file": "m.py",
            "community": "7",
        },
    ]
    graph_path = tmp_path / "g.json"
    graph_path.write_text(json.dumps({"built_at_commit": "x", "nodes": nodes, "links": []}))
    reader = GraphifyReader(graph_path)
    store = Store(tmp_path / "t.db")

    results = propose([_draft(anchors=[{"name": "run"}])], store, reader)
    assert results[0].status == "written"
    assert results[0].anchors_skipped == [
        {"name": "run", "reason": "ambiguous", "candidates": ["a", "b"]}
    ]


def test_propose_resolved_anchor_leaves_anchors_skipped_empty(tmp_path):
    store = Store(tmp_path / "t.db")
    reader = GraphifyReader(FIXTURE)
    results = propose([_draft()], store, reader)
    assert results[0].status == "written"
    assert results[0].anchors_skipped == []


def test_propose_no_reader_leaves_anchors_skipped_empty(tmp_path):
    store = Store(tmp_path / "t.db")
    results = propose([_draft()], store, None)
    assert results[0].status == "written"
    assert results[0].anchors_skipped == []


# -- D1: best-effort commit stamping ------------------------------------------------------


def test_propose_stamps_commit_from_the_stores_own_repo_even_when_cwd_is_elsewhere(
    tmp_path, monkeypatch
):
    """CORRECTION-2 (code review): _capture_commit must resolve HEAD from the STORE's own
    repo (its directory's location), never the ambient process cwd — otherwise D5's
    code-drift diff would run against the wrong repo entirely in the nested-store
    topology. The store lives INSIDE the repo; the process cwd is a different, git-less
    directory — the commit must still be stamped from the repo the STORE is in."""
    import subprocess

    repo = tmp_path / "repo"
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

    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)  # NOT the repo -- proves cwd is irrelevant

    store = Store(repo / ".sidegraph")  # the store lives INSIDE the repo
    results = propose([_draft(anchors=[])], store, None)
    assert results[0].status == "written"
    d = store.get_decision(results[0].decision_id)
    assert d.provenance.commit == head


def test_propose_commit_none_when_store_is_outside_any_repo_even_if_cwd_has_one(
    tmp_path, monkeypatch
):
    """The store lives OUTSIDE any repo; the process cwd is inside an UNRELATED git repo
    -- proves the commit stamp follows the STORE's own location, never the ambient cwd
    (never raising either way, design D1)."""
    import subprocess

    unrelated_repo = tmp_path / "unrelated-repo"
    unrelated_repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=unrelated_repo, check=True)
    monkeypatch.chdir(unrelated_repo)

    store = Store(tmp_path / "t.db")  # NOT inside unrelated_repo
    results = propose([_draft(anchors=[])], store, None)
    assert results[0].status == "written"
    d = store.get_decision(results[0].decision_id)
    assert d.provenance.commit is None


# -- D7.3: session_id fallback via TELEMETRY_SESSION_KEY -----------------------------------


def test_propose_falls_back_to_telemetry_session_key_when_fresh(tmp_path):
    """A caller that passes no session_id (E8: author=None session=None on Stop-channel
    captures) gets the marker host.hooks.session_start now stamps unconditionally
    (design D7.3), when it's fresh (< 24h)."""
    from datetime import UTC, datetime

    from sidegraph.config import TELEMETRY_SESSION_KEY

    store = Store(tmp_path / "t.db")
    store.set_meta(TELEMETRY_SESSION_KEY, f"session-xyz|{datetime.now(UTC).isoformat()}")

    results = propose([_draft(anchors=[])], store, None)  # no session_id passed
    assert results[0].status == "written"
    d = store.get_decision(results[0].decision_id)
    assert d.provenance.session_id == "session-xyz"


def test_propose_explicit_session_id_always_wins_over_fallback(tmp_path):
    from datetime import UTC, datetime

    from sidegraph.config import TELEMETRY_SESSION_KEY

    store = Store(tmp_path / "t.db")
    store.set_meta(TELEMETRY_SESSION_KEY, f"marker-session|{datetime.now(UTC).isoformat()}")

    results = propose([_draft(anchors=[])], store, None, session_id="explicit-session")
    assert results[0].status == "written"
    d = store.get_decision(results[0].decision_id)
    assert d.provenance.session_id == "explicit-session"


def test_propose_ignores_stale_telemetry_session_marker(tmp_path):
    """A marker older than 24h is worse than no attribution at all (design D7.3) -- the
    fallback returns None, same as no marker existing."""
    from datetime import UTC, datetime, timedelta

    from sidegraph.config import TELEMETRY_SESSION_KEY

    store = Store(tmp_path / "t.db")
    stale = (datetime.now(UTC) - timedelta(hours=25)).isoformat()
    store.set_meta(TELEMETRY_SESSION_KEY, f"stale-session|{stale}")

    results = propose([_draft(anchors=[])], store, None)
    assert results[0].status == "written"
    d = store.get_decision(results[0].decision_id)
    assert d.provenance.session_id is None


def test_propose_no_telemetry_marker_leaves_session_id_none(tmp_path):
    store = Store(tmp_path / "t.db")
    results = propose([_draft(anchors=[])], store, None)
    assert results[0].status == "written"
    d = store.get_decision(results[0].decision_id)
    assert d.provenance.session_id is None


def test_propose_malformed_telemetry_marker_never_raises(tmp_path):
    from sidegraph.config import TELEMETRY_SESSION_KEY

    store = Store(tmp_path / "t.db")
    store.set_meta(TELEMETRY_SESSION_KEY, "not-the-expected-shape")

    results = propose([_draft(anchors=[])], store, None)
    assert results[0].status == "written"
    d = store.get_decision(results[0].decision_id)
    assert d.provenance.session_id is None


def test_propose_survives_get_meta_failure_during_session_id_fallback(tmp_path, monkeypatch):
    """NIT-5 (code review): _session_id_fallback's own store.get_meta read wasn't actually
    guarded, despite the docstring's "never raises" claim -- a broken/locked store would
    have crashed the whole propose call over a best-effort attribution guess."""
    store = Store(tmp_path / "t.db")

    def boom(self, key):
        raise RuntimeError("boom")

    monkeypatch.setattr(Store, "get_meta", boom)
    results = propose([_draft(anchors=[])], store, None)
    assert results[0].status == "written"
    d = store.get_decision(results[0].decision_id)
    assert d.provenance.session_id is None


def test_format_proposal_renders_fields():
    d = Decision(
        title="T",
        kind=DecisionKind.GOTCHA,
        context="why",
        choice="what",
        rejected="alt-approach",
        valid_from=datetime.now(UTC),
        provenance=Provenance(source="agent", session_id="s9"),
    )
    text = format_proposal(d)
    assert d.id in text and "T" in text and "what" in text and "alt-approach" in text
    assert "s9" in text


def test_draft_bare_string_tags_coerced():
    # Same liberal-input rule as the add_decision tool: a draft arriving with
    # tags as a comma-separated string validates instead of failing the pipeline.
    from sidegraph.capture import DraftDecision

    d = DraftDecision(
        title="t", kind="adr", context="c", choice="ch", tags="Security, needs review"
    )
    assert d.tags == ["Security", "needs review"]


def test_propose_reports_unresolved_anchor_as_orphaned(tmp_path):
    """The agent-initiated twin of
    test_add_decision_impl_reports_unresolved_anchor_as_orphaned. This path matters MORE:
    on the airflow corpus, where 29% of Tier-2 bindings are orphaned-at-birth, nearly every
    orphaned record carries ``provenance.source == "agent"`` -- they came through here, not
    through add_decision. An agent that never learns its anchor missed cannot correct it.
    """
    from sidegraph.engine.reader import GraphifyReader

    store = Store(tmp_path / "t.db")
    reader = GraphifyReader(FIXTURE)

    [result] = propose(
        [
            {
                "title": "anchored at a name the graph does not have",
                "kind": "gotcha",
                "context": "ctx",
                "choice": "choice",
                "anchors": [{"name": "no_such_symbol_anywhere", "file_path": "trader/exec.py"}],
            }
        ],
        store,
        reader,
    )
    assert result.status == "written"
    assert [e["canonical_name"] for e in result.anchors_orphaned] == ["no_such_symbol_anywhere"]
    assert result.anchors_skipped == []


def test_propose_resolved_anchor_reports_no_orphans(tmp_path):
    from sidegraph.engine.reader import GraphifyReader

    store = Store(tmp_path / "t.db")
    reader = GraphifyReader(FIXTURE)

    [result] = propose(
        [
            {
                "title": "anchored at a real node",
                "kind": "gotcha",
                "context": "ctx",
                "choice": "choice",
                "anchors": [{"name": "Trader", "file_path": "trader/exec.py"}],
            }
        ],
        store,
        reader,
    )
    assert result.status == "written"
    assert result.anchors_orphaned == []
