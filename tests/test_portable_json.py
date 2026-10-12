"""JSON produced by Graphify and host configuration is UTF-8 on every default locale."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from sidegraph.engine.reader import GraphifyReader


@pytest.mark.slow
def test_graph_labels_and_settings_use_utf8_with_an_ascii_default(tmp_path):
    # The forced-ASCII child needs an ASCII filesystem path; native Unicode paths are
    # covered separately. Windows uses a cp1252 text-default shim in the same child.
    graph = tmp_path / "graph.json"
    graph.write_text(
        json.dumps({"nodes": [{"id": "n", "label": "café"}], "links": []}, ensure_ascii=False),
        encoding="utf-8",
    )
    (tmp_path / ".graphify_labels.json").write_text('{"0": "mémoire"}', encoding="utf-8")
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir()
    settings.write_text('{"name": "研究 café"}', encoding="utf-8")
    script = """
import builtins, io, json, os, sys
from pathlib import Path
if os.name == 'nt':
    old_open, old_io_open = builtins.open, io.open
    def default_encoding(fn):
        def open_text(file, mode='r', buffering=-1, encoding=None, *a, **kw):
            if 'b' not in mode and encoding is None:
                encoding = 'cp1252'
            return fn(file, mode, buffering, encoding, *a, **kw)
        return open_text
    builtins.open, io.open = default_encoding(old_open), default_encoding(old_io_open)
from sidegraph.engine.reader import GraphifyReader
from sidegraph.host.claude_settings import current_ratify_policy, ensure_ratify_policy_setting
root = Path(sys.argv[1])
reader = GraphifyReader(root / 'graph.json')
assert reader._raw['nodes'][0]['label'] == 'caf\\u00e9'
assert reader.community_labels() == {'0': 'm\\u00e9moire'}
assert current_ratify_policy(root).outcome == 'unset'
assert ensure_ratify_policy_setting(root).outcome == 'written'
data = json.loads((root / '.claude/settings.json').read_text(encoding='utf-8'))
assert data['name'] == '\\u7814\\u7a76 caf\\u00e9'
fresh = root / 'fresh'
assert ensure_ratify_policy_setting(fresh, value='m\\u00e9moire').outcome == 'written'
data = json.loads((fresh / '.claude/settings.json').read_text(encoding='utf-8'))
assert data['env']['SIDEGRAPH_RATIFY_POLICY'] == 'm\\u00e9moire'
"""
    env = {**os.environ, "PYTHONUTF8": "0", "PYTHONCOERCECLOCALE": "0", "LC_ALL": "C"}
    completed = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)], env=env, capture_output=True, timeout=15
    )
    assert completed.returncode == 0, completed.stderr.decode("utf-8", errors="replace")


def test_native_spaced_unicode_paths_preserve_graph_and_settings(tmp_path):
    from sidegraph.host.claude_settings import ensure_ratify_policy_setting

    root = tmp_path / "mémoire 研究"
    root.mkdir()
    graph = root / "graph.json"
    graph.write_text('{"nodes": [{"id":"n", "label":"研究"}], "links": []}', encoding="utf-8")
    assert GraphifyReader(graph)._raw["nodes"][0]["label"] == "研究"
    assert ensure_ratify_policy_setting(root).outcome == "written"


def test_invalid_utf8_optional_labels_are_ignored(tmp_path):
    graph = tmp_path / "graph.json"
    graph.write_text('{"nodes": [], "links": []}', encoding="utf-8")
    (tmp_path / ".graphify_labels.json").write_bytes(b"\xff")
    assert GraphifyReader(graph).community_labels() == {}


def test_transient_incomplete_utf8_graph_is_retried(tmp_path, monkeypatch):
    from sidegraph.engine import reader as module

    graph = tmp_path / "graph.json"
    graph.write_bytes(b"\xff")
    reads = 0
    original = Path.read_text

    def read(path, *args, **kwargs):
        nonlocal reads
        if path == graph:
            reads += 1
            if reads == 2:
                graph.write_text('{"nodes": [], "links": []}', encoding="utf-8")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read)
    monkeypatch.setattr(module.time, "sleep", lambda _: None)
    assert GraphifyReader(graph)._raw["nodes"] == []
    assert reads == 2


def test_permanently_invalid_utf8_graph_has_bounded_retries(tmp_path, monkeypatch):
    from sidegraph.engine import reader as module

    graph = tmp_path / "graph.json"
    graph.write_bytes(b"\xff")
    waits = []
    monkeypatch.setattr(module.time, "sleep", waits.append)
    with pytest.raises(UnicodeDecodeError):
        GraphifyReader(graph)
    assert len(waits) == 3
