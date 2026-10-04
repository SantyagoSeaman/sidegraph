"""``bash_paths.read_paths``: the repo files a Bash read command names (T10).

The hook decides from the line alone which files a ``sed``, ``grep``, ``rg`` or ``cat`` reads,
so the extractor is a table of cases rather than a single property. Each case lists the files
it must return, relative to the project root, in the order the line names them. The cases are
the shapes that broke an extractor read straight off the spec's rules: a pattern that is also a
file name, a value after ``-A``, a ``cd`` that does not leak out of ``( … )``, a heredoc body
that holds a command, a ``cd -`` that makes the working directory unknown, and so on. Every path
is synthetic; nothing here names a real project.

see design/superpowers/specs/2026-10-03-records-at-the-point-of-reading-design.md (D4, T10)
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from sidegraph.bash_paths import read_paths

ROOT = "@ROOT@"
OUTSIDE = "@OUTSIDE@"

# command, cwd relative to the root, files the line reads (root-relative, in order)
CASES: list[tuple[str, str, str, list[str]]] = [
    # -- cat and the plain shapes -------------------------------------------------------------
    ("cat-one", "cat src/a.py", ".", ["src/a.py"]),
    ("cat-two", "cat src/a.py src/b.py", ".", ["src/a.py", "src/b.py"]),
    ("cat-repeat-listed-once", "cat src/a.py src/b.py src/a.py", ".", ["src/a.py", "src/b.py"]),
    ("cat-flags", "cat -n src/a.py", ".", ["src/a.py"]),
    ("cat-double-dash", "cat -- src/a.py", ".", ["src/a.py"]),
    ("cat-absolute-command", "/bin/cat src/a.py", ".", ["src/a.py"]),
    ("cat-stdin-dash", "cat - src/a.py", ".", ["src/a.py"]),
    ("ls-then-cat", "ls; cat src/a.py", ".", ["src/a.py"]),
    ("and-or-chain", "ls && cat src/a.py || echo no", ".", ["src/a.py"]),
    ("cat-into-head", "cat src/a.py | head", ".", ["src/a.py"]),
    (
        "pipe-two-readers",
        "sed -n 1,5p src/a.py | grep -n x src/b.py",
        ".",
        ["src/a.py", "src/b.py"],
    ),
    ("background-ampersand", "cat src/a.py & cat src/b.py", ".", ["src/a.py", "src/b.py"]),
    ("comment-ignored", "cat src/a.py # and src/b.py", ".", ["src/a.py"]),
    ("relative-to-the-payload-cwd", "cat a.py", "src", ["src/a.py"]),
    ("dotdot-resolved", "cat src/x/../a.py", ".", ["src/a.py"]),
    # -- sed ----------------------------------------------------------------------------------
    ("sed-n-script", "sed -n '1,9p' src/a.py", ".", ["src/a.py"]),
    (
        "sed-cd-then-read",
        "cd src && sed -n 1,9p sidegraph/store.py",
        ".",
        ["src/sidegraph/store.py"],
    ),
    ("sed-e-script", "sed -e 's/a/b/' src/a.py", ".", ["src/a.py"]),
    ("sed-two-e", "sed -e s/a/b/ -e s/c/d/ src/a.py", ".", ["src/a.py"]),
    ("sed-f-value-is-not-read", "sed -f src/b.py src/a.py", ".", ["src/a.py"]),
    ("sed-cluster-ne", "sed -ne '1p' src/a.py", ".", ["src/a.py"]),
    ("sed-cluster-En", "sed -En 's/a/b/p' src/a.py", ".", ["src/a.py"]),
    ("sed-long-expression", "sed --expression='s/a/b/' src/a.py", ".", ["src/a.py"]),
    ("sed-n-e-p", "sed -n -e p src/a.py", ".", ["src/a.py"]),
    ("sed-two-files", "sed -s -n '$p' src/a.py src/b.py", ".", ["src/a.py", "src/b.py"]),
    ("sed-inplace-bsd-empty-suffix", "sed -i '' 's/a/b/' src/a.py", ".", ["src/a.py"]),
    ("sed-inplace-attached-suffix", "sed -i.bak 's/a/b/' src/a.py", ".", ["src/a.py"]),
    ("sed-inplace-gnu", "sed -i 's/a/b/' src/a.py", ".", ["src/a.py"]),
    ("sed-inplace-then-e", "sed -i -e 's/a/b/' src/a.py", ".", ["src/a.py"]),
    # -- grep ---------------------------------------------------------------------------------
    ("grep-pattern-only-dot", "grep -rn foo .", ".", []),
    ("grep-pattern-only-stdin", "ls | grep foo", ".", []),
    ("grep-two-files", "grep -n foo src/a.py src/b.py", ".", ["src/a.py", "src/b.py"]),
    (
        "grep-pattern-is-a-file-name",
        "cd src/x && grep -n store.py hooks.py",
        ".",
        ["src/x/hooks.py"],
    ),
    ("grep-v-pattern-looks-like-a-path", "grep -v src/a.py", ".", []),
    ("grep-v-pattern-then-file", "grep -v src/a.py src/b.py", ".", ["src/b.py"]),
    ("grep-e-pattern", "grep -e foo src/a.py", ".", ["src/a.py"]),
    ("grep-e-only-a-pattern", "grep -e src/a.py", ".", []),
    ("grep-two-e", "grep -e foo -e bar src/a.py src/b.py", ".", ["src/a.py", "src/b.py"]),
    ("grep-long-regexp-equals", "grep --regexp=foo src/a.py", ".", ["src/a.py"]),
    ("grep-long-regexp-separate", "grep --regexp foo src/a.py", ".", ["src/a.py"]),
    ("grep-A-separate", "grep -A 3 foo src/a.py", ".", ["src/a.py"]),
    ("grep-A-attached", "grep -A3 foo src/a.py", ".", ["src/a.py"]),
    ("grep-cluster-nA", "grep -nA 2 foo src/a.py", ".", ["src/a.py"]),
    ("grep-m", "grep -m 5 foo src/a.py", ".", ["src/a.py"]),
    ("grep-C", "grep -C 2 foo src/a.py", ".", ["src/a.py"]),
    ("grep-numeric-context", "grep -5 foo src/a.py", ".", ["src/a.py"]),
    ("grep-max-count-long", "grep --max-count 3 foo src/a.py", ".", ["src/a.py"]),
    ("grep-include-value-is-not-read", "grep --include src/b.py foo src/a.py", ".", ["src/a.py"]),
    ("grep-include-equals", "grep --include=*.py -rn foo src/a.py", ".", ["src/a.py"]),
    ("grep-f-value-is-not-read", "grep -f src/b.py src/a.py", ".", ["src/a.py"]),
    ("grep-color-bare", "grep --color foo src/a.py", ".", ["src/a.py"]),
    ("grep-double-dash-pattern", "grep -- -v src/a.py", ".", ["src/a.py"]),
    # -- rg -----------------------------------------------------------------------------------
    ("rg-n", "rg -n foo src/a.py", ".", ["src/a.py"]),
    ("rg-glob-value-is-not-read", "rg -g src/b.py foo src/a.py", ".", ["src/a.py"]),
    ("rg-type", "rg -t py foo src/a.py", ".", ["src/a.py"]),
    ("rg-context", "rg -A 3 -B 1 foo src/a.py", ".", ["src/a.py"]),
    ("rg-long-glob", "rg --glob '*.py' foo src/a.py", ".", ["src/a.py"]),
    ("rg-e", "rg -e foo src/a.py", ".", ["src/a.py"]),
    ("rg-files-lists-paths", "rg --files src/a.py src/b.py", ".", ["src/a.py", "src/b.py"]),
    ("rg-files-directory", "rg --files src", ".", []),
    ("rg-color-takes-a-value", "rg --color never foo src/a.py", ".", ["src/a.py"]),
    # -- newlines, subshells, bash -c, assignments --------------------------------------------
    ("newlines", "cd src\ncat a.py\ngrep foo b.py", ".", ["src/a.py", "src/b.py"]),
    (
        "subshell-cd-stays-inside",
        "(cd src && cat a.py); cat notes.txt",
        ".",
        ["src/a.py", "notes.txt"],
    ),
    ("subshell-cd-does-not-leak", "(cd src && cat a.py) && cat a.py", ".", ["src/a.py"]),
    ("bash-c", "bash -c 'cat src/a.py'", ".", ["src/a.py"]),
    ("sh-c-with-cd", 'sh -c "cd src && cat a.py"', ".", ["src/a.py"]),
    ("zsh-c-grep", "zsh -c 'grep foo src/a.py'", ".", ["src/a.py"]),
    ("bash-lc-cluster", "bash -lc 'cat src/a.py'", ".", ["src/a.py"]),
    ("bash-c-cd-does-not-leak", "bash -c 'cd src' && cat a.py", ".", []),
    ("bash-c-one-level-only", "bash -c \"bash -c 'cat src/a.py'\"", ".", []),
    ("assignment-prefix", "FOO=1 grep foo src/a.py", ".", ["src/a.py"]),
    ("two-assignments", "A=1 B=2 cat src/a.py", ".", ["src/a.py"]),
    ("quoted-assignment", 'FOO="a b" cat src/a.py', ".", ["src/a.py"]),
    # -- shell keywords and wrappers before the command word ----------------------------------
    ("brace-group", "{ cat src/a.py; }", ".", ["src/a.py"]),
    ("for-do", "for f in x; do cat src/a.py; done", ".", ["src/a.py"]),
    ("if-then", "if grep -q foo src/a.py; then cat src/b.py; fi", ".", ["src/a.py", "src/b.py"]),
    ("else", "if true; then echo y; else cat src/a.py; fi", ".", ["src/a.py"]),
    ("negation", "! grep -q foo src/a.py", ".", ["src/a.py"]),
    ("while", "while grep -q foo src/a.py; do sleep 1; done", ".", ["src/a.py"]),
    ("until", "until grep -q foo src/a.py; do sleep 1; done", ".", ["src/a.py"]),
    ("time", "time cat src/a.py", ".", ["src/a.py"]),
    ("timeout", "timeout 5 cat src/a.py", ".", ["src/a.py"]),
    ("timeout-duration-suffix", "timeout 5s cat src/a.py", ".", ["src/a.py"]),
    ("timeout-signal", "timeout -s KILL 5 grep foo src/a.py", ".", ["src/a.py"]),
    ("timeout-long-signal", "timeout --signal=KILL 5 cat src/a.py", ".", ["src/a.py"]),
    ("env", "env cat src/a.py", ".", ["src/a.py"]),
    ("env-assignments", "env A=1 B=2 grep foo src/a.py", ".", ["src/a.py"]),
    ("env-unset", "env -u FOO cat src/a.py", ".", ["src/a.py"]),
    ("env-absolute", "/usr/bin/env cat src/a.py", ".", ["src/a.py"]),
    ("sudo", "sudo cat src/a.py", ".", ["src/a.py"]),
    ("sudo-user", "sudo -u root cat src/a.py", ".", ["src/a.py"]),
    ("command", "command cat src/a.py", ".", ["src/a.py"]),
    ("nice", "nice cat src/a.py", ".", ["src/a.py"]),
    ("nice-adjustment", "nice -n 10 cat src/a.py", ".", ["src/a.py"]),
    ("nice-numeric", "nice -10 cat src/a.py", ".", ["src/a.py"]),
    ("nohup", "nohup cat src/a.py", ".", ["src/a.py"]),
    (
        "wrappers-stack",
        "sudo -u x env A=1 nice -n 5 timeout 10 grep foo src/a.py",
        ".",
        ["src/a.py"],
    ),
    (
        "keyword-then-wrapper-then-assignment",
        "if A=1 timeout 3 cat src/a.py; then :; fi",
        ".",
        ["src/a.py"],
    ),
    ("wrapper-then-bash-c", "sudo bash -c 'cat src/a.py'", ".", ["src/a.py"]),
    ("wrapper-then-cd", "if cd src; then cat a.py; fi", ".", ["src/a.py"]),
    ("wrapper-then-an-unwired-command", "sudo head src/a.py", ".", []),
    ("wrapper-with-no-command", "timeout 5", ".", []),
    ("time-with-no-command", "time", ".", []),
    ("env-with-no-command", "env A=1", ".", []),
    ("keyword-as-an-argument-is-not-skipped", "echo then cat src/a.py", ".", []),
    # -- redirects and heredocs ---------------------------------------------------------------
    ("redirect-out-target-dropped", "cat > src/a.py", ".", []),
    ("redirect-append-target-dropped", "cat >> src/a.py", ".", []),
    ("redirect-after-the-file", "cat src/a.py > notes.txt", ".", ["src/a.py"]),
    ("redirect-no-space", "cat src/a.py >notes.txt", ".", ["src/a.py"]),
    ("redirect-stderr-null", "cat src/a.py 2>/dev/null", ".", ["src/a.py"]),
    ("redirect-stderr-to-stdout", "cat src/a.py 2>&1 | head", ".", ["src/a.py"]),
    ("redirect-both", "grep foo src/a.py &>notes.txt", ".", ["src/a.py"]),
    ("redirect-in-target-dropped", "cat < src/a.py", ".", []),
    (
        "heredoc-body-dropped",
        "cat > src/b.py <<'EOF'\ncat src/a.py\nEOF\ncat notes.txt",
        ".",
        ["notes.txt"],
    ),
    ("heredoc-dash", "cat <<-EOF\n\tsrc/a.py\n\tEOF\ncat notes.txt", ".", ["notes.txt"]),
    (
        "heredoc-double-quoted-delimiter",
        'cat <<"END"\ncat src/a.py\nEND\ncat notes.txt',
        ".",
        ["notes.txt"],
    ),
    ("heredoc-text-is-not-a-file", "cat <<EOF\nsrc/a.py\nEOF", ".", []),
    # -- cd tracking --------------------------------------------------------------------------
    ("cd-relative", "cd src && cat a.py", ".", ["src/a.py"]),
    ("cd-dotdot", "cd src/x && cat ../a.py", ".", ["src/a.py"]),
    ("cd-absolute", f"cd {ROOT}/src && cat a.py", ".", ["src/a.py"]),
    ("pushd-makes-it-unknown", "pushd src && cat a.py", ".", []),
    (
        "pushd-absolute-still-resolves",
        f"pushd src >/dev/null; cat {ROOT}/src/a.py",
        ".",
        ["src/a.py"],
    ),
    ("popd-makes-it-unknown", "pushd src; popd; cat notes.txt", ".", []),
    ("cd-dash-makes-it-unknown", "cd src && cd - && cat src/a.py", ".", []),
    ("cd-dash-absolute-resolves", f"cd src; cd -; cat {ROOT}/notes.txt", ".", ["notes.txt"]),
    ("cd-home-makes-it-unknown", "cd ~ && cat src/a.py", ".", []),
    ("cd-tilde-path-is-known", "cd ~/proj/src && cat a.py", ".", ["src/a.py"]),
    ("bare-cd-makes-it-unknown", "cd && cat src/a.py", ".", []),
    ("cd-variable-makes-it-unknown", "cd $HOME && cat src/a.py", ".", []),
    (
        "cd-substitution-makes-it-unknown",
        'cd "$(git rev-parse --show-toplevel)" && cat src/a.py',
        ".",
        [],
    ),
    ("unknown-stays-unknown-through-a-relative-cd", "cd - && cd src && cat a.py", ".", []),
    (
        "an-absolute-cd-makes-it-known-again",
        f"cd - && cd {ROOT}/src && cat a.py",
        ".",
        ["src/a.py"],
    ),
    ("unknown-ends-with-the-subshell", "(cd - && cat src/b.py); cat src/a.py", ".", ["src/a.py"]),
    # -- paths --------------------------------------------------------------------------------
    ("home-tilde", "cat ~/proj/src/a.py", ".", ["src/a.py"]),
    ("other-users-tilde", "cat ~root/src/a.py", ".", []),
    ("quoted-path-with-a-space", 'cat "src/my file.py"', ".", ["src/my file.py"]),
    ("escaped-space", "cat src/my\\ file.py", ".", ["src/my file.py"]),
    ("single-quoted", "cat 'src/a.py'", ".", ["src/a.py"]),
    ("directory-dropped", "cat src/pkg", ".", []),
    ("glob-not-expanded", "cat src/*.py", ".", []),
    ("variable-skipped", "cat $F src/a.py", ".", ["src/a.py"]),
    ("substitution-skipped", "cat $(ls) `ls` src/a.py", ".", ["src/a.py"]),
    ("missing-file", "cat src/zzz.py", ".", []),
    ("outside-the-root", f"cat {OUTSIDE}/outside.py /etc/hosts", ".", []),
    ("dotdot-out-of-the-root", "cat ../outside/outside.py", ".", []),
    ("symlink-out-of-the-root", "cat src/escape.py", ".", []),
    ("symlink-inside-the-root-resolves", "cat src/alias.py", ".", ["src/a.py"]),
    # -- commands that are not wired extract nothing ------------------------------------------
    ("awk-not-wired", "awk '{print $1}' src/a.py", ".", []),
    ("head-not-wired", "head -n 5 src/a.py", ".", []),
    ("tail-not-wired", "tail -f src/a.py", ".", []),
    ("less-not-wired", "less src/a.py", ".", []),
    ("git-grep-not-wired", "git grep foo -- src/a.py", ".", []),
    ("echo-not-wired", "echo src/a.py", ".", []),
    # -- input that is not a command line -----------------------------------------------------
    ("empty", "", ".", []),
    ("blank", "   \n  ", ".", []),
    ("unbalanced-quote", "cat 'src/a.py", ".", []),
    ("only-operators", "&& ; ||", ".", []),
]


@pytest.fixture
def tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """A project at ``<home>/proj`` with the files the table names, a directory, a symlink out of
    the root and one inside it, and an ``outside`` directory beside the project."""
    home = tmp_path / "home"
    root = home / "proj"
    outside = home / "outside"
    for rel in (
        "src/a.py",
        "src/b.py",
        "src/my file.py",
        "src/x/hooks.py",
        "src/x/store.py",
        "src/sidegraph/store.py",
        "src/pkg/__init__.py",
        "notes.txt",
    ):
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x\n")
    outside.mkdir(parents=True)
    (outside / "outside.py").write_text("x\n")
    os.symlink(outside / "outside.py", root / "src" / "escape.py")
    os.symlink(root / "src" / "a.py", root / "src" / "alias.py")
    monkeypatch.setenv("HOME", str(home))
    return root, outside


@pytest.mark.parametrize(
    ("command", "cwd", "expected"),
    [pytest.param(c, d, e, id=i) for i, c, d, e in CASES],
)
def test_the_files_a_line_reads(
    tree: tuple[Path, Path], command: str, cwd: str, expected: list[str]
) -> None:
    root, outside = tree
    line = command.replace(ROOT, str(root)).replace(OUTSIDE, str(outside))
    assert read_paths(line, str(root / cwd), str(root)) == expected


def test_a_cwd_outside_the_root_still_resolves_absolute_and_tilde_paths(
    tree: tuple[Path, Path], tmp_path: Path
) -> None:
    """A payload ``cwd`` in a sibling checkout resolves nothing relative into this root, but a
    path that names a file here by its absolute or ``~`` form still does."""
    root, _ = tree
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    assert read_paths("cat src/a.py", str(elsewhere), str(root)) == []
    assert read_paths(f"cat {root}/src/a.py ~/proj/notes.txt", str(elsewhere), str(root)) == [
        "src/a.py",
        "notes.txt",
    ]


def test_a_root_reached_through_a_symlink_maps_like_a_touch(
    tree: tuple[Path, Path], tmp_path: Path
) -> None:
    """Both sides are ``realpath``-resolved, as ``hooks._touch_path`` does: a project opened
    through a symlinked path still yields repo-relative names, not ``..`` paths."""
    root, _ = tree
    link = tmp_path / "link"
    os.symlink(root, link)
    assert read_paths("cat src/a.py", str(link), str(link)) == ["src/a.py"]
    assert read_paths(f"cat {root}/src/a.py", str(link), str(link)) == ["src/a.py"]


def test_it_never_raises_on_hostile_input(tree: tuple[Path, Path]) -> None:
    root, _ = tree
    for line in (
        "cat " + "'" * 50,
        "(" * 200 + "cat src/a.py" + ")" * 200,
        "cat \x00 src/a.py",
        "cat " + "a/" * 5000 + "b.py",
        "<<<<<<<< >>>>>>> |||||| &&&&&",
        "bash -c",
        "cd",
        "sed",
        "sed -e",
        "grep -e",
        "rg --files",
    ):
        assert isinstance(read_paths(line, str(root), str(root)), list)
