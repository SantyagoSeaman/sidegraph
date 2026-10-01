"""A symlinked directory or store file INSIDE the store is refused at open (see
design/superpowers/specs/2026-09-29-store-symlinks-and-bootstrap-guards-design.md D1).
A symlinked store ROOT stays allowed (the guard below)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from sidegraph.schema import Decision, DecisionKind, Provenance
from sidegraph.store import (
    _ARCHIVE_SUBDIR,
    _CANONICAL_SUBDIRS,
    Store,
)

_SUBDIRS = (*_CANONICAL_SUBDIRS, _ARCHIVE_SUBDIR)
_TOP_LEVEL_FILES = ("format", "stamping_live_since", "index.db", ".gitignore")
_SIDECARS = ("index.db-journal", "index.db-wal", "index.db-shm")


def _decision() -> Decision:
    return Decision(
        title="a decision",
        kind=DecisionKind.ADR,
        context="c",
        choice="ch",
        valid_from=datetime.now(UTC),
        provenance=Provenance(source="manual"),
    )


@pytest.mark.parametrize("sub", _SUBDIRS)
def test_store_refuses_symlinked_subdir(tmp_path, sub):
    root = tmp_path / ".sidegraph"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / sub).symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="is a symlink"):
        Store(root)
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize("live", [True, False], ids=["live", "dangling"])
@pytest.mark.parametrize("name", _TOP_LEVEL_FILES)
def test_store_refuses_symlinked_top_level_file(tmp_path, name, live):
    # A fresh Store writes the marker text a valid `format` link must point at.
    with Store(tmp_path / "template") as fresh:
        marker_text = (fresh.path / "format").read_text(encoding="utf-8")
    root = tmp_path / ".sidegraph"
    root.mkdir()
    target = tmp_path / "elsewhere" / name
    if live:
        target.parent.mkdir()
        target.write_text(marker_text if name == "format" else "", encoding="utf-8")
    (root / name).symlink_to(target)
    with pytest.raises(ValueError, match="is a symlink"):
        Store(root)


@pytest.mark.parametrize("name", _SIDECARS)
def test_store_refuses_dangling_sqlite_sidecar_symlink(tmp_path, name):
    root = tmp_path / ".sidegraph"
    root.mkdir()
    (root / name).symlink_to(tmp_path / "nowhere" / name)
    with pytest.raises(ValueError, match="is a symlink"):
        Store(root)


def test_store_root_symlink_is_allowed(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / ".sidegraph"
    link.symlink_to(real, target_is_directory=True)
    with Store(link) as store:
        decision = store.add_decision(_decision())
    assert (real / "decisions" / f"{decision.id}.json").is_file()
