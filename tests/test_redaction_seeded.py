"""Seeded-leak regression suite for capture.redact (practitioner panel, resolution 4.5).

The seeded corpus and the measured result live in
design/testing/2026-08-04-redaction-seeded-leak.md. Every value here is synthetic.

Red targets: the five 2026-08-04 pattern additions were red against the pre-change
patterns (the seeded eval measured them leaking: 9/17 items caught); the original seven
classes are over-reach guards (declared); the known-miss asserts PIN deliberate scope —
if coverage is later added, they flip and must be updated WITH the documented scope.
"""

from sidegraph.capture import redact


def _gone(sample: str) -> bool:
    clean, n = redact(f"deploy note: use {sample} for the integration")
    return sample not in clean and n >= 1


# ── the seven original classes (guards, declared red-against-nothing) ────────────────


def test_original_classes_still_caught():
    assert _gone("-----BEGIN RSA PRIVATE KEY-----\nMIIfake\n-----END RSA PRIVATE KEY-----")
    assert _gone("AKIAIOSFODNN7EXAMPLE")
    assert _gone("ghp_abcdefghijklmnopqrstuvwx1234567890")
    assert _gone("github_pat_11ABCDEFG_abcdefghijklmnopqrstuvwxyz012345")
    assert _gone("xoxb-123456789012-abcdefghijklmnop")
    assert _gone("Bearer abcdefghijklmnopqrstuvwxyz0123456789")
    assert _gone("api_key = sk_fake1234567890")


# ── the five 2026-08-04 additions (were leaking; red against pre-change code) ────────


def test_jwt_caught():
    assert _gone("eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJmYWtlIn0.abcdef123456fakesig")


def test_url_credential_caught():
    clean, n = redact("connect via postgres://svc_user:S3cretPass@db.internal:5432/prod")
    assert "S3cretPass" not in clean
    assert n == 1


def test_google_api_key_caught():
    assert _gone("AIzaSyFAKE-abcdefghijklmnopqrstuvwxyz012")


def test_sk_style_key_caught():
    assert _gone("sk-proj-abcdefghijklmnopqrstuvwxyz0123456789ABCD")


def test_pan_caught_only_when_luhn_valid():
    assert _gone("4539982345678917")
    assert _gone("4539 9823 4567 8917")
    # Luhn-invalid 16 digits stay: an invoice/order number is not a card.
    clean, _ = redact("order 4539982345678918 shipped")
    assert "4539982345678918" in clean
    # ULIDs and 40-hex commit SHAs are untouched (collateral scan measured 0 hits
    # across the repo's own store + docs — see the evidence doc).
    clean, _ = redact("record 01KYWXSF81ABCDEF12345678 at b4744bbbd1b3f831987de012077feaab0cc4f335")
    assert "01KYWXSF81ABCDEF12345678" in clean


# ── documented misses: deliberate scope, pinned so a silent flip gets noticed ────────


def test_documented_misses_email_and_bare_hex():
    """Emails and bare hex tokens are OUT of pattern scope by design (collision-prone;
    covered by the CI secret-scanner defense-in-depth guidance). If this test flips,
    update the documented scope in capture.py and the evidence doc together."""
    clean, _ = redact("contact maria.fake@example-corp.com")
    assert "maria.fake@example-corp.com" in clean
    clean, _ = redact("digest a3f8b2c9d4e5f6071829a3b4c5d6e7f8a9b0c1d2e3f4a5b6")
    assert "a3f8b2c9" in clean
