"""CLI + filesystem contract tests for sidegraph-export-okf (see
design/superpowers/specs/2026-07-23-okf-export-design.md): exit codes, out-dir safety,
exact-snapshot semantics, byte-determinism, OKF v0.1 conformance, link integrity, and
the canonical-store-untouched guarantee."""

import contextlib
import errno
import os
import re
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from sidegraph.cli import doctor_main, export_okf_main, verify_main, viz_main
from sidegraph.okf import build_bundle
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


def _tree(root: Path) -> dict[str, bytes]:
    """Every file under ``root`` as ``{relpath: bytes}`` (``_files`` sees only ``*.md``)."""
    return {
        str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()
    }


def _okf_leftovers(parent: Path) -> list[str]:
    return sorted(p.name for p in parent.iterdir() if ".okf-" in p.name)


def _make_writable(root: Path) -> None:
    """Give every directory under ``root`` owner rwx, so pytest can clean the tree up."""
    for dirpath, dirnames, _ in os.walk(root):
        os.chmod(dirpath, 0o755)
        for d in dirnames:
            with contextlib.suppress(OSError):
                os.chmod(os.path.join(dirpath, d), 0o755)


def _seed_previous_export(out: Path) -> None:
    """A previous sidegraph export with nested directories, for the read-only cases."""
    (out / "decisions" / "nested").mkdir(parents=True)
    (out / "facts").mkdir()
    (out / "index.md").write_text("---\ngenerator: sidegraph\n---\n", encoding="utf-8")
    (out / "decisions" / "a.md").write_text("old a\n", encoding="utf-8")
    (out / "decisions" / "nested" / "n.md").write_text("old n\n", encoding="utf-8")
    (out / "facts" / "b.md").write_text("old b\n", encoding="utf-8")


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
        assert _tree(out) == {rel: text.encode() for rel, text in build_bundle(Store(db)).items()}
        assert not _okf_leftovers(tmp_path)

    def test_refuses_a_symlinked_out_dir(self, tmp_path: Path, capsys) -> None:
        db = _seed(tmp_path)
        empty = tmp_path / "elsewhere-empty"
        empty.mkdir()
        previous = tmp_path / "elsewhere-export"
        assert _export(db, previous) == 0
        (previous / "KEEP").write_text("mine\n", encoding="utf-8")
        before = _tree(previous)
        for target in (empty, previous):
            link = tmp_path / "link"
            link.symlink_to(target, target_is_directory=True)
            assert _export(db, link) == 2
            assert "symlink" in capsys.readouterr().err
            link.unlink()
        assert _tree(empty) == {}
        assert _tree(previous) == before

    def test_refuses_a_dangling_symlink(self, tmp_path: Path, capsys) -> None:
        db = _seed(tmp_path)
        link = tmp_path / "link"
        link.symlink_to(tmp_path / "nowhere", target_is_directory=True)
        assert _export(db, link) == 2
        err = capsys.readouterr().err
        assert "symlink" in err and "File exists" not in err

    def test_failed_write_keeps_the_previous_export(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        db = _seed(tmp_path)
        out = tmp_path / "bundle"
        assert _export(db, out) == 0
        (out / "decisions" / "extra.md").write_text("old\n", encoding="utf-8")
        before = _tree(out)
        real, calls = Path.write_text, []

        def flaky(self: Path, *a: object, **kw: object) -> int:
            calls.append(self)
            if len(calls) == 2:
                raise OSError("disk full")
            return real(self, *a, **kw)  # type: ignore[arg-type]

        monkeypatch.setattr(Path, "write_text", flaky)
        assert _export(db, out) == 2
        monkeypatch.undo()
        assert "disk full" in capsys.readouterr().err
        assert _tree(out) == before
        assert not _okf_leftovers(tmp_path)

    def test_fresh_and_replaced_out_dir_modes(self, tmp_path: Path) -> None:
        db = _seed(tmp_path)
        out = tmp_path / "bundle"
        old_umask = os.umask(0o027)
        try:
            assert _export(db, out) == 0
            assert out.stat().st_mode & 0o777 == 0o750
            out.chmod(0o711)
            assert _export(db, out) == 0
            assert out.stat().st_mode & 0o777 == 0o711
        finally:
            os.umask(old_umask)

    def test_second_rename_failure_rolls_back(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        db = _seed(tmp_path)
        out = tmp_path / "bundle"
        assert _export(db, out) == 0
        (out / "decisions" / "extra.md").write_text("old\n", encoding="utf-8")
        before = _tree(out)
        real, fired = Path.rename, []

        def flaky(self: Path, dst: object) -> Path:
            if ".okf-new-" in str(self):
                fired.append(self)
                raise OSError("rename failed")
            return real(self, dst)  # type: ignore[arg-type]

        monkeypatch.setattr(Path, "rename", flaky)
        assert _export(db, out) == 2
        monkeypatch.undo()
        assert len(fired) == 1
        assert "rename failed" in capsys.readouterr().err
        assert _tree(out) == before
        assert not _okf_leftovers(tmp_path)

    def test_mount_point_out_dir_falls_back_to_in_place(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db = _seed(tmp_path)
        out = tmp_path / "bundle"
        assert _export(db, out) == 0
        (out / "decisions" / "extra.md").write_text("old\n", encoding="utf-8")
        monkeypatch.setattr(os.path, "ismount", lambda p: True)
        made: list[object] = []
        real = tempfile.mkdtemp
        monkeypatch.setattr(tempfile, "mkdtemp", lambda *a, **kw: made.append(a) or real(*a, **kw))
        assert _export(db, out) == 0
        assert not made
        assert _tree(out) == {rel: t.encode() for rel, t in build_bundle(Store(db)).items()}

    def test_out_dir_containing_cwd_falls_back_to_in_place(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db = _seed(tmp_path)
        out = tmp_path / "bundle"
        assert _export(db, out) == 0
        monkeypatch.chdir(out)
        assert _export(db, Path(".")) == 0
        assert Path(os.getcwd()).resolve() == out.resolve()
        assert (out / "index.md").is_file()

    @pytest.mark.skipif(
        hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores directory modes"
    )
    def test_unwritable_parent_falls_back_to_in_place(self, tmp_path: Path) -> None:
        db = _seed(tmp_path)
        parent = tmp_path / "ro"
        out = parent / "bundle"
        assert _export(db, out) == 0
        (out / "decisions" / "extra.md").write_text("old\n", encoding="utf-8")
        parent.chmod(0o555)
        try:
            assert _export(db, out) == 0
        finally:
            parent.chmod(0o755)
        assert _tree(out) == {rel: t.encode() for rel, t in build_bundle(Store(db)).items()}

    def test_first_rename_failure_falls_back_in_place(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db = _seed(tmp_path)
        out = tmp_path / "bundle"
        assert _export(db, out) == 0
        (out / "decisions" / "extra.md").write_text("old\n", encoding="utf-8")
        real = Path.rename

        def flaky(self: Path, dst: object) -> Path:
            if ".okf-old-" in str(dst):
                raise OSError("busy")
            return real(self, dst)  # type: ignore[arg-type]

        monkeypatch.setattr(Path, "rename", flaky)
        assert _export(db, out) == 0
        monkeypatch.undo()
        assert _tree(out) == {rel: t.encode() for rel, t in build_bundle(Store(db)).items()}
        assert not _okf_leftovers(tmp_path)

    def test_in_place_fallback_writes_to_the_validated_target(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # An ancestor symlink switched after the checks must not redirect the in-place
        # clear and write into a directory nobody validated.
        db = _seed(tmp_path)
        real_a, real_b = tmp_path / "A", tmp_path / "B"
        assert _export(db, real_b / "bundle") == 0  # B mirrors A, so a misdirected clear bites
        (real_b / "bundle" / "keep.md").write_text("foreign\n", encoding="utf-8")
        link = tmp_path / "link"
        real_a.mkdir()
        link.symlink_to(real_a)
        assert _export(db, link / "bundle") == 0
        (real_a / "bundle" / "keep.md").write_text("old\n", encoding="utf-8")

        def switch_then_in_place(target: Path) -> bool:
            link.unlink()
            link.symlink_to(real_b)
            return True

        monkeypatch.setattr("sidegraph.okf._must_emit_in_place", switch_then_in_place)
        assert _export(db, link / "bundle") == 0
        assert (real_b / "bundle" / "keep.md").read_text(encoding="utf-8") == "foreign\n"
        assert _tree(real_a / "bundle") == {
            rel: t.encode() for rel, t in build_bundle(Store(db)).items()
        }

    def test_a_regular_file_parent_keeps_the_callers_spelling(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        db = _seed(tmp_path)
        (tmp_path / "afile").write_text("x\n", encoding="utf-8")
        monkeypatch.chdir(tmp_path)
        assert export_okf_main(["--db", str(db), "--out", "afile/bundle"]) == 2
        err = capsys.readouterr().err
        assert "afile/bundle" in err and ".okf-new-" not in err
        assert not _okf_leftovers(tmp_path)

    def test_in_place_old_dir_cleanup_failure_is_a_warning(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        db = _seed(tmp_path)
        out = tmp_path / "bundle"
        assert _export(db, out) == 0
        (out / "decisions" / "extra.md").write_text("old\n", encoding="utf-8")
        real_rename, real_rmdir = Path.rename, Path.rmdir

        def flaky_rename(self: Path, dst: object) -> Path:
            if ".okf-old-" in str(dst):
                raise OSError("busy")
            return real_rename(self, dst)  # type: ignore[arg-type]

        def flaky_rmdir(self: Path) -> None:
            if ".okf-old-" in str(self):
                raise OSError("stuck")
            real_rmdir(self)

        monkeypatch.setattr(Path, "rename", flaky_rename)
        monkeypatch.setattr(Path, "rmdir", flaky_rmdir)
        assert _export(db, out) == 0
        monkeypatch.undo()
        assert "okf-old-" in capsys.readouterr().err
        assert _tree(out) == {rel: t.encode() for rel, t in build_bundle(Store(db)).items()}
        assert not [n for n in _okf_leftovers(tmp_path) if ".okf-new-" in n]

    def test_fallback_path_still_refuses_a_foreign_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        db = _seed(tmp_path)
        out = tmp_path / "precious"
        out.mkdir()
        (out / "keep.txt").write_text("mine\n", encoding="utf-8")
        monkeypatch.setattr(os.path, "ismount", lambda p: True)
        assert _export(db, out) == 2
        assert "refusing to overwrite" in capsys.readouterr().err
        assert _tree(out) == {"keep.txt": b"mine\n"}

    def test_post_swap_cleanup_failure_is_a_warning(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        db = _seed(tmp_path)
        out = tmp_path / "bundle"
        assert _export(db, out) == 0
        capsys.readouterr()
        real = shutil.rmtree

        def flaky(path: object, *a: object, **kw: object) -> None:
            if ".okf-old-" in str(path):
                raise OSError("cannot remove")
            real(path, *a, **kw)  # type: ignore[arg-type]

        monkeypatch.setattr(shutil, "rmtree", flaky)
        assert _export(db, out) == 0
        monkeypatch.undo()
        assert ".okf-old-" in capsys.readouterr().err

    def test_rename_back_failure_reports_the_preserved_export(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        db = _seed(tmp_path)
        out = tmp_path / "bundle"
        assert _export(db, out) == 0
        (out / "decisions" / "extra.md").write_text("old\n", encoding="utf-8")
        before = _tree(out)
        real = Path.rename

        def flaky(self: Path, dst: object) -> Path:
            if ".okf-new-" in str(self) or ".okf-old-" in str(self):
                raise OSError("rename failed")
            return real(self, dst)  # type: ignore[arg-type]

        monkeypatch.setattr(Path, "rename", flaky)
        assert _export(db, out) == 2
        monkeypatch.undo()
        assert "previous export is preserved at" in capsys.readouterr().err
        (old,) = [p for p in tmp_path.iterdir() if ".okf-old-" in p.name]
        assert _tree(old) == before

    def test_mode_restore_failure_is_a_warning(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        db = _seed(tmp_path)
        out = tmp_path / "bundle"
        assert _export(db, out) == 0
        out.chmod(0o711)
        capsys.readouterr()

        def denied(self: Path, *a: object, **kw: object) -> None:
            raise PermissionError("no chmod")

        monkeypatch.setattr(Path, "chmod", denied)
        monkeypatch.chdir(tmp_path)
        assert _export(db, Path("bundle")) == 0
        monkeypatch.undo()
        assert "warning: could not restore mode on bundle:" in capsys.readouterr().err

    def test_mode_read_failure_after_the_swap_still_cleans_up(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        db = _seed(tmp_path)
        out = tmp_path / "bundle"
        assert _export(db, out) == 0
        out.chmod(0o711)  # a non-default mode, so the post-swap read happens
        capsys.readouterr()
        target = str(out.resolve())
        real_rename, real_stat = Path.rename, Path.stat
        swapped: list[bool] = []

        def rename(self: Path, dst: object) -> Path:
            result = real_rename(self, dst)  # type: ignore[arg-type]
            if ".okf-new-" in str(self) and str(dst) == target:
                swapped.append(True)
            return result

        def stat(self: Path, *a: object, **kw: object) -> os.stat_result:
            if swapped and str(self) == target:
                raise OSError(errno.EIO, "Input/output error")
            return real_stat(self, *a, **kw)  # type: ignore[arg-type]

        monkeypatch.setattr(Path, "rename", rename)
        monkeypatch.setattr(Path, "stat", stat)
        code = _export(db, out)
        monkeypatch.undo()
        assert code == 0
        assert swapped
        assert "warning: could not restore mode" in capsys.readouterr().err
        assert (out / "index.md").is_file()
        assert not _okf_leftovers(tmp_path)

    def test_interrupt_at_the_first_rename_keeps_out_and_leaves_nothing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db = _seed(tmp_path)
        out = tmp_path / "bundle"
        assert _export(db, out) == 0
        (out / "decisions" / "extra.md").write_text("old\n", encoding="utf-8")
        before = _tree(out)
        real = Path.rename

        def interrupted(self: Path, dst: object) -> Path:
            result = real(self, dst)  # type: ignore[arg-type]
            if ".okf-old-" in str(dst):
                raise KeyboardInterrupt
            return result

        monkeypatch.setattr(Path, "rename", interrupted)
        with pytest.raises(KeyboardInterrupt):
            _export(db, out)
        monkeypatch.undo()
        assert _tree(out) == before
        assert not _okf_leftovers(tmp_path)

    @pytest.mark.skipif(
        hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores directory modes"
    )
    def test_errors_name_the_callers_spelling(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        db = _seed(tmp_path)
        (tmp_path / "ro").mkdir()
        (tmp_path / "ro").chmod(0o555)
        monkeypatch.chdir(tmp_path)
        try:
            assert export_okf_main(["--db", str(db), "--out", "ro/x"]) == 2
        finally:
            (tmp_path / "ro").chmod(0o755)
        err = capsys.readouterr().err
        assert "ro/x" in err and str(tmp_path) not in err

    @pytest.mark.skipif(
        hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores directory modes"
    )
    def test_a_read_only_previous_export_is_replaced_and_keeps_its_mode(
        self, tmp_path: Path
    ) -> None:
        db = _seed(tmp_path)
        out = tmp_path / "bundle"
        (out / "decisions").mkdir(parents=True)
        (out / "facts").mkdir()
        (out / "index.md").write_text("---\ngenerator: sidegraph\n---\n", encoding="utf-8")
        (out / "decisions" / "a.md").write_text("old a\n", encoding="utf-8")
        (out / "facts" / "b.md").write_text("old b\n", encoding="utf-8")
        out.chmod(0o555)
        try:
            assert _export(db, out) == 0
            assert out.stat().st_mode & 0o777 == 0o555
            assert _tree(out) == {rel: t.encode() for rel, t in build_bundle(Store(db)).items()}
            assert not _okf_leftovers(tmp_path)
        finally:
            out.chmod(0o755)

    @pytest.mark.skipif(
        hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores directory modes"
    )
    def test_a_read_only_previous_export_is_never_partly_cleared(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        db = _seed(tmp_path)
        out = tmp_path / "bundle"
        (out / "decisions").mkdir(parents=True)
        (out / "facts").mkdir()
        (out / "index.md").write_text("---\ngenerator: sidegraph\n---\n", encoding="utf-8")
        (out / "decisions" / "a.md").write_text("old a\n", encoding="utf-8")
        (out / "facts" / "b.md").write_text("old b\n", encoding="utf-8")
        before = _tree(out)
        out.chmod(0o555)
        real = Path.chmod

        def flaky(self: Path, mode: int, *a: object, **kw: object) -> None:
            if mode & 0o200 and ".okf-" not in str(self):
                raise OSError(errno.EPERM, "chmod refused")
            real(self, mode, *a, **kw)  # type: ignore[arg-type]

        real_rename = Path.rename

        def refuse_first_swap(self: Path, dst: object) -> Path:
            # APFS refuses this rename by itself; Linux needs only parent-dir write
            # permission and would let it through. Refuse it explicitly so the
            # widening-retry path runs on every platform.
            if ".okf-old-" in str(dst) and self == out:
                raise PermissionError(errno.EACCES, "rename refused")
            return real_rename(self, dst)  # type: ignore[arg-type]

        monkeypatch.setattr(Path, "chmod", flaky)
        monkeypatch.setattr(Path, "rename", refuse_first_swap)
        try:
            assert _export(db, out) == 2
        finally:
            monkeypatch.undo()
            out.chmod(0o755)
        assert "bundle" in capsys.readouterr().err
        assert _tree(out) == before
        assert not _okf_leftovers(tmp_path)

    def test_a_restrictive_umask_still_exports_to_a_fresh_path(self, tmp_path: Path) -> None:
        db = _seed(tmp_path)
        out = tmp_path / "bundle"
        old_umask = os.umask(0o222)
        try:
            assert _export(db, out) == 0
            assert out.stat().st_mode & 0o777 == 0o777 & ~0o222
        finally:
            os.umask(old_umask)
            _make_writable(tmp_path)
        assert _tree(out) == {rel: t.encode() for rel, t in build_bundle(Store(db)).items()}
        assert not _okf_leftovers(tmp_path)

    def test_a_restrictive_umask_exports_twice_to_the_same_path(self, tmp_path: Path) -> None:
        db = _seed(tmp_path)
        out = tmp_path / "bundle"
        old_umask = os.umask(0o222)
        try:
            assert _export(db, out) == 0
            assert _export(db, out) == 0
        finally:
            os.umask(old_umask)
            _make_writable(tmp_path)
        assert _tree(out) == {rel: t.encode() for rel, t in build_bundle(Store(db)).items()}
        assert not _okf_leftovers(tmp_path)

    @pytest.mark.skipif(
        hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores directory modes"
    )
    def test_a_previous_export_with_a_read_only_child_dir_is_replaced(self, tmp_path: Path) -> None:
        db = _seed(tmp_path)
        out = tmp_path / "bundle"
        _seed_previous_export(out)
        (out / "decisions" / "nested").chmod(0o555)
        try:
            assert _export(db, out) == 0
        finally:
            _make_writable(tmp_path)
        assert _tree(out) == {rel: t.encode() for rel, t in build_bundle(Store(db)).items()}
        assert not _okf_leftovers(tmp_path)

    @pytest.mark.skipif(
        hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores directory modes"
    )
    def test_a_fully_read_only_previous_export_is_replaced_and_keeps_its_mode(
        self, tmp_path: Path
    ) -> None:
        db = _seed(tmp_path)
        out = tmp_path / "bundle"
        _seed_previous_export(out)
        for dirpath, dirnames, _ in os.walk(out, topdown=False):
            for d in dirnames:
                os.chmod(os.path.join(dirpath, d), 0o555)
        out.chmod(0o555)
        try:
            assert _export(db, out) == 0
            assert out.stat().st_mode & 0o777 == 0o555
            assert _tree(out) == {rel: t.encode() for rel, t in build_bundle(Store(db)).items()}
            assert not _okf_leftovers(tmp_path)
        finally:
            _make_writable(tmp_path)

    @pytest.mark.skipif(
        hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores directory modes"
    )
    def test_a_failed_swap_restores_a_read_only_previous_export_with_its_mode(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db = _seed(tmp_path)
        out = tmp_path / "bundle"
        _seed_previous_export(out)
        before = _tree(out)
        out.chmod(0o555)
        real = Path.rename

        def flaky(self: Path, target: object) -> Path:
            if self.name == "bundle" and ".okf-new-" in str(self.parent):
                raise OSError(errno.ENOSPC, "injected: the swap fails")
            return real(self, target)  # type: ignore[arg-type]

        monkeypatch.setattr(Path, "rename", flaky)
        try:
            assert _export(db, out) != 0
        finally:
            monkeypatch.undo()
        try:
            assert out.stat().st_mode & 0o777 == 0o555
            assert _tree(out) == before
            assert not _okf_leftovers(tmp_path)
        finally:
            _make_writable(tmp_path)

    @pytest.mark.skipif(
        hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores directory modes"
    )
    def test_the_in_place_path_refuses_a_read_only_child_without_clearing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db = _seed(tmp_path)
        out = tmp_path / "bundle"
        _seed_previous_export(out)
        before = _tree(out)
        (out / "facts").chmod(0o555)  # decisions/ sorts first and is writable
        monkeypatch.setattr("sidegraph.okf._must_emit_in_place", lambda target: True)
        try:
            assert _export(db, out) == 2
        finally:
            monkeypatch.undo()
            _make_writable(tmp_path)
        assert _tree(out) == before

    @pytest.mark.skipif(
        hasattr(os, "geteuid") and os.geteuid() == 0, reason="root ignores directory modes"
    )
    def test_an_unreadable_out_dir_keeps_the_callers_spelling(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
    ) -> None:
        db = _seed(tmp_path)
        out = tmp_path / "bundle"
        assert _export(db, out) == 0
        monkeypatch.chdir(tmp_path)
        capsys.readouterr()
        out.chmod(0o000)
        try:
            assert export_okf_main(["--db", str(db), "--out", "bundle"]) == 2
        finally:
            out.chmod(0o755)
        err = capsys.readouterr().err
        assert "bundle" in err and str(tmp_path) not in err


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
