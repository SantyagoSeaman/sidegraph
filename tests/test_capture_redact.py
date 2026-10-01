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


def test_redact_consumes_a_double_quoted_value():
    clean, n = redact('password: "one two"')
    assert (clean, n) == ("[REDACTED]", 1)


def test_redact_consumes_a_single_quoted_value():
    clean, n = redact("password = 'correct horse battery staple'")
    assert (clean, n) == ("[REDACTED]", 1)


@pytest.mark.parametrize(
    ("text", "expected", "secret_word"),
    [
        ('{"password": "hunter2 two"}', '{"[REDACTED]}', "hunter2"),
        ("{'api_key': 'sk live 123'}", "{'[REDACTED]}", "live"),
        ("password: `hunter2 two`", "[REDACTED]", "two"),
    ],
)
def test_redact_quoted_key_and_backtick_value(text, expected, secret_word):
    clean, n = redact(text)
    assert n >= 1
    assert clean == expected
    assert secret_word not in clean


def test_redact_apostrophe_in_prose_unchanged():
    clean, n = redact("api_key='sk-123 in the env; don't commit it")
    assert clean == "[REDACTED] in the env; don't commit it"
    assert n == 1


def test_redact_unterminated_double_quote_consumes_to_next_quote():
    # Accepted: an unterminated opening quote pairs with the next one on the line, so the
    # pattern redacts more, never less.
    clean, _ = redact('password: "abc def and later "quoted" words')
    assert clean == "[REDACTED] words"


@pytest.mark.parametrize(
    ("text", "secret_word"),
    [
        ("password = '''hunter2'''", "hunter2"),
        ('API_KEY = """sk_live_abc123"""', "abc123"),
        ("token: ```abc123secret```", "abc123secret"),
        ('password="ab"cd"', "cd"),
        ('token="p1"+"p2secret"', "p2secret"),
        ('password: ""hunter2', "hunter2"),
    ],
)
def test_redact_never_less_than_before_on_glued_quotes(text, secret_word):
    clean, n = redact(text)
    assert n >= 1
    assert secret_word not in clean


def test_redact_prose_after_quoted_value_survives():
    clean, n = redact('token="a b" then prose')
    assert (clean, n) == ("[REDACTED] then prose", 1)


@pytest.mark.parametrize(
    "text",
    [
        "- `password`: DB_PASSWORD = hunter2",
        '"password": secret = "hunter2 two"',
        '{"token": password: hunter2}',
    ],
)
def test_redact_quoted_key_before_a_spaced_keyword_assignment(text):
    # The quoted-key form must not let its `\S+` value swallow the NEXT keyword and leave
    # the real secret behind: the unquoted-key entry runs first and takes it.
    clean, n = redact(text)
    assert n >= 1
    assert "hunter2" not in clean
    assert "[REDACTED]" in clean


@pytest.mark.parametrize(
    ("text", "leftover"),
    [
        ('password: "alpha\\" beta"', "beta"),
        ("password = 'it\\'s a secret'", "secret"),
    ],
)
def test_redact_quoted_value_with_escaped_quote(text, leftover):
    # An escaped quote does not close the value: the tail after it is still the secret.
    clean, n = redact(text)
    assert n >= 1
    assert leftover not in clean
    assert clean == "[REDACTED]"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            '{"password":"hunter2 two","reason":"needed for migration"}',
            '{"[REDACTED],"reason":"needed for migration"}',
        ),
        ('{"token":abc123,"x":1}', '{"[REDACTED],"x":1}'),
    ],
)
def test_redact_quoted_key_stops_at_the_next_json_field(text, expected):
    clean, n = redact(text)
    assert clean == expected
    assert n == 1


def test_redact_unterminated_escape_aware_value_keeps_the_glued_tail():
    # No unescaped closing quote: falls back to the plain quote pair, whose glued tail a
    # bare `\S+` would have left behind.
    clean, _ = redact('token: "y{{ \\"ysecret')
    assert "ysecret" not in clean


def test_redact_glued_next_field_after_unquoted_key_is_over_redacted():
    # Entry 1 (unquoted key) keeps its `\S*` suffix so it never redacts less than the old
    # `\S+`. The cost is accepted: a next field glued to the quoted value by a comma is
    # taken too. The secret is gone; the text after the comma is over-redacted.
    clean, n = redact('password: "hunter2 two",reason:"needed for migration"')
    assert n >= 1
    assert "hunter2" not in clean
    assert "two" not in clean
    assert "reason" not in clean
