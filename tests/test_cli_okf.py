"""CLI + filesystem contract tests for sidegraph-export-okf (see
design/superpowers/specs/2026-07-23-okf-export-design.md): exit codes, out-dir safety,
exact-snapshot semantics, byte-determinism, OKF v0.1 conformance, link integrity, and
the canonical-store-untouched guarantee."""

import re
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from sidegraph.cli import doctor_main, export_okf_main, verify_main, viz_main
from sidegraph.schema import (
    AnchorBinding,
    Decision,
    DecisionKind,
    DecisionStatus,
    Descriptor,
    Domain,
    DomainStatus,
    Entity,
    Fact,
    Provenance,
)
from sidegraph.store import Store

NOW = datetime(2026, 7, 23, 12, 0, 0, tzinfo=UTC)


def _seed(tmp_path: Path) -> Path:
    """A store exercising every concept type: 2 decisions (one superseded), 1 fact,
    1 domain (+ its domain: entity binding), 1 concrete entity."""
    db = tmp_path / "store"
    s = Store(db)
    e = s.upsert_entity(
        Entity(
            canonical_name="auth.login",
            descriptor=Descriptor(name="login", file_path="src/auth.py"),
        )
    )
    s.add_domain(
        Domain(
            slug="auth",
            title="Authentication",
            summary="Signing users in.",
            status=DomainStatus.ACCEPTED,
            provenance=Provenance(source="manual"),
        )
    )
    old = s.add_decision(
        Decision(
            title="Old way",
            kind=DecisionKind.ADR,
            status=DecisionStatus.ACCEPTED,
            context="c",
            choice="old",
            valid_from=NOW,
            provenance=Provenance(source="manual"),
        )
    )
    d = s.add_decision(
        Decision(
            title="Use sessions",
            kind=DecisionKind.ADR,
            status=DecisionStatus.ACCEPTED,
            context="c",
            choice="sessions",
            valid_from=NOW,
            supersedes=old.id,
            provenance=Provenance(source="manual"),
        )
    )
    f = s.add_fact(
        Fact(
            statement="Sessions scale fine.",
            source="load test",
            supports=[d.id],
            status=DecisionStatus.ACCEPTED,
            valid_from=NOW,
            provenance=Provenance(source="manual"),
        )
    )
    s.add_binding(AnchorBinding(record_id=d.id, entity_id=e.entity_id, tier=2))
    dom_ent = s.get_or_create_abstract_entity("domain:auth")
    s.add_binding(AnchorBinding(record_id=d.id, entity_id=dom_ent.entity_id, tier=1))
    s.add_binding(AnchorBinding(record_id=f.id, entity_id=e.entity_id, tier=2))
    return db


def _export(db: Path, out: Path) -> int:
    return export_okf_main(["--db", str(db), "--out", str(out)])


def _files(out: Path) -> dict[str, str]:
    return {
        str(p.relative_to(out)): p.read_text(encoding="utf-8") for p in sorted(out.rglob("*.md"))
    }


class TestExitCodes:
    def test_export_succeeds_and_prints_summary(self, tmp_path: Path, capsys) -> None:
        db = _seed(tmp_path)
        out = tmp_path / "bundle"
        assert _export(db, out) == 0
        assert (
            capsys.readouterr().out.strip()
            == f"wrote {out}/ (2 decisions, 1 facts, 1 domains, 1 entities)"
        )
        assert (out / "index.md").is_file()

    def test_uninitialized_store_exits_2(self, tmp_path: Path, capsys) -> None:
        assert _export(tmp_path / "missing", tmp_path / "bundle") == 2
        assert "run `sidegraph-init` first" in capsys.readouterr().err
        assert not (tmp_path / "missing").exists()  # never auto-created


