"""The four hook entries anchor to the project root with ``--repo-root``, as ``sidegraph-mcp`` does.

The Claude plugin manifests no longer ``cd "${CLAUDE_PROJECT_DIR}"`` before the hook; they pass
``--repo-root`` and the entry moves itself. The flag is removed before the entry refuses
unexpected arguments, so any other argument is still refused. Anchoring must happen before the
payload is read, which is before any store or project read.
see design/superpowers/specs/2026-11-03-claude-plugin-bundled-runtime-design.md (§4.6, T16, T17)
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.test_hot_path_imports import ANCHORED, _repo

ENTRIES = [
    ("sidegraph.host.hooks", "session_start"),
    ("sidegraph.host.hooks", "stop"),
    ("sidegraph.host.hooks", "pre_tool_use"),
    ("sidegraph.host.subagent", "subagent_start"),
]

# Replaces stdin with an object that reports the cwd at the moment the entry first reads its
# payload, then exits. Every entry reads the payload after its argument handling.
PROBE = (
    "import os, sys\n"
    "sys.argv = ['entry', *{args!r}]\n"
    "class _Stdin:\n"
    "    def _stop(self):\n"
    "        sys.stderr.write('CWD=' + os.getcwd() + '\\n')\n"
    "        raise SystemExit(0)\n"
    "    def read(self, *a):\n"
    "        self._stop()\n"
    "    class buffer:\n"
    "        @staticmethod\n"
    "        def read(*a):\n"
    "            _Stdin()._stop()\n"
    "sys.stdin = _Stdin()\n"
    "from {module} import {entry}\n"
    "{entry}()\n"
)


def _run_probe(
    module: str, entry: str, args: list[str], project: Path
) -> subprocess.CompletedProcess:
    sub = project / "sub"
    sub.mkdir(exist_ok=True)
    env = {**os.environ, "CLAUDE_PROJECT_DIR": str(project)}
    return subprocess.run(
        [sys.executable, "-c", PROBE.format(module=module, entry=entry, args=args)],
        input="{}",
        capture_output=True,
        text=True,
        env=env,
        cwd=sub,
    )


def _cwd_of(done: subprocess.CompletedProcess) -> str:
    assert "CWD=" in done.stderr, (done.returncode, done.stdout, done.stderr)
    line = [ln for ln in done.stderr.splitlines() if ln.startswith("CWD=")][-1]
    return os.path.realpath(line[len("CWD=") :])


@pytest.mark.parametrize(("module", "entry"), ENTRIES)
def test_the_flag_moves_the_entry_to_the_project_before_the_payload_is_read(
    module, entry, tmp_path
):
    project = tmp_path / "project"
    project.mkdir()
    done = _run_probe(module, entry, ["--repo-root"], project)
    assert _cwd_of(done) == os.path.realpath(project)


@pytest.mark.parametrize(("module", "entry"), ENTRIES)
def test_without_the_flag_the_entry_stays_where_it_was_started(module, entry, tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    done = _run_probe(module, entry, [], project)
    assert _cwd_of(done) == os.path.realpath(project / "sub")


@pytest.mark.parametrize(("module", "entry"), ENTRIES)
def test_the_flag_does_not_excuse_another_argument(module, entry, tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    done = _run_probe(module, entry, ["--repo-root", "--bogus"], project)
    assert done.returncode == 2
    assert "unexpected argument '--bogus'" in done.stderr
    assert "unexpected argument '--repo-root'" not in done.stderr


@pytest.mark.parametrize(("module", "entry"), ENTRIES)
def test_help_after_the_flag_prints_the_usage(module, entry, tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    done = _run_probe(module, entry, ["--repo-root", "--help"], project)
    assert done.returncode == 0
    assert "usage" in done.stdout.lower()


@pytest.mark.parametrize(("module", "entry"), ENTRIES)
def test_the_usage_names_the_flag(module, entry, tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    done = _run_probe(module, entry, ["--help"], project)
    assert done.returncode == 0
    assert "[--repo-root]" in done.stdout
    assert "Takes no arguments." not in done.stdout
    assert "except --repo-root" in done.stdout


# -- T17: the real entries from a subdirectory answer as they do from the root -----------------


def _hook(module: str, entry: str, payload: dict, cwd: Path, store_dir: Path) -> str:
    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_PROJECT_DIR"}
    env["SIDEGRAPH_DIR"] = ".sidegraph"
    # The store sits at the project root: a relative SIDEGRAPH_DIR only resolves there once the
    # entry has moved itself, which is what the flag is for.
    assert store_dir.name == ".sidegraph"
    done = subprocess.run(
        [
            sys.executable,
            "-c",
            f"import sys; sys.argv = ['entry', '--repo-root']\n"
            f"from {module} import {entry}\n{entry}()\n",
        ],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=env,
        cwd=cwd,
    )
    assert done.returncode == 0, done.stderr
    return done.stdout


@pytest.mark.slow
def test_pre_tool_use_from_a_subdirectory_answers_as_from_the_root(tmp_path):
    root, store_dir = _repo(tmp_path)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    sub = root / "sub"
    sub.mkdir()

    def payload(session: str) -> dict:
        # One session id per run: a session is told about a file's records only once.
        return {
            "session_id": session,
            "tool_name": "Read",
            "tool_input": {"file_path": str(root / ANCHORED)},
        }

    from_sub = _hook("sidegraph.host.hooks", "pre_tool_use", payload("s-sub"), sub, store_dir)
    from_root = _hook("sidegraph.host.hooks", "pre_tool_use", payload("s-root"), root, store_dir)
    assert from_sub == from_root
    assert "Never call foo twice" in from_sub


@pytest.mark.slow
def test_session_start_from_a_subdirectory_answers_as_from_the_root(tmp_path):
    root, store_dir = _repo(tmp_path)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    sub = root / "sub"
    sub.mkdir()
    from_sub = _hook(
        "sidegraph.host.hooks",
        "session_start",
        {"session_id": "s-sub", "source": "startup"},
        sub,
        store_dir,
    )
    from_root = _hook(
        "sidegraph.host.hooks",
        "session_start",
        {"session_id": "s-root", "source": "startup"},
        root,
        store_dir,
    )
    # The map is the memory. A host-health notice (``systemMessage``, for instance a stale
    # console script on PATH) is shown once per machine and is not about the anchor.
    sub_map = json.loads(from_sub)["hookSpecificOutput"]
    assert sub_map == json.loads(from_root)["hookSpecificOutput"]
    assert "Never call foo twice" in sub_map["additionalContext"]
