"""A reload re-checks the digest under the write reservation.

A process that sees a digest mismatch while another process is between publishing a canonical
file and committing the index used to rebuild the whole index the moment the writer committed,
although the commit had made the index current. The rebuild resets the sync-derived rows and
marks membership uncertified, which silences the PreToolUse hook until the next sync.
see design/superpowers/specs/2026-11-05-hook-wait-and-digest-recheck.md (D3)
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from sidegraph.schema import Decision, DecisionKind, DecisionStatus, Provenance
from sidegraph.store import Store


def _decision(title: str) -> Decision:
    return Decision(
        title=title,
        kind=DecisionKind.GOTCHA,
        status=DecisionStatus.ACCEPTED,
        context="c",
        choice=title,
        valid_from=datetime.now(UTC),
        provenance=Provenance(source="manual"),
    )


def _count_rebuilds(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record every full rebuild (format 1 or 2) a Store performs."""
    calls: list[str] = []
    for name in ("_rebuild_format1_in_transaction", "_rebuild_index_in_transaction"):
        original = getattr(Store, name)

        def spy(self, *args, _original=original, _name=name, **kwargs):
            calls.append(_name)
            return _original(self, *args, **kwargs)

        monkeypatch.setattr(Store, name, spy)
    return calls


def test_a_digest_that_looked_stale_but_matches_under_the_lock_does_not_rebuild(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The first walk sees the writer's in-flight file (a digest that is not the stored one); by
    the time this process holds the reservation the writer has committed and the walk matches.
    Red against the unchecked reload: it rebuilds once."""
    store = Store(tmp_path / "s")
    store.add_decision(_decision("one"))
    real = Store._compute_canonical_digest
    walks = {"n": 0}

    def in_flight_then_settled(self):
        walks["n"] += 1
        digest, stats = real(self)
        return ("in-flight-" + digest, stats) if walks["n"] == 1 else (digest, stats)

    monkeypatch.setattr(Store, "_compute_canonical_digest", in_flight_then_settled)
    calls = _count_rebuilds(monkeypatch)

    store.refresh_if_stale()

    assert walks["n"] >= 2, "the reload never walked the files again under the lock"
    assert calls == []
    store.close()


def test_a_real_change_between_the_checks_still_rebuilds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Passes before and after: a file that really changes after the first walk and before the
    walk under the reservation must still be rebuilt, with the digest of the second walk."""
    root = tmp_path / "s"
    store = Store(root)
    store.add_decision(_decision("one"))
    record = next(
        p
        for p in sorted(root.rglob("*"))
        if p.is_file() and p.suffix in {".json", ""} and p.parent.name in {"decisions", "shards"}
    )
    real = Store._compute_canonical_digest
    walks = {"n": 0}
    second_walk: list[str] = []

    def changes_after_the_first_walk(self):
        walks["n"] += 1
        digest, stats = real(self)
        if walks["n"] == 1:
            stat = record.stat()
            os.utime(record, ns=(stat.st_atime_ns, stat.st_mtime_ns + 2_000_000_000))
            return "in-flight-" + digest, stats
        if walks["n"] == 2:
            second_walk.append(digest)
        return digest, stats

    monkeypatch.setattr(Store, "_compute_canonical_digest", changes_after_the_first_walk)
    calls = _count_rebuilds(monkeypatch)

    store.refresh_if_stale()

    assert len(calls) == 1
    # The rebuild stamps what the second walk saw, not what the first walk reported.
    assert second_walk and store.get_meta("canonical_digest") == second_walk[0]
    store.close()


def test_a_forced_reload_does_not_recheck(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Passes before and after: a reload the caller asked for (a stale skip list, a stamp
    re-write) is not a race and must rebuild even when the digest matches."""
    store = Store(tmp_path / "s")
    store.add_decision(_decision("one"))
    calls = _count_rebuilds(monkeypatch)
    digest, _stats = store._compute_canonical_digest()

    with store._lock:
        store._reload_index_from_canonical(digest)

    assert len(calls) == 1
    store.close()


_WRITER = """
import sys, time
from datetime import UTC, datetime
from sidegraph.schema import Decision, DecisionKind, DecisionStatus, Provenance
from sidegraph.store import Store
store = Store(sys.argv[1])
print("ready", flush=True)
for n in range(int(sys.argv[2])):
    store.add_decision(Decision(title=f"w{n}", kind=DecisionKind.GOTCHA,
        status=DecisionStatus.ACCEPTED, context="c", choice=f"w{n}",
        valid_from=datetime.now(UTC), provenance=Provenance(source="manual")))
    time.sleep(0.04)
print("done", flush=True)
"""


def test_another_process_writing_does_not_make_this_one_rebuild(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The load-research repro: another process adds decisions 40 ms apart while this one checks the
    files in a loop. Red against the unchecked reload: about 28 of 30 writes cost a rebuild."""
    root = tmp_path / "s"
    Store(root).close()
    writer = subprocess.Popen(
        [sys.executable, "-c", _WRITER, str(root), "30"],
        stdout=subprocess.PIPE,
        text=True,
    )
    assert writer.stdout is not None
    assert writer.stdout.readline().strip() == "ready"
    store = Store(root)
    calls = _count_rebuilds(monkeypatch)
    deadline = time.monotonic() + 60
    while writer.poll() is None and time.monotonic() < deadline:
        store.refresh_if_stale()
    writer.wait(timeout=30)
    store.refresh_if_stale()

    assert calls == []
    store.close()


def test_a_file_that_vanished_since_the_index_loaded_it_still_rebuilds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Red against a recheck that trusts the digest alone: a writer's stamp ignores a file that
    disappeared (a checkout removing a record), so digest and stamp match while the index still
    holds the removed record (found by the Sol review round 1)."""
    root = tmp_path / "s"
    store = Store(root)
    gone = store.add_decision(_decision("removed by a checkout"))
    store.add_decision(_decision("kept"))
    record = next(
        p for p in root.rglob(f"{gone.id}*") if p.is_file() and p.parent.name == "decisions"
    )
    record.unlink()
    with store._mutation():
        store._touch_digest()  # the unrelated writer's stamp: the removed file is ignored
    digest, _stats = store._compute_canonical_digest()
    assert store.get_meta("canonical_digest") == digest
    calls = _count_rebuilds(monkeypatch)

    with store._lock:
        store._reload_index_from_canonical(digest, recheck=True)

    assert len(calls) == 1
    assert store.get_decision(gone.id) is None
    store.close()
