from datetime import UTC, datetime
from pathlib import Path

import pytest

from sidegraph import capture
from sidegraph.capture import (
    RatifyPolicy,
    format_proposal,
    propose,
    propose_domains,
    propose_facts,
)
from sidegraph.engine.reader import GraphifyReader
from sidegraph.schema import Decision, DecisionKind, DecisionStatus, Provenance
from sidegraph.store import Store

FIXTURE = Path(__file__).parent / "fixtures" / "mini_graph.json"


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


def test_session_id_fallback_ignores_a_marker_stamped_in_the_future(tmp_path):
    """A negative age (clock stepped back, or a malformed stamp) is not fresh: the
    fallback returns None rather than attributing a stale session id."""
    from datetime import timedelta

    from sidegraph.config import TELEMETRY_SESSION_KEY

    store = Store(tmp_path / "t.db")
    future = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    store.set_meta(TELEMETRY_SESSION_KEY, f"future-session|{future}")

    assert capture._session_id_fallback(store) is None
    results = propose([_draft(anchors=[])], store, None)
    assert results[0].status == "written"
    assert store.get_decision(results[0].decision_id).provenance.session_id is None


def test_session_id_fallback_keeps_a_recent_marker(tmp_path):
    from datetime import timedelta

    from sidegraph.config import TELEMETRY_SESSION_KEY

    store = Store(tmp_path / "t.db")
    recent = (datetime.now(UTC) - timedelta(seconds=10)).isoformat()
    store.set_meta(TELEMETRY_SESSION_KEY, f"recent-session|{recent}")

    assert capture._session_id_fallback(store) == "recent-session"


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


SECRET_INITIATIVE = "AKIAQWERTYUIOPASDFGH"


def _initiative_names(store):
    rows = store._conn.execute(
        "SELECT canonical_name FROM entities WHERE canonical_name LIKE 'initiative:%'"
    ).fetchall()
    return [r["canonical_name"] for r in rows]


def _all_entity_names(store):
    rows = store._conn.execute("SELECT canonical_name FROM entities").fetchall()
    return [r["canonical_name"] for r in rows]


def test_propose_redacts_initiative(tmp_path):
    store = Store(tmp_path / "t.db")
    results = propose([_draft(anchors=[], initiative=f"proj-{SECRET_INITIATIVE}")], store, None)
    assert results[0].status == "written"
    assert results[0].redactions >= 1
    names = _all_entity_names(store)
    assert names
    assert not any(SECRET_INITIATIVE in n for n in names)
    assert "initiative:proj-[REDACTED]" in names


@pytest.mark.parametrize("initiative", [SECRET_INITIATIVE, "   "])
def test_propose_all_secret_or_blank_initiative_binds_nothing(tmp_path, initiative):
    store = Store(tmp_path / "t.db")
    results = propose([_draft(anchors=[], initiative=initiative)], store, None)
    assert results[0].status == "written"
    assert _initiative_names(store) == []
    assert not [b for b in store.bindings_for_record(results[0].decision_id) if b.tier == 0]


def test_propose_redacts_a_secret_derived_branch_name(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "sidegraph.capture._derive_initiative", lambda *a, **k: f"feature-{SECRET_INITIATIVE}"
    )
    store = Store(tmp_path / "t.db")
    results = propose([_draft(anchors=[])], store, None)
    assert results[0].redactions >= 1
    names = _initiative_names(store)
    assert names and not any(SECRET_INITIATIVE in n for n in names)


def test_deduped_result_counts_initiative_redaction(tmp_path):
    store = Store(tmp_path / "t.db")
    reader = GraphifyReader(FIXTURE)
    first = propose([_draft()], store, reader)
    store.ratify(first[0].decision_id)
    second = propose([_draft(initiative=SECRET_INITIATIVE)], store, reader)
    assert second[0].status == "deduped"
    assert second[0].redactions >= 1


def test_explicit_blank_initiative_does_not_fall_back(tmp_path, monkeypatch):
    # An explicit blank is an explicit answer: it does not fall back to the branch name.
    monkeypatch.setattr("sidegraph.capture._derive_initiative", lambda *a, **k: "feature-x")
    store = Store(tmp_path / "t.db")
    propose([_draft(anchors=[], initiative="   ")], store, None)
    assert _initiative_names(store) == []


