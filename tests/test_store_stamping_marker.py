"""``stamping_live_since``: the store's creation marker (design brief
.superpowers/sdd/2026-09-15-doctor-blind-window/brief.md).

``doctor.py``'s ``unratified-accept`` check scopes its scan to records created at/after
the earliest ``ratified_at`` stamp in the store, and reports nothing at all when the store
has no stamp to scope from (see that check's docstring). A store that has never ratified
anything therefore stays blind to the exact bug the check exists to catch, forever — even
after a later ratification arms the check, because every record already there predates
that first stamp. This marker gives the check an alternative scope-start that does not
depend on any ratification ever having happened: the instant THIS store came into being,
recorded once, by a version that already writes it.

Every test here is store-side (the write path — a store contract, per CLAUDE.md). Which
target is genuinely red against unfixed (pre-marker) code and which is a guard that cannot
fail against a correct implementation:

- ``test_new_store_gets_the_marker`` — RED pre-fix (the method/file does not exist yet;
  ``AttributeError``/missing file).
- ``test_reopening_a_still_empty_new_store_does_not_rewrite_the_marker`` — a guard: proves
  idempotency of the (new) mechanism, cannot fail differently against unfixed code because
  unfixed code has no marker to rewrite in the first place.
- ``test_existing_store_never_acquires_the_marker_after_the_fact`` — a guard for the same
  reason: pins the non-negotiable "never backfilled" rule going forward.
- ``test_two_openers_on_a_first_ever_open_leave_one_valid_marker`` — a guard once the
  mechanism exists (there is no unfixed-code equivalent to race against), but it is the one
  place the brief's race requirement is actually exercised end to end.
- ``test_store_with_everything_archived_does_not_get_the_marker`` — pins this session's
  ruling on the empty-but-archived case (see docstring below); a guard against a REGRESSION
  of that ruling, not a bug this session found.
- ``test_forced_interleaving_keeps_the_earlier_marker_not_the_last_writer`` (fix round 2,
  review Minor 1) — RED against the pre-round-2 `_atomic_write_text_race_tolerant`-based
  publish (measured: last-writer-wins let a LATER opener's marker replace an EARLIER one
  that already had a real record written after it, so the marker ends up newer than a
  record it was supposed to predate). GREEN against the keep-first ``os.link`` publish.
- ``test_marker_never_acquired_regardless_of_which_subdir_holds_the_record`` (fix round 2,
  review Minor 5) — parametrized guard: kills the mutant that restricts
  ``_store_has_any_records`` to ``decisions/`` alone, which every un-parametrized test above
  happened not to reach (all of them used a decision as their "existing record").
"""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime

import pytest

import sidegraph.store as store_module
from sidegraph.schema import Decision, DecisionKind, Provenance
from sidegraph.store import _CANONICAL_SUBDIRS, _STAMPING_MARKER_NAME, Store


def _decision(title: str) -> Decision:
    return Decision(
        title=title,
        kind=DecisionKind.ADR,
        context="c",
        choice="ch",
        valid_from=datetime.now(UTC),
        provenance=Provenance(source="manual"),
    )


def test_new_store_gets_the_marker(tmp_path):
    db = tmp_path / "s"
    Store(db).close()

    marker = db / _STAMPING_MARKER_NAME
    assert marker.is_file()
    text = marker.read_text(encoding="utf-8")
    assert text.endswith("\n")
    stamped = datetime.fromisoformat(text.strip())
    assert stamped.tzinfo is not None  # aware, like every other timestamp this store writes
    # sanity: the stamp is close to "now", not some placeholder/epoch value
    assert abs((datetime.now(UTC) - stamped).total_seconds()) < 60


def test_reopening_a_still_empty_new_store_does_not_rewrite_the_marker(tmp_path):
    db = tmp_path / "s"
    Store(db).close()
    marker = db / _STAMPING_MARKER_NAME
    original = marker.read_text(encoding="utf-8")

    # Reopen twice more, still with zero records — the marker must be untouched, not
    # re-derived from "now" on every open.
    Store(db).close()
    Store(db).close()

    assert marker.read_text(encoding="utf-8") == original


