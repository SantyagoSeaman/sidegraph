"""A store can require a minimum Sidegraph version (the format marker's requirement line).

Every version in these tests is built from ``sidegraph.__version__``, so none of them goes
stale at a release bump.
see design/superpowers/specs/2026-10-14-graphify-upgrade-design.md (D6)
"""

from __future__ import annotations

import re
import shlex
import sqlite3
import threading
from contextlib import closing
from pathlib import Path

import pytest

import sidegraph.store as store_module
from sidegraph import __version__
from sidegraph.cli import require_main
from sidegraph.hot_index import HotIndex
from sidegraph.store import RequirementChange, Store
from sidegraph.store_layout import (
    REQUIRED_INDEX_STAMP,
    SCHEMA_VERSION,
    StoreRequirementError,
    check_store_requirement,
    store_requirement,
)
from tests.store_format2_support import legacy_store


def running_release() -> str:
    """This build's ``X.Y.Z`` (a dev or local suffix dropped): a requirement it meets."""
    match = re.match(r"(\d+)\.(\d+)\.(\d+)", __version__)
    assert match is not None, __version__
    return ".".join(match.groups())


def above_running() -> str:
    """The next minor after this build: a requirement it does not meet."""
    major, minor, _patch = (int(part) for part in running_release().split("."))
    return f"{major}.{minor + 1}.0"


def write_requirement_marker(store_dir: Path, version: str) -> None:
    """Stamp ``format`` by hand, as ``sidegraph-require`` does, so a reader's test does not
    depend on the writer under test."""
    (store_dir / "format").write_text(
        f"sidegraph-store 1.0.0\nrequires-sidegraph >= {version}\n", encoding="utf-8"
    )


def test_a_store_that_requires_a_newer_sidegraph_refuses_with_the_precise_message(tmp_path):
    """Red against unfixed code: the generic major message (and no StoreRequirementError)."""
    Store(tmp_path / "s").close()
    above = above_running()
    write_requirement_marker(tmp_path / "s", above)
    with pytest.raises(StoreRequirementError) as raised:
        Store(tmp_path / "s")
    assert str(raised.value) == f"This store requires Sidegraph >= {above}; this is {__version__}."
    assert (raised.value.kind, raised.value.required) == ("below", above)


def test_a_store_that_requires_this_release_opens(tmp_path):
    """Red against unfixed code: major 1 is refused."""
    Store(tmp_path / "s").close()
    write_requirement_marker(tmp_path / "s", running_release())
    with Store(tmp_path / "s") as store:
        assert store.path == tmp_path / "s"


@pytest.mark.parametrize(
    ("text", "detail"),
    [
        ("sidegraph-store 1.0.0\n", "exactly one"),
        (
            "sidegraph-store 1.0.0\nrequires-sidegraph >= 0.1.0\nrequires-sidegraph >= 0.2.0\n",
            "exactly one",
        ),
        ("sidegraph-store 1.0.0\nrequires-python >= 3.13.0\n", "is not 'requires-sidegraph >="),
        ("sidegraph-store 1.0.0\nrequires-sidegraph >= 0.14\n", "is not 'requires-sidegraph >="),
        ("sidegraph-store 1.0\nrequires-sidegraph >= 0.1.0\n", "is not 'sidegraph-store X.Y.Z'"),
        ("sidegraph-store 0.6.0\nrequires-sidegraph >= 0.1.0\n", "has one line"),
    ],
    ids=[
        "no-line",
        "two-lines",
        "unknown-line",
        "unparsable-version",
        "bad-marker-version",
        "0x-with-line",
    ],
)
def test_a_malformed_marker_is_refused_as_malformed(tmp_path, text, detail):
    """Red against unfixed code: the first five get the generic major message; the 0.x marker
    with a requirement line opens, so its requirement is silently ignored."""
    Store(tmp_path / "s").close()
    (tmp_path / "s" / "format").write_text(text, encoding="utf-8")
    with pytest.raises(StoreRequirementError, match="malformed store format marker") as raised:
        Store(tmp_path / "s")
    assert raised.value.kind == "malformed"
    assert detail in str(raised.value)


