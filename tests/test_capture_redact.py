import pytest

from sidegraph.capture import redact


@pytest.mark.parametrize(
    "secret",
    [
        "AKIAIOSFODNN7EXAMPLE",  # AWS access key
        "ghp_abcdefghijklmnopqrstuvwxyz123456",  # GitHub token
        "github_pat_11ABCDEFG0123456789_abcdef",  # GitHub fine-grained
        "xoxb-1234567890-abcdefghijkl",  # Slack token
        "Bearer abcdefghijklmnopqrstuvwxyz0123456789",  # bearer token
        "api_key=sk-live-abc123def456",  # k/v assignment
        "password: hunter2secret",  # k/v assignment
        "AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMIK7MDENGbPxRfiCYEXAMPLEKEY",  # env-var form
        "DB_PASSWORD=hunter2plus",  # env-var form
        "OPENAI_API_KEY=sk-abc123def456",  # env-var form
    ],
)
def test_redact_scrubs_secret(secret):
    clean, n = redact(f"we set {secret} in the config")
    assert n >= 1
    for token in secret.split()[-1:]:
        assert token not in clean
    assert "[REDACTED]" in clean


def test_redact_keyword_inside_word_not_redacted():
    text = "tokenizer = tiktoken; structure_tokens: 4000"
    clean, n = redact(text)
    assert clean == text
    assert n == 0


def test_redact_private_key_block():
    text = (
        "key material:\n-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIEpAIBAAKCAQEA7\n-----END RSA PRIVATE KEY-----\ndone"
    )
    clean, n = redact(text)
    assert n == 1
    assert "PRIVATE KEY" not in clean
    assert "MIIEpAIBAAKCAQEA7" not in clean
    assert clean.startswith("key material:") and clean.endswith("done")


def test_redact_clean_text_untouched():
    text = "chose SQLite over Kùzu for the store; tokens per char is ~0.25"
    clean, n = redact(text)
    assert clean == text
    assert n == 0


def test_redact_counts_multiple():
    clean, n = redact("a AKIAIOSFODNN7EXAMPLE b AKIAIOSFODNN7EXAMPL2 c")
    assert n == 2
    assert clean.count("[REDACTED]") == 2
