"""Retrieval telemetry storage (design/superpowers/specs/
2026-07-25-retrieval-telemetry-design.md §3).

Index-only by construction: the whole point is that reading the store never dirties git."""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime

import sidegraph.store as store_module
from sidegraph.schema import Decision, DecisionKind, DecisionStatus, Provenance
from sidegraph.store import Store


def _record_render(store):
    store.record_render_event(
        "sess-1",
        intent=None,
        selected=5,
        emitted=3,
        degraded=1,
        dropped_for_budget=2,
        chars_used=4200,
        had_rejected=True,
        had_superseded=False,
    )


def test_records_shows_and_seeds(tmp_path):
    store = Store(tmp_path / "s")
    store.record_retrieval(["r1", "r2"], ["a.py"])
    assert store.retrieval_shows() == {"r1": 1, "r2": 1}
    assert store.retrieval_seed_queries() == {"a.py": 1}
    store.close()


def test_a_seed_counts_once_per_call_not_once_per_record(tmp_path):
    """Otherwise a query returning ten records would look like ten visits to the area,
    and the ratio the whole report rests on would be meaningless."""
    store = Store(tmp_path / "s")
    store.record_retrieval(["r1", "r2", "r3"], ["a.py"])
    assert store.retrieval_seed_queries() == {"a.py": 1}
    store.close()


def test_counts_accumulate_across_calls(tmp_path):
    store = Store(tmp_path / "s")
    store.record_retrieval(["r1"], ["a.py"])
    store.record_retrieval(["r1"], ["a.py", "b.py"])
    assert store.retrieval_shows()["r1"] == 2
    assert store.retrieval_seed_queries() == {"a.py": 2, "b.py": 1}
    store.close()


def test_last_shown_at_and_last_seen_at_advance_across_calls(tmp_path, monkeypatch):
    """§4.3: repeated calls accumulate rather than overwrite, and `last_shown_at`/
    `last_seen_at` advance. Nothing reads these two columns today (only the counts are
    exposed via `retrieval_shows()`/`retrieval_seed_queries()`), so pin them here directly
    against the raw index -- otherwise this spec clause has no test at all."""
    ticks = iter([datetime(2026, 1, 1, tzinfo=UTC), datetime(2026, 1, 2, tzinfo=UTC)])

    class _Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return next(ticks)

    store = Store(tmp_path / "s")
    monkeypatch.setattr(store_module, "datetime", _Clock)  # only the two calls below tick
    store.record_retrieval(["r1"], ["a.py"])
    first_shown, first_seen = store._conn.execute(
        "SELECT (SELECT last_shown_at FROM retrieval_shows WHERE record_id = 'r1'),"
        "       (SELECT last_seen_at FROM retrieval_seeds WHERE seed = 'a.py')"
    ).fetchone()

    store.record_retrieval(["r1"], ["a.py"])
    second_shown, second_seen = store._conn.execute(
        "SELECT (SELECT last_shown_at FROM retrieval_shows WHERE record_id = 'r1'),"
        "       (SELECT last_seen_at FROM retrieval_seeds WHERE seed = 'a.py')"
    ).fetchone()

    assert second_shown > first_shown
    assert second_seen > first_seen
    store.close()


def test_a_repeated_id_within_one_call_counts_once(tmp_path):
    """One render showing the same record twice is still one showing."""
    store = Store(tmp_path / "s")
    store.record_retrieval(["r1", "r1"], ["a.py"])
    assert store.retrieval_shows() == {"r1": 1}
    store.close()


def test_empty_inputs_are_a_no_op(tmp_path):
    store = Store(tmp_path / "s")
    store.record_retrieval([], [])
    assert store.retrieval_shows() == {} and store.retrieval_seed_queries() == {}
    store.close()


def test_telemetry_survives_a_canonical_reload(tmp_path):
    """The reload DROP names six record tables explicitly, so the show and seed counters, the
    touch/seed event journal and the render-event journal survive by construction -- pinned
    here because someone could later add them to that list, and losing history on every
    `git pull` would make the counts meaningless for exactly the long-lived question they
    answer."""
    store = Store(tmp_path / "s")
    store.record_retrieval(["r1"], ["a.py"])
    store.record_touch("sess-1", "a.py", "Read")
    _record_render(store)
    store._reload_index_from_canonical("forced-digest")
    assert store.retrieval_shows() == {"r1": 1}
    assert store.retrieval_seed_queries() == {"a.py": 1}
    assert [e["key"] for e in store.retrieval_events()] == ["a.py"]
    assert len(store.render_events()) == 1
    store.close()


def test_recording_produces_no_canonical_diff(tmp_path):
    """The invariant most at risk from this feature: a READ must not dirty git. Every journal
    writer -- retrieval counters, touch/seed events and render events -- records into the one
    index, which git must ignore (asked directly: `git status --porcelain` collapses an
    untracked directory to one line, so it alone could not tell a tracked index.db).

    A real decision is committed first (fix-wave review, Minor-2): a bare empty store has
    no canonical record files at all, so the original version of this test could not have
    noticed a write landing in `decisions/` -- it only proved index.db churn stays
    gitignored, not that recording leaves committed files alone.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    store = Store(repo / ".sidegraph")
    store.add_decision(
        Decision(
            title="a real decision",
            kind=DecisionKind.ADR,
            status=DecisionStatus.ACCEPTED,
            context="c",
            choice="ch",
            valid_from=datetime.now(UTC),
            provenance=Provenance(source="manual"),
        )
    )
    store.close()
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "store"], cwd=repo, check=True)

    store = Store(repo / ".sidegraph")
    store.record_retrieval(["r1", "r2"], ["a.py", "b.py"])
    store.record_touch("sess-1", "a.py", "Read")
    store.record_retrieval_events("sess-1", ["a.py"], [("rec-1", "b.py")])
    _record_render(store)
    store.close()

    ignored = subprocess.run(
        ["git", "check-ignore", ".sidegraph/index.db"], cwd=repo, capture_output=True, text=True
    )
    assert ignored.returncode == 0, "index.db must be ignored by the store's own .gitignore"
    porcelain = subprocess.run(
        ["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True, check=True
    )
    assert porcelain.stdout == "", porcelain.stdout