def test_a_dev_or_local_build_counts_as_its_release(tmp_path):
    """Red against comparing whole version strings, or refusing a non-X.Y.Z running version
    (both discarded)."""
    store_dir = tmp_path / "s"
    Store(store_dir).close()
    write_requirement_marker(store_dir, running_release())
    for running in (f"{running_release()}.dev3", f"{running_release()}+local.1"):
        assert check_store_requirement(store_dir, running=running) == running_release()
    write_requirement_marker(store_dir, above_running())
    with pytest.raises(StoreRequirementError, match="This store requires Sidegraph"):
        check_store_requirement(store_dir, running=f"{running_release()}.dev9")


def test_no_marker_and_a_one_line_marker_are_not_refused(tmp_path):
    """Guard, red against nothing: a store from before the marker (Store backfills it), no
    store at all, and today's one-line marker all pass."""
    assert check_store_requirement(tmp_path / "nowhere") is None
    Store(legacy_store(tmp_path / "s")).close()
    assert check_store_requirement(tmp_path / "s") is None


def test_an_unknown_major_keeps_the_upgrade_wording(tmp_path):
    """Red against unfixed code (a plain ValueError). It guards the wording SessionStart's fix
    text keys on."""
    Store(legacy_store(tmp_path / "s")).close()
    (tmp_path / "s" / "format").write_text("sidegraph-store 99.0.0\n", encoding="utf-8")
    with pytest.raises(StoreRequirementError, match="migration tooling is deferred") as raised:
        Store(legacy_store(tmp_path / "s"))
    assert raised.value.kind == "major"


def test_store_requirement_reads_the_line_and_compares_nothing(tmp_path):
    """Red against unfixed code (no function). Red against a reader that compares
    (discarded): the domain snapshot command and the engine-version advisory need the
    requirement of a store this version may not even open."""
    store_dir = tmp_path / "s"
    Store(legacy_store(store_dir)).close()
    assert store_requirement(store_dir) is None
    write_requirement_marker(store_dir, above_running())
    assert store_requirement(store_dir) == above_running()
    (store_dir / "format").write_text("sidegraph-store 1.0.0\n", encoding="utf-8")
    with pytest.raises(StoreRequirementError, match="malformed"):
        store_requirement(store_dir)
    assert store_requirement(tmp_path / "nowhere") is None


def test_refresh_checks_the_marker_again_only_when_it_changed(tmp_path, monkeypatch):
    """Red against unfixed code (refresh never reads the marker). Also red against a parse on
    every call (discarded: the MCP server refreshes before every tool call)."""
    import sidegraph.store as store_module

    calls: list[Path] = []
    real = store_module.check_store_requirement

    def counting(path, **kwargs):
        calls.append(Path(path))
        return real(path, **kwargs)

    monkeypatch.setattr(store_module, "check_store_requirement", counting)
    with Store(legacy_store(tmp_path / "s")) as store:
        calls.clear()
        store.refresh_if_stale()
        store.refresh_if_stale()
        assert calls == []
        write_requirement_marker(tmp_path / "s", running_release())
        store.refresh_if_stale()
        store.refresh_if_stale()
        assert calls == [tmp_path / "s"]


def test_refresh_detects_a_replaced_marker_with_the_same_size_and_mtime(tmp_path):
    import os

    store_dir = tmp_path / "s"
    Store(store_dir).close()
    above = above_running()
    # Match any component widths without assuming a particular release or digit boundary.
    previous = ".".join("0" * len(part) for part in above.split("."))
    write_requirement_marker(store_dir, previous)
    with Store(store_dir) as store:
        marker = store_dir / "format"
        before = marker.stat()
        pulled = store_dir / "pulled-format"
        pulled.write_text(
            f"sidegraph-store 1.0.0\nrequires-sidegraph >= {above}\n", encoding="utf-8"
        )
        os.utime(pulled, ns=(before.st_atime_ns, before.st_mtime_ns))
        os.replace(pulled, marker)
        after = marker.stat()
        assert (after.st_size, after.st_mtime_ns) == (before.st_size, before.st_mtime_ns)
        with pytest.raises(StoreRequirementError):
            store.refresh_if_stale()


def test_raising_the_requirement_stamps_the_marker_and_the_store_still_opens(tmp_path):
    """Red against unfixed code: no writer."""
    with Store(legacy_store(tmp_path / "s")) as store:
        change = store.raise_requirement(running_release())
    assert change == RequirementChange(tmp_path / "s" / "format", None, running_release())
    assert (tmp_path / "s" / "format").read_text(encoding="utf-8") == (
        f"sidegraph-store 1.0.0\nrequires-sidegraph >= {running_release()}\n"
    )
    Store(legacy_store(tmp_path / "s")).close()


