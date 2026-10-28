"""Builders for grouped-drift tests: a git repo plus a hand-written loose-JSON store.

Records are canonical JSON files (the convention of ``test_doctor_code_drift.py``), with an
``index.db`` of ``live`` bindings so the scan has computed binding statuses.
# see design/superpowers/specs/2026-10-27-grouped-drift-triage-design.md
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path

from sidegraph import doctor, drift_groups
from sidegraph.doctor import CodeDriftScan, _binding_entries, _iter_records


def git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True, check=True
    ).stdout.strip()


def rid(n: int) -> str:
    """A deterministic 26-character id; a larger ``n`` sorts later, like a newer ULID."""
    return f"{n:026d}"


class GroupRepo:
    """A git repo with ``.sidegraph/`` records anchored to its files."""

    def __init__(self, tmp_path: Path) -> None:
        self.root = tmp_path / "repo"
        self.root.mkdir(parents=True)
        self.store = self.root / ".sidegraph"
        git(self.root, "init", "-q")
        git(self.root, "config", "user.email", "test@example.com")
        git(self.root, "config", "user.name", "Test")
        self._entities: dict[str, str] = {}
        self._rows: list[tuple[str, str, str]] = []

    def commit(self, files: dict[str, str | None], message: str = "change") -> str:
        """Write (or, for ``None``, remove) ``files``, commit, return the new HEAD."""
        for name, content in files.items():
            path = self.root / name
            if content is None:
                git(self.root, "rm", "-q", "-f", "--", name)
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            git(self.root, "add", "--", name)
        git(self.root, "commit", "-q", "--allow-empty", "-m", message)
        return self.head()

    def head(self) -> str:
        return git(self.root, "rev-parse", "HEAD")

    def _write(self, rel: str, payload: object) -> None:
        path = self.store / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")

    def entity(self, file_path: str, name: str | None = None) -> str:
        """The entity for ``file_path`` (file-level unless ``name`` is a symbol/section)."""
        name = name or file_path
        key = f"{file_path}\0{name}"
        if key not in self._entities:
            eid = f"E{len(self._entities):025d}"
            self._entities[key] = eid
            self._write(
                f"entities/{eid}.json",
                {
                    "entity_id": eid,
                    "canonical_name": name,
                    "descriptor": {"name": name, "file_path": file_path},
                },
            )
        return self._entities[key]

    def record(
        self,
        n: int,
        *,
        commit: str,
        files: list[str | tuple[str, str]],
        kind: object = "gotcha",
        title: object = None,
        status: str = "accepted",
        tier: int = 2,
        binding_status: str = "live",
    ) -> str:
        """An open decision captured at ``commit``, anchored to ``files`` (a ``(path, name)``
        pair anchors a symbol or section instead of the whole file)."""
        record_id = rid(n)
        self._write(
            f"decisions/{record_id}.json",
            {
                "id": record_id,
                "status": status,
                "kind": kind,
                "title": f"record {n}" if title is None else title,
                "valid_to": None,
                "provenance": {"source": "agent", "commit": commit},
            },
        )
        bindings = []
        for item in files:
            path, name = (item, None) if isinstance(item, str) else item
            eid = self.entity(path, name)
            bindings.append({"entity_id": eid, "tier": tier, "weight": 1.0})
            self._rows.append((record_id, eid, binding_status))
        self._write(f"bindings/{record_id}.json", bindings)
        return record_id

    def write_index(self) -> None:
        """Computed binding statuses, so deleted files stay in the scan."""
        index = self.store / "index.db"
        index.unlink(missing_ok=True)
        conn = sqlite3.connect(index)
        conn.execute("CREATE TABLE anchor_bindings (record_id TEXT, entity_id TEXT, data TEXT)")
        for record_id, entity_id, status in self._rows:
            conn.execute(
                "INSERT INTO anchor_bindings VALUES (?, ?, ?)",
                (record_id, entity_id, json.dumps({"status": status})),
            )
        conn.commit()
        conn.close()

    def records(self):
        return (
            _iter_records(self.store, "decisions"),
            _binding_entries(self.store),
            _iter_records(self.store, "entities"),
        )

    def scan(self) -> CodeDriftScan:
        self.write_index()
        decisions, bindings, entities = self.records()
        return doctor._scan_code_drift(decisions, bindings, entities, self.store, self.root)

    def build(self, **kwargs):
        """``(scan, DriftGroups | None)`` from one scan of the current state."""
        scan = self.scan()
        decisions, bindings, entities = self.records()
        return scan, drift_groups.build(scan, decisions, bindings, entities, self.store, **kwargs)


def entries_of(groups) -> dict[str, dict[str, dict]]:
    """``{file: {record_id: record_dict}}`` from a ``DriftGroups``'s JSON form."""
    return {
        g["file"]: {r["record_id"]: r for r in g["records"]} for g in groups.to_dict()["groups"]
    }
