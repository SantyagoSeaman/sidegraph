"""Streaming recognizers for assignment keys and URL credentials.

Each instance owns one immutable text phase. Reuse it across value windows so malformed
URL passwords reaching the same suffix stop are examined only once. Key chains retain
segment metadata, rather than repeatedly applying an unanchored prefix expression.
"""

from __future__ import annotations

from array import array
from bisect import bisect_left
from collections.abc import Iterator
from dataclasses import dataclass

_FOLD = str.maketrans({"İ": "i", "ı": "i", "ſ": "s", "K": "k"})
_KEYWORDS = ("apikey", "token", "secret", "password")


def _alpha(ch: str) -> bool:
    return "a" <= ch <= "z" or "A" <= ch <= "Z" or ch in "İıſK"


def _alnum(ch: str) -> bool:
    return _alpha(ch) or "0" <= ch <= "9"


def _word(ch: str) -> bool:
    return ch == "_" or ch.isalnum()


@dataclass(slots=True)
class _Chain:
    starts: array[int]
    ends: array[int]
    eligible: array[int]
    name_ends: array[int]
    previous_name: array[int]
    last_full: int
    end: int
    operators: tuple[int | None, int | None]


class PhaseScanner:
    """Scan immutable text, sharing recognition caches between monotonic windows."""

    def __init__(self, text: str) -> None:
        self.text = text
        self._chain: _Chain | None = None
        self._chain_scan_start = -1
        self._assignment_chain: _Chain | None = None
        self._assignment_cursor = -1
        self._assignment_candidate = 0
        self._name_chain: _Chain | None = None
        self._name_window = (-1, -1)
        self._name_indexes = (0, 0, 0)
        self._name_hit = (-1, -1)
        self._password_start = -1
        self._password_stop = -1
        self._scheme_run: tuple[int, int] | None = None

    def _boundary(self, start: int) -> bool:
        return start == 0 or not _word(self.text[start - 1])

    def _operator(self, end: int, quoted: bool) -> int | None:
        text, size = self.text, len(self.text)
        if quoted:
            if end == size or text[end] not in "\"'`":
                return None
            end += 1
        while end < size and text[end].isspace():
            end += 1
        if end == size or text[end] not in "=:":
            return None
        end += 1
        while end < size and text[end].isspace():
            end += 1
        return end

    def _read_chain(self, cursor: int) -> _Chain | None:
        cached = self._chain
        if cached is not None and self._chain_scan_start <= cursor < cached.end:
            return cached
        scan_start = cursor
        text, size = self.text, len(self.text)
        while cursor < size and not _alnum(text[cursor]):
            cursor += 1
        if cursor == size:
            return None
        starts, ends = array("q"), array("q")
        while True:
            start = cursor
            while cursor < size and _alnum(text[cursor]):
                cursor += 1
            starts.append(start)
            ends.append(cursor)
            if cursor + 1 >= size or text[cursor] not in "_-" or not _alnum(text[cursor + 1]):
                break
            cursor += 1
        name_ends, previous_name, eligible = array("q"), array("q"), array("q")
        last_name = last_full = -1
        for i, segment_start in enumerate(starts):
            word = text[segment_start : ends[i]].translate(_FOLD).lower()
            if self._boundary(starts[i]):
                eligible.append(i)
            name_end = 0
            for keyword in _KEYWORDS:
                if word.startswith(keyword):
                    name_end = starts[i] + len(keyword)
                    if word == keyword:
                        last_full = i
                    break
            next_word = (
                text[starts[i + 1] : ends[i + 1]].translate(_FOLD).lower()
                if word == "api" and i + 1 < len(starts)
                else ""
            )
            if next_word.startswith("key"):
                name_end = starts[i + 1] + 3
                if next_word == "key":
                    last_full = i
            name_ends.append(name_end)
            if name_end:
                last_name = i
            previous_name.append(last_name)
        chain = _Chain(
            starts,
            ends,
            eligible,
            name_ends,
            previous_name,
            last_full,
            cursor,
            (self._operator(cursor, False), self._operator(cursor, True)),
        )
        self._chain = chain
        self._chain_scan_start = scan_start
        return chain

    def assignments(self, *, quoted_key: bool = False) -> Iterator[tuple[int, int]]:
        """Yield full KEY matches as (key start, value start), without value parsing."""
        # Independent current-chain cache: shadow queries can advance while this
        # iterator is suspended at a value. Each stream examines a key chain once.
        keys = PhaseScanner(self.text)
        cursor = 0
        while (chain := keys._read_chain(cursor)) is not None:
            cursor = chain.end
            value_start = chain.operators[quoted_key]
            if value_start is None or chain.last_full < 0:
                continue
            if chain.eligible:
                segment = chain.eligible[0]
                if segment <= chain.last_full:
                    yield chain.starts[segment], value_start

    def next_assignment(self, cursor: int, *, quoted_key: bool = False) -> tuple[int, int] | None:
        """Search from the actual value cursor, including suffixes of cached key chains."""
        while (chain := self._read_chain(cursor)) is not None:
            if self._assignment_chain is chain and cursor >= self._assignment_cursor:
                candidate = self._assignment_candidate
                while (
                    candidate < len(chain.eligible)
                    and chain.starts[chain.eligible[candidate]] < cursor
                ):
                    candidate += 1
            else:
                left = bisect_left(chain.starts, cursor)
                candidate = bisect_left(chain.eligible, left)
            self._assignment_chain = chain
            self._assignment_cursor = cursor
            self._assignment_candidate = candidate
            value_start = chain.operators[quoted_key]
            if (
                value_start is not None
                and candidate < len(chain.eligible)
                and chain.eligible[candidate] <= chain.last_full
            ):
                return chain.starts[chain.eligible[candidate]], value_start
            cursor = chain.end
        return None

    def name_spans(self, start: int, end: int) -> Iterator[tuple[int, int]]:
        """Yield exact bounded NAME finditer spans, including greedy prefix fallback."""
        if start >= end:
            return
        cursor = start
        while cursor < end:
            chain = self._read_chain(cursor)
            if chain is None or chain.starts[0] >= end:
                return
            previous_start, previous_end = self._name_window
            if self._name_chain is chain and start >= previous_start and end >= previous_end:
                left, bound, candidate = self._name_indexes
                while left < len(chain.starts) and chain.starts[left] < cursor:
                    left += 1
                while bound < len(chain.starts) and chain.starts[bound] < end:
                    bound += 1
                while candidate < len(chain.eligible) and chain.eligible[candidate] < left:
                    candidate += 1
            else:
                left = bisect_left(chain.starts, cursor)
                bound = bisect_left(chain.starts, end)
                candidate = bisect_left(chain.eligible, left)
            self._name_chain = chain
            self._name_window = start, end
            self._name_indexes = left, bound, candidate
            right = bound - 1
            while right >= 0:
                keyword = chain.previous_name[right]
                if keyword < 0:
                    break
                if chain.name_ends[keyword] > end:
                    right = keyword - 1
                    continue
                if candidate == len(chain.eligible) or chain.eligible[candidate] > keyword:
                    break
                segment = chain.eligible[candidate]
                self._name_hit = chain.starts[segment], segment
                yield chain.starts[segment], chain.name_ends[keyword]
                cursor = chain.name_ends[keyword]
                while left < len(chain.starts) and chain.starts[left] < cursor:
                    left += 1
                while candidate < len(chain.eligible) and chain.eligible[candidate] < left:
                    candidate += 1
            cursor = max(cursor, chain.end)

    def assignment_at(self, start: int, *, quoted_key: bool = False) -> int | None:
        """Return an uncapped full KEY end at start, or None."""
        chain = self._read_chain(start)
        if chain is None:
            return None
        segment = (
            self._name_hit[1]
            if self._name_chain is chain and self._name_hit[0] == start
            else bisect_left(chain.starts, start)
        )
        if (
            segment == len(chain.starts)
            or chain.starts[segment] != start
            or not self._boundary(start)
            or segment > chain.last_full
        ):
            return None
        return chain.operators[quoted_key]

    def shadows_assignment(self, start: int, end: int) -> bool:
        """Bound NAME discovery while allowing full KEY/operator ends past end."""
        return any(
            self.assignment_at(hit_start, quoted_key=quoted) is not None
            for hit_start, _ in self.name_spans(start, end)
            for quoted in (False, True)
        )

    def scheme_starts(self, start: int, end: int) -> Iterator[int]:
        """Yield bounded full scheme:// starts; credential bodies remain uncapped."""
        text, size = self.text, len(self.text)
        end = min(end, size)
        cursor = start
        while cursor < end:
            if not (_alnum(text[cursor]) or text[cursor] in "+.-"):
                cursor += 1
                continue
            run_start = cursor
            cached = self._scheme_run
            if cached is not None and cached[0] <= cursor < cached[1]:
                run_end = cached[1]
            else:
                while cursor < size and (_alnum(text[cursor]) or text[cursor] in "+.-"):
                    cursor += 1
                run_end = cursor
                self._scheme_run = run_start, run_end
            candidate = run_start
            while candidate < min(run_end, end):
                if _alpha(text[candidate]) and self._boundary(candidate):
                    break
                candidate += 1
            if candidate < run_end and run_end + 3 <= end and text.startswith("://", run_end):
                yield candidate
                cursor = run_end + 3
            else:
                cursor = run_end

    def credential_end(self, start: int) -> int | None:
        """Return full URL credential end, sharing the next password stop cache."""
        text, size = self.text, len(self.text)
        cursor = start
        if cursor >= size or not _alpha(text[cursor]) or not self._boundary(cursor):
            return None
        cached = self._scheme_run
        if cached is not None and cached[0] <= cursor < cached[1]:
            cursor = cached[1]
        else:
            cursor += 1
            while cursor < size and (_alnum(text[cursor]) or text[cursor] in "+.-"):
                cursor += 1
            self._scheme_run = start, cursor
        if not text.startswith("://", cursor):
            return None
        cursor += 3
        user_start = cursor
        while cursor < size and text[cursor] not in "/:@" and not text[cursor].isspace():
            cursor += 1
        if cursor in (user_start, size) or text[cursor] != ":":
            return None
        cursor += 1
        password_start = cursor
        if not self._password_start <= cursor <= self._password_stop:
            while cursor < size and text[cursor] != "@" and not text[cursor].isspace():
                cursor += 1
            self._password_start, self._password_stop = password_start, cursor
        else:
            cursor = self._password_stop
        return (
            cursor + 1
            if cursor > password_start and cursor < size and text[cursor] == "@"
            else None
        )

    def url_spans(self) -> Iterator[tuple[int, int]]:
        """Yield final URL credential matches with regex finditer nonoverlap."""
        cursor = 0
        for start in self.scheme_starts(0, len(self.text)):
            if start >= cursor:
                end = self.credential_end(start)
                if end is not None:
                    yield start, end
                    cursor = end