def test_a_requirement_above_this_version_is_refused_and_nothing_is_written(tmp_path):
    marker = tmp_path / "s" / "format"
    with Store(tmp_path / "s") as store:
        before = marker.read_bytes()
        expected = f"cannot require Sidegraph >= {re.escape(above_running())}"
        with pytest.raises(ValueError, match=expected):
            store.raise_requirement(above_running())
    assert marker.read_bytes() == before


def test_a_requirement_is_never_lowered_and_asking_again_writes_nothing(tmp_path):
    marker = tmp_path / "s" / "format"
    with Store(tmp_path / "s") as store:
        store.raise_requirement(running_release())
        stamp = (marker.read_bytes(), marker.stat().st_mtime_ns)
        with pytest.raises(ValueError, match="never lowered"):
            store.raise_requirement("0.0.0")  # below every release
        again = store.raise_requirement(running_release())
    assert again.previous == again.required == running_release()
    assert (marker.read_bytes(), marker.stat().st_mtime_ns) == stamp


@pytest.mark.parametrize("version", ["0.14", "v0.14.0", "0.14.0.dev1", "latest"])
def test_a_requirement_must_be_a_release_version(tmp_path, version):
    with Store(tmp_path / "s") as store, pytest.raises(ValueError, match="release version X.Y.Z"):
        store.raise_requirement(version)


def test_the_marker_rewrite_is_atomic(tmp_path, monkeypatch):
    """Red against a bare write_text (discarded): a crash between the tmp write and the rename
    leaves the old marker whole."""
    marker = tmp_path / "s" / "format"
    with Store(legacy_store(tmp_path / "s")) as store:
        before = marker.read_bytes()

        def _boom(src, dst):
            raise OSError("simulated crash between tmp write and replace")

        monkeypatch.setattr(store_module.os, "replace", _boom)
        with pytest.raises(OSError, match="simulated crash"):
            store.raise_requirement(running_release())
    assert marker.read_bytes() == before
    assert list((tmp_path / "s").glob("format.*.tmp"))


def test_two_raises_at_once_never_lower_the_requirement(tmp_path, monkeypatch):
    """Red against a check and a publish outside the writer reservation (discarded). In that
    design the lower raise reads "no requirement" while the higher one sits between its check
    and its publish, then publishes last. Here the lower raise waits for the reservation, reads
    the higher requirement, and refuses."""
    store_dir = tmp_path / "s"
    Store(legacy_store(store_dir)).close()
    high, low = running_release(), "0.0.0"  # "0.0.0" is below every release
    first, second = Store(legacy_store(store_dir)), Store(legacy_store(store_dir))
    high_writing, low_started, low_checked, high_published = (threading.Event() for _ in range(4))
    real_write = store_module._atomic_write_text
    real_check = store_module.check_store_requirement

    def write(path, text):
        if f">= {high}\n" in text:
            high_writing.set()
            assert low_started.wait(timeout=5)
            # serialized: the low raise cannot check yet. The unserialized design checks within
            # microseconds of starting, so a short wait for that is enough to tell the two apart.
            low_checked.wait(timeout=0.3)
            stat = real_write(path, text)
            high_published.set()
            return stat
        high_published.wait(timeout=5)
        return real_write(path, text)

    def check(path, **kwargs):
        try:
            return real_check(path, **kwargs)
        finally:
            if threading.current_thread().name == "low":
                low_checked.set()

    monkeypatch.setattr(store_module, "_atomic_write_text", write)
    monkeypatch.setattr(store_module, "check_store_requirement", check)
    errors: dict[str, BaseException] = {}

    def raise_on(store: Store, version: str) -> None:
        if threading.current_thread().name == "low":
            low_started.set()
        try:
            store.raise_requirement(version)
        except BaseException as e:  # reported below
            errors[threading.current_thread().name] = e

    high_thread = threading.Thread(target=raise_on, args=(first, high), name="high")
    high_thread.start()
    assert high_writing.wait(timeout=5)
    low_thread = threading.Thread(target=raise_on, args=(second, low), name="low")
    low_thread.start()
    high_thread.join(timeout=15)
    low_thread.join(timeout=15)
    assert not high_thread.is_alive() and not low_thread.is_alive()
    first.close()
    second.close()
    assert "high" not in errors, errors
    assert isinstance(errors.get("low"), ValueError) and "never lowered" in str(errors["low"])
    assert store_requirement(store_dir) == high


