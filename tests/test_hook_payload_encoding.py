"""The hook payload is UTF-8 on every platform, whatever the stdin code page.

Claude Code and Codex write the payload as raw UTF-8 JSON. Python decodes a piped stdin with the
ANSI code page on Windows (cp1252), so an accented or Cyrillic character in ``transcript_path``,
``cwd`` or a Read ``file_path`` turned into mojibake: Stop could not open the transcript and
PreToolUse could not resolve the file. These tests run the hooks in real subprocesses, the way a
host does. ``PYTHONIOENCODING=cp1252`` reproduces the Windows default on POSIX; the case with no
override is the native default on Windows.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.test_host_pretool_records import Project
from tests.test_host_stop import _substantial

ENCODINGS = [pytest.param(None, id="default"), pytest.param("cp1252", id="cp1252")]

NON_ASCII_DIR = "probe é проект"


def _child_env(encoding: str | None, **extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in ("PYTHONIOENCODING", "PYTHONUTF8")}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    if encoding:
        env["PYTHONIOENCODING"] = encoding
    return {**env, **extra}


def _run_child(code: str, payload: dict, env: dict[str, str], cwd: Path | None = None) -> str:
    result = subprocess.run(
        [sys.executable, "-c", code],
        input=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        capture_output=True,
        env=env,
        cwd=cwd,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    return result.stdout.decode("ascii")


@pytest.mark.parametrize("encoding", ENCODINGS)
def test_read_payload_round_trips_non_ascii_paths(encoding):
    payload = {
        "transcript_path": "C:\\Users\\José\\probe é\\s.jsonl",
        "cwd": "/srv/пользователь/проект",
    }
    code = (
        "import json\n"
        "from sidegraph.host.hooks import _read_payload\n"
        "print(json.dumps(_read_payload()))\n"
    )
    out = _run_child(code, payload, _child_env(encoding))
    assert json.loads(out) == payload


@pytest.mark.parametrize("encoding", ENCODINGS)
def test_stop_opens_a_transcript_under_a_non_ascii_directory(tmp_path, encoding):
    directory = tmp_path / NON_ASCII_DIR
    directory.mkdir()
    transcript = _substantial(directory, name="s1.jsonl")
    payload = {"session_id": "s1", "stop_hook_active": False, "transcript_path": transcript}
    env = _child_env(encoding, SIDEGRAPH_DIR=str(tmp_path / "s.db"))
    out = _run_child("from sidegraph.host.hooks import stop; stop()", payload, env)
    assert json.loads(out)["decision"] == "block"


@pytest.mark.parametrize("encoding", ENCODINGS)
def test_pre_tool_use_delivers_a_record_under_a_non_ascii_project_dir(tmp_path, encoding):
    project = Project(tmp_path / NON_ASCII_DIR)
    try:
        project.record("src/store.py", "Gotcha of the store")
        payload = {
            "session_id": "enc-1",
            "tool_name": "Read",
            "tool_input": {"file_path": str(project.root / "src" / "store.py")},
            "cwd": str(project.root),
        }
        env = _child_env(
            encoding,
            CLAUDE_PROJECT_DIR=str(project.root),
            SIDEGRAPH_DIR=str(project.store_dir),
        )
        out = _run_child(
            "from sidegraph.host.hooks import pre_tool_use; pre_tool_use()",
            payload,
            env,
            cwd=project.root,
        )
    finally:
        project.close()
    assert "Gotcha of the store" in json.loads(out)["hookSpecificOutput"]["additionalContext"]
