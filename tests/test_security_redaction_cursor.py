"""Assignment search resumes at the consumed value cursor inside a key chain."""

import pytest

from sidegraph.capture import redact


@pytest.mark.parametrize("operator", ["=", ":", " : ", " = "])
@pytest.mark.parametrize("group", [" ", "-"])
def test_pan_expansion_does_not_hide_next_suffix_assignment(operator, group):
    card = group.join(["4111", "1111", "1111", "1111"])
    raw = "password:" + card + "-token" + operator + "abc kept"
    expected = ("[REDACTED]-[REDACTED] kept", 2) if group == " " else ("[REDACTED]", 1)
    assert redact(raw) == expected


@pytest.mark.parametrize("quoted", [False, True])
def test_next_assignment_matches_immutable_search_at_every_cursor(quoted):
    from sidegraph.capture import _ASSIGNMENT_KEYS
    from sidegraph.redaction_scan import PhaseScanner

    cases = [
        "password:4111 1111 1111 1111-token=abc kept",
        "a-token-password: x",
        "foo-token-password: x token=tail",
        "word a__prefix-token: x",
        "αprefix-token=abc",
        "a--token-password' : value",
        "İ_API_KEY` : x a-secret=tail",
        "tokenizers: no actual-token_suffix=valid",
    ]
    for text in cases:
        scanner = PhaseScanner(text)
        for cursor in [*range(len(text) + 1), *range(len(text), -1, -1)]:
            match = _ASSIGNMENT_KEYS[quoted].search(text, cursor)
            expected = (match.start(), match.end()) if match else None
            assert scanner.next_assignment(cursor, quoted_key=quoted) == expected