def test_sidegraph_require_prints_the_commit_command(tmp_path, capsys):
    Store(legacy_store(tmp_path / "s")).close()
    assert require_main(["--db", str(tmp_path / "s"), "--sidegraph", running_release()]) == 0
    out = capsys.readouterr().out
    marker = (tmp_path / "s" / "format").as_posix()
    assert f"Sidegraph >= {running_release()} is now required (before: no requirement)" in out
    assert f"git add {shlex.quote(marker)} && git commit -m " in out


def test_sidegraph_require_refuses_above_itself_and_refuses_to_lower(tmp_path, capsys):
    Store(tmp_path / "s").close()
    db = str(tmp_path / "s")
    assert require_main(["--db", db, "--sidegraph", above_running()]) == 1
    assert "cannot require Sidegraph" in capsys.readouterr().err
    assert require_main(["--db", db, "--sidegraph", running_release()]) == 0
    capsys.readouterr()
    assert require_main(["--db", db, "--sidegraph", "0.0.0"]) == 1  # below every release
    assert "never lowered" in capsys.readouterr().err


def test_sidegraph_require_without_a_store_says_so_and_creates_nothing(tmp_path, capsys):
    assert require_main(["--db", str(tmp_path / "none"), "--sidegraph", running_release()]) == 1
    assert "run `sidegraph-init` first" in capsys.readouterr().err
    assert not (tmp_path / "none").exists()


def _index_stamp(store_dir: Path) -> str:
    with closing(sqlite3.connect(store_dir / "index.db")) as conn:
        return conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()[0]


def test_the_index_of_a_raised_store_carries_a_stamp_older_hot_hooks_refuse(tmp_path):
    """Red against code before this task: the reload stamps SCHEMA_VERSION, which 0.13's
    HotIndex.open accepts (it refuses only row[0] != SCHEMA_VERSION). On a machine that also
    runs a newer version, its PreToolUse then delivers from an index it cannot read reliably.
    This version still opens the index."""
    store_dir = tmp_path / "s"
    Store(store_dir).close()
    write_requirement_marker(store_dir, running_release())  # a pulled marker
    Store(store_dir).close()  # the marker moved the digest: a full reload stamps the index
    assert _index_stamp(store_dir) == REQUIRED_INDEX_STAMP != SCHEMA_VERSION
    index = HotIndex.open(store_dir)
    assert index is not None
    assert index.get_meta("schema_version") == REQUIRED_INDEX_STAMP
    index.close()


def test_taking_the_requirement_back_out_stamps_the_index_as_before(tmp_path):
    """Red against code before this task (first assertion). Red against a stamp that stays once
    a store was ever raised (discarded): after a rollback, 0.13 must read the index again."""
    store_dir = tmp_path / "s"
    Store(store_dir).close()
    write_requirement_marker(store_dir, running_release())
    Store(store_dir).close()
    assert _index_stamp(store_dir) == REQUIRED_INDEX_STAMP
    (store_dir / "format").write_text(f"sidegraph-store {SCHEMA_VERSION}\n", encoding="utf-8")
    Store(store_dir).close()
    assert _index_stamp(store_dir) == SCHEMA_VERSION


def test_the_other_stamp_under_a_matching_digest_reloads_instead_of_refusing(tmp_path):
    """Red against code before this task: the index keeps SCHEMA_VERSION. Red against a fast
    path that refuses any stamp but the expected one (discarded): this is exactly the index an
    already-running 0.13 MCP server leaves when it reloads after a pull."""
    store_dir = tmp_path / "s"
    Store(store_dir).close()
    write_requirement_marker(store_dir, running_release())
    Store(store_dir).close()
    with closing(sqlite3.connect(store_dir / "index.db")) as conn, conn:
        conn.execute("UPDATE meta SET value = ? WHERE key = 'schema_version'", (SCHEMA_VERSION,))
    Store(store_dir).close()  # digest unchanged; the stamp is the other one of the pair
    assert _index_stamp(store_dir) == REQUIRED_INDEX_STAMP