def test_existing_store_never_acquires_the_marker_after_the_fact(tmp_path):
    """Non-negotiable (brief + CLAUDE.md-adjacent write-path rule): a store that already
    held a record the first time a stamping version opened it must never get this marker,
    on any later open, no matter how many times it is reopened afterward.

    Simulated the same way a real legacy store would look to this code: records present,
    marker absent. The cleanest way to construct exactly that state without hand-crafting
    canonical JSON is to let a real (new) store earn its marker, add a record, then delete
    the marker — reproducing "a store version that never wrote this file, with records
    already in it" byte-for-byte. What's under test is what happens on the NEXT open, not
    the deletion itself.
    """
    db = tmp_path / "s"
    with Store(db) as store:
        store.add_decision(_decision("first ever record"))
    marker = db / _STAMPING_MARKER_NAME
    assert marker.is_file()  # sanity: it WAS written on genuine creation
    marker.unlink()

    with Store(db) as reopened:
        assert not marker.is_file()
        reopened.add_decision(_decision("second record, post simulated legacy"))
    assert not marker.is_file()

    # Repeated opens of this now-legacy-shaped store keep not acquiring it.
    Store(db).close()
    Store(db).close()
    assert not marker.is_file()


def test_store_with_everything_archived_does_not_get_the_marker(tmp_path):
    """Ruling (brief's open question, pinned here): an empty-but-archived store — every
    decision compacted into an ``archive/*.jsonl`` segment, zero hot files left — is an
    EXISTING store with real history, not a new one. Treating empty hot directories alone
    as "no records" would stamp ``stamping_live_since`` at the moment of THIS open, long
    after the store's (and its records') real creation — backfilling the exact thing this
    marker must never do. Archive segments count as records for this check.

    Constructed the same simulated-legacy way as the test above: earn the marker
    honestly via genuine creation, compact everything into the archive, delete the marker
    to reproduce "a pre-marker version's store that has since been fully compacted", then
    reopen and confirm the marker is NOT written back despite every hot directory being
    empty.
    """
    db = tmp_path / "s"
    with Store(db) as store:
        d = store.add_decision(_decision("will be rejected and archived"))
        store.drop(d.id)
        report = store.compact()
        assert report.decisions_compacted == 1
        assert not (db / "decisions" / f"{d.id}.json").exists()  # hot file gone
        assert any((db / "archive").glob("*.jsonl"))  # archived instead

    marker = db / _STAMPING_MARKER_NAME
    marker.unlink()

    with Store(db):
        pass
    assert not marker.is_file()


def test_two_openers_on_a_first_ever_open_leave_one_valid_marker(tmp_path):
    """The race the brief calls out by name: two openers reaching a store's genuinely
    first-ever open at the same time must leave exactly one valid marker behind, never a
    crash, a truncated/empty file, or two files. Racing writers here do NOT write
    identical content (each stamps its own ``datetime.now(UTC)``), unlike the format
    marker/.gitignore case — but the ``os.link`` keep-first publish (fix round 2; see
    ``Store._ensure_stamping_marker``) tolerates that by construction: only the very first
    successful link is ever visible, so there is only ever one file to observe, never a
    torn or empty one.

    Threads (not separate processes): the race lives entirely in filesystem calls inside
    `_ensure_stamping_marker` (tmp-write + ``os.link``), which release the GIL, and each
    opener is its own ``Store`` instance with its own independent lock — nothing here
    serializes two `Store(path)` constructions on the same fresh directory. Mirrors the
    store's own established practice for this exact race class (see
    ``test_store_concurrent_open.py``'s cross-process probe for the format marker/
    .gitignore). Unforced races like this one don't reproduce the last-writer-wins bug
    review round 2 found (measured: 0 bad across many rounds here and in the reviewer's
    own probe) — see ``test_forced_interleaving_keeps_the_earlier_marker_not_the_last_writer``
    below for the deterministic reproduction that does.
    """
    for round_no in range(20):
        db = tmp_path / f"round-{round_no}"
        barrier = threading.Barrier(6)
        errors: list[BaseException] = []

        def worker(db=db, barrier=barrier, errors=errors) -> None:
            try:
                barrier.wait(timeout=10)
                Store(db).close()
            except BaseException as e:  # noqa: BLE001 - surfaced via `errors`, not a crash
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert not errors, f"round {round_no}: {errors}"

        marker = db / _STAMPING_MARKER_NAME
        assert marker.is_file(), f"round {round_no}: marker missing"
        text = marker.read_text(encoding="utf-8")
        assert text.endswith("\n")
        stamped = datetime.fromisoformat(text.strip())  # never truncated/corrupted
        assert stamped.tzinfo is not None


