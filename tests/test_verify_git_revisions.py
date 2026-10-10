"""Git baseline resolution must be option-safe and stay fixed during one verification."""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from sidegraph.cli import doctor_main, verify_main
from sidegraph.gitenv import git_env
from sidegraph.schema import Decision, DecisionKind, Provenance
from sidegraph.store import Store
from sidegraph.verify import verify_against, verify_snapshot


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=repo, env=git_env(), capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


@pytest.fixture(params=["sha1", "sha256"])
def tracked_edit(tmp_path: Path, request):
    repo = tmp_path / "repo"
    repo.mkdir()
    result = subprocess.run(
        ["git", "init", "-q", f"--object-format={request.param}"],
        cwd=repo,
        env=git_env(),
        capture_output=True,
        text=True,
    )
    if (
        request.param == "sha256"
        and result.returncode != 0
        and ("unknown option" in result.stderr or "unknown hash algorithm" in result.stderr)
    ):
        pytest.skip(f"Git lacks SHA-256 initialization: {result.stderr.strip()}")
    assert result.returncode == 0, result.stderr
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    store = repo / ".sidegraph"
    with Store(store) as opened:
        record = opened.add_decision(
            Decision(
                title="Tracked baseline",
                kind=DecisionKind.ADR,
                context="Synthetic context",
                choice="Original choice",
                valid_from=datetime(2026, 1, 10, tzinfo=UTC),
                provenance=Provenance(source="manual"),
            )
        )
    _git(repo, "add", ".sidegraph")
    _git(repo, "-c", "core.hooksPath=/dev/null", "commit", "-q", "-m", "baseline")
    baseline = _git(repo, "rev-parse", "HEAD")
    path = store / "decisions" / f"{record.id}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["choice"] = "Hand edited choice"
    path.write_text(json.dumps(data) + "\n", encoding="utf-8")
    assert verify_snapshot(store) == []  # The immutable edit is valid within a snapshot.
    return repo, store, path, baseline


@pytest.mark.parametrize(
    "ref", ["--output=sentinel", "--name-only", "--no-index", "--", "", "bad\0ref"]
)
def test_option_ref_rejects_without_side_effects(tracked_edit, ref):
    repo, store, path, _ = tracked_edit
    sentinel = repo / "sentinel"
    sentinel.write_bytes(b"preserve this sentinel\n")
    before = path.read_bytes()
    try:
        verify_against(store, ref)
    except ValueError:
        rejected = True
    else:
        rejected = False
    # Check the observable effect before checking the error: rejection alone is insufficient.
    assert sentinel.read_bytes() == b"preserve this sentinel\n"
    assert path.read_bytes() == before
    assert rejected


@pytest.mark.parametrize("main", [verify_main, doctor_main], ids=["verify", "doctor"])
def test_cli_option_ref_is_operational_and_preserves_files(tracked_edit, main, capsys):
    repo, store, path, _ = tracked_edit
    sentinel = repo / "sentinel"
    sentinel.write_bytes(b"preserve this sentinel\n")
    before = path.read_bytes()
    code = main(["--db", str(store), "--against=--output=sentinel"])
    output = capsys.readouterr()
    assert sentinel.read_bytes() == b"preserve this sentinel\n"
    assert path.read_bytes() == before
    assert code == 1
    assert output.out.strip()


@pytest.mark.parametrize("oid", ["a" * 40, "b" * 64])
@pytest.mark.parametrize("ending", ["", "\n"])
def test_resolver_accepts_one_full_oid_and_protects_argv(tmp_path, monkeypatch, oid, ending):
    from sidegraph import verify

    calls = []

    def run(args, *, cwd):
        calls.append((args, cwd))
        return subprocess.CompletedProcess(args, 0, stdout=oid + ending, stderr="")

    monkeypatch.setattr(verify, "_run_git", run)
    resolve = getattr(verify, "_resolve_commit_oid", None)
    assert callable(resolve), "one protected commit resolver is required"
    assert resolve(tmp_path, "refs/tags/baseline") == oid
    assert calls == [
        (["rev-parse", "--verify", "--end-of-options", "refs/tags/baseline^{commit}"], tmp_path)
    ]


