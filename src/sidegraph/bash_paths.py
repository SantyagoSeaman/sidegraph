"""The repo files a Bash read command names: the pure path extractor behind the PreToolUse hook.

The hook runs on a Bash call only to deliver the records of the files the line reads, so it must
tell, from the text of the line alone, which regular files a ``sed``, ``grep``, ``rg`` or ``cat``
reads. ``read_paths`` does that and nothing else: no ``Store``, no pydantic, no I/O beyond
``realpath`` and ``isfile`` on candidate paths. It is deliberately a small reader of shell, not a
shell: it follows ``cd`` (and what scopes it), skips what a command takes as a pattern or an option
value, and drops anything it cannot resolve to an existing file inside the root. A line it cannot
read (an unbalanced quote, a construct it does not model) yields fewer files, never an exception.

The rules are the ones the spec lists (D4); the case table in ``tests/test_bash_paths.py`` pins
each one.

see design/superpowers/specs/2026-10-03-records-at-the-point-of-reading-design.md (D4)
"""

from __future__ import annotations

import contextlib
import os
import re
from collections.abc import Sequence
from typing import NamedTuple

_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")
_NUMERIC_OPTION = re.compile(r"-\d+")
_SHELL_COMMAND_FLAGS = re.compile(r"-[A-Za-z]*c[A-Za-z]*")
_SHELLS = frozenset({"bash", "sh", "zsh"})

# What may stand before the command word of a simple command. A reserved word opens the command
# that follows it (``if grep -q x f; then cat g; fi`` is two commands whose first words are ``if``
# and ``then``). A wrapper runs the command that follows it, after its own options: each maps to
# the options that take the next argument as a value. ``timeout`` also takes a duration.
_KEYWORDS = frozenset({"{", "do", "then", "else", "!", "if", "while", "until", "time"})
_WRAPPER_VALUE_OPTIONS: dict[str, frozenset[str]] = {
    "timeout": frozenset({"-s", "-k", "--signal", "--kill-after"}),
    "env": frozenset({"-u", "--unset", "-C", "--chdir"}),
    "sudo": frozenset(
        {"-u", "-g", "-C", "-h", "-p", "-r", "-t", "-U", "-D", "-R", "-T"}
        | {"--user", "--group", "--host", "--prompt", "--role", "--type", "--chdir"}
    ),
    "command": frozenset(),
    "nice": frozenset({"-n", "--adjustment"}),
    "nohup": frozenset(),
}


class _Spec(NamedTuple):
    """How one read command takes its arguments.

    ``short_value`` and ``long_value`` are the options that take a value (the next argument, or
    attached: ``-A3``, ``--glob=x``); ``short_pattern`` and ``long_pattern`` are the ones among
    them whose value IS the pattern, so that no positional is left to be one. ``takes_pattern``
    is whether the first positional is a pattern (grep, rg) or a script (sed) rather than a file.
    """

    takes_pattern: bool = False
    short_value: frozenset[str] = frozenset()
    long_value: frozenset[str] = frozenset()
    short_pattern: frozenset[str] = frozenset()
    long_pattern: frozenset[str] = frozenset()
    # ``sed -i[SUFFIX]``: the suffix is attached (GNU) or the next argument when empty (BSD).
    optional_attached: frozenset[str] = frozenset()
    # An option that leaves no pattern to take (``rg --files``).
    patternless: frozenset[str] = frozenset()


_GREP_SHORT = frozenset("efABCmdD")
_GREP_LONG = frozenset(
    {
        "regexp",
        "file",
        "max-count",
        "context",
        "after-context",
        "before-context",
        "include",
        "exclude",
        "exclude-dir",
        "exclude-from",
        "include-dir",
        "directories",
        "devices",
        "label",
        "binary-files",
        "group-separator",
    }
)
_RG_SHORT = frozenset("ABCeEfgjmMrtTd")
_RG_LONG = frozenset(
    {
        "regexp",
        "file",
        "glob",
        "iglob",
        "type",
        "type-not",
        "type-add",
        "type-clear",
        "max-count",
        "max-depth",
        "maxdepth",
        "max-columns",
        "max-filesize",
        "context",
        "after-context",
        "before-context",
        "replace",
        "threads",
        "sort",
        "sortr",
        "encoding",
        "pre",
        "pre-glob",
        "path-separator",
        # grep's ``--color`` is attached-only; ripgrep's takes the next argument.
        "color",
        "colors",
        "context-separator",
        "field-context-separator",
        "field-match-separator",
        "ignore-file",
        "engine",
        "dfa-size-limit",
        "regex-size-limit",
        "hyperlink-format",
    }
)

