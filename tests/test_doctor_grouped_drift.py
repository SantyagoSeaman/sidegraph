"""Doctor's grouped code-drift output: the ``group_drift`` seam, the hot-path import graph,
the CLI text/JSON surface and the partial-clone guarantee.
# see design/superpowers/specs/2026-10-27-grouped-drift-triage-design.md
"""

from __future__ import annotations

import ast
import contextlib
import io
import json
import os
import random
import re
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

from sidegraph import doctor
from sidegraph.cli import doctor_main
from sidegraph.doctor import curate
from tests.drift_checks_support import git, project  # noqa: F401

# ruff: noqa: F811  (the imported ``project`` fixture is redefined as a test argument)

SRC = Path(__file__).resolve().parents[1] / "src"


def _count_git_calls(monkeypatch, call) -> int:
    """Run ``call`` with the global ``subprocess.run`` spied; count calls whose argv[0] is git."""
    real = subprocess.run
    seen: list[list[str]] = []

    def spy(argv, *args, **kwargs):
        if isinstance(argv, list) and argv and argv[0] == "git":
            seen.append(list(argv))
        return real(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", spy)
    call()
    monkeypatch.setattr(subprocess, "run", real)
    return len(seen)


CURATE_GIT_CALLS = 5  # measured on unmodified code: root resolution plus the scan's calls


def test_curate_git_call_count_unchanged_by_refactor(project, monkeypatch):
    """Guard (N): ``curate`` without ``group_drift`` makes today's git calls and no more."""
    _root, store, _decision = project
    count = _count_git_calls(monkeypatch, lambda: curate(store.path))
    assert count == CURATE_GIT_CALLS


def test_curate_group_drift_false_matches_default(project, monkeypatch):
    from sidegraph.drift_groups import DriftGroups

    _root, store, _decision = project
    off = _count_git_calls(monkeypatch, lambda: curate(store.path, group_drift=False))
    assert off == CURATE_GIT_CALLS
    assert curate(store.path, group_drift=False).drift_groups is None
    assert isinstance(curate(store.path, group_drift=True).drift_groups, DriftGroups)


_HEADER = (
    "import sys\n"
    f"sys.path.insert(0, {str(SRC)!r})\n"
    "from pathlib import Path\n"
    "import sidegraph\n"
    f"assert Path(sidegraph.__file__).resolve().is_relative_to(Path({str(SRC)!r}))\n"
)


def _run_isolated(code: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-I", "-c", _HEADER + code, *args],
        capture_output=True,
        text=True,
    )


def test_import_graph_leaves_drift_groups_out_of_hot_path():
    """Guard (N): the hook paths never import ``drift_groups``."""
    result = _run_isolated(
        "import sidegraph.sync, sidegraph.doctor, sidegraph.retrieval, sidegraph.host.hooks\n"
        "assert 'sidegraph.drift_groups' not in sys.modules\n"
    )
    assert result.returncode == 0, result.stderr


def test_group_drift_true_imports_drift_groups(project):
    _root, store, _decision = project
    result = _run_isolated(
        "from sidegraph.doctor import curate\n"
        "curate(sys.argv[1], group_drift=True)\n"
        "assert 'sidegraph.drift_groups' in sys.modules\n",
        str(store.path),
    )
    assert result.returncode == 0, result.stderr


# --- partial clone: grouping must not fetch ---------------------------------------------


def _git_version() -> tuple[int, int]:
    text = subprocess.run(["git", "--version"], capture_output=True, text=True).stdout
    import re

    match = re.search(r"(\d+)\.(\d+)", text)
    return (int(match[1]), int(match[2])) if match else (0, 0)


def _missing_objects(clone: Path) -> int:
    out = subprocess.run(
        ["git", "rev-list", "--objects", "--missing=print", "--all"],
        cwd=clone,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return sum(1 for line in out.splitlines() if line.startswith("?"))


def _partial_clone_fixture(tmp_path: Path, *, commit_store: bool = False):
    """A blob-filtered clone whose baseline blob (``A.py`` at capture) is not local, plus the
    store (kept in the source, which the doctor is pointed at) and the clone's root."""
    from tests.drift_groups_support import GroupRepo, rid

    src = GroupRepo(tmp_path / "src")
    base = src.commit({"A.py": "a = 1\n"}, "base")
    src.record(1, commit=base, files=["A.py"])
    if commit_store:
        for part in ("decisions", "bindings", "entities"):
            git(src.root, "add", f".sidegraph/{part}")
    src.commit({"A.py": "a = 2\n"}, "drift")
    src.write_index()
    git(src.root, "config", "uploadpack.allowFilter", "true")
    git(src.root, "config", "uploadpack.allowAnySHA1InWant", "true")
    clone = tmp_path / "clone"
    git(
        tmp_path,
        "clone",
        "-q",
        "--no-local",
        "--filter=blob:none",
        f"file://{src.root}",
        str(clone),
    )
    return src, clone, rid(1)


@pytest.mark.skipif(_git_version() < (2, 45), reason="GIT_NO_LAZY_FETCH needs Git 2.45")
def test_partial_clone_no_lazy_fetch(tmp_path):
    src, clone, record = _partial_clone_fixture(tmp_path)
    before = _missing_objects(clone)
    assert before >= 1  # the baseline blob is genuinely absent
    report = curate(src.store, repo_root=clone, group_drift=True)
    assert report.drift_groups is not None
    entry = next(r for g in report.drift_groups.groups for r in g.records)
    assert entry.record_id == record
    assert entry.diff["unavailable"] == "baseline object not in this clone"
    assert _missing_objects(clone) == before


# --- the CLI surface: grouped text, --flat, the drift_groups JSON key ---------------------

_FLAT_TEXT = [
    "0 violation(s), 3 finding(s)",
    "binding-status check skipped (binding statuses are not computed for the current graph "
    "\u2014 run sidegraph-sync)",
    "code-drift  decision:<ID>  <TMP>/.sidegraph/decisions/<ID>.json  anchored file(s) changed "
    "since capture (commit <SHA8>): code.py (binding status unknown; deleted paths excluded)",
    "code-drift  decision:<ID>  <TMP>/.sidegraph/decisions/<ID>.json  anchored file(s) changed "
    "since capture (commit <SHA8>): code.py (binding status unknown; deleted paths excluded)",
    "graph checks skipped (no code graph at <TMP>/graphify-out/graph.json \u2014 build it from "
    "the repository root by running `graphify update .` with the environment variable "
    "`GRAPHIFY_NO_AUTO_REFRESH=1` set)",
    "orphaned-records check skipped (binding statuses are not computed for the current graph "
    "\u2014 run sidegraph-sync)",
    "store-not-migrated  <TMP>/.sidegraph  Sidegraph: the store uses the pre-0.17 layout, one "
    "file per record. Run `sidegraph-migrate` on the default branch (or the "
    "`sidegraph:compact` skill) after the team checklist in "
    "https://github.com/SantyagoSeaman/sidegraph/blob/main/docs/guides/migrating-to-0.17.md.",
]
# The findings of the same fixture, captured from the unchanged CLI before this change
# (plus the ``first_seen`` key the drift-recency change added, null here).
_FINDINGS = [
    {
        "code": "code-drift",
        "path": "<TMP>/.sidegraph/decisions/<ID>.json",
        "detail": "anchored file(s) changed since capture (commit <SHA8>): code.py "
        "(binding status unknown; deleted paths excluded)",
        "record_id": "<ID>",
        "record_type": "decision",
        "first_seen": None,
    },
    {
        "code": "code-drift",
        "path": "<TMP>/.sidegraph/decisions/<ID>.json",
        "detail": "anchored file(s) changed since capture (commit <SHA8>): code.py "
        "(binding status unknown; deleted paths excluded)",
        "record_id": "<ID>",
        "record_type": "decision",
        "first_seen": None,
    },
    {
        "code": "store-not-migrated",
        "path": "<TMP>/.sidegraph",
        "detail": "Sidegraph: the store uses the pre-0.17 layout, one file per record. Run "
        "`sidegraph-migrate` on the default branch (or the `sidegraph:compact` skill) after the "
        "team checklist in "
        "https://github.com/SantyagoSeaman/sidegraph/blob/main/docs/guides/migrating-to-0.17.md.",
        "record_id": None,
        "record_type": None,
        "first_seen": None,
    },
]


def _normalise(text: str, tmp: Path) -> str:
    text = text.replace(str(tmp), "<TMP>")
    text = re.sub(r"\b[0-9A-HJKMNP-TV-Z]{26}\b", "<ID>", text)
    text = re.sub(r"\b(?:[0-9a-f]{64}|[0-9a-f]{40})\b", "<SHA>", text)
    return re.sub(r"\(commit [0-9a-f]{8}\)", "(commit <SHA8>)", text)


def _cli(store: Path, *args: str) -> tuple[int, str]:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = doctor_main(["--db", str(store), *args])
    return code, buf.getvalue()


@pytest.fixture
def two_records(project):
    """The ``project`` store with a second decision on the same drifted file."""
    from sidegraph.schema import Decision, Provenance

    root, store, decision = project
    other = store.add_decision(
        Decision(
            title="Second rule",
            kind="gotcha",
            status="accepted",
            context="c",
            choice="x",
            rejected="y",
            valid_from=datetime.now(UTC),
            provenance=Provenance(source="manual", commit=decision.provenance.commit),
        )
    )
    for binding in store.bindings_for_record(decision.id):
        store.add_binding(binding.model_copy(update={"record_id": other.id}))
    return root, store, {decision.id, other.id}


def test_flat_text_matches_main_output(two_records):
    root, store, _ids = two_records
    code, text = _cli(store.path, "--flat")
    assert code == 0
    assert sorted(_normalise(text, root).splitlines()) == _FLAT_TEXT


def test_flat_json_has_no_drift_groups_key(two_records):
    _root, store, _ids = two_records
    _code, out = _cli(store.path, "--flat", "--json")
    assert set(json.loads(out)) == {"clean", "violations", "findings", "skipped"}


def test_flat_makes_no_extra_git_calls(two_records, monkeypatch):
    _root, store, _ids = two_records
    flat = _count_git_calls(monkeypatch, lambda: _cli(store.path, "--flat"))
    # The CLI adds no git call of its own on top of curate()'s (measured: offset 0).
    assert flat == _count_git_calls(monkeypatch, lambda: curate(store.path)) + 0


def test_findings_json_is_unchanged_by_grouping(two_records):
    root, store, _ids = two_records
    _code, grouped = _cli(store.path, "--json")
    _code, flat = _cli(store.path, "--flat", "--json")
    grouped_findings = json.loads(grouped)["findings"]
    assert grouped_findings == json.loads(flat)["findings"]
    normalised = json.loads(_normalise(json.dumps(grouped_findings), root))
    assert sorted(normalised, key=lambda f: (f["code"], f["path"])) == sorted(
        _FINDINGS, key=lambda f: (f["code"], f["path"])
    )


def test_33_json_key_set_with_per_record_drift(two_records):
    _root, store, ids = two_records
    _code, out = _cli(store.path, "--json")
    doc = json.loads(out)
    assert set(doc) == {"clean", "violations", "findings", "skipped", "drift_groups"}
    assert {r["record_id"] for g in doc["drift_groups"]["groups"] for r in g["records"]} == ids


def test_default_text_groups_and_omits_per_record_lines(two_records):
    _root, store, ids = two_records
    _code, text = _cli(store.path)
    assert "code-drift: 2 record(s) in 1 file group(s)" in text
    assert " file 1/1  code.py  (2 record(s), 1 distinct diff(s))" in text
    assert not [line for line in text.splitlines() if line.startswith("code-drift  decision:")]
    assert all(rid in text for rid in ids)
    assert "0 violation(s), 3 finding(s)" in text  # the summary still counts findings
    assert re.search(r"\[diff 1\]", text)


def _group_lines(text: str) -> list[str]:
    """The indented record/diff lines of the code-drift block."""
    return [line for line in text.splitlines() if line.startswith("   ")]


def _scope_project(project):
    from tests.drift_checks_support import require_check
    from tests.test_doctor_code_drift import _anchor, _commit

    root, store, decision = project
    require_check(root, decision.id)
    _commit(root, "dependency.py", "dependency = 1\n")
    _anchor(store, decision, "dependency.py")
    return root, store, decision, _commit


def test_7_scope_text(project):
    root, store, decision, commit = _scope_project(project)
    _code, text = _cli(store.path)
    lines = [line for line in _group_lines(text) if decision.id in line]
    assert lines and all("scope changed; the file may also have changed" in ln for ln in lines)
    unedited = next(ln for ln in lines if "[diff" not in ln)
    assert "content changed" not in unedited
    commit(root, "code.py", "def policy():\n    return 2\n")
    _code, text = _cli(store.path)
    assert any("[diff" in ln and decision.id in ln for ln in _group_lines(text))


def test_27_mode_and_type_text(tmp_path):
    from tests.drift_groups_support import GroupRepo

    if (
        subprocess.run(
            ["git", "config", "--bool", "core.filemode"], capture_output=True, text=True
        ).stdout.strip()
        == "false"
    ):
        pytest.skip("filesystem does not track executable modes")
    repo = GroupRepo(tmp_path)
    base = repo.commit({"mode.sh": "x\n", "link.txt": "target\n"}, "base")
    repo.record(1, commit=base, files=["mode.sh"])
    repo.record(2, commit=base, files=["link.txt"])
    os.chmod(repo.root / "mode.sh", 0o755)
    (repo.root / "link.txt").unlink()
    (repo.root / "link.txt").symlink_to("mode.sh")
    subprocess.run(["git", "add", "-A"], cwd=repo.root, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "mode and type"], cwd=repo.root, check=True)
    repo.write_index()
    _code, text = _cli(repo.store)
    assert "mode changed 100644\u2192100755, content identical" in text
    assert "type changed regular file\u2192symlink" in text


def test_9_notes_print_before_the_block_and_are_in_the_json(tmp_path, monkeypatch):
    from tests.drift_groups_support import GroupRepo

    repo = GroupRepo(tmp_path)
    first = repo.commit({"A.py": "a = 1\n"}, "first")
    second = repo.commit({"B.py": "b = 1\n"}, "second")
    repo.record(1, commit=first, files=["A.py"])
    repo.record(2, commit=second, files=["B.py"])
    repo.record(3, commit="", files=["A.py"])  # unstamped
    repo.commit({"A.py": "a = 2\n", "B.py": "b = 2\n"}, "drift")
    repo.write_index()
    real = doctor._git_diff_name_only

    def flaky(root, commit, paths, **kwargs):
        if commit == second:
            raise ValueError("boom")
        return real(root, commit, paths, **kwargs)

    monkeypatch.setattr(doctor, "_git_diff_name_only", flaky)
    monkeypatch.setattr(
        doctor.drift_checks, "latest_checks", lambda *_a, **_k: (_ for _ in ()).throw(ValueError)
    )
    _code, text = _cli(repo.store)
    _code, out = _cli(repo.store, "--json")
    notes = json.loads(out)["drift_groups"]["notes"]
    assert notes == [
        "1 record(s) predate commit stamping \u2014 not checkable",
        "code-drift: git unavailable \u2014 check skipped",
        "code-drift: scoped check unavailable \u2014 check skipped",
    ]
    lines = text.splitlines()
    header = lines.index(next(ln for ln in lines if ln.startswith("code-drift: 1 record(s)")))
    positions = [
        next(i for i, ln in enumerate(lines) if ln.startswith("code-drift  ") and ln.endswith(n))
        for n in notes
    ]
    assert positions == sorted(positions) and max(positions) < header


def test_14_path_with_space_newline_and_non_ascii(tmp_path):
    from tests.drift_groups_support import GroupRepo

    odd = "dir with space/na\u00efve\nname.py"
    repo = GroupRepo(tmp_path)
    base = repo.commit({odd: "a = 1\n"}, "base")
    repo.record(1, commit=base, files=[odd])
    repo.commit({odd: "a = 2\n"}, "drift")
    repo.write_index()
    code, text = _cli(repo.store)
    assert odd in text
    _code, out = _cli(repo.store, "--json")
    groups = json.loads(out)["drift_groups"]["groups"]
    assert [g["file"] for g in groups] == [odd]
    command = groups[0]["records"][0]["diff"]["command"]
    assert command.startswith("git diff ") and odd not in command and "na\u00efve" not in command


def test_21_text_is_independent_of_input_order(tmp_path):
    from tests.drift_groups_support import GroupRepo

    repo = GroupRepo(tmp_path)
    base = repo.commit({"A.py": "a = 1\n", "B.py": "b = 1\n"}, "base")
    for n in range(1, 6):
        repo.record(n, commit=base, files=[["A.py", "B.py"][n % 2]], kind=["adr", "gotcha"][n % 2])
    repo.commit({"A.py": "a = 2\n", "B.py": "b = 2\n"}, "drift")
    repo.write_index()
    from sidegraph import drift_groups

    scan = repo.scan()
    decisions, bindings, entities = repo.records()
    reference = drift_groups.render_groups_text(
        drift_groups.build(scan, decisions, bindings, entities, repo.store)
    )
    for seed in range(4):
        rng = random.Random(seed)
        d, e = list(decisions), list(entities)
        rng.shuffle(d)
        rng.shuffle(e)
        batches = [b._replace(entries=rng.sample(b.entries, len(b.entries))) for b in scan.batches]
        groups = drift_groups.build(scan._replace(batches=batches), d, bindings, e, repo.store)
        assert drift_groups.render_groups_text(groups) == reference


@pytest.mark.skipif(_git_version() < (2, 45), reason="GIT_NO_LAZY_FETCH needs Git 2.45")
def test_partial_clone_through_the_cli_json(tmp_path):
    _src, clone, record = _partial_clone_fixture(tmp_path, commit_store=True)
    before = _missing_objects(clone)
    assert before >= 1
    _code, out = _cli(clone / ".sidegraph", "--json")
    groups = json.loads(out)["drift_groups"]["groups"]
    entry = groups[0]["records"][0]
    assert entry["record_id"] == record
    assert entry["diff"]["unavailable"] == "baseline object not in this clone"
    assert _missing_objects(clone) == before


# --- guards: the hot path and the core's purity (both pass before and after; N) ----------

# Git calls refresh_code_drift_cache makes on the ``project`` fixture, measured on unchanged code.
REFRESH_GIT_CALLS = 3


def test_hot_path_unchanged(project, monkeypatch):
    """Guard (N, passes on both sides): the drift-cache refresh the SessionStart and Stop hooks
    run makes today's git calls and touches none of the grouping helpers."""
    from sidegraph import drift_checks
    from sidegraph.sync import refresh_code_drift_cache

    _root, store, _decision = project

    def forbidden(*_args, **_kwargs):
        raise AssertionError("grouping helper reached from the hot path")

    monkeypatch.setattr(drift_checks, "git_bytes_input", forbidden)
    monkeypatch.setattr(drift_checks, "tree_entries_all", forbidden)
    count = _count_git_calls(monkeypatch, lambda: refresh_code_drift_cache(store))
    assert count == REFRESH_GIT_CALLS


def _imports(body: list[ast.stmt], *, guarded: bool) -> list[str]:
    """Module names imported in ``body``; with ``guarded`` false, skip ``if TYPE_CHECKING:``."""
    names: list[str] = []
    for node in body:
        if isinstance(node, ast.If) and (
            isinstance(node.test, ast.Name) and node.test.id == "TYPE_CHECKING"
        ):
            if guarded:
                names += _imports(node.body, guarded=True)
            continue
        if isinstance(node, ast.Import):
            names += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            names.append("." * node.level + (node.module or ""))
            if node.module is None:  # ``from . import doctor``
                names += ["." * node.level + alias.name for alias in node.names]
        names += _imports(
            [child for child in ast.iter_child_nodes(node) if isinstance(child, ast.stmt)],
            guarded=guarded,
        )
    return names


def test_drift_groups_runtime_pure():
    """Guard (N, passes on both sides): ``drift_groups`` imports no doctor, server, host or
    engine at runtime. Its one ``doctor`` import sits under ``if TYPE_CHECKING:``."""
    tree = ast.parse((SRC / "sidegraph" / "drift_groups.py").read_text(encoding="utf-8"))
    runtime = _imports(tree.body, guarded=False)
    assert not [
        n for n in runtime if n.lstrip(".").split(".")[0] in ("doctor", "server", "host", "engine")
    ]
    under_guard = [n for n in _imports(tree.body, guarded=True) if n not in runtime]
    assert ".doctor" in under_guard
