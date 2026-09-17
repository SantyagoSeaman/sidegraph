import io
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import sidegraph.host.hooks as hooks

FIXTURE = Path(__file__).parent / "fixtures" / "mini_graph.json"


def test_session_start_emits_additional_context(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SIDEGRAPH_DB", str(tmp_path / "s.db"))
    monkeypatch.setenv("SIDEGRAPH_GRAPH", str(FIXTURE))
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    hooks.session_start()
    out = json.loads(capsys.readouterr().out)
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert out["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert "get_task_context" in ctx


def test_session_start_never_raises(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    monkeypatch.setenv("SIDEGRAPH_DB", str(tmp_path / "s.db"))

    # force an internal failure: session_start imports top_tier_map from the retrieval
    # module at call time, so patching the source symbol makes the build blow up.
    def boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr("sidegraph.retrieval.top_tier_map", boom)

    hooks.session_start()
    out = json.loads(capsys.readouterr().out)
    assert out == {}  # degraded, never raised


def test_session_start_relative_env_paths_anchor_to_claude_project_dir(
    tmp_path, monkeypatch, capsys
):
    import shutil

    project_dir = tmp_path / "project"
    (project_dir / "sub").mkdir(parents=True)
    shutil.copy(FIXTURE, project_dir / "sub" / "graph.json")
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(project_dir))
    monkeypatch.setenv("SIDEGRAPH_DIR", "sub/s.db")
    monkeypatch.setenv("SIDEGRAPH_GRAPH", "sub/graph.json")
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    hooks.session_start()
    out = json.loads(capsys.readouterr().out)
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert "## Communities" in ctx  # reader resolved via CLAUDE_PROJECT_DIR
    assert (project_dir / "sub" / "s.db").exists()  # store opened under project dir


def test_session_start_renders_domain_toc_when_cache_present(tmp_path, monkeypatch, capsys):
    from sidegraph.retrieval import TOC_CACHE_KEY
    from sidegraph.schema import Domain, Provenance
    from sidegraph.store import Store

    db = tmp_path / "s.db"
    store = Store(db)
    d = store.add_domain(
        Domain(
            slug="payments",
            title="Payments",
            summary="Handles settlement and refunds.",
            provenance=Provenance(source="manual"),
        )
    )
    store.ratify_domains(accept=[d.domain_id])
    # Simulate a prior sync pass having precomputed the cache (sync itself is exercised
    # elsewhere; this test isolates session_start's read-and-render behavior).
    store.set_meta(
        TOC_CACHE_KEY,
        json.dumps(
            {
                "domains": [
                    {
                        "slug": "payments",
                        "title": "Payments",
                        "summary": "Handles settlement and refunds.",
                        "parent_slug": None,
                        "mistakes": 3,
                        "subdomains": 0,
                    }
                ],
                "initiatives": [],
                "global_mistakes": [],
            }
        ),
    )

    monkeypatch.setenv("SIDEGRAPH_DB", str(db))
    monkeypatch.setenv("SIDEGRAPH_GRAPH", str(tmp_path / "no-graph.json"))  # reader=None
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    hooks.session_start()
    out = json.loads(capsys.readouterr().out)
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert "## Domains" in ctx
    assert "Payments" in ctx
    assert "Handles settlement and refunds." in ctx
    assert "(3 mistake(s))" in ctx


def test_session_start_augments_stale_domain_cache_with_live_unratified_memory(
    tmp_path, monkeypatch, capsys
):
    from sidegraph.retrieval import TOC_CACHE_KEY
    from sidegraph.schema import (
        Decision,
        DecisionKind,
        DecisionStatus,
        Domain,
        Provenance,
        Scope,
    )
    from sidegraph.store import Store

    db = tmp_path / "s.db"
    store = Store(db)
    domain = store.add_domain(
        Domain(
            slug="payments",
            title="Payments",
            summary="Live domain summary.",
            provenance=Provenance(source="manual"),
        )
    )
    store.ratify_domains(accept=[domain.domain_id])
    proposal = store.add_decision(
        Decision(
            title="Recheck global retry policy",
            kind=DecisionKind.GOTCHA,
            status=DecisionStatus.PROPOSED,
            context="c",
            choice="ch",
            scope=Scope.GLOBAL,
            valid_from=datetime.now(UTC),
            provenance=Provenance(source="agent"),
        )
    )
    store.set_meta(
        TOC_CACHE_KEY,
        json.dumps(
            {
                "domains": [
                    {
                        "slug": "payments",
                        "title": "Payments",
                        "summary": "Cached accepted summary.",
                        "parent_slug": None,
                        "mistakes": 7,
                        "subdomains": 0,
                    }
                ],
                "initiatives": [],
                "global_mistakes": [],
            }
        ),
    )

    monkeypatch.setenv("SIDEGRAPH_DB", str(db))
    monkeypatch.setenv("SIDEGRAPH_GRAPH", str(tmp_path / "no-graph.json"))
    monkeypatch.setenv("SIDEGRAPH_RATIFY_NUDGE", "off")
    monkeypatch.setattr("sys.stdin", io.StringIO(""))

    hooks.session_start()

    ctx = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
    assert "Cached accepted summary." in ctx
    assert "(7 mistake(s))" in ctx
    assert "Live domain summary." not in ctx
    assert "## Unratified proposals" in ctx
    assert f"[unratified] {proposal.title}" in ctx
    assert ctx.index("Payments") < ctx.index("## Unratified proposals")


def test_session_start_standing_instruction_precedes_domain_toc(tmp_path, monkeypatch, capsys):
    """F2: the standing "use get_task_context before searching" instruction must be at the
    very top of the injected context, ahead of the real domain TOC render path."""
    from sidegraph.retrieval import TOC_CACHE_KEY
    from sidegraph.schema import Domain, Provenance
    from sidegraph.store import Store

    db = tmp_path / "s.db"
    store = Store(db)
    d = store.add_domain(
        Domain(
            slug="payments",
            title="Payments",
            summary="Handles settlement and refunds.",
            provenance=Provenance(source="manual"),
        )
    )
    store.ratify_domains(accept=[d.domain_id])
    store.set_meta(
        TOC_CACHE_KEY,
        json.dumps(
            {
                "domains": [
                    {
                        "slug": "payments",
                        "title": "Payments",
                        "summary": "Handles settlement and refunds.",
                        "parent_slug": None,
                        "mistakes": 0,
                        "subdomains": 0,
                    }
                ],
                "initiatives": [],
                "global_mistakes": [],
            }
        ),
    )

    monkeypatch.setenv("SIDEGRAPH_DB", str(db))
    monkeypatch.setenv("SIDEGRAPH_GRAPH", str(tmp_path / "no-graph.json"))  # reader=None
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    hooks.session_start()
    out = json.loads(capsys.readouterr().out)
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert ctx.startswith(hooks.STANDING_SEARCH_INSTRUCTION)
    assert ctx.index(hooks.STANDING_SEARCH_INSTRUCTION) < ctx.index("## Domains")


def test_session_start_standing_instruction_precedes_fallback_map(tmp_path, monkeypatch, capsys):
    """F2: the same standing instruction must also lead the legacy community-listing
    fallback (zero-domain case), not just the real domain TOC."""
    monkeypatch.setenv("SIDEGRAPH_DB", str(tmp_path / "s.db"))
    monkeypatch.setenv("SIDEGRAPH_GRAPH", str(FIXTURE))
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    hooks.session_start()
    out = json.loads(capsys.readouterr().out)
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert ctx.startswith(hooks.STANDING_SEARCH_INSTRUCTION)
    assert ctx.index(hooks.STANDING_SEARCH_INSTRUCTION) < ctx.index("## Communities")


def test_session_start_falls_back_to_legacy_map_when_cache_has_no_domains(
    tmp_path, monkeypatch, capsys
):
    from sidegraph.retrieval import TOC_CACHE_KEY
    from sidegraph.store import Store

    db = tmp_path / "s.db"
    store = Store(db)
    store.set_meta(
        TOC_CACHE_KEY, json.dumps({"domains": [], "initiatives": [], "global_mistakes": []})
    )

    monkeypatch.setenv("SIDEGRAPH_DB", str(db))
    monkeypatch.setenv("SIDEGRAPH_GRAPH", str(FIXTURE))
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    hooks.session_start()
    out = json.loads(capsys.readouterr().out)
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert "## Domains" not in ctx
    assert "## Communities" in ctx  # legacy top_tier_map rendered instead


def test_session_start_malformed_toc_cache_degrades_to_legacy_map(tmp_path, monkeypatch, capsys):
    from sidegraph.retrieval import TOC_CACHE_KEY
    from sidegraph.store import Store

    db = tmp_path / "s.db"
    store = Store(db)
    store.set_meta(TOC_CACHE_KEY, "not valid json")

    monkeypatch.setenv("SIDEGRAPH_DB", str(db))
    monkeypatch.setenv("SIDEGRAPH_GRAPH", str(FIXTURE))
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    hooks.session_start()
    out = json.loads(capsys.readouterr().out)
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert "## Communities" in ctx  # never crashes; degrades to legacy map


def test_session_start_malformed_toc_shape_degrades_to_legacy_map_not_empty(
    tmp_path, monkeypatch, capsys
):
    """Valid JSON, truthy ``domains``, but the wrong inner shape (not a list of dicts) must
    make ``render_toc`` raise -- caught locally so the hook falls back to the legacy
    ``top_tier_map``, not all the way out to the outer handler's ``{}`` (which would lose
    the store-only content too, not just the domain TOC).

    Stamps ``last_synced_graph_version`` to the fixture's own version first so the lazy
    sync inside ``session_start`` is a no-op and does not itself overwrite the malformed
    cache with a freshly (validly) rebuilt one before the read-back below."""
    from sidegraph.engine.reader import GraphifyReader
    from sidegraph.retrieval import TOC_CACHE_KEY
    from sidegraph.store import Store
    from sidegraph.sync import LAST_SYNCED_KEY

    db = tmp_path / "s.db"
    store = Store(db)
    store.set_meta(LAST_SYNCED_KEY, GraphifyReader(FIXTURE).graph_version())
    store.set_meta(
        TOC_CACHE_KEY,
        json.dumps({"domains": "not-a-list-of-dicts", "initiatives": [], "global_mistakes": []}),
    )

    monkeypatch.setenv("SIDEGRAPH_DB", str(db))
    monkeypatch.setenv("SIDEGRAPH_GRAPH", str(FIXTURE))
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    hooks.session_start()
    out = json.loads(capsys.readouterr().out)
    assert out != {}  # never degrades all the way to a blank payload
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert "## Communities" in ctx  # legacy map rendered, not a crash


def _store_with_pending(tmp_path):
    from datetime import UTC, datetime

    from sidegraph.schema import Decision, DecisionKind, DecisionStatus, Provenance
    from sidegraph.store import Store

    store = Store(tmp_path / "s.db")
    store.add_decision(
        Decision(
            title="t",
            kind=DecisionKind.ADR,
            status=DecisionStatus.PROPOSED,
            context="c",
            choice="ch",
            valid_from=datetime.now(UTC),
            provenance=Provenance(source="agent"),
        )
    )
    return store


def test_session_start_shows_pending_ratification_line(tmp_path, monkeypatch, capsys):
    _store_with_pending(tmp_path)
    monkeypatch.setenv("SIDEGRAPH_DB", str(tmp_path / "s.db"))
    monkeypatch.setenv("SIDEGRAPH_GRAPH", str(tmp_path / "no-graph.json"))  # reader=None
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    hooks.session_start()
    ctx = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
    assert "1 record(s) awaiting ratification" in ctx
    assert "(1 decisions, 0 facts, 0 domains; oldest 0 days)" in ctx


def test_session_start_no_pending_line_when_queue_empty(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SIDEGRAPH_DB", str(tmp_path / "s.db"))
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    hooks.session_start()
    ctx = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
    assert "awaiting ratification" not in ctx


def test_session_start_ratify_nudge_off_suppresses_line(tmp_path, monkeypatch, capsys):
    _store_with_pending(tmp_path)
    monkeypatch.setenv("SIDEGRAPH_DB", str(tmp_path / "s.db"))
    monkeypatch.setenv("SIDEGRAPH_RATIFY_NUDGE", "off")
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    hooks.session_start()
    ctx = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
    assert "awaiting ratification" not in ctx


def test_session_start_pending_count_failure_keeps_map(tmp_path, monkeypatch, capsys):
    _store_with_pending(tmp_path)
    monkeypatch.setenv("SIDEGRAPH_DB", str(tmp_path / "s.db"))
    monkeypatch.setattr("sys.stdin", io.StringIO(""))

    def boom(self):
        raise RuntimeError("boom")

    monkeypatch.setattr("sidegraph.store.Store.pending_ratification_counts", boom)
    hooks.session_start()
    out = json.loads(capsys.readouterr().out)
    ctx = out["hookSpecificOutput"]["additionalContext"]  # map survived, no crash
    assert "awaiting ratification" not in ctx


def test_session_start_malformed_graph_degrades_to_store_only(tmp_path, monkeypatch, capsys):
    from sidegraph.schema import Initiative
    from sidegraph.store import Store

    # a store with an initiative (store-only content, needs no reader)
    db = tmp_path / "s.db"
    Store(db).upsert_initiative(Initiative(name="Trading Core"))
    # valid JSON but the wrong shape (nodes is a str -> AttributeError on parse)
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"built_at_commit": "x", "nodes": "not-a-list"}))
    monkeypatch.setenv("SIDEGRAPH_DB", str(db))
    monkeypatch.setenv("SIDEGRAPH_GRAPH", str(bad))
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    hooks.session_start()
    out = json.loads(capsys.readouterr().out)
    ctx = out["hookSpecificOutput"]["additionalContext"]  # NOT {} — store-only survived
    assert "Trading Core" in ctx
    assert "get_task_context" in ctx


