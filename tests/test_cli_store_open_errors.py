"""A store that exists but cannot be opened is an operational error for sidegraph-viz and
sidegraph-export-okf: one ``store not readable (<path>): <error>`` line on stderr, nothing on
stdout (``--json`` included), exit 2, nothing written.
See design note: docs/reference/cli.md, sections sidegraph-viz and sidegraph-export-okf."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from sidegraph.cli import export_okf_main, viz_main
from sidegraph.store import Store


def _broken_store(tmp_path: Path, kind: str) -> tuple[Path, str]:
    """A healthy store, then broken one way. Returns (store dir, expected message fragment)."""
    db = tmp_path / "proj" / ".sidegraph"
    Store(db).close()
    if kind == "corrupt-index":
        for side in db.glob("index.db-*"):
            side.unlink()
        (db / "index.db").write_bytes(b"not a sqlite database " * 64)
        return db, "file is not a database"
    if kind == "foreign-major":
        (db / "format").write_text("sidegraph-store 9.0.0\n", encoding="utf-8")
        return db, "major '9'"
    if kind == "requirement-above":
        (db / "format").write_text(
            "sidegraph-store 1.0.0\nrequires-sidegraph >= 99.0.0\n", encoding="utf-8"
        )
        return db, "requires Sidegraph >= 99.0.0"
    raise AssertionError(kind)


KINDS = ["corrupt-index", "foreign-major", "requirement-above"]


def _viz_json(db: Path, tmp_path: Path) -> int:
    return viz_main(["--db", str(db), "--out", str(tmp_path / "g"), "--json"])


def _viz_html(db: Path, tmp_path: Path) -> int:
    return viz_main(["--db", str(db), "--out", str(tmp_path / "g")])


def _okf(db: Path, tmp_path: Path) -> int:
    return export_okf_main(["--db", str(db), "--out", str(tmp_path / "bundle")])


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("cmd", [_viz_json, _viz_html, _okf], ids=["viz-json", "viz", "okf"])
def test_unopenable_store_is_one_line_exit_2(cmd, kind, tmp_path, capsys) -> None:
    db, fragment = _broken_store(tmp_path, kind)
    rc = cmd(db, tmp_path)
    out, err = capsys.readouterr()
    assert rc == 2, (rc, out, err)
    assert out == ""
    lines = err.splitlines()
    assert len(lines) == 1, err
    assert lines[0].startswith(f"store not readable ({' '.join(str(db).split())}): "), err
    assert fragment in lines[0], err
    # Guard, red against nothing: unfixed code also raised before writing.
    assert not (tmp_path / "g.html").exists()
    assert not (tmp_path / "g.json").exists()
    assert not (tmp_path / "bundle").exists()


@pytest.mark.parametrize("cmd", [_viz_json, _viz_html, _okf], ids=["viz-json", "viz", "okf"])
def test_multiline_open_error_is_one_line(cmd, tmp_path, capsys, monkeypatch) -> None:
    db = tmp_path / "proj" / ".sidegraph"
    Store(db).close()

    def fail_open(path):
        assert Path(path) == db
        raise ValueError("line one\nline two")

    monkeypatch.setattr("sidegraph.cli.Store", fail_open)
    assert cmd(db, tmp_path) == 2
    out, err = capsys.readouterr()
    assert out == ""
    display_path = " ".join(str(db).split())
    assert err == f"store not readable ({display_path}): line one line two\n"
    assert not (tmp_path / "g.html").exists()
    assert not (tmp_path / "g.json").exists()
    assert not (tmp_path / "bundle").exists()


@pytest.mark.skipif(os.name == "nt", reason="Newlines are invalid in Windows filenames")
@pytest.mark.parametrize("cmd", [_viz_json, _viz_html, _okf], ids=["viz-json", "viz", "okf"])
def test_multiline_store_path_is_displayed_on_one_line(cmd, tmp_path, capsys) -> None:
    db = tmp_path / "project\nname" / ".sidegraph"
    Store(db).close()
    (db / "format").write_text("sidegraph-store 9.0.0\n", encoding="utf-8")
    assert cmd(db, tmp_path) == 2
    out, err = capsys.readouterr()
    assert out == ""
    assert len(err.splitlines()) == 1
    assert err.startswith(f"store not readable ({' '.join(str(db).split())}): ")
    assert "major '9'" in err
    assert not (tmp_path / "g.html").exists()
    assert not (tmp_path / "g.json").exists()
    assert not (tmp_path / "bundle").exists()


@pytest.mark.parametrize(
    ("cmd", "builder"),
    [(_viz_json, "build_graph"), (_viz_html, "build_graph"), (_okf, "build_bundle")],
    ids=["viz-json", "viz", "okf"],
)
def test_builder_errors_propagate(cmd, builder, tmp_path, capsys, monkeypatch) -> None:
    """A wider constructor handler must not relabel builder bugs as unreadable stores."""
    db = tmp_path / "proj" / ".sidegraph"
    Store(db).close()
    error = RuntimeError("builder failed")

    def fail_build(store, **kwargs):
        assert store.path == db
        store.close()
        raise error

    monkeypatch.setattr(f"sidegraph.cli.{builder}", fail_build)
    with pytest.raises(RuntimeError) as caught:
        cmd(db, tmp_path)
    assert caught.value is error
    assert capsys.readouterr() == ("", "")
    assert not (tmp_path / "g.html").exists()
    assert not (tmp_path / "g.json").exists()
    assert not (tmp_path / "bundle").exists()