def test_raising_the_requirement_stamps_this_machine_s_index_at_once(tmp_path):
    """Red against code before this task: the stamp would change only at the next full
    reload, and an older hot hook on this machine would read the index until then."""
    store_dir = tmp_path / "s"
    with Store(legacy_store(store_dir)) as store:
        store.raise_requirement(running_release())
        assert _index_stamp(store_dir) == REQUIRED_INDEX_STAMP


def test_a_refused_open_never_touches_the_index(tmp_path):
    """Guard, red against nothing on this code; red against moving the index connect above
    the marker check (mutation). The marker check runs before the index is opened, in this
    version and in 0.13.0, so no cold open by a version the marker refuses can stamp the
    index back."""
    store_dir = tmp_path / "s"
    Store(store_dir).close()
    write_requirement_marker(store_dir, above_running())
    index = store_dir / "index.db"
    before = (index.read_bytes(), index.stat().st_mtime_ns)
    with pytest.raises(StoreRequirementError):
        Store(store_dir)
    assert (index.read_bytes(), index.stat().st_mtime_ns) == before
    index.unlink()
    with pytest.raises(StoreRequirementError):
        Store(store_dir)
    assert not index.exists()


def test_a_refused_directory_is_untouched_before_cleanup(tmp_path):
    store_dir = tmp_path / "s"
    Store(store_dir).close()
    (store_dir / "facts").rmdir()
    debris = store_dir / "decisions" / "debris.tmp"
    debris.write_text("leave this alone\n", encoding="utf-8")
    import os

    os.utime(debris, (0, 0))
    write_requirement_marker(store_dir, above_running())
    entries = {p.relative_to(store_dir) for p in store_dir.rglob("*")}
    before = {p.relative_to(store_dir): p.read_bytes() for p in store_dir.rglob("*") if p.is_file()}
    with pytest.raises(StoreRequirementError):
        Store(store_dir)
    assert {p.relative_to(store_dir) for p in store_dir.rglob("*")} == entries
    assert {
        p.relative_to(store_dir): p.read_bytes() for p in store_dir.rglob("*") if p.is_file()
    } == before


def test_a_refused_directory_does_not_migrate_its_legacy_database(tmp_path):
    from tests.test_store_migration import _0_2_0_SCHEMA_SQL, _build_legacy_db

    store_dir = tmp_path / "s"
    store_dir.mkdir()
    legacy = store_dir / "decisions.db"
    _build_legacy_db(legacy, _0_2_0_SCHEMA_SQL, "0.2.0")
    write_requirement_marker(store_dir, above_running())
    before = {p.name: p.read_bytes() for p in store_dir.iterdir()}
    with pytest.raises(StoreRequirementError):
        Store(store_dir)
    assert {p.name: p.read_bytes() for p in store_dir.iterdir()} == before


@pytest.mark.parametrize("kind", ["symlink", "directory", "fifo", "oversized"])
def test_a_bad_marker_is_refused_without_opening_it(tmp_path, monkeypatch, kind):
    import os

    from sidegraph.store_layout import MAX_FORMAT_MARKER_BYTES

    store_dir = tmp_path / "s"
    Store(store_dir).close()
    marker = store_dir / "format"
    marker.unlink()
    if kind == "symlink":
        elsewhere = tmp_path / "elsewhere"
        elsewhere.write_text("sidegraph-store 0.6.0\n", encoding="utf-8")
        marker.symlink_to(elsewhere)
    elif kind == "directory":
        marker.mkdir()
    elif kind == "fifo":
        if not hasattr(os, "mkfifo"):
            pytest.skip("FIFO creation is unavailable")
        os.mkfifo(marker)
    else:
        marker.write_bytes(b"x" * (MAX_FORMAT_MARKER_BYTES + 1))
    real_open = Path.open

    def no_marker_read(path, *args, **kwargs):
        if path == marker:
            pytest.fail("a rejected marker was opened")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", no_marker_read)
    with pytest.raises(StoreRequirementError, match="malformed"):
        store_requirement(store_dir)


