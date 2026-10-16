"""Compatibility of rationale labels from the verified Graphify cut forms."""

from __future__ import annotations

from typing import Literal

_LABEL_WIDTH = 80
_CUT_MARK = "…"
_FormKind = Literal["ELL", "LEN80", "NONE"]


def _form(text: str, length_reliable: bool) -> tuple[str, _FormKind]:
    flat = " ".join(text.split())
    if flat.endswith(_CUT_MARK):
        return flat.removesuffix(_CUT_MARK).rstrip(), "ELL"
    if length_reliable and len(text) == _LABEL_WIDTH:
        return flat, "LEN80"
    return flat, "NONE"


def rationale_labels_agree(
    stored: str,
    new: str,
    *,
    stored_length_reliable: bool,
    new_length_reliable: bool,
) -> bool:
    """Compare observed labels, with required trust flags for pre-normalization length.

    Equal normalized labels agree, including an equal ELL/NONE pair or two bare ellipses.
    An unequal empty body cannot prefix-match another label. Unequal same-class labels
    and unequal ELL/NONE pairs disagree. Other pairs need evidence on the shorter prefix.
    Callers compare already-redacted text and disable length evidence where a
    redaction marker makes its original length unknown. This cannot recover underlying
    text or prove that every complete docstring edit is different.
    """
    a, a_kind = _form(stored, stored_length_reliable)
    b, b_kind = _form(new, new_length_reliable)
    if a == b:
        return True
    if not a or not b:
        return False
    if a_kind == b_kind:
        return False
    if {a_kind, b_kind} == {"ELL", "NONE"}:
        return False
    if len(a) < len(b):
        return a_kind != "NONE" and b.startswith(a)
    if len(b) < len(a):
        return b_kind != "NONE" and a.startswith(b)
    return False
