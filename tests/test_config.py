"""Shared store-path resolution (see docs/reference/configuration.md): precedence, the
SIDEGRAPH_DB legacy-dispatch rule, and the one-line deprecation notice. cli.py/server.py/
host/hooks.py all route through this module -- see the CLI-level integration checks in
test_cli_ratify.py/test_cli_sync.py and the hook-level check in test_host_stop.py for
confirmation the wiring itself is correct."""

from __future__ import annotations

import sidegraph.config as config
from sidegraph.config import DEFAULT_STORE_DIR, resolve_store_path


def _reset(monkeypatch):
    monkeypatch.setattr(config, "_deprecation_warned", False)


# -- precedence -----------------------------------------------------------------------


def test_explicit_wins_over_everything(tmp_path, monkeypatch):
    monkeypatch.setenv("SIDEGRAPH_DIR", str(tmp_path / "dir-env"))
    monkeypatch.setenv("SIDEGRAPH_DB", str(tmp_path / "db-env"))
    assert resolve_store_path("explicit-path") == "explicit-path"


def test_sidegraph_dir_wins_over_sidegraph_db(tmp_path, monkeypatch, capsys):
    _reset(monkeypatch)
    monkeypatch.setenv("SIDEGRAPH_DIR", str(tmp_path / "dir-env"))
    monkeypatch.setenv("SIDEGRAPH_DB", str(tmp_path / "db-env"))
    assert resolve_store_path() == str(tmp_path / "dir-env")
    # SIDEGRAPH_DIR winning means SIDEGRAPH_DB was never "actually used" -- no deprecation
    # note, no dispatch applied to it at all.
    assert capsys.readouterr().err == ""


def test_sidegraph_dir_used_when_sidegraph_db_unset(tmp_path, monkeypatch):
    monkeypatch.delenv("SIDEGRAPH_DB", raising=False)
    monkeypatch.setenv("SIDEGRAPH_DIR", str(tmp_path / "dir-env"))
    assert resolve_store_path() == str(tmp_path / "dir-env")


def test_existing_dot_sidegraph_preferred_over_default_when_no_env(tmp_path, monkeypatch):
    monkeypatch.delenv("SIDEGRAPH_DIR", raising=False)
    monkeypatch.delenv("SIDEGRAPH_DB", raising=False)
    (tmp_path / DEFAULT_STORE_DIR).mkdir()
    monkeypatch.chdir(tmp_path)
    assert resolve_store_path() == DEFAULT_STORE_DIR


