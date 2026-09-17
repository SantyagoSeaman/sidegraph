"""The sweep must not be able to see a live write (design D4).

_sweep_stale_tmp_files cannot tell crash debris from another opener's in-flight tmp file, so
it used to delete live buffers and _atomic_write_text_race_tolerant absorbed that with 3
retries -- a constant smaller than the number of concurrent openers it has to survive. An age
gate removes the sweep's ability to see a live buffer at all: debris is old by definition, a
live buffer lives microseconds.
"""

from __future__ import annotations

import os
import time

from sidegraph.store import Store


def _age(path, seconds: float) -> None:
    t = time.time() - seconds
    os.utime(path, (t, t))


def test_a_fresh_tmp_survives_a_concurrent_open(tmp_path):
    """The failure this closes: another process's open deleting a live in-flight buffer."""
    store = Store(tmp_path / "s")
    store.close()

    live = tmp_path / "s" / "decisions" / "01AAA.json.tmp"
    live.write_text("{}")

    reopened = Store(tmp_path / "s")
    reopened.close()

    assert live.exists(), "the sweep deleted a live in-flight tmp buffer"


def test_a_fresh_root_tmp_survives_a_concurrent_open(tmp_path):
    store = Store(tmp_path / "s")
    store.close()

    live = tmp_path / "s" / "format.12345.deadbeef0000.tmp"
    live.write_text("x")

    reopened = Store(tmp_path / "s")
    reopened.close()

    assert live.exists()


def test_a_fresh_archive_tmp_survives_a_concurrent_open(tmp_path):
    """archive/ is the THIRD _unlink_if_stale call site (the other two are the store root and
    the canonical record dirs). Without this, forgetting to route it through the age gate
    stays green."""
    store = Store(tmp_path / "s")
    store.close()

    archive = tmp_path / "s" / "archive"
    archive.mkdir(exist_ok=True)
    live = archive / "segment-live.jsonl.tmp"
    live.write_text("{}")

    reopened = Store(tmp_path / "s")
    reopened.close()

    assert live.exists()


def test_aged_debris_is_still_swept(tmp_path):
    """The gate must not turn the sweep off: real crash debris still goes, at all three
    call sites."""
    store = Store(tmp_path / "s")
    store.close()

    archive = tmp_path / "s" / "archive"
    archive.mkdir(exist_ok=True)

    debris = [
        tmp_path / "s" / "decisions" / "leftover.json.tmp",
        tmp_path / "s" / "format.999.abcdef000000.tmp",
        archive / "segment-leftover.jsonl.tmp",
    ]
    for path in debris:
        path.write_text("{}")
        _age(path, 3600)

    reopened = Store(tmp_path / "s")
    reopened.close()

    for path in debris:
        assert not path.exists(), path