_SPECS: dict[str, _Spec] = {
    "cat": _Spec(),
    "sed": _Spec(
        takes_pattern=True,
        short_value=frozenset("efl"),
        long_value=frozenset({"expression", "file", "line-length"}),
        short_pattern=frozenset("ef"),
        long_pattern=frozenset({"expression", "file"}),
        optional_attached=frozenset("i"),
    ),
    "grep": _Spec(
        takes_pattern=True,
        short_value=_GREP_SHORT,
        long_value=_GREP_LONG,
        short_pattern=frozenset("ef"),
        long_pattern=frozenset({"regexp", "file"}),
    ),
    "rg": _Spec(
        takes_pattern=True,
        short_value=_RG_SHORT,
        long_value=_RG_LONG,
        short_pattern=frozenset("ef"),
        long_pattern=frozenset({"regexp", "file"}),
        patternless=frozenset({"files", "type-list"}),
    ),
}


class _Unreadable(Exception):
    """The text ends inside a quote or a ``$( … )``: nothing after it can be trusted."""


# -- tokens -------------------------------------------------------------------------------------

_WORD = "w"
_OP = "op"


def _scan_group(text: str, i: int, close: str) -> int:
    """The index just after the ``close`` that ends a group whose opener sits before ``i``.

    Quoted runs inside are skipped, so a ``)`` in a quoted string does not end ``$( … )``.
    """
    opener = {")": "(", "}": "{"}.get(close)
    depth = 1
    n = len(text)
    while i < n:
        c = text[i]
        if c == "\\":
            i += 2
            continue
        if c in "'\"":
            j = i + 1
            while j < n and text[j] != c:
                j += 2 if (c == '"' and text[j] == "\\") else 1
            if j >= n:
                raise _Unreadable
            i = j + 1
            continue
        if c == opener:
            depth += 1
        elif c == close:
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    raise _Unreadable


def _read_word(text: str, i: int) -> tuple[str, int]:
    """One shell word starting at ``i``: its text with quotes and escapes removed, and where it
    ended. ``$( … )``, ``${ … }`` and backticks stay in the text verbatim, so a word that holds
    one carries a ``$`` or a backtick and is skipped later."""
    out: list[str] = []
    n = len(text)
    while i < n:
        c = text[i]
        if c in " \t\r\n;&|()<>":
            break
        if c == "\\":
            if text.startswith("\\\n", i):
                i += 2
            elif i + 1 < n:
                out.append(text[i + 1])
                i += 2
            else:
                i += 1
        elif c == "'":
            j = text.find("'", i + 1)
            if j < 0:
                raise _Unreadable
            out.append(text[i + 1 : j])
            i = j + 1
        elif c == '"':
            i += 1
            while True:
                if i >= n:
                    raise _Unreadable
                d = text[i]
                if d == '"':
                    i += 1
                    break
                if d == "\\" and i + 1 < n:
                    nxt = text[i + 1]
                    if nxt == "\n":
                        pass
                    elif nxt in '"\\$`':
                        out.append(nxt)
                    else:
                        out.append(d + nxt)
                    i += 2
                elif text.startswith("$(", i) or text.startswith("${", i):
                    j = _scan_group(text, i + 2, ")" if text[i + 1] == "(" else "}")
                    out.append(text[i:j])
                    i = j
                elif d == "`":
                    j = _scan_group(text, i + 1, "`")
                    out.append(text[i:j])
                    i = j
                else:
                    out.append(d)
                    i += 1
        elif c == "$" and (text.startswith("$(", i) or text.startswith("${", i)):
            j = _scan_group(text, i + 2, ")" if text[i + 1] == "(" else "}")
            out.append(text[i:j])
            i = j
        elif c == "`":
            j = _scan_group(text, i + 1, "`")
            out.append(text[i:j])
            i = j
        else:
            out.append(c)
            i += 1
    return "".join(out), i


