"""The project-root ``.ignore`` helper adds the store to it, or does nothing, and never
disturbs the bytes that were already there.

# see design/superpowers/specs/2026-11-07-root-ignore-for-store-design.md (D2)
"""

from __future__ import annotations

import errno
import os
import shutil
import subprocess
from pathlib import Path

import pytest

BLOCK = (
    "# Sidegraph: keeps recursive search (rg, editor search) out of the decision store.\n"
    "# Read memory through the Sidegraph MCP; without it: sidegraph-export-adr --json, "
    "sidegraph-export-okf --out <dir>, sidegraph-doctor.\n"
    "# Explicit paths still search it: rg PATTERN .sidegraph/\n"
    ".sidegraph/\n"
)


def _helper():
    from sidegraph import search_ignore

    return search_ignore


def _project(tmp_path: Path) -> Path:
    (tmp_path / ".sidegraph").mkdir(parents=True, exist_ok=True)
    return tmp_path


def _run(project: Path):
    return _helper().ensure_search_ignore(project, project / ".sidegraph")


def test_creates_the_block_in_a_fresh_project(tmp_path):
    """Red target: init without the helper leaves no file and `rg --hidden` hits the store."""
    project = _project(tmp_path)
    result = _run(project)
    assert result.outcome == "created"
    assert result.line == ".sidegraph/"
    assert (project / ".ignore").read_text() == BLOCK


@pytest.mark.parametrize(
    "old",
    [b"node_modules/\n", b"node_modules/", b"a\r\nb\r\n", b"zeta/\nalpha/\n"],
)
def test_appends_after_existing_lines_byte_for_byte(tmp_path, old):
    """Red target: a rewrite-and-sort implementation, or a missing separator."""
    project = _project(tmp_path)
    (project / ".ignore").write_bytes(old)
    result = _run(project)
    assert result.outcome == "appended"
    content = (project / ".ignore").read_bytes()
    assert content.startswith(old)
    added = content[len(old) :]
    crlf = b"\r\n" in old
    eol = b"\r\n" if crlf else b"\n"
    if not old.endswith(b"\n"):
        assert added.startswith(eol)
        added = added[len(eol) :]
    assert added == BLOCK.replace("\n", eol.decode()).encode()
    assert content.count(b".sidegraph/") == 2  # the comment path and the pattern


@pytest.mark.parametrize(
    "line",
    [
        ".sidegraph",
        ".sidegraph/",
        "/.sidegraph",
        "/.sidegraph/",
        ".sidegraph/**",
        "!.sidegraph/",
        ".sidegraph/  ",
        ".sidegraph/\t",
    ],
)
def test_a_file_that_names_the_store_is_left_alone(tmp_path, line):
    """Red target: an implementation that appends after a `!` line."""
    project = _project(tmp_path)
    target = project / ".ignore"
    target.write_bytes(f"keep/\n{line}\n".encode())
    before = target.read_bytes(), target.stat().st_mtime_ns
    result = _run(project)
    assert result.outcome == "already_present"
    assert (target.read_bytes(), target.stat().st_mtime_ns) == before


def test_comments_that_mention_the_store_do_not_count(tmp_path):
    """Red target: a substring match that treats a comment as naming the store."""
    project = _project(tmp_path)
    (project / ".ignore").write_text("# .sidegraph/\n")
    assert _run(project).outcome == "appended"


def test_second_call_adds_nothing(tmp_path):
    """Red target: a non-idempotent append."""
    project = _project(tmp_path)
    _run(project)
    first = (project / ".ignore").read_bytes()
    assert _run(project).outcome == "already_present"
    assert (project / ".ignore").read_bytes() == first


def _unsafe(tmp_path, kind):
    project = _project(tmp_path / "project")
    target = project / ".ignore"
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"outside\n")
    if kind == "symlink":
        try:
            target.symlink_to(outside)
        except OSError:
            pytest.skip("symlinks are not available")
        return project, outside
    if kind == "directory":
        target.mkdir()
        return project, None
    if kind == "hardlink":
        target.write_bytes(b"x/\n")
        try:
            os.link(target, tmp_path / "second-link")
        except OSError:
            pytest.skip("hard links are not available")
        return project, target
    if kind == "large":
        target.write_bytes(b"x" * (65 * 1024))
        return project, target
    target.write_bytes(b"\xff\xfe")
    return project, target


