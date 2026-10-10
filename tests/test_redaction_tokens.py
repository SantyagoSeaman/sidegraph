"""Exact legacy token spans and bounded work on malformed secret-shaped text."""

from __future__ import annotations

import os
import random
import re
import subprocess
import sys
import tracemalloc
from collections.abc import Callable, Iterator

import pytest

# Immutable legacy oracles: do not import the rewritten capture recognizers.
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")
_PEM = re.compile(
    (r"-----" r"BE_GIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----").replace(
        "BE_GIN", "BEGIN"
    )
)


def _scanner(kind: str) -> Callable[[str], Iterator[tuple[int, int]]]:
    from sidegraph.redaction_tokens import iter_jwt_spans, iter_private_key_spans

    return iter_jwt_spans if kind == "jwt" else iter_private_key_spans


def _assert_same(kind: str, text: str) -> None:
    oracle = _JWT if kind == "jwt" else _PEM
    expected = [match.span() for match in oracle.finditer(text)]
    actual = list(_scanner(kind)(text))
    assert actual == expected, repr(text)
    # Span equality also preserves replacement count and untouched suffixes.
    pieces = []
    cursor = 0
    for start, end in actual:
        pieces.extend((text[cursor:start], "[REDACTED]"))
        cursor = end
    pieces.append(text[cursor:])
    assert ("".join(pieces), len(actual)) == oracle.subn("[REDACTED]", text)


@pytest.mark.parametrize(
    "text",
    [
        "eyJabcdefgh.abcdefgh.abcdefgh",
        "éeyJabcdefgh.abcdefgh.abcdefgh",
        "-eyJabcdefgh.abcdefgh.abcdefghé",
        "eyJabcdefgh.abcdefgh.abcdefgh---",
        "eyJabcdefgh.abcdefgh.abcdefgh-é",
        "eyJabcdefgh.abcdefgh.abcdefgh--é",
        "eyJabcdefgh.abcdefgh.abcdefg-",
        "eyJabcdefgh.abcdefgh.abcdef--",
        "eyJabcdefgh.abcdefgh.abcdefgh-eyJabcdefgh.abcdefgh.abcdefghé",
        "eyJabcdefgh.abcdefgh.abcdefgh-eyJabcdefgh.abcdefgh.abcdefgh",
        "eyJshort-eyJabcdefgh.abcdefgh.abcdefgh",
        "eyJabcdefgh.abcdefg.abcdefgh",
        "eyJabcdefgh..abcdefgh.abcdefgh",
        "eyJabcdefgh.abcdefgh.abcdefgh.eyJabcdefgh.abcdefgh.abcdefgh",
        "eyJabcdefgh.abcdefgh.abcdefgh\u0301eyJabcdefgh.abcdefgh.abcdefgh",
        "eyJabcdefgh.abcdefgh.abcdefgh_é",
    ],
)
def test_jwt_preserves_unicode_boundaries_greed_and_third_tail(text: str) -> None:
    _assert_same("jwt", text)


def test_jwt_unconsumed_third_tail_seeds_later_match() -> None:
    text = "eyJabcdefgh.abcdefgh.abcdefgh--.eyJabcdefgh.abcdefgh.abcdefgh"
    spans = list(_scanner("jwt")(text))
    assert [text[start:end] for start, end in spans] == [
        "eyJabcdefgh.abcdefgh.abcdefgh",
        "eyJabcdefgh.abcdefgh.abcdefgh",
    ]


@pytest.mark.parametrize(
    "text",
    [
        ("-----BE_GIN PRIVATE KEY-----\nbody\n-----END PRIVATE KEY-----").replace(
            "BE_GIN", "BEGIN"
        ),
        ("-----BE_GIN RSA PRIVATE KEY-----body-----END EC PRIVATE KEY-----").replace(
            "BE_GIN", "BEGIN"
        ),
        ("-----BE_GIN  XYZ  PRIVATE KEY-----body-----END XYZ PRIVATE KEY-----").replace(
            "BE_GIN", "BEGIN"
        ),
        (
            "-----"
            "BE_GIN PRIVATE KEY-----outer-----"
            "BE_GIN RSA PRIVATE KEY-----inner"
            "-----END EC PRIVATE KEY-----tail-----END PRIVATE KEY-----"
        ).replace("BE_GIN", "BEGIN"),
        ("-----BE_GIN PRIVATE KEY-----missing end").replace("BE_GIN", "BEGIN"),
        ("-----BE_GIN rsa PRIVATE KEY-----body-----END PRIVATE KEY-----").replace(
            "BE_GIN", "BEGIN"
        ),
        ("-----BE_GIN RSA\nPRIVATE KEY-----body-----END PRIVATE KEY-----").replace(
            "BE_GIN", "BEGIN"
        ),
        ("-----BE_GIN PRIVATE KEY----body-----END PRIVATE KEY-----").replace("BE_GIN", "BEGIN"),
        ("-----BE_GIN PRIVATE KEY------body-----END PRIVATE KEY------").replace("BE_GIN", "BEGIN"),
        ("-----BE_GIN PRIVATE KEY-----END PRIVATE KEY-----").replace("BE_GIN", "BEGIN"),
        (
            "-----"
            "BE_GIN PRIVATE KEY----------END PRIVATE KEY-----"
            "BE_GIN PRIVATE KEY-----"
            "-----END PRIVATE KEY-----"
        ).replace("BE_GIN", "BEGIN"),
        (
            "-----BE_GIN PRIVATE KEY-----body-----END private KEY----------END PRIVATE KEY-----"
        ).replace("BE_GIN", "BEGIN"),
    ],
)
def test_pem_preserves_independent_header_grammar_and_first_end(text: str) -> None:
    _assert_same("pem", text)