def test_empty_string_initiative_falls_back_to_the_derived_one(tmp_path, monkeypatch):
    # Agents send "" for an unset optional field; that is not an explicit answer.
    monkeypatch.setattr("sidegraph.capture._derive_initiative", lambda *a, **k: "feature-x")
    store = Store(tmp_path / "t.db")
    propose([_draft(anchors=[], initiative="")], store, None)
    assert _initiative_names(store) == ["initiative:feature-x"]


def test_padded_initiative_name_is_trimmed(tmp_path):
    store = Store(tmp_path / "t.db")
    propose([_draft(anchors=[], initiative="  proj  ")], store, None)
    assert _initiative_names(store) == ["initiative:proj"]


def test_propose_dedups_a_secret_bearing_title(tmp_path):
    # The store holds the redacted title, so dedup must compare that, not the raw one.
    store = Store(tmp_path / "t.db")
    reader = GraphifyReader(FIXTURE)
    draft = _draft(title=f"Rotate key {SECRET_INITIATIVE} monthly")
    first = propose([draft], store, reader)
    store.ratify(first[0].decision_id)
    second = propose([draft], store, reader)
    assert first[0].status == "written"
    assert second[0].status == "deduped"
    assert second[0].reason == f"duplicate of {first[0].decision_id}"


# -- failure isolation (per draft, per fact, per domain) ----------------------------------