@pytest.mark.parametrize("kind", ["symlink", "directory", "hardlink", "large", "binary"])
def test_unsafe_files_are_left_untouched(tmp_path, kind):
    """Red target: a version without the O_NOFOLLOW and file-kind checks."""
    (tmp_path / "project").mkdir()
    project, watched = _unsafe(tmp_path, kind)
    before = watched.read_bytes() if watched else None
    result = _run(project)
    assert result.outcome == "left_untouched"
    assert result.reason
    if watched:
        assert watched.read_bytes() == before


def test_store_patterns(tmp_path):
    """Red target: a version that always writes `.sidegraph/`."""
    helper = _helper()
    assert helper.store_pattern(tmp_path, tmp_path / ".sidegraph") == ".sidegraph/"
    assert helper.store_pattern(tmp_path, tmp_path / "memory") == "/memory/"
    assert helper.store_pattern(tmp_path, tmp_path / "a" / "b") == "/a/b/"
    outside = tmp_path.parent / "elsewhere-store"
    assert helper.store_pattern(tmp_path, outside) is None
    result = helper.ensure_search_ignore(tmp_path, outside)
    assert result.outcome == "outside_project"
    assert not (tmp_path / ".ignore").exists()


def test_a_store_through_a_symlinked_ancestor_stays_inside_the_project(tmp_path):
    """Red target: a lexical ancestor comparison."""
    project = tmp_path / "project"
    (project / "real").mkdir(parents=True)
    try:
        (project / "link").symlink_to(project / "real")
    except OSError:
        pytest.skip("symlinks are not available")
    assert _helper().store_pattern(project, project / "link" / ".sidegraph") == "/real/.sidegraph/"


def test_never_raises(tmp_path, monkeypatch):
    """Red target: a helper that lets an OSError reach init."""
    project = _project(tmp_path)

    def deny(*args, **kwargs):
        raise PermissionError(errno.EACCES, "denied")

    monkeypatch.setattr(os, "open", deny)
    result = _run(project)
    assert result.outcome == "left_untouched"
    assert result.reason == "EACCES"


@pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep is not installed")
def test_recursive_rg_skips_the_store(tmp_path):
    """Red target: an unfixed init, where `rg --hidden` hits the store."""
    project = _project(tmp_path)
    (project / "src").mkdir()
    (project / "src" / "a.txt").write_text("SENTINEL\n")
    (project / ".sidegraph" / "x.shard").write_text("SENTINEL\n")
    assert _run(project).outcome == "created"

    def rg(*args):
        return subprocess.run(
            ["rg", "-n", *args], cwd=project, capture_output=True, text=True, check=False
        ).stdout

    recursive = rg("--hidden", "SENTINEL", ".")
    assert "src/a.txt" in recursive and ".sidegraph" not in recursive
    assert "x.shard" in rg("SENTINEL", ".sidegraph")


def test_missing_nofollow_uses_the_portable_path(tmp_path, monkeypatch):
    """Red target: a helper that fails or writes through a symlink where O_NOFOLLOW is missing."""
    monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)
    fresh = _project(tmp_path / "fresh")
    assert _run(fresh).outcome == "created"
    existing = _project(tmp_path / "existing")
    (existing / ".ignore").write_bytes(b"node_modules/\n")
    assert _run(existing).outcome == "appended"
    linked = _project(tmp_path / "linked")
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"outside\n")
    try:
        (linked / ".ignore").symlink_to(outside)
    except OSError:
        pytest.skip("symlinks are not available")
    assert _run(linked).outcome == "left_untouched"
    assert outside.read_bytes() == b"outside\n"


@pytest.mark.parametrize(
    "line",
    [
        ".sidegraph",
        ".sidegraph/",
        "!.sidegraph/",
        "**/.sidegraph/",
        "!**/.sidegraph/",
        "**/.sidegraph/**",
        "/**/.sidegraph/",
        "!/**/.sidegraph/",
    ],
)
def test_an_unanchored_line_names_a_nested_default_store(tmp_path, line):
    """Red target: a root-level `.sidegraph/` or `!.sidegraph/` line (it applies at every depth)
    is not recognised for a store at `pkg/.sidegraph`, so the helper appends over the choice."""
    (tmp_path / "pkg" / ".sidegraph").mkdir(parents=True)
    target = tmp_path / ".ignore"
    target.write_bytes(f"keep/\n{line}\n".encode())
    before = target.read_bytes()
    result = _helper().ensure_search_ignore(tmp_path, tmp_path / "pkg" / ".sidegraph")
    assert result.outcome == "already_present"
    assert target.read_bytes() == before