def _tokenize(text: str) -> list[tuple[str, str]]:
    """Words and operators, with redirections (and their targets) and heredoc bodies removed.

    An operator token is one of ``;`` ``&&`` ``||`` ``|`` ``&`` newline ``(`` ``)``. A redirect
    (``>``, ``>>``, ``<``, ``2>``, ``&>``, ``>&``, ``<<``, ``<<<`` …) is dropped together with the
    word it takes, and a ``<<DELIM`` also drops the lines up to ``DELIM``.
    """
    tokens: list[tuple[str, str]] = []
    n = len(text)
    i = 0
    last_word_end = -1
    expect: str | None = (
        None  # "skip": the next word is a redirect target; "heredoc": its delimiter
    )
    heredocs: list[str] = []
    while i < n:
        c = text[i]
        if c in " \t\r":
            i += 1
        elif text.startswith("\\\n", i):
            i += 2
        elif c == "\n":
            tokens.append((_OP, "\n"))
            i += 1
            expect = None
            for delimiter in heredocs:
                while i < n:
                    j = text.find("\n", i)
                    line = text[i:] if j < 0 else text[i:j]
                    i = n if j < 0 else j + 1
                    if line.strip() == delimiter:
                        break
            heredocs = []
        elif c == "#":
            j = text.find("\n", i)
            i = n if j < 0 else j
        elif c in "<>" and text[i + 1 : i + 2] == "(":
            j = _scan_group(text, i + 2, ")")
            if expect is None:
                tokens.append((_WORD, text[i:j]))
                last_word_end = j
            expect = None
            i = j
        elif c in "<>" or (c == "&" and text[i + 1 : i + 2] == ">"):
            if c == "&":
                width = 3 if text[i + 2 : i + 3] == ">" else 2
            elif text.startswith(("<<<", "<<-"), i):
                width = 3
            elif text[i : i + 2] in ("<<", ">>", ">&", "<&", ">|", "<>"):
                width = 2
            else:
                width = 1
            # a file descriptor written right before the operator belongs to it: 2>file
            if tokens and tokens[-1][0] == _WORD and tokens[-1][1].isdigit() and last_word_end == i:
                tokens.pop()
            heredoc = text.startswith("<<", i) and not text.startswith("<<<", i)
            expect = "heredoc" if heredoc else "skip"
            i += width
        elif c in ";&|()":
            two = text[i : i + 2]
            if two in ("&&", "||"):
                operator, width = two, 2
            elif two == "|&":
                operator, width = "|", 2
            elif two == ";;":
                operator, width = ";", 2
            else:
                operator, width = c, 1
            tokens.append((_OP, operator))
            expect = None
            i += width
        else:
            word, end = _read_word(text, i)
            i = max(end, i + 1)
            if expect == "heredoc":
                heredocs.append(word)
            elif expect is None:
                tokens.append((_WORD, word))
                last_word_end = end
            expect = None
    return tokens


# -- walking the commands -----------------------------------------------------------------------


class _Walk:
    """What one pass over a line needs: where paths resolve to and what has been found."""

    __slots__ = ("found", "home", "root")

    def __init__(self, root: str, home: str) -> None:
        self.root = root  # realpath of the project root
        self.home = home
        self.found: list[str] = []


def _opaque(arg: str) -> bool:
    """An argument whose value the line does not state: a variable, a command substitution."""
    return "$" in arg or "`" in arg or "\0" in arg


def _positionals(spec: _Spec, args: Sequence[str]) -> list[str]:
    """The arguments of a read command that are neither options, option values nor its pattern."""
    positionals: list[str] = []
    have_pattern = False
    options_ended = False
    i = 0
    while i < len(args):
        arg = args[i]
        i += 1
        if options_ended or not arg.startswith("-") or arg == "-":
            positionals.append(arg)
        elif arg == "--":
            options_ended = True
        elif arg.startswith("--"):
            name, attached, _ = arg[2:].partition("=")
            if name in spec.long_pattern:
                have_pattern = True
            if name in spec.patternless:
                have_pattern = True
            if name in spec.long_value and not attached:
                i += 1
        elif _NUMERIC_OPTION.fullmatch(arg):
            continue
        else:
            cluster = arg[1:]
            for j, letter in enumerate(cluster):
                last = j == len(cluster) - 1
                if letter in spec.short_value:
                    if letter in spec.short_pattern:
                        have_pattern = True
                    if last:
                        i += 1
                    break
                if letter in spec.optional_attached:
                    # bare ``-i`` followed by an empty argument is the BSD empty suffix
                    if last and i < len(args) and args[i] == "":
                        i += 1
                    break
    if spec.takes_pattern and not have_pattern and positionals:
        positionals = positionals[1:]
    return positionals


def _resolve(arg: str, cwd: str | None, walk: _Walk) -> str | None:
    """The root-relative name of the existing regular file ``arg`` names, or ``None``.

    Both sides go through ``realpath``, as ``hooks._touch_path`` does, so a project reached
    through a symlink and a symlink out of it are handled the same way a touch is.
    """
    if not arg or arg == "-" or _opaque(arg):
        return None
    if arg == "~" or (arg.startswith("~") and not arg.startswith("~/")):
        return None
    if arg.startswith("~/"):
        arg = os.path.join(walk.home, arg[2:])
    elif not os.path.isabs(arg):
        if cwd is None:
            return None
        arg = os.path.join(cwd, arg)
    try:
        target = os.path.realpath(arg)
        rel = os.path.relpath(target, walk.root)
        if rel == os.curdir or rel == os.pardir or rel.startswith(os.pardir + os.sep):
            return None
        if not os.path.isfile(target):
            return None
    except (OSError, ValueError):
        return None
    return rel