@pytest.mark.parametrize("ref", [None, 1, b"HEAD", [], {}, "", "--output=sentinel", "bad\0ref"])
def test_resolver_rejects_invalid_raw_input_before_git(tmp_path, monkeypatch, ref):
    from sidegraph import verify

    def unexpected_run(*args, **kwargs):
        pytest.fail("invalid raw revision reached Git")

    monkeypatch.setattr(verify, "_run_git", unexpected_run)
    resolve = getattr(verify, "_resolve_commit_oid", None)
    assert callable(resolve), "one protected commit resolver is required"
    with pytest.raises(ValueError):
        resolve(tmp_path, ref)


@pytest.mark.parametrize(
    ("output", "returncode"),
    [
        ("", 0),
        ("a" * 39, 0),
        ("a" * 41, 0),
        ("a" * 63, 0),
        ("a" * 65, 0),
        ("g" * 40, 0),
        ("A" * 40, 0),
        (" " + "a" * 40, 0),
        ("a" * 40 + " ", 0),
        ("a" * 40 + "\r", 0),
        ("a" * 40 + "\r\n", 0),
        ("a" * 40 + "\v", 0),
        ("a" * 40 + "\u0085", 0),
        ("a" * 40 + "\n\n", 0),
        ("a" * 40 + "\n" + "b" * 40 + "\n", 0),
        ("a" * 40 + "\n", 1),
    ],
)
def test_resolver_rejects_malformed_decoded_output(tmp_path, monkeypatch, output, returncode):
    from sidegraph import verify

    # This fake covers impossible/malformed Git output at the decoded-text boundary.
    def run(args, *, cwd):
        return subprocess.CompletedProcess(args, returncode, stdout=output, stderr="failure")

    monkeypatch.setattr(verify, "_run_git", run)
    resolve = getattr(verify, "_resolve_commit_oid", None)
    assert callable(resolve), "one protected commit resolver is required"
    with pytest.raises(ValueError):
        resolve(tmp_path, "HEAD")


def test_moving_ref_uses_one_frozen_baseline_for_diff_and_every_show(tracked_edit, monkeypatch):
    from sidegraph import verify

    repo, store, path, baseline = tracked_edit
    _git(repo, "add", ".sidegraph")
    _git(repo, "-c", "core.hooksPath=/dev/null", "commit", "-q", "-m", "edited tree")
    edited_commit = _git(repo, "rev-parse", "HEAD")
    assert edited_commit != baseline
    _git(repo, "update-ref", "refs/heads/moving", baseline)
    before = path.read_bytes()
    original = verify._run_git
    calls = []

    def run(args, *, cwd, **kwargs):
        calls.append((list(args), cwd))
        result = original(args, cwd=cwd, **kwargs)
        if args[0] == "diff":
            # Move the real branch after Git enumerates A -> B. Reading "moving" now sees B.
            _git(repo, "update-ref", "refs/heads/moving", edited_commit)
        return result

    monkeypatch.setattr(verify, "_run_git", run)
    findings = verify_against(store, "moving")
    assert path.read_bytes() == before
    assert [(item.code, item.path) for item in findings] == [
        (verify.ILLEGAL_FIELD_CHANGE, path.relative_to(repo).as_posix())
    ]
    resolutions = [args for args, _ in calls if args[:2] == ["rev-parse", "--verify"]]
    assert resolutions == [["rev-parse", "--verify", "--end-of-options", "moving^{commit}"]]
    diffs = [(args, cwd) for args, cwd in calls if args[0] == "diff"]
    assert diffs == [
        (
            ["diff", "--no-renames", "--name-status", "-z", baseline, "--", str(store.resolve())],
            repo,
        )
    ]
    shows = [(args, cwd) for args, cwd in calls if args[0] == "show"]
    assert shows
    assert all(args[1].startswith(baseline + ":") and cwd == repo for args, cwd in shows)


@pytest.mark.parametrize("kind", ["HEAD", "HEAD~1", "branch", "tag", "annotated", "full"])
def test_commitish_baselines_detect_immutable_edit(tracked_edit, kind):
    from sidegraph.verify import ILLEGAL_FIELD_CHANGE

    repo, store, path, baseline = tracked_edit
    if kind == "HEAD~1":
        _git(repo, "add", ".sidegraph")
        _git(repo, "-c", "core.hooksPath=/dev/null", "commit", "-q", "-m", "edited tree")
        ref = "HEAD~1"
    elif kind == "branch":
        _git(repo, "branch", "baseline-branch", baseline)
        ref = "baseline-branch"
    elif kind == "tag":
        _git(repo, "tag", "baseline-tag", baseline)
        ref = "baseline-tag"
    elif kind == "annotated":
        _git(repo, "tag", "-a", "baseline-tag", baseline, "-m", "annotated baseline")
        ref = "baseline-tag"
    else:
        ref = baseline if kind == "full" else "HEAD"
    before = path.read_bytes()
    findings = verify_against(store, ref)
    assert path.read_bytes() == before
    assert [(item.code, item.path) for item in findings] == [
        (ILLEGAL_FIELD_CHANGE, path.relative_to(repo).as_posix())
    ]


