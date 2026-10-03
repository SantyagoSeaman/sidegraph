"""The seed ladder reads a wrong seed the way the agent meant it, or says it cannot.

``seed_ladder.tolerate`` turns the seeds an agent passed into ``exact`` ones (resolved as given),
``guessed`` ones (rewritten, never silently), an ``unresolved`` list of file spellings, and one
note per rewrite or refusal. These tests drive it over synthetic graphs, with a real directory
for the repository root where the on-disk guard needs one. Names are neutral: nothing here comes
from a real corpus.
# see design/superpowers/specs/2026-10-02-tolerant-seeds-design.md (D1, tests A1-A10, A17)
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from sidegraph.engine.reader import GraphifyReader
from sidegraph.retrieval import Seed


def _ladder():
    """The module under test, imported at call time so each test fails on its own."""
    from sidegraph import seed_ladder

    return seed_ladder


def make_reader(
    tmp_path: Path,
    files: list[str],
    extra: list[dict] | None = None,
    links: list[dict] | None = None,
) -> GraphifyReader:
    """One code node per file (``fn_<i>()``), plus ``extra`` nodes and ``links``."""
    nodes = [
        {
            "id": f"f{i}",
            "label": f"fn_{i}()",
            "norm_label": f"fn_{i}()",
            "file_type": "code",
            "source_file": f,
            "community": 1,
        }
        for i, f in enumerate(files)
    ]
    for n in extra or []:
        nodes.append(
            {
                "id": n["id"],
                "label": n["label"],
                "norm_label": n["label"].lower(),
                "file_type": n.get("file_type", "code"),
                "source_file": n["file"],
                "community": n.get("community", 1),
            }
        )
    path = tmp_path / "graph.json"
    path.write_text(json.dumps({"built_at_commit": "x", "nodes": nodes, "links": links or []}))
    return GraphifyReader(path)


@pytest.fixture
def root(tmp_path: Path) -> Path:
    r = (tmp_path / "repo").resolve()
    r.mkdir()
    return r


def case_insensitive_fs(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    """Make ``Path.is_file`` under ``root`` ignore letter case, as macOS and Windows do, so a
    test of the case rules means the same on any host."""
    real = Path.is_file

    def is_file(self: Path, *args, **kwargs) -> bool:
        try:
            parts = self.relative_to(root).parts
        except ValueError:
            return real(self, *args, **kwargs)
        cur = root
        for part in parts:
            match = next((n for n in os.listdir(cur) if n.lower() == part.lower()), None)
            if match is None:
                return False
            cur = cur / match
        return real(cur)

    monkeypatch.setattr(Path, "is_file", is_file)


def _one_note(ladder):
    (note,) = ladder.notes
    return note


# -- A1: normalisation -------------------------------------------------------------------


@pytest.mark.parametrize(
    "spelling",
    [
        "./pkg/m.py",
        "././pkg/m.py",
        "pkg//m.py",
        "pkg/../pkg/m.py",
        "pkg/m.py:12",
        "pkg/m.py:12:3",
        "  pkg/m.py\n",
        "ABS",
    ],
)
def test_a1_each_normalisation_reads_as_the_file_in_the_graph(tmp_path, root, spelling):
    reader = make_reader(tmp_path, ["pkg/m.py", "pkg/other.py"])
    given = str(root / "pkg" / "m.py") if spelling == "ABS" else spelling

    ladder = _ladder().tolerate([Seed(file_path=given)], reader, root)

    assert ladder.exact == []
    assert ladder.guessed == [Seed(file_path="pkg/m.py")]
    assert ladder.unresolved == []
    note = _one_note(ladder)
    assert (note.given, note.outcome, note.read_as) == (given, "normalised", ("pkg/m.py",))


def test_a1_an_absolute_path_that_goes_through_a_symlink_is_made_relative(tmp_path, root):
    link = tmp_path / "link"
    link.symlink_to(root)
    reader = make_reader(tmp_path, ["pkg/m.py"])

    ladder = _ladder().tolerate([Seed(file_path=str(link / "pkg" / "m.py"))], reader, root)

    assert ladder.guessed == [Seed(file_path="pkg/m.py")]
    assert _one_note(ladder).outcome == "normalised"


# -- A2: suffix and basename ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("given", "outcome"),
    [
        ("app/ui/views/Library.swift", "suffix"),  # an invented prefix
        ("ui/views/Library.swift", "suffix"),  # a missing prefix
        ("app/Library.swift", "basename"),  # nothing but the file name matches
        ("Library.swift", "basename"),
    ],
)
def test_a2_an_unknown_path_reads_as_the_only_file_it_can_mean(tmp_path, root, given, outcome):
    reader = make_reader(tmp_path, ["src/ui/views/Library.swift", "src/ui/Other.swift"])

    ladder = _ladder().tolerate([Seed(file_path=given)], reader, root)

    assert ladder.guessed == [Seed(file_path="src/ui/views/Library.swift")]
    note = _one_note(ladder)
    assert (note.outcome, note.read_as) == (outcome, ("src/ui/views/Library.swift",))
    assert ladder.exact == [] and ladder.unresolved == []


def test_a2_a_file_the_graph_holds_is_exact_and_leaves_no_note(tmp_path, root):
    reader = make_reader(tmp_path, ["pkg/m.py"])

    ladder = _ladder().tolerate([Seed(file_path="pkg/m.py")], reader, root)

    assert ladder.exact == [Seed(file_path="pkg/m.py")]
    assert ladder.guessed == [] and ladder.notes == [] and ladder.unresolved == []


# -- A3, A4: ambiguity is reported, never guessed -------------------------------------------


def test_a3_two_files_with_one_name_are_ambiguous_and_nothing_is_read(tmp_path, root):
    reader = make_reader(tmp_path, ["a/View.swift", "b/View.swift", "c/Other.swift"])

    ladder = _ladder().tolerate([Seed(file_path="View.swift")], reader, root)

    assert ladder.exact == [] and ladder.guessed == []
    note = _one_note(ladder)
    assert note.outcome == "ambiguous"
    assert note.read_as == ("a/View.swift", "b/View.swift")
    assert note.total == 2
    assert ladder.unresolved == []  # the note already explains the seed


def test_a3_only_five_candidates_are_listed(tmp_path, root):
    reader = make_reader(tmp_path, [f"d{i}/View.swift" for i in range(9)])

    note = _one_note(_ladder().tolerate([Seed(file_path="View.swift")], reader, root))

    assert note.total == 9
    assert note.read_as == tuple(f"d{i}/View.swift" for i in range(5))


def test_a4_the_total_is_the_count_at_the_tail_that_was_ambiguous(tmp_path, root):
    """``x/View.swift`` matches two files; the shorter tail ``View.swift`` would match three.
    The answer stops at the first tail that matches anything."""
    reader = make_reader(tmp_path, ["a/x/View.swift", "b/x/View.swift", "c/y/View.swift"])

    note = _one_note(_ladder().tolerate([Seed(file_path="app/x/View.swift")], reader, root))

    assert note.outcome == "ambiguous"
    assert note.total == 2
    assert note.read_as == ("a/x/View.swift", "b/x/View.swift")


def test_a4_a_longer_tail_that_is_unique_wins_over_a_shorter_ambiguous_one(tmp_path, root):
    reader = make_reader(tmp_path, ["a/x/View.swift", "b/y/View.swift"])

    ladder = _ladder().tolerate([Seed(file_path="app/x/View.swift")], reader, root)

    assert ladder.guessed == [Seed(file_path="a/x/View.swift")]
    assert _one_note(ladder).outcome == "suffix"


# -- A5: directories ---------------------------------------------------------------------


def test_a5_a_small_directory_reads_the_eight_shallowest_files(tmp_path, root):
    files = [f"pkg/a{i}.py" for i in range(6)] + [f"pkg/sub/b{i}.py" for i in range(6)]
    reader = make_reader(tmp_path, [*files, "other/z.py"])

    ladder = _ladder().tolerate([Seed(file_path="pkg/")], reader, root)

    expected = [f"pkg/a{i}.py" for i in range(6)] + ["pkg/sub/b0.py", "pkg/sub/b1.py"]
    assert ladder.guessed == [Seed(file_path=f) for f in expected]
    note = _one_note(ladder)
    assert note.outcome == "directory"
    assert note.read_as == tuple(expected)
    assert note.total == 12


def test_a5_a_directory_without_a_trailing_slash_is_a_directory_too(tmp_path, root):
    reader = make_reader(tmp_path, ["pkg/a.py", "pkg/b.py"])

    ladder = _ladder().tolerate([Seed(file_path="pkg")], reader, root)

    assert [s.file_path for s in ladder.guessed] == ["pkg/a.py", "pkg/b.py"]
    assert _one_note(ladder).outcome == "directory"


def test_a5_a_large_directory_is_listed_not_read(tmp_path, root):
    files = (
        [f"big/core/f{i}.py" for i in range(10)]
        + [f"big/ui/f{i}.py" for i in range(8)]
        + [f"big/a/f{i}.py" for i in range(3)]
        + [f"big/b/f{i}.py" for i in range(3)]
        + [f"big/c/f{i}.py" for i in range(3)]
        + [f"big/d/f{i}.py" for i in range(2)]
        + ["big/top.py"]
    )
    assert len(files) == 30
    reader = make_reader(tmp_path, files)

    ladder = _ladder().tolerate([Seed(file_path="big/")], reader, root)

    assert ladder.guessed == [] and ladder.exact == [] and ladder.unresolved == []
    note = _one_note(ladder)
    assert note.outcome == "directory-too-large"
    assert note.total == 30
    assert note.read_as == ("big/core 10", "big/ui 8", "big/a 3", "big/b 3", "big/c 3")


def test_a5_the_gate_is_three_times_the_cap(tmp_path, root):
    at_gate = make_reader(tmp_path, [f"d/f{i:02d}.py" for i in range(24)])
    over = make_reader(tmp_path, [f"d/f{i:02d}.py" for i in range(25)])

    small = _one_note(_ladder().tolerate([Seed(file_path="d/")], at_gate, root))
    large = _one_note(_ladder().tolerate([Seed(file_path="d/")], over, root))

    assert (small.outcome, small.total, len(small.read_as)) == ("directory", 24, 8)
    assert (large.outcome, large.total) == ("directory-too-large", 25)


def test_a5_a_trailing_slash_means_a_directory_and_skips_the_file_rungs(tmp_path, root):
    reader = make_reader(tmp_path, ["pkg/m.py"])

    as_dir = _ladder().tolerate([Seed(file_path="m.py/")], reader, root)
    as_file = _ladder().tolerate([Seed(file_path="m.py")], reader, root)

    assert as_dir.guessed == [] and as_dir.notes == [] and as_dir.unresolved == ["m.py"]
    assert as_file.guessed == [Seed(file_path="pkg/m.py")]


# -- A6: the file exists on disk but not in the graph ------------------------------------------


def test_a6_a_file_on_disk_is_never_guessed_to_be_another_file(tmp_path, root):
    """``pkg/n.py`` exists but the graph lacks it; the graph holds a different ``n.py``. The
    seed is the file on disk, not the one with the same name."""
    (root / "pkg").mkdir()
    (root / "pkg" / "n.py").write_text("x = 1\n")
    reader = make_reader(tmp_path, ["other/n.py", "pkg/m.py"])

    ladder = _ladder().tolerate([Seed(file_path="pkg/n.py")], reader, root)

    assert ladder.exact == [] and ladder.guessed == [] and ladder.notes == []
    assert ladder.unresolved == ["pkg/n.py"]


def test_a6_the_guard_reports_the_normalised_spelling(tmp_path, root):
    (root / "pkg").mkdir()
    (root / "pkg" / "n.py").write_text("x = 1\n")
    reader = make_reader(tmp_path, ["other/n.py"])

    ladder = _ladder().tolerate([Seed(file_path="./pkg//n.py:3")], reader, root)

    assert ladder.guessed == [] and ladder.unresolved == ["pkg/n.py"]


def test_a6_a_graph_outside_git_has_no_root_and_no_guard(tmp_path):
    reader = make_reader(tmp_path, ["other/n.py"])

    ladder = _ladder().tolerate([Seed(file_path="pkg/n.py")], reader, None)

    assert ladder.guessed == [Seed(file_path="other/n.py")]


# -- A7: outside the repository ------------------------------------------------------------


@pytest.mark.parametrize("given", ["../x.py", "a/../../x.py", ".", "./", "..", "/etc/other/x.py"])
def test_a7_a_path_outside_the_root_is_unresolved_under_its_own_spelling(tmp_path, root, given):
    reader = make_reader(tmp_path, ["x.py"])  # the name exists, which is the trap

    ladder = _ladder().tolerate([Seed(file_path=given)], reader, root)

    assert ladder.exact == [] and ladder.guessed == [] and ladder.notes == []
    assert ladder.unresolved == [given]


def test_a7_an_absolute_path_with_no_root_stays_unresolved(tmp_path):
    reader = make_reader(tmp_path, ["x.py"])

    ladder = _ladder().tolerate([Seed(file_path="/somewhere/x.py")], reader, None)

    assert ladder.guessed == [] and ladder.unresolved == ["/somewhere/x.py"]


def test_a7_a_path_that_names_nothing_is_unresolved_in_its_normalised_spelling(tmp_path, root):
    reader = make_reader(tmp_path, ["pkg/m.py"])

    ladder = _ladder().tolerate([Seed(file_path="./nope/Missing.py")], reader, root)

    assert ladder.guessed == [] and ladder.notes == []
    assert ladder.unresolved == ["nope/Missing.py"]


# -- letter case, a root behind a symlink, a file named like a location ---------------------


def test_a_path_in_the_wrong_letter_case_reads_as_the_file_the_graph_holds(
    tmp_path, root, monkeypatch
):
    """On a case-insensitive filesystem ``Scripts/x.sh`` "exists" because ``scripts/x.sh``
    does; the on-disk guard must not take that for a different file the graph lacks."""
    (root / "scripts").mkdir()
    (root / "scripts" / "x.sh").write_text("echo\n")
    case_insensitive_fs(monkeypatch, root)
    reader = make_reader(tmp_path, ["scripts/x.sh", "pkg/m.py"])

    ladder = _ladder().tolerate([Seed(file_path="Scripts/x.sh")], reader, root)

    assert ladder.guessed == [Seed(file_path="scripts/x.sh")]
    assert ladder.unresolved == [] and ladder.exact == []
    note = _one_note(ladder)
    assert (note.outcome, note.read_as) == ("case", ("scripts/x.sh",))
    assert _ladder().read_block([note]).splitlines()[1] == (
        "- `Scripts/x.sh` → read as `scripts/x.sh` (guessed: different letter case)"
    )


def test_a_wrong_case_with_no_root_reads_the_same(tmp_path):
    reader = make_reader(tmp_path, ["scripts/x.sh"])

    ladder = _ladder().tolerate([Seed(file_path="SCRIPTS/x.sh")], reader, None)

    assert ladder.guessed == [Seed(file_path="scripts/x.sh")]
    assert _one_note(ladder).outcome == "case"


def test_two_files_that_differ_only_by_case_are_ambiguous(tmp_path, root):
    reader = make_reader(tmp_path, ["scripts/x.sh", "Scripts/x.sh"])

    ladder = _ladder().tolerate([Seed(file_path="SCRIPTS/x.sh")], reader, root)

    assert ladder.guessed == []
    note = _one_note(ladder)
    assert (note.outcome, note.total) == ("ambiguous", 2)
    assert note.read_as == ("Scripts/x.sh", "scripts/x.sh")


def test_a_file_that_exists_with_exactly_that_case_is_not_swapped_for_a_namesake(tmp_path, root):
    """``Scripts/x.sh`` is a file on disk the graph lacks, and the graph holds ``scripts/x.sh``:
    two files on a case-sensitive filesystem. The guard comes before the case rule."""
    (root / "Scripts").mkdir()
    (root / "Scripts" / "x.sh").write_text("echo\n")
    reader = make_reader(tmp_path, ["scripts/x.sh"])

    ladder = _ladder().tolerate([Seed(file_path="Scripts/x.sh")], reader, root)

    assert ladder.guessed == [] and ladder.notes == []
    assert ladder.unresolved == ["Scripts/x.sh"]


def test_is_file_exact_compares_every_component_with_its_case(tmp_path, root, monkeypatch):
    (root / "scripts").mkdir()
    (root / "scripts" / "x.sh").write_text("echo\n")
    case_insensitive_fs(monkeypatch, root)
    exact = _ladder().is_file_exact

    assert exact(root, "scripts/x.sh")
    assert not exact(root, "Scripts/x.sh")
    assert not exact(root, "scripts/X.sh")
    assert not exact(root, "scripts")  # a directory
    assert not exact(root, "scripts/missing.sh")


def test_an_entity_file_in_the_wrong_case_is_rewritten_too(tmp_path, root):
    reader = _entity_reader(tmp_path)
    seed = Seed(name="Cache", file_path="SRC/Cache.py")

    ladder = _ladder().tolerate([seed], reader, root)

    assert ladder.exact == [seed]
    assert ladder.guessed == [Seed(name="Cache", file_path="src/cache.py")]
    assert _one_note(ladder).outcome == "case"


def test_an_absolute_path_under_a_root_that_is_a_symlink_is_made_relative(tmp_path):
    real = tmp_path / "real"
    (real / "pkg").mkdir(parents=True)
    link = tmp_path / "link"
    link.symlink_to(real)
    reader = make_reader(tmp_path, ["pkg/m.py"])

    ladder = _ladder().tolerate([Seed(file_path=str(real / "pkg" / "m.py"))], reader, link)

    assert ladder.guessed == [Seed(file_path="pkg/m.py")]
    assert _one_note(ladder).outcome == "normalised"


def test_a_file_named_like_a_location_is_read_before_the_location_is_stripped(tmp_path, root):
    """``docs/notes:12`` is a file; ``./docs/notes:12`` is it with a stray ``./``, not line 12
    of ``docs/notes``."""
    reader = make_reader(tmp_path, ["docs/notes:12", "docs/notes"])

    literal = _ladder().tolerate([Seed(file_path="./docs/notes:12")], reader, root)
    located = _ladder().tolerate([Seed(file_path="docs/notes:12:3")], reader, root)

    assert literal.guessed == [Seed(file_path="docs/notes:12")]
    assert located.guessed == [Seed(file_path="docs/notes")]  # a location after all


# -- A8: entity seeds ----------------------------------------------------------------------


def _entity_reader(tmp_path: Path) -> GraphifyReader:
    return make_reader(
        tmp_path,
        ["src/cache.py", "src/store.py"],
        extra=[
            {"id": "cache", "label": "Cache", "file": "src/cache.py"},
            {"id": "load", "label": ".load()", "file": "src/cache.py"},
            {"id": "fresh", "label": "Fresh", "file": "src/store.py"},
            {"id": "run1", "label": "run()", "file": "src/cache.py"},
            {"id": "run2", "label": "run()", "file": "src/cache.py"},
            {"id": "dup_a", "label": "Dup", "file": "src/cache.py"},
            {"id": "dup_b", "label": "Dup", "file": "src/store.py"},
            {"id": "msg", "label": ".message()", "file": "src/store.py"},
            {"id": "twin", "label": "Message", "file": "src/store.py"},
            {"id": "store", "label": "Store", "file": "src/store.py"},
        ],
        links=[
            {"source": "cache", "target": "load", "relation": "method"},
            {"source": "store", "target": "msg", "relation": "method"},
        ],
    )


def test_a8_a_wrong_file_path_resolves_through_the_file_rungs(tmp_path, root):
    reader = _entity_reader(tmp_path)
    seed = Seed(name="Cache", file_path="app/src/cache.py")

    ladder = _ladder().tolerate([seed], reader, root)

    # The original stays an exact seed (a stored descriptor under it still matches, at the
    # tier it always had); only the rewrite is guessed.
    assert ladder.exact == [seed]
    assert ladder.guessed == [Seed(name="Cache", file_path="src/cache.py")]
    note = _one_note(ladder)
    assert (note.outcome, note.name, note.read_as) == ("suffix", "Cache", ("src/cache.py",))
    assert note.given == "Cache (app/src/cache.py)"
    assert ladder.unresolved == []


def test_a8_a_normalised_entity_file_keeps_its_original_in_exact(tmp_path, root):
    reader = _entity_reader(tmp_path)
    seed = Seed(name="Cache", file_path="./src/cache.py")

    ladder = _ladder().tolerate([seed], reader, root)

    assert ladder.exact == [seed]
    assert ladder.guessed == [Seed(name="Cache", file_path="src/cache.py")]
    assert _one_note(ladder).outcome == "normalised"


def test_a8_a_file_rung_hit_where_the_name_is_absent_does_not_count(tmp_path, root):
    """The unique file matches, but the entity is not in it: not a rewrite. The seed stays as
    given, and its file path is reported as the not-in-graph block reports it."""
    reader = _entity_reader(tmp_path)
    seed = Seed(name="Nope", file_path="app/src/cache.py")

    ladder = _ladder().tolerate([seed], reader, root)

    assert ladder.exact == [seed] and ladder.guessed == [] and ladder.notes == []
    assert ladder.unresolved == ["app/src/cache.py"]


def test_a8_a_plain_name_with_a_wrong_file_is_tried_by_name_alone(tmp_path, root):
    """The file tolerance finds nothing; the name is unique in the graph, so the seed reads as
    that symbol, said to be a guess."""
    reader = _entity_reader(tmp_path)
    seed = Seed(name="Fresh", file_path="app/gone/fresh_store.py")

    ladder = _ladder().tolerate([seed], reader, root)

    assert ladder.exact == [seed]
    assert ladder.guessed == [Seed(name="Fresh", file_path="src/store.py")]
    assert ladder.unresolved == []
    note = _one_note(ladder)
    assert (note.outcome, note.read_as) == ("name", ("src/store.py",))
    assert _ladder().read_block([note]).splitlines()[1] == (
        "- `Fresh (app/gone/fresh_store.py)` → read as `Fresh` in `src/store.py` "
        "(guessed: the given file is not in the code graph)"
    )


def test_a8_an_ambiguous_name_with_a_wrong_file_is_reported_and_the_original_kept(tmp_path, root):
    """Nothing is read, but the original stays exact: a record stored under that name and file
    is found by the store lookup alone, as before seeds were read tolerantly."""
    reader = _entity_reader(tmp_path)
    seed = Seed(name="Dup", file_path="app/gone/dup.py")

    ladder = _ladder().tolerate([seed], reader, root)

    assert ladder.exact == [seed] and ladder.guessed == [] and ladder.unresolved == []
    note = _one_note(ladder)
    assert note.outcome == "ambiguous" and note.total == 2
    assert note.read_as == ("Dup (src/cache.py)", "Dup (src/store.py)")


def test_a8_an_ambiguous_member_with_a_wrong_file_keeps_the_original_too(tmp_path, root):
    reader = make_reader(
        tmp_path,
        ["a.py", "b.py"],
        extra=[
            {"id": "ta", "label": "Type", "file": "a.py"},
            {"id": "ma", "label": ".run()", "file": "a.py"},
            {"id": "tb", "label": "Type", "file": "b.py"},
            {"id": "mb", "label": ".run()", "file": "b.py"},
        ],
        links=[
            {"source": "ta", "target": "ma", "relation": "method"},
            {"source": "tb", "target": "mb", "relation": "method"},
        ],
    )
    seed = Seed(name="Type.run", file_path="app/gone/type.py")

    ladder = _ladder().tolerate([seed], reader, root)

    assert ladder.exact == [seed] and ladder.guessed == []
    assert _one_note(ladder).outcome == "ambiguous"


def test_an_entity_in_a_file_that_exists_on_disk_but_not_in_the_graph_is_never_moved(
    tmp_path, root
):
    """The on-disk guard is for entity seeds too: ``pkg/n.py`` is a file the graph lacks, so
    the seed is not read as the same name in another file, and the file is reported."""
    (root / "pkg").mkdir()
    (root / "pkg" / "n.py").write_text("def a():\n    pass\n")
    reader = make_reader(tmp_path, ["pkg/m.py"])  # holds `fn_0()`
    seed = Seed(name="fn_0", file_path="pkg/n.py")

    ladder = _ladder().tolerate([seed], reader, root)

    assert ladder.exact == [seed] and ladder.guessed == [] and ladder.notes == []
    assert ladder.unresolved == ["pkg/n.py"]


def test_an_entity_in_a_file_on_disk_is_not_read_by_member_either(tmp_path, root):
    (root / "pkg").mkdir()
    (root / "pkg" / "n.py").write_text("def a():\n    pass\n")
    reader = _entity_reader(tmp_path)
    seed = Seed(name="Cache.load", file_path="pkg/n.py")

    ladder = _ladder().tolerate([seed], reader, root)

    assert ladder.exact == [seed] and ladder.guessed == [] and ladder.notes == []
    assert ladder.unresolved == ["pkg/n.py"]


def test_candidates_that_all_lack_a_file_are_counted_not_offered(tmp_path, root):
    reader = make_reader(
        tmp_path,
        ["x.py"],
        extra=[
            {"id": "g1", "label": "Ghost", "file": ""},
            {"id": "g2", "label": "Ghost", "file": ""},
        ],
    )
    seed = Seed(name="Ghost")

    ladder = _ladder().tolerate([seed], reader, root)

    # An ambiguous seed is never expanded: it is not an exact seed either.
    assert ladder.exact == [] and ladder.guessed == []
    note = _one_note(ladder)
    assert (note.outcome, note.total, note.read_as) == ("unresolved-name", 2, ())
    assert _ladder().read_block([note]).splitlines()[1] == (
        "- `Ghost` → matches 2 symbols with no source file in the code graph"
    )


def test_a_large_directory_lists_top_level_files_when_subdirectories_leave_room(tmp_path, root):
    files = [f"big/f{i:02d}.py" for i in range(25)] + ["big/sub/x.py", "big/sub/y.py"]
    reader = make_reader(tmp_path, files)

    note = _one_note(_ladder().tolerate([Seed(file_path="big/")], reader, root))

    assert note.total == 27 and not note.of_files
    assert note.read_as == (
        "big/sub 2",
        "big/f00.py",
        "big/f01.py",
        "big/f02.py",
        "big/f03.py",
    )
    assert _ladder().read_block([note]).splitlines()[1] == (
        "- `big/` → a directory of 27 files, too many to read: pass a file or a smaller "
        "directory (big/sub 2, big/f00.py, big/f01.py, big/f02.py, big/f03.py, …)"
    )


def test_a_large_directory_whose_subdirectories_cover_it_says_nothing_is_missing(tmp_path, root):
    files = [f"big/a/f{i}.py" for i in range(13)] + [f"big/b/f{i}.py" for i in range(12)]
    reader = make_reader(tmp_path, files)

    note = _one_note(_ladder().tolerate([Seed(file_path="big/")], reader, root))

    assert _ladder().read_block([note]).splitlines()[1].endswith("(big/a 13, big/b 12)")


def test_a8_type_dot_member_with_no_file_resolves_by_name(tmp_path, root):
    reader = _entity_reader(tmp_path)
    seed = Seed(name="Cache.load")

    ladder = _ladder().tolerate([seed], reader, root)

    assert ladder.exact == [seed]
    assert ladder.guessed == [Seed(name="Cache.load", file_path="src/cache.py")]
    note = _one_note(ladder)
    assert (note.outcome, note.name, note.read_as) == ("name", "Cache.load", ("src/cache.py",))


def test_a8_type_dot_member_with_a_file_the_graph_lacks_resolves_by_name(tmp_path, root):
    reader = _entity_reader(tmp_path)
    seed = Seed(name="Cache.load", file_path="src/old/cache_legacy.py")

    ladder = _ladder().tolerate([seed], reader, root)

    assert ladder.exact == [seed]
    assert ladder.guessed == [Seed(name="Cache.load", file_path="src/cache.py")]
    note = _one_note(ladder)
    assert note.outcome == "name" and note.given == "Cache.load (src/old/cache_legacy.py)"
    assert ladder.unresolved == []


def test_a8_a_case_only_twin_in_the_candidates_file_is_unresolved(tmp_path, root):
    reader = _entity_reader(tmp_path)

    ladder = _ladder().tolerate([Seed(name="Store.message")], reader, root)

    assert ladder.guessed == []
    assert _one_note(ladder).outcome == "unresolved-name"


def test_a8_an_ambiguous_name_inside_the_given_file_stays_exact(tmp_path, root):
    reader = _entity_reader(tmp_path)
    seed = Seed(name="run", file_path="src/cache.py")

    ladder = _ladder().tolerate([seed], reader, root)

    assert ladder.exact == [seed] and ladder.guessed == [] and ladder.notes == []


def test_a8_an_ambiguous_name_only_seed_is_reported_and_dropped(tmp_path, root):
    reader = _entity_reader(tmp_path)

    ladder = _ladder().tolerate([Seed(name="Dup")], reader, root)

    assert ladder.exact == [] and ladder.guessed == []
    note = _one_note(ladder)
    assert note.outcome == "ambiguous" and note.name == "Dup"
    assert note.read_as == ("Dup (src/cache.py)", "Dup (src/store.py)")
    assert note.total == 2


def test_a8_a_name_only_seed_that_resolves_exactly_is_exact(tmp_path, root):
    reader = _entity_reader(tmp_path)
    seed = Seed(name="fresh")  # case-insensitive, as `resolve` always was

    ladder = _ladder().tolerate([seed], reader, root)

    assert ladder.exact == [seed] and ladder.notes == []


def test_a8_an_entity_seed_whose_file_is_in_the_graph_is_never_moved(tmp_path, root):
    """The file is real and in the graph; the name is not in it. No guess across files, but the
    seed no longer goes quiet: it says what it did not find, and where."""
    reader = _entity_reader(tmp_path)
    seed = Seed(name="Store.load", file_path="src/store.py")

    ladder = _ladder().tolerate([seed], reader, root)

    assert ladder.exact == [seed] and ladder.guessed == [] and ladder.unresolved == []
    note = _one_note(ladder)
    assert (note.outcome, note.read_as, note.name) == (
        "unresolved-name",
        ("src/store.py",),
        "Store.load",
    )
    assert _ladder().read_block([note]).splitlines()[1] == (
        "- `Store.load (src/store.py)` → matches no symbol in `src/store.py`"
    )


def test_a_large_directory_with_no_subdirectories_lists_files_and_says_so(tmp_path, root):
    reader = make_reader(tmp_path, [f"flat/f{i:02d}.py" for i in range(30)])

    note = _one_note(_ladder().tolerate([Seed(file_path="flat/")], reader, root))

    assert (note.outcome, note.total) == ("directory-too-large", 30)
    assert note.read_as == tuple(f"flat/f{i:02d}.py" for i in range(5))
    assert _ladder().read_block([note]).splitlines()[1] == (
        "- `flat/` → a directory of 30 files and no subdirectories, too many to read: pass a "
        "file (flat/f00.py, flat/f01.py, flat/f02.py, flat/f03.py, flat/f04.py, …)"
    )


def test_an_ambiguous_entity_counts_distinct_choices_and_skips_path_less_nodes(tmp_path, root):
    """Six nodes answer to ``Many``: three overloads in ``x.py``, one in ``y.py``, and two
    engine artifacts with no file. The agent can pick between two places, and both are listed:
    no "(2 of 6)" for choices that were already all shown."""
    reader = make_reader(
        tmp_path,
        ["x.py", "y.py"],
        extra=[
            {"id": "m1", "label": "Many", "file": "x.py"},
            {"id": "m2", "label": "Many", "file": "x.py"},
            {"id": "m3", "label": "Many", "file": "x.py"},
            {"id": "m4", "label": "Many", "file": "y.py"},
            {"id": "m5", "label": "Many", "file": ""},
            {"id": "m6", "label": "Many", "file": ""},
        ],
    )

    note = _one_note(_ladder().tolerate([Seed(name="Many")], reader, root))

    assert note.outcome == "ambiguous"
    assert note.read_as == ("Many (x.py)", "Many (y.py)")
    assert note.total == 2
    assert _ladder().read_block([note]).splitlines()[1] == (
        "- `Many` → ambiguous, did you mean `Many (x.py)`, `Many (y.py)`? "
        "Pass `file_path` to say which."
    )


# -- A9: a name that matches nothing -------------------------------------------------------


def test_a9_an_unresolved_name_only_entity_gets_its_own_note_and_stays_in_exact(tmp_path, root):
    """Kept in ``exact``: the store may still hold a descriptor under that name."""
    reader = _entity_reader(tmp_path)
    seed = Seed(name="Frobnicate")

    ladder = _ladder().tolerate([seed], reader, root)

    assert ladder.exact == [seed] and ladder.guessed == []
    note = _one_note(ladder)
    assert (note.outcome, note.given, note.name) == ("unresolved-name", "Frobnicate", "Frobnicate")


# -- A10: nothing to tolerate --------------------------------------------------------------


def test_a10_seeds_that_resolve_as_given_change_nothing(tmp_path, root):
    reader = _entity_reader(tmp_path)
    seeds = [
        Seed(file_path="src/cache.py"),
        Seed(name="Cache", file_path="src/cache.py"),
        Seed(name="Fresh"),
    ]

    ladder = _ladder().tolerate(seeds, reader, root)

    assert ladder.exact == seeds
    assert ladder.guessed == [] and ladder.unresolved == [] and ladder.notes == []
    assert _ladder().needs_tolerance(seeds, reader) is False
    assert _ladder().read_block(ladder.notes) == ""


def test_needs_tolerance_is_true_for_each_seed_that_needs_a_rung_past_exact(tmp_path):
    reader = _entity_reader(tmp_path)
    needs = _ladder().needs_tolerance
    assert needs([Seed(file_path="./src/cache.py")], reader)
    assert needs([Seed(file_path="nope.py")], reader)
    assert needs([Seed(name="Frobnicate")], reader)
    assert needs([Seed(name="Dup")], reader)
    assert needs([Seed(name="Cache", file_path="app/src/cache.py")], reader)
    # a file the graph holds, with a name it lacks, gets a note saying so
    assert needs([Seed(name="Nope", file_path="src/cache.py")], reader)
    assert not needs([], reader)


# -- order, duplicates, telemetry paths ---------------------------------------------------


def test_input_order_is_kept_and_a_directory_expands_in_place(tmp_path, root):
    reader = make_reader(tmp_path, ["pkg/a.py", "pkg/b.py", "x/c.py", "y/d.py"])

    ladder = _ladder().tolerate(
        [Seed(file_path="d.py"), Seed(file_path="pkg/"), Seed(file_path="c.py")], reader, root
    )

    assert [s.file_path for s in ladder.guessed] == ["y/d.py", "pkg/a.py", "pkg/b.py", "x/c.py"]
    assert [n.given for n in ladder.notes] == ["d.py", "pkg/", "c.py"]


def test_a_rewrite_that_duplicates_an_exact_or_an_earlier_seed_is_dropped(tmp_path, root):
    reader = make_reader(tmp_path, ["pkg/m.py", "pkg/n.py"])

    ladder = _ladder().tolerate(
        [
            Seed(file_path="pkg/m.py"),
            Seed(file_path="./pkg/m.py"),
            Seed(file_path="n.py"),
            Seed(file_path="./pkg/n.py"),
        ],
        reader,
        root,
    )

    assert ladder.exact == [Seed(file_path="pkg/m.py")]
    assert ladder.guessed == [Seed(file_path="pkg/n.py")]


def test_the_ladder_lists_the_path_each_seed_contributes_to_telemetry(tmp_path, root):
    """Spec D7: a single-file rewrite records what was read; a directory records its own key;
    an unresolved seed records as given; an exact seed as given."""
    big = [f"big/f{i:02d}.py" for i in range(30)]
    reader = make_reader(tmp_path, ["pkg/m.py", "d/a.py", "d/b.py", *big])
    seeds = [
        Seed(file_path="pkg/m.py"),  # exact
        Seed(file_path="./pkg/m.py"),  # normalised
        Seed(file_path="app/m.py"),  # basename
        Seed(file_path="d/"),  # directory
        Seed(file_path="big/"),  # too large
        Seed(file_path="Nope.py"),  # unresolved
        Seed(name="Frobnicate"),  # no file
        Seed(name="Frobnicate", file_path="src/missing.py"),  # a file, unresolved
    ]

    ladder = _ladder().tolerate(seeds, reader, root)

    assert ladder.seed_paths == [
        "pkg/m.py",
        "pkg/m.py",
        "pkg/m.py",
        "d",
        "big",
        "Nope.py",
        "src/missing.py",
    ]


def test_a_directory_records_its_normalised_spelling_whatever_it_was_written_as(tmp_path, root):
    reader = make_reader(tmp_path, ["pkg/a.py", "pkg/b.py", *[f"big/f{i}.py" for i in range(30)]])

    ladder = _ladder().tolerate(
        [Seed(file_path="pkg:3"), Seed(file_path="./big/"), Seed(file_path="pkg/")], reader, root
    )

    assert ladder.seed_paths == ["pkg", "big", "pkg"]


def test_a_name_rewrite_records_the_file_that_was_read_not_the_one_given(tmp_path, root):
    reader = _entity_reader(tmp_path)

    ladder = _ladder().tolerate(
        [
            Seed(name="Cache.load", file_path="src/old/cache_legacy.py"),  # a wrong file
            Seed(name="Cache.load"),  # no file
            Seed(name="Fresh", file_path="app/gone/fresh_store.py"),  # unique by name
            Seed(name="Dup", file_path="app/gone/dup.py"),  # ambiguous: as given
        ],
        reader,
        root,
    )

    assert ladder.seed_paths == [
        "src/cache.py",
        "src/cache.py",
        "src/store.py",
        "app/gone/dup.py",
    ]


def test_the_root_is_looked_up_only_when_a_rung_needs_it(tmp_path):
    reader = _entity_reader(tmp_path)
    calls: list[str] = []

    def lookup() -> Path | None:
        calls.append("root")
        return None

    name_only = [Seed(name="Frobnicate"), Seed(name="Dup"), Seed(name="Cache.load")]
    _ladder().tolerate(name_only, reader, lookup)
    assert calls == []

    _ladder().tolerate([Seed(file_path="a/b.py"), Seed(file_path="c/d.py")], reader, lookup)
    assert calls == ["root"]  # once, however many seeds ask


# -- A17: the block --------------------------------------------------------------------------


def test_a17_at_most_ten_note_lines_then_a_count_of_the_rest(tmp_path, root):
    names = [f"d{i}/f{i}.py" for i in range(12)]
    reader = make_reader(tmp_path, names)
    ladder = _ladder().tolerate([Seed(file_path=f"./{n}") for n in names], reader, root)
    assert len(ladder.notes) == 12

    block = _ladder().read_block(ladder.notes)

    lines = block.splitlines()
    assert lines[0] == "## How your seeds were read"
    assert len(lines) == 12
    assert [ln for ln in lines[1:11] if ln.startswith("- `")] == lines[1:11]
    assert lines[11] == "… and 2 more seeds"
    assert "f9.py" in lines[10] and "f10.py" not in block and "f11.py" not in block


def test_a17_ten_notes_need_no_tail(tmp_path, root):
    names = [f"d{i}/f{i}.py" for i in range(10)]
    reader = make_reader(tmp_path, names)
    ladder = _ladder().tolerate([Seed(file_path=f"./{n}") for n in names], reader, root)

    lines = _ladder().read_block(ladder.notes).splitlines()

    assert len(lines) == 11 and "more seeds" not in lines[-1]


def test_the_block_says_each_outcome_in_a_line(tmp_path, root):
    big = [f"src/core/f{i}.py" for i in range(10)] + [f"src/ui/f{i}.py" for i in range(20)]
    reader = make_reader(
        tmp_path,
        [
            "pkg/n.py",
            "src/ui/views/Library.swift",
            "a/View.swift",
            "b/View.swift",
            "pkg/x.py",
            *big,
        ],
        extra=[
            {"id": "cache", "label": "Cache", "file": "src/cache.py"},
            {"id": "load", "label": ".load()", "file": "src/cache.py"},
        ],
        links=[{"source": "cache", "target": "load", "relation": "method"}],
    )
    seeds = [
        Seed(file_path="./pkg/n.py"),
        Seed(file_path="app/Library.swift"),
        Seed(file_path="ui/views/Library.swift"),
        Seed(file_path="pkg/"),
        Seed(file_path="src/"),
        Seed(file_path="View.swift"),
        Seed(name="Cache.load", file_path="src/old/cache_legacy.py"),
        Seed(name="Frobnicate"),
    ]

    ladder = _ladder().tolerate(seeds, reader, root)
    lines = _ladder().read_block(ladder.notes).splitlines()

    assert lines == [
        "## How your seeds were read",
        "- `./pkg/n.py` → read as `pkg/n.py` (normalised)",
        "- `app/Library.swift` → read as `src/ui/views/Library.swift` "
        "(guessed: the only file with that name)",
        "- `ui/views/Library.swift` → read as `src/ui/views/Library.swift` "
        "(guessed: the only file with that path ending)",
        "- `pkg/` → a directory: read as all 2 files under it",
        "- `src/` → a directory of 32 files, too many to read: pass a file or a smaller "
        "directory (src/ui 21, src/core 10, src/cache.py)",
        "- `View.swift` → ambiguous, did you mean `a/View.swift`, `b/View.swift`? "
        "Pass the repo-relative path.",
        "- `Cache.load (src/old/cache_legacy.py)` → read as `Cache.load` in `src/cache.py` "
        "(guessed: the given file is not in the code graph)",
        "- `Frobnicate` → matches no symbol in the code graph",
    ]


def test_the_block_names_a_truncated_directory_and_a_truncated_candidate_list(tmp_path, root):
    reader = make_reader(
        tmp_path,
        [f"d{i}/View.swift" for i in range(9)] + [f"pkg/f{i}.py" for i in range(12)],
    )

    ladder = _ladder().tolerate(
        [Seed(file_path="View.swift"), Seed(file_path="pkg/")], reader, root
    )
    ambiguous, directory = _ladder().read_block(ladder.notes).splitlines()[1:]

    assert ambiguous == (
        "- `View.swift` → ambiguous, did you mean `d0/View.swift`, `d1/View.swift`, "
        "`d2/View.swift`, `d3/View.swift`, `d4/View.swift`, … (5 of 9)? "
        "Pass the repo-relative path."
    )
    assert directory == ("- `pkg/` → a directory: read as 8 of 12 files under it, shallowest first")


def test_the_block_names_an_ambiguous_entity_by_name_and_file(tmp_path, root):
    reader = _entity_reader(tmp_path)

    ladder = _ladder().tolerate([Seed(name="Dup")], reader, root)
    (line,) = _ladder().read_block(ladder.notes).splitlines()[1:]

    assert line == (
        "- `Dup` → ambiguous, did you mean `Dup (src/cache.py)`, `Dup (src/store.py)`? "
        "Pass `file_path` to say which."
    )


# -- part B: which file a file seed was read as ----------------------------------------------


def test_each_file_seed_is_reported_as_the_one_file_it_was_read_as_or_not_at_all(tmp_path, root):
    """The nearest-anchored lookup needs one path per file seed: the file read, or the
    normalised spelling of one nothing could read. A directory and an ambiguous seed name no
    single file, and an entity seed is not a file seed."""
    reader = make_reader(
        tmp_path,
        ["pkg/m.py", "pkg/a.py", "pkg/b.py", "app/View.swift", "lib/View.swift", "src/only.py"],
    )
    seeds = [
        Seed(file_path="pkg/m.py"),  # exact
        Seed(file_path="./pkg/a.py"),  # normalised
        Seed(file_path="x/only.py"),  # basename
        Seed(file_path="pkg/"),  # a directory
        Seed(file_path="View.swift"),  # ambiguous
        Seed(file_path="./pkg/new.py"),  # unresolved, read in its normalised spelling
        Seed(name="fn_0", file_path="pkg/m.py"),  # an entity seed
    ]

    ladder = _ladder().tolerate(seeds, reader, root)

    assert [(f.path, f.outcome) for f in ladder.file_seeds] == [
        ("pkg/m.py", "exact"),
        ("pkg/a.py", "normalised"),
        ("src/only.py", "basename"),
        ("pkg/new.py", "unresolved"),
    ]
