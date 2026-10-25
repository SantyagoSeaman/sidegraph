"""Shared builders for the non-file-entry store tests (a directory, FIFO or dangling link named
``*.json`` in a record directory).

Support module (no tests): the public tests and their release-time FIFO companions use it.
see design/superpowers/specs/2026-10-03-store-survives-a-bad-file-design.md (D1, D2)
"""

from __future__ import annotations

import os
import signal
from datetime import UTC, datetime
from pathlib import Path

import pytest

from sidegraph.schema import Decision, DecisionKind, Provenance
from sidegraph.store import Store

NOT_A_FILE = "not a regular file"
UNSAFE_NAME = "unsafe filename"
SAFE = "01ABCDEFGHJKMNPQRSTVWXYZ00"  # a well-formed id, never a real record's
SEGMENT = "2020-01-01-1-deadbeef0000.jsonl"

needs_fifo = pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="os.mkfifo is unavailable")


def _prov() -> Provenance:
    return Provenance(source="manual")


def _seed(tmp_path: Path) -> tuple[Path, Decision]:
    """A closed store holding one decision; every case plants one entry and reads it back."""
    db = tmp_path / ".sidegraph"
    with Store(db) as s:
        decision = s.add_decision(
            Decision(
                title="an adr",
                kind=DecisionKind.ADR,
                context="c",
                choice="ch",
                valid_from=datetime.now(UTC),
                provenance=_prov(),
            )
        )
    return db, decision


class _Timeout:
    """Fails a FIFO test where the unfixed code would block for ever. The alarm's exception is a
    ``TimeoutError``, an ``OSError`` that production code may catch and report as an unreadable
    file, so the block also records that the alarm fired and ``__exit__`` fails on it."""

    def __enter__(self) -> None:
        self.fired = False

        def fire(*_: object) -> None:
            self.fired = True
            raise TimeoutError("blocked reading a FIFO")

        self._old = signal.signal(signal.SIGALRM, fire)
        signal.alarm(10)

    def __exit__(self, *_: object) -> None:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, self._old)
        assert not self.fired, "blocked reading a FIFO"