@pytest.mark.parametrize("line", ["/.sidegraph/", ".sidegraph/**", "/.sidegraph/**"])
def test_an_anchored_line_for_the_root_store_does_not_name_a_nested_one(tmp_path, line):
    """Red target for `.sidegraph/**`: an inner slash anchors the rule to the ignore file's
    directory, so it does not hide `pkg/.sidegraph` (`rg` lists the nested store's files)."""
    (tmp_path / "pkg" / ".sidegraph").mkdir(parents=True)
    (tmp_path / ".ignore").write_bytes(f"{line}\n".encode())
    result = _helper().ensure_search_ignore(tmp_path, tmp_path / "pkg" / ".sidegraph")
    assert result.outcome == "appended"
    assert result.line == "/pkg/.sidegraph/"


def test_glob_characters_in_a_custom_store_path_are_escaped(tmp_path):
    """Red target: `/memory[1]/` is a character class that matches `memory1`, not the store."""
    helper = _helper()
    assert helper.store_pattern(tmp_path, tmp_path / "memory[1]") == r"/memory\[1\]/"
    assert helper.store_pattern(tmp_path, tmp_path / "a*b?c") == r"/a\*b\?c/"
    if os.name != "nt":  # on Windows a backslash is a separator, never part of a name
        assert helper.store_pattern(tmp_path, tmp_path / "x\\y") == r"/x\\y/"
    result = helper.ensure_search_ignore(tmp_path, tmp_path / "memory[1]")
    assert result.outcome == "created"
    assert (tmp_path / ".ignore").read_text().endswith("\n" + r"/memory\[1\]/" + "\n")
    assert (
        helper.ensure_search_ignore(tmp_path, tmp_path / "memory[1]").outcome == "already_present"
    )


def test_a_store_path_with_a_line_break_is_left_untouched(tmp_path):
    """Red target: a newline in the path would write extra ignore rules."""
    store = tmp_path / "a\nb"
    try:
        store.mkdir()
    except OSError:
        pytest.skip("the filesystem refuses a newline in a name")
    result = _helper().ensure_search_ignore(tmp_path, store)
    assert result.outcome == "left_untouched"
    assert not (tmp_path / ".ignore").exists()


def test_an_unescaped_glob_line_does_not_name_a_custom_store(tmp_path):
    """Red target: `/memory[1]/` is a class that matches `memory1`, not the store `memory[1]`, but
    the raw text was accepted as already naming it, so init reported the store covered."""
    (tmp_path / "memory[1]").mkdir()
    (tmp_path / ".ignore").write_bytes(b"/memory[1]/\n")
    result = _helper().ensure_search_ignore(tmp_path, tmp_path / "memory[1]")
    assert result.outcome == "appended"
    assert result.line == r"/memory\[1\]/"
    (tmp_path / ".ignore").write_bytes(b"/memory\\[1\\]/\n")
    assert (
        _helper().ensure_search_ignore(tmp_path, tmp_path / "memory[1]").outcome
        == "already_present"
    )


@pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep is not installed")
def test_an_escaped_custom_store_hides_only_itself(tmp_path):
    """Red target: the unescaped class hid a sibling directory and showed the store."""
    (tmp_path / "memory[1]").mkdir()
    (tmp_path / "memory1").mkdir()
    (tmp_path / "memory[1]" / "row.json").write_text("SENTINEL\n")
    (tmp_path / "memory1" / "code.py").write_text("SENTINEL\n")
    assert _helper().ensure_search_ignore(tmp_path, tmp_path / "memory[1]").outcome == "created"
    out = subprocess.run(
        ["rg", "-l", "--hidden", "SENTINEL", "."],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    assert "memory1/code.py" in out and "row.json" not in out


@pytest.mark.parametrize("swap", ["hardlink", "other_file", "symlink"])
def test_a_file_swapped_before_it_is_opened_is_not_written(tmp_path, monkeypatch, swap):
    """Red target: the read and the append used two descriptors tied only by an inode number
    (a freed number is reused on ext4), and a symlink swapped in before the read, where
    O_NOFOLLOW is missing, was read and appended through. One descriptor, compared with the
    `lstat` of the path, is written or the file is left alone."""
    if swap == "symlink":
        monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)
    project = _project(tmp_path / "project")
    target = project / ".ignore"
    target.write_bytes(b"node_modules/\n")
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"outside\n")
    # built beside the original, so the old file still exists and the inode numbers differ
    staged = project / "staged"
    try:
        if swap == "hardlink":
            os.link(outside, staged)
        elif swap == "symlink":
            staged.symlink_to(outside)
        else:
            staged.write_bytes(b"replacement/\n")
    except OSError:
        pytest.skip("links are not available")
    real_open = os.open
    swapped = []

    def swapping_open(path, flags, *args, **kwargs):
        if Path(path) == target and not swapped:
            swapped.append(True)
            os.replace(staged, target)
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", swapping_open)
    result = _run(project)
    assert swapped
    assert result.outcome == "left_untouched"
    assert outside.read_bytes() == b"outside\n"
    assert target.read_bytes() in (b"outside\n", b"replacement/\n")


@pytest.mark.parametrize("line", ["  .sidegraph/", " .sidegraph/  ", "\t.sidegraph/"])
def test_an_indented_line_does_not_name_the_store(tmp_path, line):
    """Red target: leading whitespace is part of an ignore pattern (`rg` then matches a directory
    named `  .sidegraph`, and the store stays searchable), but the line was trimmed on both sides
    and taken as naming the store."""
    project = _project(tmp_path)
    (project / ".ignore").write_bytes(f"{line}\n".encode())
    result = _run(project)
    assert result.outcome == "appended"
    assert result.line == ".sidegraph/"


@pytest.mark.skipif(
    os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="needs permission bits that bind the current user",
)
@pytest.mark.parametrize("names_the_store", [True, False])
def test_a_read_only_ignore_is_read_but_never_written(tmp_path, names_the_store):
    """Red target: opening the file read-write made a mode-0444 `.ignore` that already names the
    store come back `left_untouched (EACCES)`, and init told the person to add a line that is
    there. A file that does not name the store still cannot be appended to."""
    project = _project(tmp_path)
    target = project / ".ignore"
    target.write_bytes(b"keep/\n.sidegraph/\n" if names_the_store else b"keep/\n")
    target.chmod(0o444)
    before = target.read_bytes()
    result = _run(project)
    if names_the_store:
        assert result.outcome == "already_present"
        assert result.line == ".sidegraph/"
    else:
        assert (result.outcome, result.reason) == ("left_untouched", "EACCES")
    assert target.read_bytes() == before


@pytest.mark.parametrize("change", ["hardlink", "moved_and_replaced", "grown"])
def test_a_file_that_changes_while_it_is_read_is_not_written(tmp_path, monkeypatch, change):
    """Red target: the descriptor was checked before the read and never again, so a hard link
    added, or the file moved away and replaced, during the read was still appended to."""
    project = _project(tmp_path / "project")
    target = project / ".ignore"
    target.write_bytes(b"node_modules/\n")
    outside = tmp_path / "outside.txt"
    real_read = os.read
    changed = []

    def changing_read(fd, size):
        data = real_read(fd, size)
        if not data and not changed:  # the last read, so a growth is not read
            changed.append(True)
            try:
                if change == "hardlink":
                    os.link(target, outside)
                elif change == "moved_and_replaced":
                    os.rename(target, outside)
                    target.write_bytes(b"replacement/\n")
                else:
                    with open(target, "ab") as handle:
                        handle.write(b"more/\n")
            except OSError:
                pytest.skip("links are not available")
        return data

    monkeypatch.setattr(os, "read", changing_read)
    result = _run(project)
    assert changed
    assert result.outcome == "left_untouched"
    if outside.exists():
        assert b"Sidegraph" not in outside.read_bytes()
    assert b"Sidegraph" not in target.read_bytes()