# -- D7.2: double-injection dedupe (E7 observation) ----------------------------------------


def _run_session_start(monkeypatch, capsys, db, session_id=None):
    monkeypatch.setenv("SIDEGRAPH_DIR", str(db))
    payload = {"session_id": session_id} if session_id is not None else {}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    hooks.session_start()
    return json.loads(capsys.readouterr().out)


def test_second_session_start_within_window_same_session_is_silent(tmp_path, monkeypatch, capsys):
    db = tmp_path / "s.db"
    first = _run_session_start(monkeypatch, capsys, db, session_id="dup-1")
    assert "hookSpecificOutput" in first  # the first call emits normally

    second = _run_session_start(monkeypatch, capsys, db, session_id="dup-1")
    assert second == {}  # the duplicate exits silently


def test_session_start_emits_for_a_different_session_id(tmp_path, monkeypatch, capsys):
    db = tmp_path / "s.db"
    _run_session_start(monkeypatch, capsys, db, session_id="dup-2a")
    second = _run_session_start(monkeypatch, capsys, db, session_id="dup-2b")
    assert "hookSpecificOutput" in second  # a different session is never a duplicate


def test_session_start_emits_again_after_dedupe_window_expires(tmp_path, monkeypatch, capsys):
    from sidegraph.store import Store

    db = tmp_path / "s.db"
    stale = (datetime.now(UTC) - timedelta(seconds=61)).isoformat()
    monkeypatch.setenv("SIDEGRAPH_DIR", str(db))
    Store(db).set_meta(hooks._SESSION_START_KEY, f"dup-3|{stale}")

    out = _run_session_start(monkeypatch, capsys, db, session_id="dup-3")
    assert "hookSpecificOutput" in out  # window expired -- not a duplicate