def test_forced_interleaving_keeps_the_earlier_marker_not_the_last_writer(tmp_path, monkeypatch):
    """Review round 2, Minor 1 (a real bug, not a nit): forced interleaving of two
    openers' first-ever open. Confirmed RED against the pre-round-2, ``os.replace``-based
    publish (extracted commit ``1964cd6`` via ``git archive`` into the scratchpad, run
    with ``PYTHONDONTWRITEBYTECODE=1``, ``sidegraph.__file__`` printed to confirm the
    extract was what imported): the marker ends up LATER than a record written before it,
    exactly this test's failure mode. Unforced races never reproduce it (measured: 0 bad
    across the 20 rounds x 6 threads above, and the reviewer's own 6-process x 25-round
    probe) — only a forced interleaving reaches the window.

    Scenario:
    1. Opener A reaches its "no records yet" check and pauses there (patched below,
       mirroring exactly where the real race lives: between the check and the write).
    2. Opener B, released once A is paused, opens for the first time (publishes an
       EARLIER marker via keep-first ``os.link``), then adds a real record — a later
       "bypass" decision, i.e. exactly the shape `_check_unratified_accepts` hunts.
    3. A resumes, computes its OWN LATER timestamp, and attempts to publish too.

    Keep-first must leave B's EARLIER marker in place, predating the bypass record; A's
    later attempt must be silently discarded via ``FileExistsError``, never replacing it.
    """
    db = tmp_path / "s"
    a_reached_check = threading.Event()
    b_done = threading.Event()
    real_has_records = store_module._store_has_any_records

    def patched(path):
        # Compute the (stale) result BEFORE pausing -- exactly what a genuine
        # check-then-write race looks like: A already observed "no records" and is about
        # to act on that observation while B changes the world underneath it.
        result = real_has_records(path)
        if not a_reached_check.is_set():
            a_reached_check.set()
            assert b_done.wait(timeout=10), "opener B never finished"
        return result

    monkeypatch.setattr(store_module, "_store_has_any_records", patched)

    bypass_record: dict[str, datetime] = {}

    def opener_b() -> None:
        assert a_reached_check.wait(timeout=10), "opener A never reached its check"
        with Store(db) as store:
            d = store.add_decision(
                Decision(
                    title="bypass",
                    kind=DecisionKind.ADR,
                    context="c",
                    choice="ch",
                    valid_from=datetime.now(UTC),
                    provenance=Provenance(source="agent"),
                )
            )
            bypass_record["valid_from"] = d.valid_from
        b_done.set()

    t_b = threading.Thread(target=opener_b)
    t_b.start()

    Store(db).close()  # opener A
    t_b.join(timeout=15)

    marker = db / _STAMPING_MARKER_NAME
    assert marker.is_file()
    stamped = datetime.fromisoformat(marker.read_text(encoding="utf-8").strip())
    assert stamped < bypass_record["valid_from"], (
        f"marker {stamped} postdates the bypass record {bypass_record['valid_from']} "
        "written before it -- last-writer-wins regression"
    )


@pytest.mark.parametrize("subdir", _CANONICAL_SUBDIRS)
def test_marker_never_acquired_regardless_of_which_subdir_holds_the_record(tmp_path, subdir):
    """Review round 2, Minor 5: kills the mutant that restricts ``_store_has_any_records``
    to ``decisions/`` alone, which survived all of fix round 1's tests because every one of
    them happened to use a decision as its "existing record". A bare file in ANY of the
    six canonical subdirs must block the marker — a domains-only store (the normal shape
    right after ``sidegraph-domains bootstrap``, before any decision exists) is real, not
    hypothetical.

    ``_store_has_any_records`` itself only checks file EXISTENCE under each subdir (see its
    docstring), never content — but ``Store.__init__`` still cold-reloads and
    pydantic-validates every canonical file on a fresh index regardless, so each
    placeholder below is the minimal SCHEMA-VALID shape for its kind (a plain ``[]`` for
    ``bindings``, which is a list of entries, not a record file itself), written directly
    to disk before ``Store`` ever opens this directory.
    """
    minimal_content = {
        "decisions": {
            "title": "x",
            "kind": "adr",
            "context": "c",
            "choice": "ch",
            "valid_from": "2026-01-01T00:00:00+00:00",
            "provenance": {"source": "manual"},
        },
        "facts": {
            "statement": "x",
            "source": "y",
            "valid_from": "2026-01-01T00:00:00+00:00",
            "provenance": {"source": "manual"},
        },
        "domains": {
            "slug": "x",
            "title": "x",
            "summary": "x",
            "provenance": {"source": "manual"},
        },
        "entities": {"canonical_name": "x"},
        "bindings": [],
        "initiatives": {"name": "x"},
    }[subdir]

    db = tmp_path / "s"
    (db / subdir).mkdir(parents=True)
    (db / subdir / "placeholder.json").write_text(json.dumps(minimal_content), encoding="utf-8")

    Store(db).close()

    assert not (db / _STAMPING_MARKER_NAME).is_file()