def test_default_dot_sidegraph_when_nothing_exists(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("SIDEGRAPH_DIR", raising=False)
    monkeypatch.delenv("SIDEGRAPH_DB", raising=False)
    monkeypatch.chdir(tmp_path)
    assert resolve_store_path() == DEFAULT_STORE_DIR
    err = capsys.readouterr().err
    assert "creating new store" in err
    assert "SIDEGRAPH_DIR" in err


def test_warn_on_create_false_suppresses_the_creation_notice(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("SIDEGRAPH_DIR", raising=False)
    monkeypatch.delenv("SIDEGRAPH_DB", raising=False)
    monkeypatch.chdir(tmp_path)
    assert resolve_store_path(warn_on_create=False) == DEFAULT_STORE_DIR
    assert capsys.readouterr().err == ""


# -- SIDEGRAPH_DB legacy-dispatch rule (pinned from review) ----------------------------


def test_sidegraph_db_existing_legacy_file_passes_through_unchanged(tmp_path, monkeypatch):
    """A real legacy single-file store -- Store migrates it on open, so the resolver must
    hand it back verbatim, never redirected to its parent."""
    _reset(monkeypatch)
    legacy = tmp_path / "sidegraph.db"
    legacy.write_text("not a real sqlite file, but existence is all resolution checks")
    monkeypatch.delenv("SIDEGRAPH_DIR", raising=False)
    monkeypatch.setenv("SIDEGRAPH_DB", str(legacy))
    assert resolve_store_path() == str(legacy)


def test_sidegraph_db_existing_directory_used_directly_never_its_parent(tmp_path, monkeypatch):
    _reset(monkeypatch)
    existing_dir = tmp_path / "some-store-dir"
    existing_dir.mkdir()
    monkeypatch.delenv("SIDEGRAPH_DIR", raising=False)
    monkeypatch.setenv("SIDEGRAPH_DB", str(existing_dir))
    assert resolve_store_path() == str(existing_dir)


def test_sidegraph_db_existing_canonical_layout_dir_used_directly(tmp_path, monkeypatch):
    _reset(monkeypatch)
    existing_dir = tmp_path / ".sidegraph"
    existing_dir.mkdir()
    (existing_dir / "format").write_text("sidegraph-store 0.4.0\n")
    (existing_dir / "decisions").mkdir()
    monkeypatch.delenv("SIDEGRAPH_DIR", raising=False)
    monkeypatch.setenv("SIDEGRAPH_DB", str(existing_dir))
    assert resolve_store_path() == str(existing_dir)


def test_sidegraph_db_nonexistent_decisions_db_rescued_to_parent_dir(tmp_path, monkeypatch):
    """THE regression trap (pinned from review): every old config snippet in the wild sets
    SIDEGRAPH_DB=.sidegraph/decisions.db. That path never exists on disk under the
    git-native store (there is no such file) -- resolution must rescue it to the PARENT
    (".sidegraph"), never create a fresh canonical layout literally named "decisions.db"."""
    _reset(monkeypatch)
    monkeypatch.delenv("SIDEGRAPH_DIR", raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SIDEGRAPH_DB", ".sidegraph/decisions.db")
    resolved = resolve_store_path()
    assert resolved == ".sidegraph"


def test_sidegraph_db_nonexistent_bare_filename_falls_through_to_default_store_dir(
    tmp_path, monkeypatch
):
    """Review finding (Important 1): a BARE filename (no directory component at all --
    the historic SIDEGRAPH_DB=sidegraph.db default) must NOT rescue to ".", the current
    directory itself. That would make the entire cwd the store: canonical layout,
    .gitignore, and the root tmp-sweep all landing directly in whatever directory the
    process happens to run from (observed in practice: it clobbered a user's own
    build-artifact.tmp sitting at repo root). A bare filename has no real "directory it
    lives in" to rescue to, so it falls through to DEFAULT_STORE_DIR instead, exactly like
    the plain "nothing resolved at all" case."""
    _reset(monkeypatch)
    monkeypatch.delenv("SIDEGRAPH_DIR", raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SIDEGRAPH_DB", "sidegraph.db")
    assert resolve_store_path() == ".sidegraph"


def test_sidegraph_db_bare_filename_dispatch_actually_used_by_store_open_leaves_cwd_clean(
    tmp_path, monkeypatch
):
    """End-to-end proof: opening a Store at the resolved path creates the canonical layout
    under ".sidegraph", and a foreign *.tmp file already sitting at the repo root (the
    exact artifact the pre-fix bug clobbered) survives untouched."""
    from sidegraph.store import Store

    _reset(monkeypatch)
    monkeypatch.delenv("SIDEGRAPH_DIR", raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SIDEGRAPH_DB", "sidegraph.db")

    foreign_tmp = tmp_path / "build-artifact.tmp"
    foreign_tmp.write_text("not ours")

    resolved = resolve_store_path()
    Store(resolved)

    assert (tmp_path / ".sidegraph" / "format").is_file()
    assert not (tmp_path / "decisions").exists()  # nothing created directly at cwd
    assert not (tmp_path / "format").exists()
    assert foreign_tmp.exists() and foreign_tmp.read_text() == "not ours"


def test_sidegraph_db_dispatch_actually_used_by_store_open(tmp_path, monkeypatch):
    """End-to-end proof the rescue is real, not just a string computation: opening a Store
    at the resolved path creates the canonical layout in ``.sidegraph``, and NOT a
    directory literally named ``decisions.db``."""
    from sidegraph.store import Store

    _reset(monkeypatch)
    monkeypatch.delenv("SIDEGRAPH_DIR", raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SIDEGRAPH_DB", ".sidegraph/decisions.db")
    resolved = resolve_store_path()
    Store(resolved)
    assert (tmp_path / ".sidegraph" / "format").is_file()
    assert (tmp_path / ".sidegraph" / "decisions").is_dir()
    assert not (tmp_path / ".sidegraph" / "decisions.db").exists()


# -- root anchoring (host/hooks.py's CLAUDE_PROJECT_DIR use case) ----------------------


def test_root_anchors_relative_sidegraph_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("SIDEGRAPH_DIR", "sub/store")
    assert resolve_store_path(root=str(tmp_path)) == str(tmp_path / "sub" / "store")


def test_root_leaves_absolute_sidegraph_dir_untouched(tmp_path, monkeypatch):
    absolute = str(tmp_path / "abs-store")
    monkeypatch.setenv("SIDEGRAPH_DIR", absolute)
    assert resolve_store_path(root=str(tmp_path / "unrelated")) == absolute


def test_root_anchors_the_default_when_nothing_set(tmp_path, monkeypatch):
    monkeypatch.delenv("SIDEGRAPH_DIR", raising=False)
    monkeypatch.delenv("SIDEGRAPH_DB", raising=False)
    assert resolve_store_path(root=str(tmp_path), warn_on_create=False) == str(
        tmp_path / DEFAULT_STORE_DIR
    )


def test_root_anchors_before_sidegraph_db_dispatch(tmp_path, monkeypatch):
    """Anchoring must happen BEFORE the existence checks the dispatch rule relies on --
    otherwise a relative SIDEGRAPH_DB would be checked against the wrong (cwd) directory."""
    _reset(monkeypatch)
    monkeypatch.delenv("SIDEGRAPH_DIR", raising=False)
    monkeypatch.setenv("SIDEGRAPH_DB", "sub/decisions.db")
    resolved = resolve_store_path(root=str(tmp_path))
    assert resolved == str(tmp_path / "sub")


# -- deprecation notice: exactly once per process ---------------------------------------


def test_deprecation_notice_emitted_once_across_repeated_calls(tmp_path, monkeypatch, capsys):
    _reset(monkeypatch)
    monkeypatch.delenv("SIDEGRAPH_DIR", raising=False)
    monkeypatch.setenv("SIDEGRAPH_DB", str(tmp_path / "s.db"))

    resolve_store_path()
    first_err = capsys.readouterr().err
    assert "SIDEGRAPH_DB is deprecated" in first_err

    resolve_store_path()
    resolve_store_path()
    second_err = capsys.readouterr().err
    assert second_err == ""  # not repeated


def test_deprecation_notice_not_emitted_when_sidegraph_db_unused(tmp_path, monkeypatch, capsys):
    _reset(monkeypatch)
    monkeypatch.delenv("SIDEGRAPH_DIR", raising=False)
    monkeypatch.delenv("SIDEGRAPH_DB", raising=False)
    monkeypatch.chdir(tmp_path)
    resolve_store_path(warn_on_create=False)
    assert capsys.readouterr().err == ""
