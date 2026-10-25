"""Shared setup for the drift-check tests: a two-commit repo with one bound decision, built once."""

from __future__ import annotations

import asyncio
import atexit
import shutil
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import fastmcp
import pytest

from sidegraph import server
from sidegraph.doctor import scan_code_drift
from sidegraph.schema import AnchorBinding, Decision, Descriptor, Entity, Provenance
from sidegraph.store import Store
from tests.store_format2_support import format1_store


def git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True, check=True
    ).stdout.strip()


_PROJECT_TEMPLATE: list[tuple[Path, str]] = []


def _project_template() -> tuple[Path, str]:
    """The repo the ``project`` fixture hands out, built once per process: two commits, a
    format-1 store with one accepted decision bound to ``code.py``. Each test copies it."""
    if not _PROJECT_TEMPLATE:
        holder = Path(tempfile.mkdtemp(prefix="sidegraph-drift-template-"))
        atexit.register(shutil.rmtree, holder, ignore_errors=True)
        root = holder / "repo"
        root.mkdir()
        git(root, "init", "-q")
        git(root, "config", "user.name", "Synthetic Reviewer")
        git(root, "config", "user.email", "fixture@example.invalid")
        source = root / "code.py"
        source.write_text("def policy():\n    return 1\n", encoding="utf-8")
        git(root, "add", "code.py")
        git(root, "commit", "-q", "-m", "initial policy")
        capture = git(root, "rev-parse", "HEAD")
        with Store(format1_store(root / ".sidegraph")) as store:
            decision = store.add_decision(
                Decision(
                    title="Keep policy pure",
                    kind="constraint",
                    status="accepted",
                    context="Callers need a deterministic return value.",
                    choice="Policy returns one without external effects.",
                    rejected="Global state makes callers order-dependent.",
                    valid_from=datetime.now(UTC),
                    provenance=Provenance(source="manual", commit=capture),
                )
            )
            entity = store.upsert_entity(
                Entity(
                    canonical_name="policy",
                    descriptor=Descriptor(name="policy", file_path="code.py"),
                )
            )
            store.add_binding(
                AnchorBinding(record_id=decision.id, entity_id=entity.entity_id, tier=2)
            )
        source.write_text("def policy():\n    return (1)\n", encoding="utf-8")
        git(root, "add", "code.py")
        git(root, "commit", "-q", "-m", "format-only movement")
        _PROJECT_TEMPLATE.append((root, decision.id))
    return _PROJECT_TEMPLATE[0]


@pytest.fixture
def project(tmp_path, monkeypatch):
    template, decision_id = _project_template()
    root = tmp_path / "repo"
    shutil.copytree(template, root, symlinks=True)
    with Store(root / ".sidegraph") as store:
        decision = store.get_decision(decision_id)
        assert decision is not None
        monkeypatch.setattr(server, "_store", store)
        monkeypatch.setattr(server, "_load_reader", lambda: None)
        yield root, store, decision


def record_check(root: Path, decision_id: str, **overrides):
    async def run():
        async with fastmcp.Client(server.mcp) as client:
            arguments = {
                "decision_id": decision_id,
                "checked_commit": git(root, "rev-parse", "HEAD"),
                "files": ["code.py"],
                "rationale": "Only formatting moved; return value and purity still hold.",
                **overrides,
            }
            return await client.call_tool("record_drift_check", arguments, raise_on_error=False)

    return asyncio.run(run())


def drift_ids(store: Store) -> set[str]:
    return {
        entry.record_id for batch in scan_code_drift(store.path).batches for entry in batch.entries
    }


def require_check(root, decision_id, **overrides):
    result = record_check(root, decision_id, **overrides)
    assert not result.is_error, result.content
    return result.structured_content