def _change_directory(cwd: str | None, args: Sequence[str], walk: _Walk) -> str | None:
    """The working directory after ``cd args``; ``None`` when the line no longer states it."""
    operands = [a for a in args if a != "--" and (a == "-" or not a.startswith("-"))]
    if not operands:
        return None  # a bare ``cd`` goes home
    target = operands[0]
    if target == "-" or _opaque(target) or target == "~":
        return None
    if target.startswith("~/"):
        return os.path.normpath(os.path.join(walk.home, target[2:]))
    if target.startswith("~"):
        return None
    if os.path.isabs(target):
        return os.path.normpath(target)
    if cwd is None:
        return None
    return os.path.normpath(os.path.join(cwd, target))


def _skip_wrapper(name: str, words: Sequence[str], i: int) -> int:
    """The index of the first word after the options (and the duration, for ``timeout``) of the
    wrapper ``name`` whose own word sits before ``i``."""
    value_options = _WRAPPER_VALUE_OPTIONS[name]
    while i < len(words) and words[i].startswith("-"):
        i += 2 if words[i] in value_options else 1
    return i + 1 if name == "timeout" else i


def _command_words(words: Sequence[str]) -> list[str]:
    """``words`` from the command word on: without leading ``VAR=value`` assignments, reserved
    words (:data:`_KEYWORDS`) and wrappers (:data:`_WRAPPER_VALUE_OPTIONS`), in any order and
    stacked. Empty when nothing runs (``timeout 5``, a bare ``if``)."""
    i = 0
    while i < len(words):
        word = words[i]
        wrapper = os.path.basename(word)
        if _ASSIGNMENT.match(word) or word in _KEYWORDS:
            i += 1
        elif wrapper in _WRAPPER_VALUE_OPTIONS:
            i = _skip_wrapper(wrapper, words, i + 1)
        else:
            break
    return list(words[i:])


def _script_of(args: Sequence[str]) -> str | None:
    """The ``-c`` script of a ``bash``-like command (``-c``, ``-lc``, ``-ec``), if it has one."""
    for j, arg in enumerate(args[:-1]):
        if _SHELL_COMMAND_FLAGS.fullmatch(arg):
            return args[j + 1]
    return None


def _walk_line(text: str, cwd: str | None, walk: _Walk, depth: int) -> None:
    """Find the files read by ``text`` and append them to ``walk.found``."""
    commands: list[list[str] | str] = []  # a command's words, or "(" / ")"
    current: list[str] = []
    for kind, value in _tokenize(text):
        if kind == _WORD:
            current.append(value)
            continue
        if current:
            commands.append(current)
            current = []
        if value in ("(", ")"):
            commands.append(value)
    if current:
        commands.append(current)

    saved: list[str | None] = []
    for command in commands:
        if command == "(":
            saved.append(cwd)
            continue
        if command == ")":
            if saved:
                cwd = saved.pop()
            continue
        words = _command_words(command)
        if not words:
            continue
        name = os.path.basename(words[0])
        args = words[1:]
        if name == "cd":
            cwd = _change_directory(cwd, args, walk)
        elif name in ("pushd", "popd"):
            cwd = None
        elif name in _SHELLS:
            script = _script_of(args)
            if script is not None and depth == 0:
                with contextlib.suppress(_Unreadable):
                    _walk_line(script, cwd, walk, depth + 1)
        elif name in _SPECS:
            for arg in _positionals(_SPECS[name], args):
                rel = _resolve(arg, cwd, walk)
                if rel is not None and rel not in walk.found:
                    walk.found.append(rel)


def read_paths(command: str, cwd: str, root: str) -> list[str]:
    """The repo-relative regular files that the read commands in ``command`` name, in order.

    ``cwd`` is where the host ran the line and ``root`` the project root the names are made
    relative to. A file counts when a ``cat``, ``sed``, ``grep`` or ``rg`` in the line (after the
    ``cd`` that precedes it) names it, it exists, and it lies inside ``root``; a directory, an
    unexpanded glob, an argument built from ``$`` or a backtick, and a path outside ``root`` are
    dropped. Never raises: a line it cannot read (an unbalanced quote) gives no files.
    # see design/superpowers/specs/2026-10-03-records-at-the-point-of-reading-design.md (D4)
    """
    try:
        walk = _Walk(root=os.path.realpath(root), home=os.path.expanduser("~"))
        _walk_line(command, cwd or root, walk, 0)
        return walk.found
    except Exception:
        return []
