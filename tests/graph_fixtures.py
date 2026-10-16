"""Small hand-built graph.json fixtures for the reader, anchoring and capture tests. Not a test
module: pytest collects only ``test_*.py``."""

from __future__ import annotations

import json
from pathlib import Path

from sidegraph.engine.reader import GraphifyReader

CLI = "src/sidegraph/cli.py"
BOOT_CLI = "src/sidegraph/bootstrap/cli.py"


def write_graph(tmp_path: Path, name: str, data: dict) -> Path:
    p = tmp_path / name
    p.write_text(json.dumps(data))
    return p


def node(node_id, label, source_file, *, file_type="code", location="L1", community=1) -> dict:
    out = {
        "id": node_id,
        "label": label,
        "norm_label": label.lower(),
        "file_type": file_type,
        "source_file": source_file,
        "community": community,
    }
    if location is not None:
        out["source_location"] = location
    return out


def reader_of(tmp_path: Path, *nodes: dict, links=(), graph=None) -> GraphifyReader:
    data = {"built_at_commit": "x", "nodes": list(nodes), "links": list(links)}
    if graph is not None:
        data["graph"] = graph
    return GraphifyReader(write_graph(tmp_path, "graph.json", data))


def two_cli_graph(tmp_path: Path) -> GraphifyReader:
    """0.9.90's shape: two ``cli.py`` files carry path-suffix labels, one holds a symbol, and a
    package specifier is its own ``source_file`` (openspec's ``@inquirer/prompts``)."""
    return reader_of(
        tmp_path,
        node("src_sidegraph_cli", "sidegraph/cli.py", CLI),
        node("src_sidegraph_bootstrap_cli", "bootstrap/cli.py", BOOT_CLI, community=2),
        node("src_sidegraph_cli_main", "main()", CLI, location="L10"),
        node(
            "inquirer_prompts", "@inquirer/prompts", "@inquirer/prompts", location=None, community=3
        ),
    )