def test_session_start_naive_stamp_is_not_suppressed_and_self_heals(tmp_path, monkeypatch, capsys):
    """CORRECTION-6 (code review): a naive-but-parseable prev_stamp used to raise
    TypeError on the `now - prev_dt` subtraction, escape past the inner except (which only
    catches ValueError), and skip the set_meta write entirely -- the ledger got stuck on
    the bad stamp forever. Fixed: treated as "not a duplicate", AND the ledger
    self-heals (rewritten to a fresh, valid, AWARE stamp) so the next call compares
    correctly."""
    from sidegraph.store import Store

    db = tmp_path / "s.db"
    naive = datetime.now().isoformat()  # no tzinfo -- schema requires aware, this doesn't have it
    monkeypatch.setenv("SIDEGRAPH_DIR", str(db))
    Store(db).set_meta(hooks._SESSION_START_KEY, f"dup-naive|{naive}")

    out = _run_session_start(monkeypatch, capsys, db, session_id="dup-naive")
    assert "hookSpecificOutput" in out  # not suppressed

    store = Store(db)
    raw = store.get_meta(hooks._SESSION_START_KEY)
    _, _, rewritten_stamp = raw.partition("|")
    assert datetime.fromisoformat(rewritten_stamp).tzinfo is not None  # self-healed to aware
    store.close()


def test_session_start_dedupe_ignores_payload_without_session_id(tmp_path, monkeypatch, capsys):
    """Every existing session_start test omits session_id entirely — the dedupe guard
    must never touch that path (design D7.2 is gated on a real session id)."""
    db = tmp_path / "s.db"
    first = _run_session_start(monkeypatch, capsys, db)
    second = _run_session_start(monkeypatch, capsys, db)
    assert "hookSpecificOutput" in first
    assert "hookSpecificOutput" in second  # never silenced without a session id


def test_session_start_duplicate_helper_never_raises_on_store_failure(monkeypatch):
    """Direct unit test of `_session_start_duplicate`'s own resilience (best-effort, per
    its docstring): a `get_meta` failure returns False (never a duplicate) rather than
    propagating -- this is what actually protects `session_start()`'s call site, which has
    no wrapping try/except of its own around this call."""

    class _BrokenStore:
        def get_meta(self, key):
            raise RuntimeError("boom")

        def set_meta(self, key, value):
            raise RuntimeError("boom")

    assert hooks._session_start_duplicate(_BrokenStore(), "s1", datetime.now(UTC)) is False
