"""``text_paths`` and ``brief_files``: the repo files a subagent's brief names (T2b, T9).

The Agent branch of the PreToolUse hook decides from the text of a brief alone which files the
subagent is about to work on, so the extractor is a table of cases rather than a single property.
Each case lists the files it must return, relative to the root, in order of appearance. The cases
are the shapes a brief names a file in (backticks, quotes, a markdown link, ``path:line``), the
shapes that must name nothing (a URL, a version string, ``e.g.``, a path outside the root, a
symlink out of it), and the two rules that need an anchored set: the unique-suffix fallback and
the one-hop read of a named document. Every path is synthetic.

see design/superpowers/specs/2026-10-03-records-in-subagent-briefs-design.md (D2, T2b, T9)
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from sidegraph.text_paths import HOP_BYTES, brief_files, text_paths

ROOT = "@ROOT@"
OUTSIDE = "@OUTSIDE@"

# Files that exist under the root; the anchored set the suffix fallback may match against.
FILES = {
    "src/a.py": "x = 1\n",
    "src/pkg/sub/x.py": "x = 1\n",
    "src/ledger.py": "x = 1\n",
    "docs/plan.md": "Step 1: change `src/ledger.py`.\n",
    "README.md": "readme\n",
    "sub/x.py": "x = 1\n",
    ".github/workflows/ci.yml": "on: push\n",
}

# name, text, files named (root-relative, in order of appearance)
CASES: list[tuple[str, str, list[str]]] = [
    # -- how a path is quoted ----------------------------------------------------------------
    ("bare", "Fix src/a.py now", ["src/a.py"]),
    ("backticks", "Edit `src/a.py`.", ["src/a.py"]),
    ("double-quotes", 'Edit "src/a.py" please', ["src/a.py"]),
    ("single-quotes", "Edit 'src/a.py' please", ["src/a.py"]),
    ("markdown-link", "See [the plan](docs/plan.md) first", ["docs/plan.md"]),
    ("markdown-link-with-code-text", "[`src/a.py`](src/a.py)", ["src/a.py"]),
    ("markdown-link-with-title", '[plan](docs/plan.md "The plan")', ["docs/plan.md"]),
    ("bare-name-with-extension", "Read README.md", ["README.md"]),
    ("dot-slash", "Fix ./src/a.py", ["src/a.py"]),
    ("dot-directory", "Fix .github/workflows/ci.yml.", [".github/workflows/ci.yml"]),
    # -- punctuation around a path -----------------------------------------------------------
    ("leading-paren", "(src/a.py)", ["src/a.py"]),
    ("leading-asterisks", "**src/a.py**", ["src/a.py"]),
    ("angle-brackets", "<src/a.py>", ["src/a.py"]),
    ("trailing-full-stop", "Fix src/a.py.", ["src/a.py"]),
    ("trailing-comma", "Fix src/a.py, then src/ledger.py;", ["src/a.py", "src/ledger.py"]),
    ("trailing-colon", "In src/a.py: change it", ["src/a.py"]),
    ("trailing-paren-and-full-stop", "(see src/a.py).", ["src/a.py"]),
    ("trailing-question-mark", "Is it src/a.py?", ["src/a.py"]),
    ("path-and-line", "src/a.py:42 and src/ledger.py:7:3", ["src/a.py", "src/ledger.py"]),
    ("path-and-anchor-line", "src/a.py#L10-L20", ["src/a.py"]),
    # -- order and repeats -------------------------------------------------------------------
    (
        "order-of-appearance-and-repeats",
        "src/pkg/sub/x.py then src/a.py then src/pkg/sub/x.py",
        ["src/pkg/sub/x.py", "src/a.py"],
    ),
    # -- absolute paths ----------------------------------------------------------------------
    ("absolute-inside-the-root", f"Fix {ROOT}/src/a.py", ["src/a.py"]),
    ("absolute-outside-the-root", f"Fix {OUTSIDE}/secret.py", []),
    ("absolute-system-file", "See /etc/hosts", []),
    ("climbs-out-of-the-root", "See ../outside/secret.py", []),
    ("climbs-out-and-back-in", "See src/../../outside/secret.py", []),
    ("home-relative", "See ~/src/a.py", []),
    # -- symlinks ----------------------------------------------------------------------------
    ("symlink-out-of-the-root", "Fix src/out.py", []),
    ("symlink-inside-the-root", "Fix src/alias.py", ["src/a.py"]),
    # -- what a path is not ------------------------------------------------------------------
    ("directory", "Look in src/ and src and src/pkg", []),
    ("missing-file", "Fix src/missing.py", []),
    ("url", "See https://example.com/src/a.py and http://x.org/docs/plan.md", []),
    ("url-with-port-and-query", "See https://example.com:8080/a/b.py?x=1.", []),
    ("version-strings", "Bump v1.2.3 to 1.2.4 and 2.0", []),
    ("abbreviations", "e.g. the ledger, i.e. the store, etc. and vs. the rest", []),
    ("slash-separated-prose", "read/write and/or input/output", []),
    ("no-token-at-all", "Refactor the payments module", []),
    ("empty", "", []),
    ("very-long-token", "x/" * 5000 + "y.py", []),
]


def _make(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "repo"
    outside = tmp_path / "outside"
    for rel, body in FILES.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(body)
    outside.mkdir()
    (outside / "secret.py").write_text("x = 1\n")
    (root / "src" / "out.py").symlink_to(outside / "secret.py")
    (root / "src" / "alias.py").symlink_to(root / "src" / "a.py")
    return root, outside


@pytest.mark.parametrize(("text", "expected"), [c[1:] for c in CASES], ids=[c[0] for c in CASES])
def test_t9_text_paths_table(tmp_path: Path, text: str, expected: list[str]) -> None:
    """Every case of the table. Red while ``text_paths`` does not exist."""
    root, outside = _make(tmp_path)
    text = text.replace(ROOT, str(root)).replace(OUTSIDE, str(outside))
    assert text_paths(text, str(root), set(FILES)) == expected


def test_t9_a_root_reached_through_a_symlink_still_resolves(tmp_path: Path) -> None:
    """The root and the token are both ``realpath``-resolved, so a workspace reached through a
    symlink (macOS ``/tmp``) names the same files as the real directory does."""
    root, _ = _make(tmp_path)
    link = tmp_path / "linked-root"
    link.symlink_to(root)
    assert text_paths(f"Fix src/a.py and {link}/src/ledger.py", str(link), set()) == [
        "src/a.py",
        "src/ledger.py",
    ]


# -- T2b: the suffix fallback -----------------------------------------------------------------


def _tree(tmp_path: Path, *rels: str) -> Path:
    root = tmp_path / "repo"
    for rel in rels:
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text("x = 1\n")
    return root


def test_t2b_a_token_relative_to_a_subdirectory_matches_the_one_anchored_file(tmp_path) -> None:
    root = _tree(tmp_path, "pkg/sub/x.py")
    assert text_paths("Fix sub/x.py", str(root), {"pkg/sub/x.py"}) == ["pkg/sub/x.py"]


def test_t2b_an_ambiguous_suffix_names_nothing(tmp_path) -> None:
    """Two anchored files end in ``/sub/x.py``: neither is used (mutation M5 takes the first)."""
    root = _tree(tmp_path, "pkg/sub/x.py", "other/sub/x.py")
    anchored = {"pkg/sub/x.py", "other/sub/x.py"}
    assert text_paths("Fix sub/x.py", str(root), anchored) == []


def test_t2b_a_token_that_climbs_never_takes_the_fallback(tmp_path) -> None:
    root = _tree(tmp_path, "pkg/sibling/sub/x.py")
    anchored = {"pkg/sibling/sub/x.py"}
    assert text_paths("Fix ../sibling/sub/x.py", str(root), anchored) == []
    assert text_paths("Fix sibling/sub/x.py", str(root), anchored) == ["pkg/sibling/sub/x.py"]


def test_t2b_an_exact_file_wins_over_a_suffix_match(tmp_path) -> None:
    root = _tree(tmp_path, "sub/x.py", "pkg/sub/x.py")
    assert text_paths("Fix sub/x.py", str(root), {"pkg/sub/x.py"}) == ["sub/x.py"]


def test_t2b_the_suffix_is_whole_path_segments(tmp_path) -> None:
    root = _tree(tmp_path, "pkg/sub/x.py")
    assert text_paths("Fix ub/x.py", str(root), {"pkg/sub/x.py"}) == []


def test_t2b_a_bare_file_name_never_takes_the_fallback(tmp_path) -> None:
    root = _tree(tmp_path, "pkg/x.py")
    assert text_paths("Fix x.py", str(root), {"pkg/x.py"}) == []


def test_t2b_an_absolute_token_never_takes_the_fallback(tmp_path) -> None:
    root = _tree(tmp_path, "pkg/sub/x.py")
    assert (
        text_paths("Fix /sub/x.py and /elsewhere/pkg/sub/x.py", str(root), {"pkg/sub/x.py"}) == []
    )


def test_t2b_a_dot_slash_token_takes_the_fallback_without_its_dot_slash(tmp_path) -> None:
    root = _tree(tmp_path, "pkg/sub/x.py")
    assert text_paths("Fix ./sub/x.py", str(root), {"pkg/sub/x.py"}) == ["pkg/sub/x.py"]


def test_t2b_an_anchored_file_that_is_gone_from_disk_still_matches(tmp_path) -> None:
    """The fallback reads the store's paths, not the disk: a deleted file's records still name
    it, and the brief that names it should hear them."""
    root = _tree(tmp_path, "README.md")
    assert text_paths("Fix sub/x.py", str(root), {"pkg/sub/x.py"}) == ["pkg/sub/x.py"]


# -- brief_files: brief first, then one hop ---------------------------------------------------


def _write(root: Path, rel: str, body: str) -> None:
    (root / rel).parent.mkdir(parents=True, exist_ok=True)
    (root / rel).write_text(body)


def test_the_hop_reads_a_named_document_and_labels_its_files(tmp_path) -> None:
    root = _tree(tmp_path, "src/payments.py", "src/ledger.py")
    _write(root, "docs/plan.md", "Step 1: change `src/ledger.py`.\n")
    found = brief_files(
        "Do docs/plan.md, then src/payments.py.", str(root), {"src/payments.py", "src/ledger.py"}
    )
    assert found == [
        (None, "docs/plan.md"),
        (None, "src/payments.py"),
        ("docs/plan.md", "src/ledger.py"),
    ]


def test_a_file_the_brief_names_keeps_its_own_label_when_a_document_names_it_too(tmp_path) -> None:
    root = _tree(tmp_path, "src/ledger.py")
    _write(root, "docs/plan.md", "Change src/ledger.py.\n")
    found = brief_files("Do docs/plan.md and src/ledger.py", str(root), set())
    assert found == [(None, "docs/plan.md"), (None, "src/ledger.py")]


def test_there_is_one_hop_and_no_more(tmp_path) -> None:
    root = _tree(tmp_path, "src/c.py")
    _write(root, "docs/a.md", "Then read docs/b.md.\n")
    _write(root, "docs/b.md", "Change src/c.py.\n")
    assert brief_files("Do docs/a.md", str(root), set()) == [
        (None, "docs/a.md"),
        ("docs/a.md", "docs/b.md"),
    ]


def test_only_text_documents_are_read(tmp_path) -> None:
    """``.md``, ``.txt`` and ``.rst`` are hopped; a source file naming another path is not."""
    root = _tree(tmp_path, "src/b.py")
    _write(root, "src/a.py", "# touches src/b.py\n")
    _write(root, "notes.txt", "Change src/b.py\n")
    _write(root, "notes.rst", "Change src/b.py\n")
    assert brief_files("See src/a.py", str(root), set()) == [(None, "src/a.py")]
    assert brief_files("See notes.txt", str(root), set()) == [
        (None, "notes.txt"),
        ("notes.txt", "src/b.py"),
    ]
    assert brief_files("See notes.rst", str(root), set()) == [
        (None, "notes.rst"),
        ("notes.rst", "src/b.py"),
    ]


def test_hop_tokens_resolve_from_the_root_and_take_the_suffix_fallback(tmp_path) -> None:
    root = _tree(tmp_path, "pkg/sub/x.py")
    _write(root, "docs/plan.md", "Change sub/x.py and ../elsewhere.py.\n")
    found = brief_files("Do docs/plan.md", str(root), {"pkg/sub/x.py"})
    assert found == [(None, "docs/plan.md"), ("docs/plan.md", "pkg/sub/x.py")]


def test_t9_the_hop_reads_at_most_sixty_four_kilobytes(tmp_path) -> None:
    """A path after the first 64 KB of a document is not seen, and one inside it is."""
    assert HOP_BYTES == 64 * 1024
    root = _tree(tmp_path, "src/early.py", "src/late.py")
    padding = "word " * (HOP_BYTES // 5 + 100)
    _write(root, "docs/big.md", f"Change src/early.py.\n{padding}\nThen src/late.py.\n")
    assert len((root / "docs/big.md").read_bytes()) > HOP_BYTES
    assert brief_files("Do docs/big.md", str(root), set()) == [
        (None, "docs/big.md"),
        ("docs/big.md", "src/early.py"),
    ]


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads a file whatever its mode")
def test_a_document_that_cannot_be_read_names_nothing(tmp_path) -> None:
    root = _tree(tmp_path, "src/a.py")
    _write(root, "docs/plan.md", "Change src/a.py.\n")
    os.chmod(root / "docs/plan.md", 0)
    try:
        found = brief_files("Do docs/plan.md", str(root), set())
    finally:
        os.chmod(root / "docs/plan.md", 0o644)
    assert found == [(None, "docs/plan.md")]


def test_a_document_that_is_not_utf8_is_still_read(tmp_path) -> None:
    root = _tree(tmp_path, "src/a.py")
    (root / "docs").mkdir()
    (root / "docs/plan.md").write_bytes(b"\xff\xfe Change src/a.py.\n")
    assert brief_files("Do docs/plan.md", str(root), set()) == [
        (None, "docs/plan.md"),
        ("docs/plan.md", "src/a.py"),
    ]


# -- containment: a hop reads only a regular file inside the root (task review) ----------------


def test_a_suffix_matched_document_that_is_a_symlink_out_of_the_root_is_not_read(tmp_path) -> None:
    """The suffix fallback names a path from the store, and nothing checked it against the disk:
    a ``plan.md`` that links out of the root was read and its files followed. The document is
    still named (records may be anchored to it); its contents are not."""
    root = _tree(tmp_path, "src/a.py")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.md").write_text("Change `src/a.py`.\n")
    (root / "pkg/docs").mkdir(parents=True)
    (root / "pkg/docs/plan.md").symlink_to(outside / "secret.md")
    found = brief_files("Do docs/plan.md", str(root), {"pkg/docs/plan.md", "src/a.py"})
    assert found == [(None, "pkg/docs/plan.md")]


def test_a_suffix_matched_document_that_is_a_fifo_does_not_hang_the_hop(tmp_path) -> None:
    """``open()`` on a FIFO with no writer blocks for ever. The hop runs in a subprocess so that a
    regression fails by timeout instead of hanging the suite; ``subprocess.run`` kills and reaps
    the child when the timeout expires, so none is left behind."""
    root = _tree(tmp_path, "src/a.py")
    (root / "pkg/docs").mkdir(parents=True)
    os.mkfifo(root / "pkg/docs/plan.md")
    code = (
        "import sys\n"
        "from sidegraph.text_paths import brief_files\n"
        "print(brief_files('Do docs/plan.md', sys.argv[1], {'pkg/docs/plan.md', 'src/a.py'}))\n"
    )
    try:
        done = subprocess.run(
            [sys.executable, "-c", code, str(root)], capture_output=True, text=True, timeout=5
        )
    except subprocess.TimeoutExpired:
        pytest.fail("the hop blocked on a FIFO")
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "[(None, 'pkg/docs/plan.md')]"


# -- project-instruction files are shown but never followed --------------------------------------


@pytest.mark.parametrize(
    "name",
    ["CLAUDE.md", "AGENTS.md", "CLAUDE.public.md", "AGENTS.public.md", "sub/CLAUDE.md"],
)
def test_no_hop_goes_through_a_project_instruction_file(tmp_path, name) -> None:
    """Every brief names ``CLAUDE.md`` first, and it names ``pyproject.toml`` and ``CHANGELOG.md``
    and the like: following it fills the block with files no task is about. The file itself is
    still named, so records anchored to it are still shown."""
    root = _tree(tmp_path, "src/x.py")
    _write(root, name, "Layout: `src/x.py`.\n")
    assert brief_files(f"Read {name} and do it", str(root), {"src/x.py"}) == [(None, name)]


def test_a_document_that_only_looks_like_an_instruction_file_is_still_followed(tmp_path) -> None:
    root = _tree(tmp_path, "src/x.py")
    _write(root, "docs/CLAUDE.md.bak.md", "Layout: `src/x.py`.\n")
    _write(root, "MY-CLAUDE.md", "Layout: `src/x.py`.\n")
    assert brief_files("Read MY-CLAUDE.md", str(root), set()) == [
        (None, "MY-CLAUDE.md"),
        ("MY-CLAUDE.md", "src/x.py"),
    ]


# -- the payload cwd: a launch from a subdirectory ----------------------------------------------


def test_a_brief_from_a_subdirectory_launch_follows_a_document_under_that_subdirectory(
    tmp_path,
) -> None:
    """The parent works in ``sub``, so its brief names ``docs/plan.md`` for ``sub/docs/plan.md``;
    neither the root nor the suffix fallback (the plan is not anchored) finds it."""
    root = _tree(tmp_path, "sub/src/b.py")
    _write(root, "sub/docs/plan.md", "Change `src/b.py`.\n")
    found = brief_files(
        "Implement docs/plan.md", str(root), {"sub/src/b.py"}, cwd=str(root / "sub")
    )
    assert found == [(None, "sub/docs/plan.md"), ("sub/docs/plan.md", "sub/src/b.py")]


def test_the_cwd_wins_over_an_unanchored_file_of_the_same_name_at_the_root(tmp_path) -> None:
    """``src/a.py`` from ``sub`` is ``sub/src/a.py``, whatever ``src/a.py`` at the root is."""
    root = _tree(tmp_path, "src/a.py", "sub/src/a.py")
    found = brief_files("Fix src/a.py", str(root), {"sub/src/a.py"}, cwd=str(root / "sub"))
    assert found == [(None, "sub/src/a.py")]


def test_a_token_the_cwd_does_not_hold_resolves_from_the_root(tmp_path) -> None:
    root = _tree(tmp_path, "src/a.py", "sub/x.py")
    found = brief_files("Fix src/a.py", str(root), set(), cwd=str(root / "sub"))
    assert found == [(None, "src/a.py")]


@pytest.mark.parametrize("where", ["root", "outside", "relative", "missing"])
def test_a_cwd_that_adds_nothing_changes_nothing(tmp_path, where) -> None:
    root = _tree(tmp_path, "src/a.py")
    cwd = {
        "root": str(root),
        "outside": str(tmp_path),
        "relative": "sub",
        "missing": str(root / "no/such/dir"),
    }[where]
    assert brief_files("Fix src/a.py", str(root), set(), cwd=cwd) == [(None, "src/a.py")]


def test_a_cwd_cannot_reach_a_file_outside_the_root(tmp_path) -> None:
    root = _tree(tmp_path, "sub/x.py")
    (tmp_path / "outside").mkdir()
    (tmp_path / "outside/secret.py").write_text("x = 1\n")
    found = brief_files("Fix ../../outside/secret.py", str(root), set(), cwd=str(root / "sub"))
    assert found == []


# -- repeated tokens are resolved once -----------------------------------------------------------


def test_a_megabyte_of_one_repeated_path_is_resolved_in_a_blink(tmp_path) -> None:
    """Each token costs a ``realpath``; a brief that pastes a log repeats one path thousands of
    times (2.2 s for 1 MB before the tokens were deduplicated)."""
    root = _tree(tmp_path, "src/a.py")
    prompt = "src/a.py " * (1024 * 1024 // 9)
    assert len(prompt) >= 1024 * 1024 - 9
    started = time.perf_counter()
    found = brief_files(prompt, str(root), set())
    assert time.perf_counter() - started < 0.5
    assert found == [(None, "src/a.py")]
