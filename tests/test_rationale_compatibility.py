import textwrap

import pytest

from sidegraph.engine.rationale import rationale_labels_agree


def _cut_096(text: str) -> str:
    """Verified 0.9.6 _add_rationale: slice first, then replace line breaks and strip."""
    return text[:80].replace("\r\n", " ").replace("\r", " ").replace("\n", " ").strip()


def _cut_newer(text: str) -> str:
    """Verified _shorten_rationale_label form in tested 0.9.61/0.9.87/0.9.90."""
    label = textwrap.shorten(text, width=80, placeholder="…")
    if label in ("", "…"):
        flat = " ".join(text.split())
        label = flat if len(flat) <= 80 else flat[:79] + "…"
    return label


_DOC = (
    "Retries are idempotent so that a redelivered message never double-charges a customer, "
    "because the broker redelivers on every consumer restart."
)
_MULTILINE = (
    "Retries are idempotent so that a redelivered{nl}        message never double-charges a "
    "customer, because the broker{nl}        redelivers on every consumer restart."
)
_COMPLETE = "Use SQLite for local state because it ships with Python and needs no server."
_LONG = _COMPLETE[:-1] + " administration or a separate daemon."
_EQUAL_COMPLETE = "Use retries for database access. Keep retries bounded and verify results first."
_OLD_REDACTED = "Use [REDACTED] for database access. Keep delay bounded and verify results first."
_SHORT = "Use SQLite for the index."
_EXTENDED_096 = _cut_096(
    _SHORT + " Keep credentials out of source and retry database writes only when safe."
)


@pytest.mark.parametrize(
    ("stored", "new", "agree"),
    [
        (_cut_096(_DOC), _cut_newer(_DOC), True),
        (_cut_newer(_DOC), _cut_096(_DOC), True),
        (_cut_096(_MULTILINE.format(nl="\n")), _cut_newer(_MULTILINE.format(nl="\n")), True),
        (_cut_096(_MULTILINE.format(nl="\r\n")), _cut_newer(_MULTILINE.format(nl="\r\n")), False),
        ("Use SQLite for the index.", "Use SQLite  for\nthe index.", True),
        (
            "Use SQLite for the index.",
            "Use SQLite for the index. Keep credentials in plaintext.",
            False,
        ),
        (
            "Use SQLite for the index. Keep credentials out of the repo.",
            "Use SQLite for the index.",
            False,
        ),
        (_cut_096(_DOC), _cut_newer("Use PostgreSQL. " + _DOC), False),
        (
            "Judge durable candidates.",
            "Judge durable candidates. Only those the anchors miss, in the order they were…",
            False,
        ),
        (_cut_newer(_LONG), _COMPLETE, False),
        (_COMPLETE, _cut_newer(_LONG), False),
        ("Use SQLite…", "Use SQLite carefully…", False),
        ("Use SQLite carefully…", "Use SQLite…", False),
        (_EQUAL_COMPLETE, _EQUAL_COMPLETE + "…", True),
        (_EQUAL_COMPLETE + "…", _EQUAL_COMPLETE, True),
        ("Use SQLite …", "Use SQLite", True),
        ("Use SQLite", "Use SQLite …", True),
        (_SHORT, _EXTENDED_096, False),
        (_EXTENDED_096, _SHORT, False),
        ("x" * 80, "x" * 80 + " suffix", True),
        ("…", "x" * 80, False),
        ("x" * 80, "…", False),
        ("…", "…", True),
    ],
    ids=[
        "096-then-newer",
        "newer-then-096",
        "multiline-lf",
        "multiline-crlf-declared",
        "whitespace",
        "uncut-extension",
        "uncut-shortening",
        "different-text",
        "uncut-old-prefix-of-a-cut",
        "cut-trimmed-to-complete",
        "complete-extended-to-cut",
        "unequal-ellipsis-extensions",
        "unequal-ellipsis-shortening",
        "equal-none-ellipsis",
        "equal-ellipsis-none",
        "literal-spaced-ellipsis-first",
        "literal-spaced-ellipsis-second",
        "complete-shorter-than-len80",
        "len80-longer-than-complete",
        "exact-80-uncut-ambiguity",
        "bare-ellipsis-before-len80",
        "len80-before-bare-ellipsis",
        "equal-bare-ellipsis",
    ],
)
def test_agreement_matches_observed_label_forms(stored, new, agree):
    """Baseline cannot import the new helper. Unequal ELL/NONE and same-class rows kill
    the discarded broad prefix rule. Equal stripped labels intentionally remain equal;
    the final row pins the accepted exact-80 ambiguity, not complete docstring identity.
    CRLF remains a no-evidence case rather than widening length inference to 79."""
    assert (
        rationale_labels_agree(
            stored,
            new,
            stored_length_reliable="[REDACTED]" not in stored,
            new_length_reliable="[REDACTED]" not in new,
        )
        is agree
    )


@pytest.mark.parametrize("text", ["x" * 81, "https://example.test/" + "x" * 100])
def test_newer_fixture_keeps_an_overlong_first_word_and_both_cuts_agree(text):
    """The archived engine falls back from a bare ellipsis to a character cut.
    Red against the incomplete fixture helper; both production directions already work.
    """
    old, new = _cut_096(text), _cut_newer(text)
    assert new == text[:79] + "…"
    assert len(old) == len(new) == 80
    for stored, received in ((old, new), (new, old)):
        assert rationale_labels_agree(
            stored, received, stored_length_reliable=True, new_length_reliable=True
        )


@pytest.mark.parametrize("reverse", [False, True], ids=["forward", "reverse"])
def test_redaction_must_not_manufacture_80_character_evidence(reverse):
    assert len(_OLD_REDACTED) == 80
    stored, new = _OLD_REDACTED, _OLD_REDACTED + " Ne"
    if reverse:
        stored, new = new, stored
    assert not rationale_labels_agree(
        stored,
        new,
        stored_length_reliable=False,
        new_length_reliable=False,
    )


@pytest.mark.parametrize(
    "kwargs",
    [{}, {"stored_length_reliable": True}, {"new_length_reliable": True}],
    ids=["neither", "only-stored", "only-new"],
)
def test_length_evidence_flags_are_required_keyword_arguments(kwargs):
    with pytest.raises(TypeError):
        rationale_labels_agree("same", "same", **kwargs)