@pytest.mark.parametrize("kind", ["tree", "blob", "range", "unknown"])
def test_noncommit_baselines_fail_before_diff_or_show(tracked_edit, monkeypatch, kind):
    from sidegraph import verify

    repo, store, path, baseline = tracked_edit
    if kind == "tree":
        ref = _git(repo, "rev-parse", "HEAD^{tree}")
    elif kind == "blob":
        ref = _git(repo, "rev-parse", f"HEAD:{path.relative_to(repo).as_posix()}")
    elif kind == "range":
        ref = f"{baseline}..HEAD"
    else:
        ref = "missing-baseline"
    original = verify._run_git
    commands = []

    def run(args, *, cwd, **kwargs):
        commands.append(args[0])
        return original(args, cwd=cwd, **kwargs)

    monkeypatch.setattr(verify, "_run_git", run)
    before = path.read_bytes()
    with pytest.raises(ValueError):
        verify_against(store, ref)
    assert path.read_bytes() == before
    assert "diff" not in commands
    assert "show" not in commands


@pytest.mark.parametrize("failure", ["unavailable", "malformed"])
def test_resolver_failure_never_falls_back_to_raw_ref(tracked_edit, monkeypatch, failure):
    from sidegraph import verify

    _, store, path, _ = tracked_edit
    original = verify._run_git
    commands = []

    def run(args, *, cwd, **kwargs):
        commands.append(args[0])
        if args[:2] == ["rev-parse", "--verify"]:
            if failure == "unavailable":
                raise ValueError("could not run git")
            return subprocess.CompletedProcess(args, 0, stdout="bad\n", stderr="")
        return original(args, cwd=cwd, **kwargs)

    monkeypatch.setattr(verify, "_run_git", run)
    before = path.read_bytes()
    with pytest.raises(ValueError):
        verify_against(store, "HEAD")
    assert path.read_bytes() == before
    assert "diff" not in commands
    assert "show" not in commands


def test_invalid_baseline_is_not_hidden_by_symlinked_internals(tracked_edit):
    repo, store, _, _ = tracked_edit
    (store / "decisions").rename(repo / "external-decisions")
    (store / "decisions").symlink_to(repo / "external-decisions", target_is_directory=True)
    with pytest.raises(ValueError):
        verify_against(store, "missing-baseline")
    # A resolved baseline still follows the existing snapshot-owned symlink early stop.
    assert verify_against(store, "HEAD") == []


def test_shallow_checkout_with_missing_baseline_is_operational(tracked_edit, tmp_path):
    repo, _, _, baseline = tracked_edit
    _git(repo, "add", ".sidegraph")
    _git(repo, "-c", "core.hooksPath=/dev/null", "commit", "-q", "-m", "edited tree")
    checkout = tmp_path / "shallow"
    _git(tmp_path, "clone", "-q", "--depth=1", repo.as_uri(), str(checkout))
    assert _git(checkout, "rev-parse", "--is-shallow-repository") == "true"
    path = next((checkout / ".sidegraph" / "decisions").glob("*.json"))
    before = path.read_bytes()
    with pytest.raises(ValueError):
        verify_against(checkout / ".sidegraph", baseline)
    assert path.read_bytes() == before


@pytest.mark.parametrize("main", [verify_main, doctor_main], ids=["verify", "doctor"])
@pytest.mark.parametrize("ref", ["", "missing-baseline"])
def test_cli_invalid_baseline_is_operational(tracked_edit, main, ref, capsys):
    _, store, path, _ = tracked_edit
    before = path.read_bytes()
    code = main(["--db", str(store), f"--against={ref}"])
    output = capsys.readouterr()
    assert path.read_bytes() == before
    assert code == 1
    assert output.out.strip()