def _raise_once(exc, default=None):
    """A stand-in that raises ``exc`` on its first call and returns ``default`` after."""
    calls = {"n": 0}

    def _fn(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise exc
        return default

    return _fn


def test_post_write_failure_keeps_the_batch_going(tmp_path, monkeypatch):
    store = Store(tmp_path / "t.db")
    real = Store.get_or_create_abstract_entity
    state = {"raised": False}

    def flaky(self, canonical_name):
        if canonical_name.startswith("initiative:") and not state["raised"]:
            state["raised"] = True
            raise OSError("disk full")
        return real(self, canonical_name)

    monkeypatch.setattr(Store, "get_or_create_abstract_entity", flaky)
    d1 = _draft(
        context=f"key {SECRET_INITIATIVE} leaked",
        anchors=[{"name": "NoSuchThing"}],
        initiative="proj",
    )
    d2 = _draft(title="A second, unrelated lesson", anchors=[], initiative="proj")
    r1, r2 = propose([d1, d2], store, None)

    assert r1.status == "written"
    assert r1.decision_id is not None
    assert "initiative" in (r1.reason or "")
    assert "OSError" in r1.reason
    assert "ratify(drop=" in r1.reason
    assert r1.redactions >= 1  # the guard falls through to the normal return
    assert r1.anchors_orphaned  # ...so do the fields the earlier steps filled
    assert r2.status == "written" and r2.reason is None
    assert [b for b in store.bindings_for_record(r2.decision_id) if b.tier == 0]


def test_accepted_record_reason_names_what_cannot_be_added(tmp_path, monkeypatch):
    store = Store(tmp_path / "t.db")
    monkeypatch.setattr(capture, "_bind_orphaned", _raise_once(OSError("disk full")))
    [r] = propose([_draft()], store, None, auto_accept=True)
    assert r.status == "written"
    assert "add_anchors" in r.reason
    assert "initiative and tags cannot be added" in r.reason
    assert store.get_decision(r.decision_id).status == DecisionStatus.ACCEPTED


def test_attached_fact_failure_is_isolated(tmp_path, monkeypatch):
    store = Store(tmp_path / "t.db")
    real = capture._is_duplicate_fact
    calls = {"n": 0}

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("index gone")
        return real(*a, **k)

    monkeypatch.setattr(capture, "_is_duplicate_fact", flaky)
    facts = [{"statement": f"fact number {i}", "source": "src"} for i in (1, 2, 3)]
    [r] = propose([_draft(anchors=[], facts=facts)], store, None)
    assert r.status == "written"
    assert [f.status for f in r.facts] == ["written", "rejected", "written"]
    assert "internal error: RuntimeError" in r.facts[1].reason


def test_fact_post_write_failure_is_isolated(tmp_path, monkeypatch):
    store = Store(tmp_path / "t.db")
    monkeypatch.setattr(capture, "_bind_orphaned", _raise_once(OSError("disk full")))
    facts = [
        {"statement": f"fact number {i}", "source": "src", "anchors": [{"name": f"Thing{i}"}]}
        for i in (1, 2)
    ]
    r1, r2 = propose_facts(facts, store, None)
    assert r1.status == "written" and r1.fact_id is not None
    assert "anchors" in r1.reason and "OSError" in r1.reason
    assert r2.status == "written" and r2.reason is None


def test_accepted_fact_reason_names_add_anchors_not_drop(tmp_path, monkeypatch):
    # Store.drop_fact refuses a fact that is not proposed, so an auto-accepted fact's guard
    # reason must not send the caller to ratify(drop=...).
    store = Store(tmp_path / "t.db")
    monkeypatch.setattr(capture, "_bind_orphaned", _raise_once(OSError("disk full")))
    facts = [{"statement": "fact number 1", "source": "src", "anchors": [{"name": "Thing1"}]}]
    [r] = propose_facts(facts, store, None, auto_accept=True)
    assert r.status == "written" and r.fact_id is not None
    assert "add_anchors" in r.reason
    assert "drop" not in r.reason
    assert store.get_fact(r.fact_id).status == DecisionStatus.ACCEPTED


def test_failed_attached_fact_keeps_the_decision_proposed(tmp_path, monkeypatch):
    store = Store(tmp_path / "t.db")
    reader = GraphifyReader(FIXTURE)
    monkeypatch.setattr(capture, "_is_duplicate_fact", _raise_once(RuntimeError("index gone")))
    facts = [{"statement": "fact number 1", "source": "src"}]
    d1 = _draft(facts=facts)
    d2 = _draft(title="A second, unrelated lesson", choice="another choice entirely")
    r1, r2 = propose([d1, d2], store, reader, ratify_policy=RatifyPolicy.AUTO_ALL)
    assert r1.status == "written"
    assert r1.facts[0].status == "rejected"
    assert store.get_decision(r1.decision_id).status == DecisionStatus.PROPOSED
    assert r1.ratified_by is None
    assert r1.reason == "attached fact failed; left proposed for review"
    assert r2.status == "written" and r2.reason is None
    assert store.get_decision(r2.decision_id).status == DecisionStatus.ACCEPTED


def test_attached_fact_post_write_failure_keeps_the_decision_proposed(tmp_path, monkeypatch):
    store = Store(tmp_path / "t.db")
    reader = GraphifyReader(FIXTURE)
    # The decision binds first, then its attached fact inherits the anchor: the SECOND
    # resolve_and_bind call is the fact's, and it raises.
    real = capture.resolve_and_bind
    calls = {"n": 0}

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("disk full")
        return real(*a, **k)

    monkeypatch.setattr(capture, "resolve_and_bind", flaky)
    facts = [{"statement": "fact number 1", "source": "src"}]
    [r] = propose([_draft(facts=facts)], store, reader, ratify_policy=RatifyPolicy.AUTO_ALL)
    assert r.facts[0].status == "written" and r.facts[0].reason
    assert store.get_decision(r.decision_id).status == DecisionStatus.PROPOSED
    assert r.reason == "attached fact failed; left proposed for review"


def test_rejected_attached_fact_keeps_the_decision_proposed(tmp_path):
    # A whitespace-only statement passes draft validation but fails Fact validation: the
    # fact is REJECTED (no exception, no guard), yet the requested draft is not fully
    # recorded, so the decision must not auto-ratify.
    store = Store(tmp_path / "t.db")
    reader = GraphifyReader(FIXTURE)
    facts = [{"statement": " ", "source": "src"}]
    d2 = _draft(title="A second, unrelated lesson", choice="another choice entirely")
    r1, r2 = propose([_draft(facts=facts), d2], store, reader, ratify_policy=RatifyPolicy.AUTO_ALL)
    assert r1.status == "written"
    assert r1.facts[0].status == "rejected"
    assert store.get_decision(r1.decision_id).status == DecisionStatus.PROPOSED
    assert r1.ratified_by is None
    assert r1.reason == "attached fact failed; left proposed for review"
    assert r2.status == "written" and r2.reason is None
    assert store.get_decision(r2.decision_id).status == DecisionStatus.ACCEPTED


def test_deduped_attached_fact_still_lets_the_decision_auto_ratify(tmp_path):
    store = Store(tmp_path / "t.db")
    reader = GraphifyReader(FIXTURE)
    facts = [{"statement": "fact number 1", "source": "src"}]
    d2 = _draft(title="A second, unrelated lesson", choice="another choice entirely", facts=facts)
    r1, r2 = propose([_draft(facts=facts), d2], store, reader, ratify_policy=RatifyPolicy.AUTO_ALL)
    assert r1.facts[0].status == "written"
    assert r2.facts[0].status == "deduped"
    assert r2.reason is None
    assert store.get_decision(r2.decision_id).status == DecisionStatus.ACCEPTED


def test_attached_fact_remedy_names_supports_not_a_standalone_repropose(tmp_path, monkeypatch):
    store = Store(tmp_path / "t.db")
    reader = GraphifyReader(FIXTURE)
    real = capture.resolve_and_bind
    calls = {"n": 0}

    def flaky(*a, **k):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("disk full")
        return real(*a, **k)

    monkeypatch.setattr(capture, "resolve_and_bind", flaky)
    facts = [{"statement": "fact number 1", "source": "src"}]
    [r] = propose([_draft(facts=facts)], store, reader)
    reason = r.facts[0].reason
    assert f"supports=[{r.decision_id}]" in reason
    assert "propose the complete draft again" not in reason


def _domain_drafts():
    return [
        {"slug": "one", "title": "One", "summary": "First area."},
        {"slug": "two", "title": "Two", "summary": "Second area."},
    ]


def test_domain_post_write_failure_is_isolated(tmp_path, monkeypatch):
    store = Store(tmp_path / "t.db")
    monkeypatch.setattr(
        capture, "_lint_domain_path_prefixes", _raise_once(OSError("disk full"), [])
    )
    r1, r2 = propose_domains(_domain_drafts(), store, None)
    assert r1.status == "proposed" and r1.domain_id is not None
    assert "path_prefixes" in r1.reason and "OSError" in r1.reason
    assert r2.status == "proposed" and r2.reason is None


@pytest.mark.parametrize("what", ["decision", "fact", "domain"])
def test_pre_write_internal_error_is_rejected_not_raised(tmp_path, monkeypatch, what):
    store = Store(tmp_path / "t.db")
    boom = RuntimeError("index gone")
    if what == "decision":
        monkeypatch.setattr(capture, "_is_duplicate", _raise_once(boom))
        r1, r2 = propose([_draft(anchors=[]), _draft(title="Another", anchors=[])], store, None)
    elif what == "fact":
        monkeypatch.setattr(capture, "_is_duplicate_fact", _raise_once(boom))
        facts = [
            {"statement": f"fact {i}", "source": "s", "anchors": [{"name": f"T{i}"}]}
            for i in (1, 2)
        ]
        r1, r2 = propose_facts(facts, store, None)
    else:
        real = Store.find_domain_by_slug
        state = {"n": 0}

        def flaky(self, slug):
            state["n"] += 1
            if state["n"] == 1:
                raise boom
            return real(self, slug)

        monkeypatch.setattr(Store, "find_domain_by_slug", flaky)
        r1, r2 = propose_domains(_domain_drafts(), store, None)
    assert r1.status == "rejected"
    assert "internal error: RuntimeError" in r1.reason
    assert "may be on disk" in r1.reason
    assert r2.status in ("written", "proposed")


def test_toc_rebuild_failure_surfaces_as_a_warning(tmp_path, monkeypatch):
    store = Store(tmp_path / "t.db")
    reader = GraphifyReader(FIXTURE)

    def boom(_store):
        raise OSError("toc gone")

    monkeypatch.setattr(capture, "build_toc", boom)
    draft = {
        "slug": "trading",
        "title": "Trading",
        "summary": "Order execution path.",
        "seed_anchors": [{"name": "Trader", "file_path": "trader/exec.py"}],
    }
    [r] = propose_domains([draft], store, reader, ratify_policy=RatifyPolicy.AUTO_ALL)
    assert r.status == "proposed"
    assert r.ratified_by == "auto:auto-all"
    assert any(w.startswith("toc: OSError") and "next sync rebuilds it" in w for w in r.warnings)


def test_keyboard_interrupt_still_propagates(tmp_path, monkeypatch):
    store = Store(tmp_path / "t.db")
    monkeypatch.setattr(capture, "_bind_orphaned", _raise_once(KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        propose([_draft()], store, None)