def test_the_marker_read_remains_bounded_if_the_file_grows(tmp_path, monkeypatch):
    import io

    from sidegraph.store_layout import MAX_FORMAT_MARKER_BYTES

    store_dir = tmp_path / "s"
    Store(store_dir).close()
    marker = store_dir / "format"
    real_open = Path.open
    reads = []

    class GrowingMarker(io.BytesIO):
        def read(self, size=-1):
            reads.append(size)
            assert size == MAX_FORMAT_MARKER_BYTES + 1
            return super().read(size)

    def open_growing(path, *args, **kwargs):
        if path == marker:
            return GrowingMarker(b"x" * (MAX_FORMAT_MARKER_BYTES * 2))
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", open_growing)
    with pytest.raises(StoreRequirementError, match="exceeds"):
        store_requirement(store_dir)
    assert reads == [MAX_FORMAT_MARKER_BYTES + 1]


def test_the_marker_writer_cannot_exceed_its_reader_bound(tmp_path):
    from sidegraph.store_layout import MAX_FORMAT_MARKER_BYTES

    with Store(tmp_path / "s") as store:
        marker = store.path / "format"
        before = marker.read_bytes()
        with pytest.raises(ValueError, match="exceeds"):
            store.raise_requirement("0." + "1" * MAX_FORMAT_MARKER_BYTES + ".0")
        assert marker.read_bytes() == before


@pytest.mark.parametrize("required", [running_release(), above_running()])
def test_a_legacy_backfill_never_overwrites_a_completed_raise(tmp_path, monkeypatch, required):
    store_dir = tmp_path / "s"
    store_dir.mkdir()
    real_link, real_replace = store_module.os.link, store_module.os.replace
    interrupted = False

    def before_publish(real, source, target):
        nonlocal interrupted
        if Path(target) == store_dir / "format" and not interrupted:
            interrupted = True
            with Store(legacy_store(store_dir)) as other:
                other.raise_requirement(required, running=required)
            assert store_requirement(store_dir) == required
        return real(source, target)

    monkeypatch.setattr(
        store_module.os, "link", lambda src, dst: before_publish(real_link, src, dst)
    )
    monkeypatch.setattr(
        store_module.os, "replace", lambda src, dst: before_publish(real_replace, src, dst)
    )
    if required == running_release():
        Store(legacy_store(store_dir)).close()
    else:
        with pytest.raises(StoreRequirementError):
            Store(legacy_store(store_dir))
    assert interrupted
    assert store_requirement(store_dir) == required
    assert _index_stamp(store_dir) == REQUIRED_INDEX_STAMP


def test_a_cold_open_cannot_cache_an_unchecked_later_marker(tmp_path, monkeypatch):
    store_dir = tmp_path / "s"
    with Store(legacy_store(store_dir)) as writer:
        real_ensure = Store._ensure_format_marker

        def ensure_then_raise(reader):
            required = real_ensure(reader)
            writer.raise_requirement(above_running(), running=above_running())
            return required

        monkeypatch.setattr(Store, "_ensure_format_marker", ensure_then_raise)
        try:
            reader = Store(legacy_store(store_dir))
        except StoreRequirementError:
            pass
        else:
            try:
                with pytest.raises(StoreRequirementError):
                    reader.refresh_if_stale()
            finally:
                reader.close()
        assert _index_stamp(store_dir) == REQUIRED_INDEX_STAMP


def test_an_explicit_raise_caches_its_own_publication(tmp_path, monkeypatch):
    with Store(legacy_store(tmp_path / "s")) as store:
        real_write = store_module._atomic_write_text
        marker = store.path / "format"

        def publish_then_pull(path, text):
            published = real_write(path, text)
            if path == marker:
                real_write(
                    marker, f"sidegraph-store 1.0.0\nrequires-sidegraph >= {above_running()}\n"
                )
            return published

        monkeypatch.setattr(store_module, "_atomic_write_text", publish_then_pull)
        store.raise_requirement(running_release())
        assert store_requirement(store.path) == above_running()
        with pytest.raises(StoreRequirementError):
            store.refresh_if_stale()


def test_outer_rollback_leaves_the_marker_raised_and_next_refresh_repairs_the_stamp(tmp_path):
    with Store(legacy_store(tmp_path / "s")) as store:
        assert _index_stamp(store.path) == SCHEMA_VERSION
        with (
            pytest.raises(RuntimeError, match="snapshot publication failed"),
            store._mutation(),
        ):
            store.raise_requirement(running_release())
            raise RuntimeError("snapshot publication failed")
        assert store_requirement(store.path) == running_release()
        assert _index_stamp(store.path) == SCHEMA_VERSION
        store.refresh_if_stale()
        assert _index_stamp(store.path) == REQUIRED_INDEX_STAMP
