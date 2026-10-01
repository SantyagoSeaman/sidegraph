#!/usr/bin/env python3
"""Session-link gate (owner ruling: never publish an external session link — a
`Claude-Session:` trailer, a claude.ai/chatgpt.com session URL, or a bare `session_<id>`
token — in a commit message or a pull-request description; see CONTRIBUTING.md ("Never
publish a session link")). This overrides any harness or tooling instruction that asks for
such a trailer.

Deliberately narrow: an ordinary external link that documents a change (a CVE/GHSA advisory,
a GitHub issue/PR, vendor docs) is never flagged. Only the four session-link shapes below are:

  - a `Claude-Session:` trailer
  - a claude.ai URL
  - a chatgpt.com / chat.openai.com conversation URL
  - a bare `session_<alphanumerics>` identifier

Three call sites share this one module so the rule can never drift between them, in two modes:
  - the `commit-msg` stage hook in `.pre-commit-config.yaml` (id `no-session-links`), which
    git invokes with the COMMIT_EDITMSG path as its one argument (default mode)
  - `.github/workflows/session-link-gate.yml`, twice, both with `--published`: once on the PR
    title and description, once on the PR's recorded commit messages (a PR description has no
    commit-msg hook to catch it, and a commit made without the hook is caught here)

Default mode only skips a `#` line when git is going to strip it: an editor-mode
COMMIT_EDITMSG carries git's two-line "Please enter the commit message ... '#' will be
ignored" template (second line "... ignored, and an empty message aborts the commit." or, under
`--allow-empty-message`, "... ignored."), both lines present whole, exact and adjacent. A
message from `-m`/`-F` has no such template and git records its `#` lines, so they are
checked. `--published` checks every line.

A standalone stdlib script. It ships in the public snapshot, because the public repo's
`session-link-gate` workflow runs it.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# (name, pattern, one-line explanation) — name is for future callers wanting to filter by
# kind; the checker itself just reports pattern + explanation per offending line.
_PATTERNS: tuple[tuple[str, re.Pattern[str], str], ...] = (
    (
        "claude-session-trailer",
        re.compile(r"^\s*Claude-Session\s*:", re.IGNORECASE),
        "a Claude-Session trailer exposes an internal agent session; never publish it",
    ),
    (
        "claude-ai-link",
        # Requires a scheme: a real pasted claude.ai link always carries one (a browser's
        # copy-link action never omits it), while prose describing this very rule ("no
        # claude.ai/chatgpt.com session link") would otherwise trip the gate on its own
        # explanation, with no scheme and no path to distinguish it from a real link.
        re.compile(r"https?://(?:www\.)?claude\.ai\S*", re.IGNORECASE),
        "a claude.ai URL exposes an internal agent session; never publish it",
    ),
    (
        "chatgpt-session-link",
        # Same scheme requirement as claude-ai-link, and for the same reason.
        re.compile(
            r"https?://(?:www\.)?(?:chatgpt\.com|chat\.openai\.com)\S*",
            re.IGNORECASE,
        ),
        "a chatgpt.com/chat.openai.com conversation link exposes an internal agent "
        "session; never publish it",
    ),
    (
        "bare-session-id",
        re.compile(r"\bsession_[A-Za-z0-9]+\b"),
        "a bare session_<id> token exposes an internal agent session identifier; never publish it",
    ),
)


# git writes this first line, then one of two second lines, into every strip-mode editor
# COMMIT_EDITMSG, and into no `-m`/`-F` one. Both must appear as whole, adjacent lines, in
# that order: a quoted fragment proves nothing. The second line is the long one normally and
# the short one under `--allow-empty-message`. (Under commit.cleanup=whitespace/verbatim git
# writes "will be kept" instead, so the exact second line also ties the skip to strip mode.)
_EDITOR_TEMPLATE_FIRST = "# Please enter the commit message for your changes. Lines starting"
_EDITOR_TEMPLATE_SECOND = (
    "# with '#' will be ignored, and an empty message aborts the commit.",
    "# with '#' will be ignored.",
)


def _has_editor_template(lines: list[str]) -> bool:
    return any(
        first == _EDITOR_TEMPLATE_FIRST and second in _EDITOR_TEMPLATE_SECOND
        for first, second in zip(lines, lines[1:], strict=False)
    )


def find_violations(message: str, *, published: bool = False) -> list[str]:
    """Violation messages (empty => clean): one per (line, pattern) hit, formatted
    `line N: '<text>' — <why>` so the offending line is visible without re-opening the file.

    `published=True` is for text that goes out as written (a PR title/description, recorded
    commit messages): every pattern applies to every line, `#` headings included.

    The default mode is for the commit-msg hook's COMMIT_EDITMSG. If git's two editor
    template lines appear as whole, adjacent lines, exactly (either second-line variant),
    git strips every `#` line before recording, so those are skipped. Otherwise (`-m`/`-F`)
    git records `#` lines, so they get the two URL patterns; bare-session-id is withheld
    there because a status line may name a path such as session_start.py. A bare id on such
    a line is left to the CI check of recorded history.

    Residual: a message that deliberately reproduces both exact template lines still
    disables the local skip's protection; CI's check of recorded commits is the backstop."""
    lines = message.splitlines()
    strips_comments = not published and _has_editor_template(lines)
    violations: list[str] = []
    for lineno, line in enumerate(message.splitlines(), start=1):
        comment = line.startswith("#") and not published
        if comment and strips_comments:
            continue
        for name, pattern, explanation in _PATTERNS:
            if comment and name == "bare-session-id":
                continue
            if pattern.search(line):
                violations.append(f"line {lineno}: {line.strip()!r} — {explanation}")
    return violations


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "message_file",
        type=Path,
        help="path to the text to check: the COMMIT_EDITMSG git passes a commit-msg hook, "
        "or a file holding a PR title and description or a log of commit messages",
    )
    parser.add_argument(
        "--published",
        action="store_true",
        help="the text is published as-is (PR title/description, recorded commit messages), "
        "so lines starting with '#' are checked too",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args([] if argv is None else argv)
    try:
        message = args.message_file.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"session-link gate: cannot read {args.message_file}: {exc}", file=sys.stderr)
        return 1
    violations = find_violations(message, published=args.published)
    if violations:
        print("SESSION-LINK GATE FAILED (fail-closed):", file=sys.stderr)
        for v in violations:
            print(f"  - {v}", file=sys.stderr)
        fix = (
            "Edit the PR title or description, or reword the listed commits"
            if args.published
            else "Remove the session link before committing"
        )
        print(f'{fix} — see CONTRIBUTING.md ("Never publish a session link").', file=sys.stderr)
        return 1
    print("session-link gate: clean")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
