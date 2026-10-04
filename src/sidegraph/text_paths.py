"""The repo files a piece of text names: the pure extractor behind the Agent PreToolUse branch.

A subagent starts from the brief its parent wrote, so the hook decides from the text of that brief
alone which files the subagent is about to work on. ``text_paths`` does that and nothing else: no
``Store``, no pydantic, no I/O beyond ``realpath`` and ``isfile`` on candidate paths. A brief names
a file in prose, so the rules are forgiving about how it is written (backticks, quotes, a markdown
link, ``path:line``, punctuation around it) and strict about what counts (a regular file inside
the root, or an anchored file reached by a unique suffix). A text it cannot read yields fewer
files, never an exception.

``brief_files`` adds the one hop: a brief often says "implement docs/plan.md", and the plan names
the files. Each document the brief names with a text extension is read once, up to 64 KB, and the
files it names follow the brief's own, each labelled with the document that named it. A document
is read only when it is a regular file inside the root on disk (a name the suffix fallback took
from the store proves nothing about the disk), and the project-instruction files (``CLAUDE.md``,
``AGENTS.md`` and their ``.public`` twins) are never read: every brief names one, and the files
they list (``pyproject.toml``, ``CHANGELOG.md``) are not what the task is about.

The rules are the ones the spec lists (D2); the case table in ``tests/test_text_paths.py`` pins
each one.

see design/superpowers/specs/2026-10-03-records-in-subagent-briefs-design.md (D2)
"""

from __future__ import annotations

import os
import re
from collections.abc import Collection

# The documents a brief's file is read as, and how much of one is read: 52 of 303 documents in
# the field study were larger than 64 KB, and the cap changed the records for 3 of 117 subagents.
HOP_EXTENSIONS = (".md", ".txt", ".rst")
HOP_BYTES = 64 * 1024

# Files that are named but never followed: the project's standing instructions, not a task's plan.
NO_HOP_NAMES = frozenset({"CLAUDE.md", "AGENTS.md", "CLAUDE.public.md", "AGENTS.public.md"})

# A token longer than this is not a path (a pasted blob that happens to hold a slash).
_MAX_TOKEN = 512

# A brief marks a path with whitespace, backticks and quotes. A markdown link ``[text](target)``
# puts its target after ``](``, which is read as one more separator.
_SPLIT = re.compile(r"[\s`'\"]+")
_LEADING = "([{<*"  # not ".": ``./x`` and ``.github/x`` start with one
_TRAILING = ".,;:!?)]}>*"
_LINE_SUFFIX = re.compile(r"(?::\d+(?::\d+)*|#L\d+(?:-L?\d+)?)$")
_EXTENSION = re.compile(r"\.[A-Za-z0-9]+$")


def _tokens(text: str) -> list[str]:
    """The path-shaped tokens of ``text`` in order of appearance: those with a ``/`` or a file
    extension, trimmed of punctuation at both ends and of a trailing ``:42`` or ``#L10-L20``."""
    out: list[str] = []
    for piece in _SPLIT.split(text.replace("](", " ")):
        token = piece.lstrip(_LEADING).rstrip(_TRAILING)
        token = _LINE_SUFFIX.sub("", token).rstrip(_TRAILING)
        if token and len(token) <= _MAX_TOKEN and ("/" in token or _EXTENSION.search(token)):
            out.append(token)
    return out


def _exact(token: str, root_real: str, base: str | None = None) -> str | None:
    """``token`` as a repo-relative path when it is an existing regular file inside the root.

    A relative token is joined to ``base`` (the root when not given). Both sides go through
    ``realpath`` (``os.path.relpath`` is lexical, so a root reached through a symlink would
    otherwise put every file outside it), which also resolves a symlink to its target: a link out
    of the root names nothing."""
    try:
        joined = token if os.path.isabs(token) else os.path.join(base or root_real, token)
        target = os.path.realpath(joined)
        rel = os.path.relpath(target, root_real)
    except (OSError, ValueError):
        return None
    if rel == os.curdir or rel == os.pardir or rel.startswith(os.pardir + os.sep):
        return None
    return rel if os.path.isfile(target) else None


