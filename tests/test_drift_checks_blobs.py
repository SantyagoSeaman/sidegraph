"""Bounded, batched blob reads for declared-section scope."""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from sidegraph import drift_checks
from sidegraph.drift_checks import BlobRead, read_blob_texts

BIG = 10**9


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    return tmp_path


def blob(repo: Path, data: bytes) -> str:
    result = subprocess.run(
        ["git", "hash-object", "-w", "--stdin"],
        cwd=repo,
        input=data,
        capture_output=True,
        check=True,
    )
    return result.stdout.decode().strip()


def read(repo: Path, oids, **caps):
    limits = {
        "file_max_bytes": BIG,
        "file_max_lines": BIG,
        "total_max_bytes": BIG,
        "total_max_lines": BIG,
    }
    limits.update(caps)
    return read_blob_texts(repo, oids, 5.0, **limits)


class _Reverse(set):
    """An iterable whose order is reverse-sorted, whatever the hash seed."""

    def __iter__(self) -> Iterator[str]:
        return iter(sorted(set.__iter__(self), reverse=True))


def test_two_git_calls_for_many_blobs(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    oids = [blob(repo, f"doc {i}\n".encode()) for i in range(5)]
    calls: list[list[str]] = []
    envs: list[dict[str, str]] = []
    real = subprocess.run

    def spy(args, *a, **k):
        calls.append(list(args))
        envs.append(k["env"])
        return real(args, *a, **k)

    monkeypatch.setattr(drift_checks.subprocess, "run", spy)
    out = read(repo, oids)
    first_read = next(i for i, c in enumerate(calls) if c[1] == "cat-file")
    assert first_read and all(c[1] == "config" for c in calls[:first_read])  # the gate first
    assert [c[1:3] for c in calls[first_read:]] == [
        ["cat-file", "--batch-check"],
        ["cat-file", "--batch"],
    ]  # not a partial clone: no version probe, two reads for all five blobs
    assert all(env.get("GIT_NO_LAZY_FETCH") == "1" for env in envs)  # never fetches a blob
    assert {o: r.text for o, r in out.items()} == {o: f"doc {i}\n" for i, o in enumerate(oids)}
    assert all(r.reason is None for r in out.values())


def test_each_failure_has_its_own_reason(repo: Path) -> None:
    good = blob(repo, b"a\nb\n")
    big = blob(repo, b"x" * 150)
    long = blob(repo, b"l\n" * 50)
    bad = blob(repo, b"\xff\xfe\n")
    tree = (
        subprocess.run(["git", "mktree"], cwd=repo, input=b"", capture_output=True, check=True)
        .stdout.decode()
        .strip()
    )
    missing = "0" * 40
    out = read(repo, [good, big, long, bad, tree, missing], file_max_bytes=120, file_max_lines=10)
    assert out[good] == BlobRead("a\nb\n", None)
    assert out[big] == BlobRead(None, "too_large")
    assert out[long] == BlobRead(None, "too_many_lines")
    assert out[bad] == BlobRead(None, "not_utf8")
    assert out[tree] == BlobRead(None, "unavailable")
    assert out[missing] == BlobRead(None, "unavailable")


@pytest.mark.parametrize("cap", ["total_max_bytes", "total_max_lines"])
def test_scan_caps_apply_in_sorted_oid_order(repo: Path, cap: str) -> None:
    oids = {blob(repo, b"one\ntwo\n"), blob(repo, b"six\nten\n")}
    first, second = sorted(oids)
    limit = 8 if cap == "total_max_bytes" else 2
    out = read(repo, _Reverse(oids), **{cap: limit})
    assert out[first].text is not None
    assert out[second] == BlobRead(None, "scope_cap")


def test_writer_style_total_line_cap_gives_scope_cap(repo: Path) -> None:
    a = blob(repo, b"x\n" * 12_000)
    b = blob(repo, b"y\n" * 12_000)
    out = read(repo, [a, b], file_max_lines=20_000, total_max_lines=20_000)
    reasons = sorted(r.reason or "" for r in out.values())
    assert reasons == ["", "scope_cap"]


def test_exhausted_timeout_raises_value_error(repo: Path) -> None:
    oid = blob(repo, b"x\n")
    with pytest.raises(ValueError):
        read_blob_texts(
            repo,
            [oid],
            0,
            file_max_bytes=BIG,
            file_max_lines=BIG,
            total_max_bytes=BIG,
            total_max_lines=BIG,
        )


@pytest.mark.parametrize(("version", "refused"), [((2, 44), True), (None, True), ((2, 45), False)])
def test_a_partial_clone_on_old_git_reads_no_blob(
    repo: Path, monkeypatch: pytest.MonkeyPatch, version, refused
) -> None:
    """Git before 2.45 ignores GIT_NO_LAZY_FETCH, so on a partial clone a missing blob could be
    fetched: the read refuses before any cat-file, and the caller compares the whole file."""
    oid = blob(repo, b"doc\n")
    subprocess.run(["git", "config", "remote.origin.promisor", "true"], cwd=repo, check=True)
    monkeypatch.setattr(drift_checks, "git_version", lambda root, timeout: version)
    calls: list[list[str]] = []
    real = subprocess.run

    def spy(args, *a, **k):
        calls.append(list(args))
        return real(args, *a, **k)

    monkeypatch.setattr(drift_checks.subprocess, "run", spy)
    if refused:
        with pytest.raises(ValueError, match="partial clone"):
            read(repo, [oid])
        assert not any(c[1] == "cat-file" for c in calls)
    else:
        assert read(repo, [oid])[oid].text == "doc\n"