class TestOutDirSafety:
    def test_refuses_non_bundle_dir_and_leaves_it_untouched(self, tmp_path: Path, capsys) -> None:
        db = _seed(tmp_path)
        out = tmp_path / "precious"
        out.mkdir()
        (out / "keep.txt").write_text("mine\n", encoding="utf-8")
        assert _export(db, out) == 2
        assert "refusing to overwrite" in capsys.readouterr().err
        assert (out / "keep.txt").read_text(encoding="utf-8") == "mine\n"
        assert not (out / "index.md").exists()

    def test_empty_existing_dir_is_used(self, tmp_path: Path) -> None:
        db = _seed(tmp_path)
        out = tmp_path / "bundle"
        out.mkdir()
        assert _export(db, out) == 0
        assert (out / "index.md").is_file()

    def test_reexport_wipes_stale_files(self, tmp_path: Path) -> None:
        db = _seed(tmp_path)
        out = tmp_path / "bundle"
        assert _export(db, out) == 0
        stale = out / "decisions" / "stale-00000000000000000000000000.md"
        stale.write_text("---\ntype: decision\n---\n\nstale\n", encoding="utf-8")
        assert _export(db, out) == 0
        assert not stale.exists()


class TestBundleContract:
    def test_determinism_byte_identical(self, tmp_path: Path) -> None:
        db = _seed(tmp_path)
        out1, out2 = tmp_path / "b1", tmp_path / "b2"
        assert _export(db, out1) == 0
        assert _export(db, out2) == 0
        files1, files2 = _files(out1), _files(out2)
        assert files1.keys() == files2.keys()
        assert files1 == files2

    def test_okf_conformance(self, tmp_path: Path) -> None:
        db = _seed(tmp_path)
        out = tmp_path / "bundle"
        assert _export(db, out) == 0
        for rel, text in _files(out).items():
            name = rel.rsplit("/", 1)[-1]
            if name in ("index.md", "log.md"):
                # Reserved files: frontmatter only on the ROOT index.md.
                if rel == "index.md":
                    fm = yaml.safe_load(text.split("---\n")[1])
                    assert fm["okf_version"] == "0.1"
                    assert fm["generator"] == "sidegraph"
                else:
                    assert not text.startswith("---"), rel
                continue
            assert text.startswith("---\n"), rel
            fm = yaml.safe_load(text.split("---\n")[1])
            assert isinstance(fm, dict), rel
            assert fm.get("type"), rel

    def test_link_integrity(self, tmp_path: Path) -> None:
        db = _seed(tmp_path)
        out = tmp_path / "bundle"
        assert _export(db, out) == 0
        links = [
            target
            for text in _files(out).values()
            for target in re.findall(r"\]\((/[^)]+)\)", text)
        ]
        assert links, "seed must produce cross-links"
        for target in links:
            assert (out / target.lstrip("/")).is_file(), target

    def test_every_written_file_ends_with_single_newline(self, tmp_path: Path) -> None:
        db = _seed(tmp_path)
        out = tmp_path / "bundle"
        assert _export(db, out) == 0
        for rel, text in _files(out).items():
            assert text.endswith("\n") and not text.endswith("\n\n"), rel

    def test_canonical_store_files_untouched(self, tmp_path: Path) -> None:
        db = _seed(tmp_path)
        canonical = {
            str(p.relative_to(db)): p.read_bytes()
            for p in sorted(db.rglob("*"))
            if p.is_file() and p.name != "index.db"
        }
        assert _export(db, tmp_path / "bundle") == 0
        after = {
            str(p.relative_to(db)): p.read_bytes()
            for p in sorted(db.rglob("*"))
            if p.is_file() and p.name != "index.db"
        }
        assert canonical == after


class TestNoCreateWarning:
    """The never-auto-creating CLIs (verify/doctor/viz/export-okf) must not emit
    resolve_store_path's "creating new store" notice on their missing-store error path —
    it would directly contradict the "run sidegraph-init first" message that follows."""

    @pytest.mark.parametrize(
        "main",
        [verify_main, doctor_main, viz_main, export_okf_main],
        ids=["verify", "doctor", "viz", "export-okf"],
    )
    def test_missing_default_store_warns_nothing_about_creating(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys, main
    ) -> None:
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("SIDEGRAPH_DIR", raising=False)
        monkeypatch.delenv("SIDEGRAPH_DB", raising=False)
        assert main([]) != 0  # operational error, store never created
        assert "creating new store" not in capsys.readouterr().err
        assert not (tmp_path / ".sidegraph").exists()
