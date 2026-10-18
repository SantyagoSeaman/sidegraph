"""Review and explicitly write deterministic ADR projections; source stores stay read-only."""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
from collections import Counter
from pathlib import Path
from typing import Any

from .adr import AdrSnapshot, read_snapshot
from .adr_files import AdrPlan, apply_plan, plan_export
from .config import resolve_store_path
from .doctor import _binding_status_snapshot, scan_code_drift
from .profiles import AdrExport, get_profile
from .schema import DecisionStatus


def _template(path: Path) -> str:
    """Read a bounded regular template, including final-leaf replacement guards."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    fd = os.open(path, flags)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > 65_536:
            raise ValueError("ADR template must be a regular UTF-8 file of at most64KiB")
        handle = os.fdopen(fd, "rb", buffering=0)
    except BaseException:
        os.close(fd)
        raise
    with handle:
        data = handle.read(65_537)
    if len(data) > 65_536:
        raise ValueError("ADR template exceeds64KiB")
    return data.decode("utf-8-sig")


def _avoid_index_open(root: Path) -> str | None:
    """A read-only SQLite WAL open can create WAL/SHM; do not open that index here.

    SQLite file format: header bytes18/19 are1 for rollback and2 for WAL.
    https://www.sqlite.org/fileformat2.html#file_format_version_numbers
    This guard is for a stable source, not a transaction against concurrent mode changes.
    """
    if any((root / name).exists() for name in ("index.db-wal", "index.db-shm")):
        return "WAL index not opened to preserve source files"
    path = root / "index.db"
    if not path.exists():
        return "binding index unavailable"
    try:
        with path.open("rb") as handle:
            header = handle.read(20)
    except OSError:
        return "binding index unreadable"
    if not header.startswith(b"SQLite format 3\x00") or header[18:20] != b"\x01\x01":
        return "WAL or unrecognized index not opened to preserve source files"
    return None


def drift_advisories(snapshot: AdrSnapshot) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Reuse capture-to-HEAD advisories without treating missing entries as clean."""
    values: dict[str, dict[str, Any]] = {}
    opened = {
        rid: decision
        for rid, decision in snapshot.exported.items()
        if decision.status is DecisionStatus.ACCEPTED and decision.valid_to is None
    }
    for rid in snapshot.exported.keys() - opened.keys():
        values[rid] = {"state": "not-applicable", "reason": "closed or archived decision"}
    if not opened:
        return values, []
    avoid = _avoid_index_open(snapshot.root)
    statuses = _binding_status_snapshot(snapshot.root) if avoid is None else None
    if statuses is not None and statuses.uncomputed:
        avoid = "binding statuses are uncomputed"
    if avoid:
        values.update({rid: {"state": "unknown", "reason": avoid} for rid in opened})
        return values, [f"Code drift unknown for {len(opened)} ADR(s): {avoid}."]
    assert statuses is not None
    scan = scan_code_drift(snapshot.root, deadline=5.0)
    batches = {batch.commit: batch for batch in scan.batches}
    changed = {
        entry.record_id: entry
        for batch in scan.batches
        if not batch.failed
        for entry in batch.entries
    }
    warnings = []
    unknown = 0
    for rid, decision in opened.items():
        commit = decision.provenance.commit
        batch = batches.get(commit) if commit else None
        scope = [
            entity.descriptor.file_path
            for binding in snapshot.bindings.get(rid, [])
            if statuses.statuses.get((rid, binding.entity_id)) != "orphaned"
            if (entity := snapshot.entities.get(binding.entity_id)) is not None
            if entity.descriptor is not None and entity.descriptor.file_path
        ]
        if (
            scan.repo_root_failed
            or scan.check_skipped
            or scan.binding_statuses_uncomputed
            or not scan.head
            or not commit
            or batch is None
            or batch.failed
            or not scope
        ):
            values[rid] = {
                "state": "unknown",
                "reason": "no completed, certified record-scope check",
            }
            unknown += 1
        elif rid in changed:
            entry = changed[rid]
            values[rid] = {
                "state": "changed",
                "paths": entry.files,
                "reason": entry.check_reason or "capture-to-HEAD file advisory",
            }
            warnings.append(
                f"{rid}: code drift advisory in {', '.join(entry.files)}; "
                "review, do not silently rewrite memory"
            )
        else:
            values[rid] = {
                "state": "not-detected",
                "reason": "completed capture-to-HEAD file check",
            }
    if unknown:
        warnings.append(f"Code drift unknown for {unknown} ADR(s); absence is not a clean verdict.")
    return values, warnings


