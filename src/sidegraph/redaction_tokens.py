"""Streaming secret token recognizers preserving the original redaction spans.

Only fixed-prefix searches and maximal ASCII runs use regexes. Failed candidates never
restart a search through an already-scanned suffix, and the JWT window holds three runs.
"""

from __future__ import annotations

import re
from collections import deque
from collections.abc import Iterator

_TOKEN_RUN = re.compile(r"[A-Za-z0-9_-]+")
_KEY_PREFIX = re.compile(r"-----BEGIN |-----END ")


def _is_word(char: str) -> bool:
    """Match Python's Unicode-aware word-character definition."""
    return char == "_" or char.isalnum()


def _jwt_start(text: str, start: int, end: int) -> int | None:
    """Find the earliest boundary-valid prefix with eight following characters."""
    cursor = start
    while cursor <= end - 11:
        candidate = text.find("eyJ", cursor, end - 8)
        if candidate < 0:
            return None
        if candidate == 0 or not _is_word(text[candidate - 1]):
            return candidate
        cursor = candidate + 3
    return None


def _jwt_end(text: str, start: int, end: int) -> int | None:
    """Retain greedy third-segment backtracking to its last Unicode boundary."""
    following_word = end < len(text) and _is_word(text[end])
    while end >= start + 8:
        preceding_word = _is_word(text[end - 1])
        if preceding_word != following_word:
            return end
        following_word = preceding_word
        end -= 1
    return None


def iter_jwt_spans(text: str) -> Iterator[tuple[int, int]]:
    """Yield legacy JWT spans in replacement order with constant run lookahead.

    Once three adjacent runs are eligible, only the first run's boundary-valid ``eyJ``
    prefixes and the third run's final boundary matter. A consumed third-run prefix may
    leave a tail that starts another match, so keep that tail in the sliding window.
    """
    runs = (match.span() for match in _TOKEN_RUN.finditer(text))
    window: deque[tuple[int, int]] = deque()
    exhausted = False
    while True:
        while len(window) < 3 and not exhausted:
            run = next(runs, None)
            if run is None:
                exhausted = True
            else:
                window.append(run)
        if len(window) < 3:
            return
        first, second, third = window
        if (
            second[0] == first[1] + 1
            and text[first[1]] == "."
            and third[0] == second[1] + 1
            and text[second[1]] == "."
            and second[1] - second[0] >= 8
            and third[1] - third[0] >= 8
        ):
            start = _jwt_start(text, *first)
            if start is not None:
                end = _jwt_end(text, *third)
                if end is not None:
                    yield start, end
                    window.popleft()
                    window.popleft()
                    window.popleft()
                    if end < third[1]:
                        window.append((end, third[1]))
                    continue
        window.popleft()


def _key_headers(text: str) -> Iterator[tuple[bool, int, int]]:
    """Yield valid BEGIN/END headers without searching arbitrary body suffixes."""
    for prefix in _KEY_PREFIX.finditer(text):
        start = prefix.end()
        end = start
        while end < len(text) and ("A" <= text[end] <= "Z" or text[end] == " "):
            end += 1
        if text.endswith("PRIVATE KEY", start, end) and text.startswith("-----", end):
            yield text.startswith("-----BEGIN ", prefix.start()), prefix.start(), end + 5


def iter_private_key_spans(text: str) -> Iterator[tuple[int, int]]:
    """Yield each BEGIN through its first later END, independently of key kind.

    Nested BEGIN headers are body content. A missing END leaves every remaining BEGIN
    unmatched; scanning that suffix once avoids the old repeated non-greedy retries.
    """
    pending: tuple[int, int] | None = None
    consumed = 0
    for is_begin, start, end in _key_headers(text):
        if start < consumed:
            continue
        if pending is None:
            if is_begin:
                pending = start, end
        elif not is_begin and start >= pending[1]:
            yield pending[0], end
            pending = None
            consumed = end