class _Suffixes:
    """The anchored files by base name, so a token is checked against the few files that share
    its last component, not against every anchored file."""

    def __init__(self, anchored: Collection[str]) -> None:
        self._by_name: dict[str, list[str]] = {}
        for path in anchored:
            self._by_name.setdefault(path.rsplit("/", 1)[-1], []).append(path)

    def only(self, token: str) -> str | None:
        """The one anchored file whose path ends in ``"/" + token``; ``None`` for none or for
        several (an ambiguous suffix names nothing). A token with no ``/`` left, an absolute or
        home-relative one, and one that climbs (``..``) never take the fallback."""
        while token.startswith("./"):
            token = token[2:]
        if "/" not in token or token.startswith(("/", "~")) or ".." in token.split("/"):
            return None
        tail = "/" + token
        matches = [p for p in self._by_name.get(token.rsplit("/", 1)[1], ()) if p.endswith(tail)]
        return matches[0] if len(matches) == 1 else None


def _named(
    text: str, root_real: str, suffixes: _Suffixes, cwd_real: str | None = None
) -> list[str]:
    """The files ``text`` names, in order of first appearance. Each distinct token is resolved
    once (every resolution is a ``realpath``, and a pasted log repeats one path thousands of
    times): from ``cwd_real`` when given, then from the root, then by the suffix fallback."""
    found: list[str] = []
    for token in dict.fromkeys(_tokens(text)):
        rel = None
        if cwd_real is not None:
            rel = _exact(token, root_real, cwd_real)
        rel = rel or _exact(token, root_real) or suffixes.only(token)
        if rel is not None and rel not in found:
            found.append(rel)
    return found


def text_paths(text: str, root: str, anchored: Collection[str]) -> list[str]:
    """The repo-relative files ``text`` names, in order of appearance, each once.

    A token resolves exactly against ``root`` when it is an existing regular file inside it (an
    absolute path in the root counts; a symlink out of it, a directory and a path that climbs out
    do not). A token with a ``/`` that did not resolve exactly then takes the suffix fallback:
    briefs name paths relative to a subdirectory (``sub/x.py`` for ``pkg/sub/x.py``), so it is
    matched against ``anchored`` (the repo-relative paths records are anchored to), and kept only
    when exactly one of them ends in it.
    see design/superpowers/specs/2026-10-03-records-in-subagent-briefs-design.md (D2)
    """
    return _named(text, os.path.realpath(root), _Suffixes(anchored))


def _read_head(path: str) -> str:
    try:
        with open(path, "rb") as fh:
            return fh.read(HOP_BYTES).decode("utf-8", "replace")
    except OSError:
        return ""


def brief_files(
    prompt: str, root: str, anchored: Collection[str], cwd: str | None = None
) -> list[tuple[str | None, str]]:
    """``[(document or None, file)]``: the files the brief names (``None``), then, for each of
    them that is a text document, the files that document names, labelled with it. One hop: a
    document named by a document is not read. A file already named keeps its first label.

    ``cwd`` is the directory the host launched the agent from (an absolute path; anything else is
    ignored). The parent writes its brief from there, so a relative token is tried against it
    first and then against the root, as the Bash path of the read hook does; a token neither holds
    takes the suffix fallback. A document is read only when exact resolution accepts it as a
    regular file inside the root, and a project-instruction file (``NO_HOP_NAMES``) is not read at
    all.
    see design/superpowers/specs/2026-10-03-records-in-subagent-briefs-design.md (D2, section 6)
    """
    root_real = os.path.realpath(root)
    cwd_real = os.path.realpath(cwd) if isinstance(cwd, str) and os.path.isabs(cwd) else None
    if cwd_real == root_real:
        cwd_real = None
    suffixes = _Suffixes(anchored)
    named = _named(prompt, root_real, suffixes, cwd_real)
    out: list[tuple[str | None, str]] = [(None, rel) for rel in named]
    seen = set(named)
    for doc in named:
        if not doc.endswith(HOP_EXTENSIONS) or doc.rsplit("/", 1)[-1] in NO_HOP_NAMES:
            continue
        on_disk = _exact(doc, root_real)
        if on_disk is None:
            continue
        text = _read_head(os.path.join(root_real, on_disk))
        for rel in _named(text, root_real, suffixes, cwd_real):
            if rel not in seen:
                seen.add(rel)
                out.append((doc, rel))
    return out