def _report(plan: AdrPlan, mode: str, advisories: dict) -> dict[str, Any]:
    return {
        "mode": mode,
        "changes": [
            {
                "path": c.path,
                "record_id": c.record_id,
                "action": c.action,
                "diff": c.diff,
                "message": c.message,
            }
            for c in plan.changes
        ],
        "warnings": plan.warnings,
        "advisories": advisories,
        "summary": dict(Counter(c.action for c in plan.changes)),
    }


def main(argv: list[str] | None = None) -> int:
    """sidegraph-export-adr: preview by default; --write applies a conflict-free plan."""
    parser = argparse.ArgumentParser(
        prog="sidegraph-export-adr",
        description="Preview record-derived ADR changes; write only with --write.",
    )
    parser.add_argument(
        "--db", help="store directory (SIDEGRAPH_DIR preferred; SIDEGRAPH_DB deprecated)"
    )
    parser.add_argument(
        "--profile", default="generic-adr", help="flow profile (default: generic-adr)"
    )
    parser.add_argument(
        "--out", help="output directory (default: the selected profile's directory)"
    )
    parser.add_argument("--template", help="UTF-8 body template (at most64KiB)")
    parser.add_argument("--filename", help="basename pattern with {id} and optional {number:04d}")
    parser.add_argument(
        "--write", action="store_true", help="explicitly apply the reviewed changes"
    )
    parser.add_argument(
        "--json", action="store_true", help="structured changes, diffs and advisories"
    )
    args = parser.parse_args(argv)
    mode = "write" if args.write else "preview"
    report: dict[str, Any] | None = None
    attempted = False
    applied = False
    try:
        config = get_profile(args.profile).adr_export
        if config is None:
            raise ValueError("flow profile has no ADR export configuration")
        fields = config.model_dump()
        if args.template:
            fields["template"] = _template(Path(args.template))
        if args.filename:
            fields["filename"] = args.filename
        config = AdrExport.model_validate(fields)
        db = Path(resolve_store_path(args.db, warn_on_create=False))
        snapshot = read_snapshot(db)
        out = Path(args.out) if args.out else db.absolute().parent / config.directory
        plan = plan_export(snapshot, out, config)
        advisories, warnings = drift_advisories(snapshot)
        plan.warnings.extend(warnings)
        report = _report(plan, mode, advisories)
        if any(c.action == "conflict" for c in plan.changes):
            report["error"] = "ADR plan contains conflicts; no writes applied"
            code = 2
        else:
            if args.write:
                attempted = True
                apply_plan(plan)
                applied = True
            code = 0
    except (OSError, ValueError) as exc:
        report = report or {
            "mode": mode,
            "changes": [],
            "warnings": [],
            "advisories": {},
            "summary": {},
        }
        report["error"] = str(exc)
        code = 2
    report["applied"] = applied
    report["partial_write_possible"] = attempted and not applied
    if attempted and not applied:
        report["warnings"].append(
            "Write failed; earlier files or a new partial file may remain. Review before retrying."
        )
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        for change in report["changes"]:
            print(
                f"{change['action']}: {change['path']}"
                + (f" — {change['message']}" if change["message"] else "")
            )
            if change["diff"]:
                sys.stdout.write(change["diff"])
        for warning in report["warnings"]:
            print(f"warning: {warning}", file=sys.stderr)
        if report.get("error"):
            print(report["error"], file=sys.stderr)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