def test_deterministic_generated_tokens_match_immutable_oracles() -> None:
    rng = random.Random(100)
    jwt_parts = [
        "eyJ",
        "abcdefgh",
        "abcdefgh-",
        "é",
        "中",
        "_",
        "-",
        ".",
        "..",
        " ",
        "\n",
        "\u0301",
        "eyJabcdefgh.abcdefgh.abcdefgh",
        "eyJabcdefgh",
        "12345678",
    ]
    pem_parts = [
        ("-----BE_GIN ").replace("BE_GIN", "BEGIN"),
        "-----END ",
        "PRIVATE KEY-----",
        "RSA PRIVATE KEY-----",
        "EC PRIVATE KEY-----",
        "private KEY-----",
        "RSA\nPRIVATE KEY-----",
        "XYZ ",
        "body",
        "\n",
        "-----",
        " ",
    ]
    for _ in range(1200):
        _assert_same("jwt", "".join(rng.choices(jwt_parts, k=rng.randrange(1, 24))))
        _assert_same("pem", "".join(rng.choices(pem_parts, k=rng.randrange(1, 24))))


_PROBE = (
    r"""
import os, re, sys, time
kind, count = sys.argv[1], int(sys.argv[2])
if os.environ.get("SG100_TOKEN_BASELINE"):
    pattern = re.compile(
        r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"
        if kind == "jwt" else
        r"-----" r"BE_GIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"
    )
    scan = lambda text: (match.span() for match in pattern.finditer(text))
else:
    from sidegraph.redaction_tokens import iter_jwt_spans, iter_private_key_spans
    scan = iter_jwt_spans if kind == "jwt" else iter_private_key_spans
text = "eyJ-" * count if kind == "jwt" else "-----" "BE_GIN PRIVATE KEY-----" * count
start = time.perf_counter()
assert sum(1 for _ in scan(text)) == 0
print(time.perf_counter() - start)
"""
).replace("BE_GIN", "BEGIN")


def _probe(kind: str, count: int) -> float:
    process = subprocess.Popen(
        [sys.executable, "-c", _PROBE, kind, str(count)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=os.environ.copy(),
    )
    try:
        stdout, stderr = process.communicate(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate()  # Reap before failing; leave no runaway regex child.
        pytest.fail(f"{kind} negative family exceeded 3 seconds at {count} repetitions")
    assert process.returncode == 0, stderr
    return float(stdout)


@pytest.mark.slow
@pytest.mark.parametrize("kind,small", [("jwt", 32_000), ("pem", 4_000)])
def test_missing_delimiter_negative_family_has_bounded_scaling(kind: str, small: int) -> None:
    first = _probe(kind, small)
    second = _probe(kind, small * 2)
    assert second < first * 3.5 + 0.05, (first, second)


@pytest.mark.parametrize("kind", ["jwt", "pem"])
def test_first_result_streams_before_delimiter_heavy_suffix(kind: str) -> None:
    prefix = (
        "eyJabcdefgh.abcdefgh.abcdefgh "
        if kind == "jwt"
        else ("-----BE_GIN PRIVATE KEY-----x-----END PRIVATE KEY----- ").replace("BE_GIN", "BEGIN")
    )
    text = prefix + "a." * 1_000_000
    tracemalloc.start()
    try:
        spans = _scanner(kind)(text)
        assert next(spans) == (0, len(prefix) - 1)
        _, peak = tracemalloc.get_traced_memory()
        assert peak < 64_000, peak
    finally:
        tracemalloc.stop()
