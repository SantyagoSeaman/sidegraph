"""Noninteractive Git helpers must not consume their parent's protocol input."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

PROFILES = (
    "verify_text",
    "verify_bytes",
    "derive_initiative",
    "ratifier_identity",
    "reader_changed_files",
    "reader_repo_root",
    "reader_freshness",
)


@pytest.mark.parametrize("profile", PROFILES)
def test_git_child_cannot_consume_parent_protocol(tmp_path: Path, profile: str) -> None:
    # Replace only the executable: real child IO receives every option from the caller.
    program = f"""
import json, subprocess, sys
from pathlib import Path
from types import SimpleNamespace
from sidegraph import capture, store, verify
from sidegraph.engine.reader import GraphifyReader
profile = {profile!r}
root = Path.cwd()
graph = root / 'graph.json'
graph.write_text(json.dumps({{'nodes': [], 'links': [], 'built_at_commit': 'a' * 40}}),
                 encoding='utf-8')
reader = GraphifyReader(graph)
output = {{
    'verify_text': b'git fixture', 'verify_bytes': b'git fixture',
    'derive_initiative': b'feature/probe', 'ratifier_identity': b'Fixture Author',
    'reader_changed_files': b'fixture.py', 'reader_repo_root': str(root).encode('utf-8'),
    'reader_freshness': b'a' * 40,
}}[profile]
real_run = subprocess.run
observations = []
def launch(command, **options):
    assert command[0] == 'git'
    child = ("import sys; sys.stderr.buffer.write(repr(sys.stdin.buffer.read()).encode('ascii')); "
             + "sys.stdout.buffer.write(" + repr(output) + ")")
    result = real_run([sys.executable, '-I', '-c', child], **options)
    stream_type = bytes if profile in ('verify_bytes', 'reader_freshness') else str
    observed = result.stderr if isinstance(result.stderr, str) else result.stderr.decode('ascii')
    observations.append({{'input': observed,
                         'types_ok': isinstance(result.stdout, stream_type)
                                     and isinstance(result.stderr, stream_type)}})
    return result
subprocess.run = launch
def verify_ok(text):
    result = verify._run_git(['probe'], root, text=text, timeout=5)
    return result.returncode == 0 and result.stdout == (output.decode('ascii') if text else output)
store_fixture = SimpleNamespace(path=root / '.sidegraph')
operations = {{
    'verify_text': lambda: verify_ok(True), 'verify_bytes': lambda: verify_ok(False),
    'derive_initiative': lambda: capture._derive_initiative(store_fixture)
                                 == 'feature-probe',
    'ratifier_identity': lambda: store._ratifier_identity() == 'Fixture Author',
    'reader_changed_files': lambda: reader.changed_files() == ['fixture.py'],
    'reader_repo_root': lambda: reader.repo_root() == root.resolve(),
    'reader_freshness': lambda: reader.freshness(root=root).state == 'fresh',
}}
operation_ok = operations[profile]()
print(json.dumps({{'observations': observations, 'operation_ok': operation_ok,
                  'parent': sys.stdin.buffer.read().decode('ascii')}}))
"""
    wire = b'{"jsonrpc":"2.0","id":7,"method":"probe"}\n'
    parent = subprocess.run(
        [sys.executable, "-I", "-c", program],
        cwd=tmp_path,
        input=wire,
        capture_output=True,
        timeout=15,
        check=True,
    )
    assert json.loads(parent.stdout) == {
        "observations": [{"input": "b''", "types_ok": True}],
        "operation_ok": True,
        "parent": wire.decode("ascii"),
    }
